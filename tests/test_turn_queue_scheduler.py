"""A82 Stage 4a — fair managed-turn scheduler (design §5).

Real temp file-backed MeshDB + a fake ``prepare`` (no backend, no network, no
CLI; autouse spawn guard).

SCH01 head-only / never overtake a delayed earlier request
SCH02 eligibility filters apply BEFORE LIMIT 25
SCH03 pass activates ≤25, one per session, frozen payload + carrier pin
SCH04 stale revision ⇒ re-prepare against the new revision
SCH05 paused / closed / unenrolled / active-slot sessions are not activated
SCH06 index-served plans (no full mesh_tasks scan / temp sort)
SCH07 loop: hint wakes; fallback only while queued rows exist; nothing per row
SCH08 expired system intent is withdrawn; humans never expire
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.control import turn_admission as ta
from src.control import turn_scheduler as ts
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _no_cli_spawn(monkeypatch):
    from src.backends import claude_driver

    def _boom(*_a, **_k):
        raise AssertionError("real CLI spawn attempted in an offline test")

    monkeypatch.setattr(claude_driver._SDKSession, "start", _boom, raising=False)
    try:
        import claude_agent_sdk

        monkeypatch.setattr(claude_agent_sdk.ClaudeSDKClient, "connect", _boom, raising=False)
    except Exception:  # noqa: BLE001
        pass
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _session(db, sid, *, enroll=True, machine="worker-a", status=SessionStatus.IDLE):
    db.upsert_session(Session(
        session_id=sid, backend="claude", repo_path="/tmp/repo", status=status,
        created_at=NOW, updated_at=NOW, machine_id=machine,
    ))
    if enroll:
        db.enroll_session(sid)


def _q(db, sid, body, **kw):
    # fleet_cap raised: these fixtures exercise scheduling, not admission caps.
    return db.enqueue_turn(session_id=sid, body=body, operation_id=f"{sid}:{body}",
                           turn_source=kw.pop("turn_source", "human"),
                           fleet_cap=kw.pop("fleet_cap", 1000), **kw)


class _Prep:
    def __init__(self):
        self.calls = []

    async def __call__(self, head, row):
        self.calls.append((row["id"], row["revision"], row["prompt"]))
        return ts.PreparedTurn(
            action="resume_session",
            payload={"prompt": f"[ctx] {row['prompt']}", "task_id": row["id"]},
            machine_id="worker-a",
        )


def _pass(db, prep, **kw):
    return asyncio.run(ts.run_scheduler_pass(db, prep, allowance=ta.SharedWaitingAllowance(), **kw))


# SCH01 --------------------------------------------------------------------- #
def test_SCH01_delayed_head_holds_only_its_own_session(tmp_path):
    db = _db(tmp_path)
    _session(db, "a")
    _session(db, "b")
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
    a1 = _q(db, "a", "a1", not_before=future, turn_source="system")
    a2 = _q(db, "a", "a2")
    b1 = _q(db, "b", "b1")
    _q(db, "b", "b2")
    heads = [h["id"] for h in db.select_eligible_turn_heads(25)]
    assert heads == [b1], "later request overtook a delayed head, or head rule broken"
    assert a1 not in heads and a2 not in heads


# SCH02 --------------------------------------------------------------------- #
def test_SCH02_eligibility_filtered_before_limit(tmp_path):
    db = _db(tmp_path)
    # 26 older sessions whose head is blocked by an active slot holder.
    for i in range(26):
        sid = f"busy-{i:02d}"
        _session(db, sid)
        t = _q(db, sid, "running")
        db.activate_turn(t)
        _q(db, sid, "waiting")
    # 3 newer eligible sessions.
    eligible = []
    for i in range(3):
        sid = f"free-{i}"
        _session(db, sid)
        eligible.append(_q(db, sid, "go"))
    heads = [h["id"] for h in db.select_eligible_turn_heads(25)]
    assert heads == eligible


# SCH03 --------------------------------------------------------------------- #
def test_SCH03_pass_activates_heads_with_frozen_payload(tmp_path):
    db = _db(tmp_path)
    ids = []
    for i in range(30):
        sid = f"s{i:02d}"
        _session(db, sid)
        ids.append(_q(db, sid, "first"))
        _q(db, sid, "second")
    prep = _Prep()
    res = _pass(db, prep)
    assert res.activated == 25 and res.selected == 25
    pending = db._conn().execute(
        "SELECT id, session_id, payload, machine_id, activated_at, intent_bytes "
        "FROM mesh_tasks WHERE queue_protocol=1 AND status='pending'").fetchall()
    assert len(pending) == 25
    assert len({r["session_id"] for r in pending}) == 25  # one per session
    assert {r["id"] for r in pending} == set(ids[:25])     # acceptance order
    for r in pending:
        assert '"[ctx] first"' in r["payload"] and r["machine_id"] == "worker-a"
        assert r["activated_at"] and r["intent_bytes"] > 0
    # Carrier poll sees exactly those (routable by pin).
    assert len(db.get_pending_managed_turns(node_id="worker-a", limit=50)) == 25
    res2 = _pass(db, prep)
    assert res2.activated == 5  # the remaining heads; the rest wait on their slot


# SCH04 --------------------------------------------------------------------- #
def test_SCH04_stale_revision_reprepared(tmp_path):
    db = _db(tmp_path)
    _session(db, "a")
    t = _q(db, "a", "original")

    class _EditingPrep(_Prep):
        async def __call__(self, head, row):
            out = await super().__call__(head, row)
            if len(self.calls) == 1:
                db.revise_turn(t, 1, body="edited")  # edit races the activation
            return out

    prep = _EditingPrep()
    res = _pass(db, prep)
    assert res.activated == 1
    assert [c[2] for c in prep.calls] == ["original", "edited"]
    row = db.get_task(t)
    assert row["status"] == "pending" and '"[ctx] edited"' in row["payload"]


def test_SCH04b_config_revision_change_reprepares(tmp_path):
    db = _db(tmp_path)
    _session(db, "a")
    t = _q(db, "a", "x")

    class _ConfigPrep(_Prep):
        async def __call__(self, head, row):
            out = await super().__call__(head, row)
            if len(self.calls) == 1:
                # a configuration writer bumps config_revision (design §3)
                db._conn().execute(
                    "UPDATE sessions SET config_revision = config_revision + 1 "
                    "WHERE session_id = 'a'")
            return out

    prep = _ConfigPrep()
    assert _pass(db, prep).activated == 1
    assert len(prep.calls) == 2
    assert db.get_task(t)["status"] == "pending"


# SCH05 --------------------------------------------------------------------- #
def test_SCH05_ineligible_sessions_not_activated(tmp_path):
    db = _db(tmp_path)
    _session(db, "paused")
    db._conn().execute("UPDATE sessions SET turn_queue_paused=1 WHERE session_id='paused'")
    _session(db, "closed")
    _session(db, "plain")
    ids = {s: _q(db, s, "x") for s in ("paused", "closed", "plain")}
    db._conn().execute("UPDATE sessions SET status='closed' WHERE session_id='closed'")
    db._conn().execute("UPDATE sessions SET turn_queue_enrolled=0 WHERE session_id='plain'")
    res = _pass(db, _Prep())
    assert res.activated == 0 and res.waiting == 3
    assert all(db.get_task(t)["status"] == "queued" for t in ids.values())


def test_SCH05b_activation_rechecks_slot_in_transaction(tmp_path):
    db = _db(tmp_path)
    _session(db, "a")
    t1 = _q(db, "a", "one")
    t2 = _q(db, "a", "two")
    db.activate_turn(t1)
    out = db.activate_prepared_turn(
        t2, expected_revision=1, expected_config_revision=1,
        action="resume_session", payload={"prompt": "two"}, machine_id="worker-a",
    )
    assert out == "ineligible"
    assert db.get_task(t2)["status"] == "queued"


def test_SCH05c_oversize_prepared_payload_stays_queued_with_reason(tmp_path, monkeypatch):
    from src.control import turn_queue as tq

    db = _db(tmp_path)
    _session(db, "a")
    t = _q(db, "a", "x")
    monkeypatch.setattr(tq, "MAX_INTENT_BYTES_PER_ROW", 64)
    out = db.activate_prepared_turn(
        t, expected_revision=1, expected_config_revision=1,
        action="resume_session", payload={"prompt": "y" * 200}, machine_id=None,
    )
    row = db.get_task(t)
    assert out == "oversize" and row["status"] == "queued"
    assert row["blocked_reason"].startswith("prepared_payload_oversize")


def test_SCH05d_prepare_failure_leaves_queued_with_reason(tmp_path):
    db = _db(tmp_path)
    _session(db, "a")
    t = _q(db, "a", "x")

    async def bad(head, row):
        raise ValueError("boom")

    res = _pass(db, bad)
    assert res.blocked == 1
    row = db.get_task(t)
    assert row["status"] == "queued" and row["blocked_reason"] == "prepare_failed: ValueError"


# SCH06 --------------------------------------------------------------------- #
def test_SCH06_query_plans_are_index_served(tmp_path):
    db = _db(tmp_path)
    _session(db, "a")
    conn = db._conn()
    # Bulk terminal history that a full scan would have to walk.
    with db._write() as w:
        for i in range(2000):
            w.execute(
                "INSERT INTO mesh_tasks (id, session_id, backend, action, payload, status, "
                "created_at, updated_at, queue_protocol, queue_sequence) "
                "VALUES (?, 'a', 'claude', 'resume_session', '{}', 'completed', ?, ?, 1, ?)",
                (f"h{i}", NOW, NOW, i + 1),
            )
    _q(db, "a", "live")
    conn.execute("ANALYZE")
    import inspect
    src = inspect.getsource(MeshDB.select_eligible_turn_heads)
    sql = src.split('f"""', 1)[1].split('"""', 1)[0]
    from src.control.db import _MANAGED_OPEN_PREDICATE
    sql = sql.replace("{_MANAGED_OPEN_PREDICATE}", _MANAGED_OPEN_PREDICATE)
    plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, (NOW, NOW, 25)))
    assert "idx_mesh_turns_waiting" in plan, plan
    assert "SCAN mesh_tasks" not in plan.replace("SCAN mesh_tasks USING", ""), plan
    totals = " | ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM mesh_tasks INDEXED BY idx_mesh_turns_session_open WHERE "
        + _MANAGED_OPEN_PREDICATE + " AND status IN ('queued','pending')"))
    assert "idx_mesh_turns_session_open" in totals, totals


# SCH07 --------------------------------------------------------------------- #
def test_SCH07_loop_hint_and_lazy_fallback(tmp_path):
    db = _db(tmp_path)
    _session(db, "a")

    async def scenario():
        sched = ts.TurnScheduler(db, _Prep(), fallback_sec=0.05, safety_net_sec=30,
                                 allowance=ta.SharedWaitingAllowance())
        task = asyncio.create_task(sched.run())
        await asyncio.sleep(0.3)
        idle_passes = sched.passes
        assert idle_passes == 1, "idle scheduler polled without waiting rows"
        baseline_tasks = len(asyncio.all_tasks())
        t1 = _q(db, "a", "one")
        _q(db, "a", "two")
        for i in range(8):  # many other sessions' turns activate too
            _session(db, f"o{i}")
            _q(db, f"o{i}", "x")
        ts.notify_turn_queue_changed()
        for _ in range(60):  # timing-robust: wait for the hinted pass(es)
            await asyncio.sleep(0.05)
            totals = db.managed_waiting_totals()
            if totals["count"] - totals["queued"] == 9:
                break
        assert db.get_task(t1)["status"] == "pending"
        # A head waiting on its slot holder: NO steady polling — rechecks back
        # off exponentially (0.05, 0.1, 0.2, 0.4, 0.8 s ... capped) ...
        before = sched.passes
        await asyncio.sleep(1.6)
        assert sched.passes - before <= 6, "steady polling of a session that cannot progress"
        # ...and nothing is held per pending/running row: 9 pending rows, at
        # most the one wait_for helper task of the timer is added.
        assert db.managed_waiting_totals()["count"] - db.managed_waiting_totals()["queued"] == 9
        assert len(asyncio.all_tasks()) <= baseline_tasks + 1
        # A delayed head wakes the loop at its own eligibility time.
        _session(db, "later")
        soon = (datetime.now(tz=timezone.utc) + timedelta(seconds=0.4)).isoformat()
        t_late = _q(db, "later", "x", not_before=soon, turn_source="system")
        ts.notify_turn_queue_changed()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if db.get_task(t_late)["status"] == "pending":
                break
        assert db.get_task(t_late)["status"] == "pending"
        sched.stop()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert ts._ACTIVE is None


# SCH08 --------------------------------------------------------------------- #
def test_SCH08_expired_system_intent_withdrawn_humans_kept(tmp_path):
    db = _db(tmp_path)
    _session(db, "a")
    _session(db, "b")
    past = (datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat()
    sys_t = _q(db, "a", "hb", expires_at=past, turn_source="system")
    hum_t = _q(db, "b", "human", expires_at=past)
    res = _pass(db, _Prep())
    assert db.get_task(sys_t)["status"] == "withdrawn" and res.withdrawn == 1
    assert db.get_task(hum_t)["status"] == "pending"


def test_SCH02b_paused_or_unenrolled_heads_do_not_consume_the_limit(tmp_path):
    """Session-state eligibility is applied in the head query, BEFORE LIMIT:
    26 older paused/unenrolled heads must not starve a newer eligible one."""
    db = _db(tmp_path)
    for i in range(26):
        sid = f"held-{i:02d}"
        _session(db, sid)
        _q(db, sid, "x")
        col = "turn_queue_paused=1" if i % 2 else "turn_queue_enrolled=0"
        db._conn().execute(f"UPDATE sessions SET {col} WHERE session_id=?", (sid,))
    _session(db, "free")
    t = _q(db, "free", "go")
    assert [h["id"] for h in db.select_eligible_turn_heads(25)] == [t]


# SCH09 (A87 rework probe) -------------------------------------------------- #
def test_SCH09_blocked_heads_do_not_starve_other_sessions(tmp_path):
    """Adopted A87 probe: 25 older heads whose preparation keeps failing must
    not monopolize LIMIT 25; they back off (exponential, capped) and a healthy
    session is activated."""
    db = _db(tmp_path)
    for i in range(25):
        _session(db, f"bad{i:02d}")
        _q(db, f"bad{i:02d}", "x")
    _session(db, "good")
    good = _q(db, "good", "y")

    async def prep(head, row):
        if str(row["session_id"]).startswith("bad"):
            raise RuntimeError("prepare keeps failing")
        return ts.PreparedTurn(action="resume_session", payload={"prompt": "y"},
                               machine_id="worker-a")

    for _ in range(3):
        asyncio.run(ts.run_scheduler_pass(db, prep, allowance=ta.SharedWaitingAllowance()))
    assert db.get_task(good)["status"] == "pending", "healthy session starved"
    rows = db._conn().execute(
        "SELECT blocked_attempts, blocked_until, blocked_reason FROM mesh_tasks "
        "WHERE session_id LIKE 'bad%'").fetchall()
    assert all(r["blocked_attempts"] == 1 and r["blocked_until"] for r in rows), \
        "blocked heads retried every pass instead of backing off"


def test_SCH09b_backoff_is_exponential_capped_and_edit_rearms(tmp_path):
    from src.control import db as dbm

    db = _db(tmp_path)
    _session(db, "a")
    t = _q(db, "a", "x")
    delays = []
    for _ in range(10):
        db.mark_turn_blocked(t, "prepare_failed: X")
        until = db.get_task(t)["blocked_until"]
        delays.append((datetime.fromisoformat(until) - datetime.now(tz=timezone.utc)).total_seconds())
    assert 2 < delays[0] < 3.5 and 5 < delays[1] < 6.5
    assert max(delays) <= dbm._TURN_BLOCK_BACKOFF_CAP_SEC + 1
    assert db.select_eligible_turn_heads(25) == []
    assert db.next_turn_wake_at() is not None
    db.revise_turn(t, 1, body="fixed")
    assert [h["id"] for h in db.select_eligible_turn_heads(25)] == [t]


def test_SCH09c_blocked_state_change_logged_once(tmp_path, caplog):
    db = _db(tmp_path)
    _session(db, "a")
    t = _q(db, "a", "x")

    async def bad(head, row):
        raise ValueError("boom")

    import logging
    caplog.set_level(logging.WARNING, logger="src.control.turn_scheduler")
    for _ in range(3):
        db._conn().execute("UPDATE mesh_tasks SET blocked_until = NULL WHERE id = ?", (t,))
        _pass(db, bad)
    assert sum("turn_prepare_failed" in r.getMessage() for r in caplog.records) == 1
    assert db.get_task(t)["blocked_attempts"] == 3


def test_SCH10_next_timeout_policy():
    r = ts.SchedulerPassResult
    assert ts._next_timeout(r(activated=25, waiting=5), 25, 60) == 0
    assert ts._next_timeout(r(waiting=0), 25, 60) is None                 # sleep until hint
    assert ts._next_timeout(r(waiting=3), 25, 60) == 60                   # paused/backed-off only
    assert ts._next_timeout(r(waiting=3, next_wake_sec=4.0), 25, 60) == 4.0
    assert ts._next_timeout(r(waiting=3, slot_waiting=1), 25, 60, 12.0) == 12.0
