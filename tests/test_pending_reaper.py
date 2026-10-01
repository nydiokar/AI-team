"""[#177] Pending-task orphan handling: stale detection, reaper, cancel-on-close,
and the stats honesty figure.

Orphaned ``pending`` mesh tasks (unknown machine_id, closed sessions, dead manager
continuations) used to accumulate forever — no TTL on the pending state. These
tests pin the four acceptance criteria of issue #177:

  1. an unknown-``machine_id`` dispatch is rejected (see test_mesh_enqueue_affinity);
  2. a pending task whose session is closed is cancelled within one reaper interval;
  3. ``cont:*`` single-flight rows are cancelled when their Case closes;
  4. ``stats()`` exposes a separate ``tasks_stale_pending`` figure.

No paid Claude/Codex CLI is invoked (test cost guard).
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.control.db import (
    CONTINUATION_MACHINE_SENTINEL,
    MeshDB,
    continuation_task_id,
    quota_resume_task_id,
)

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
GRACE = 1800          # 30 min
MAX_AGE = 604800      # 7 days


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _seed_session(db: MeshDB, session_id: str, status: str) -> None:
    with db._write() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, backend, repo_path, status, "
            "created_at, updated_at) VALUES (?, 'claude', '/tmp/x', ?, 't0', 't0')",
            (session_id, status),
        )


def _backdate(db: MeshDB, task_id: str, created_at: str) -> None:
    with db._write() as conn:
        conn.execute(
            "UPDATE mesh_tasks SET created_at = ? WHERE id = ?", (created_at, task_id)
        )


def _stale(db: MeshDB):
    rows = db.list_stale_pending_tasks(
        grace_sec=GRACE, max_age_sec=MAX_AGE, now=NOW
    )
    return {r["id"]: r["_stale_reason"] for r in rows}


# ---------------------------------------------------------------------------
# list_stale_pending_tasks — classification
# ---------------------------------------------------------------------------


def test_closed_session_pending_is_flagged_immediately(tmp_path):
    db = _db(tmp_path)
    _seed_session(db, "sess_closed", "closed")
    db.enqueue_task("task_sc", "sess_closed", None, "claude", "resume_session", {"prompt": "x"})
    # Freshly created (no grace) — a closed session never reopens.
    _backdate(db, "task_sc", _iso(NOW - timedelta(seconds=5)))
    assert _stale(db) == {"task_sc": "session_closed"}


def test_cancelled_session_pending_is_flagged(tmp_path):
    db = _db(tmp_path)
    _seed_session(db, "sess_x", "cancelled")
    db.enqueue_task("task_cx", "sess_x", None, "claude", "resume_session", {"prompt": "x"})
    assert _stale(db).get("task_cx") == "session_closed"


def test_open_session_pending_is_not_flagged(tmp_path):
    db = _db(tmp_path)
    _seed_session(db, "sess_live", "awaiting_input")
    db.enqueue_task("task_live", "sess_live", None, "claude", "resume_session", {"prompt": "x"})
    _backdate(db, "task_live", _iso(NOW - timedelta(seconds=10)))
    assert _stale(db) == {}


def test_unknown_node_pending_flagged_only_past_grace(tmp_path):
    db = _db(tmp_path)
    # No node 'kanebra' registered.
    db.enqueue_task("task_young", None, "kanebra", "claude", "resume_session", {"prompt": "x"})
    db.enqueue_task("task_old", None, "kanebra", "claude", "resume_session", {"prompt": "x"})
    _backdate(db, "task_young", _iso(NOW - timedelta(seconds=GRACE - 60)))  # inside grace
    _backdate(db, "task_old", _iso(NOW - timedelta(seconds=GRACE + 60)))    # past grace
    reasons = _stale(db)
    assert "task_young" not in reasons
    assert reasons.get("task_old") == "node_unknown"


def test_offline_node_pending_flagged_past_grace(tmp_path):
    db = _db(tmp_path)
    db.upsert_node("deadnode", "100.0.0.9", 9001, ["claude"], 2, status="offline")
    db.enqueue_task("task_off", None, "deadnode", "claude", "resume_session", {"prompt": "x"})
    _backdate(db, "task_off", _iso(NOW - timedelta(seconds=GRACE + 60)))
    assert _stale(db).get("task_off") == "node_offline"


def test_online_node_pending_is_never_flagged_even_past_age_ceiling(tmp_path):
    db = _db(tmp_path)
    db.upsert_node("livenode", "100.0.0.8", 9001, ["claude"], 2, status="online")
    db.enqueue_task("task_on", None, "livenode", "claude", "resume_session", {"prompt": "x"})
    # Older than the 7-day ceiling, but a pin to an ONLINE node is still
    # claimable work — age_exceeded must NOT reap it.
    _backdate(db, "task_on", _iso(NOW - timedelta(days=8)))
    assert _stale(db) == {}


def test_unpinned_pending_past_age_ceiling_is_flagged(tmp_path):
    db = _db(tmp_path)
    # Unpinned (any node), never claimed in 8 days ⇒ stuck ⇒ age backstop fires.
    db.enqueue_task("task_unp", None, None, "claude", "run_oneoff", {"prompt": "x"})
    _backdate(db, "task_unp", _iso(NOW - timedelta(days=8)))
    assert _stale(db).get("task_unp") == "age_exceeded"


def test_sentinel_continuation_row_not_flagged_as_node_unknown(tmp_path):
    """A cont:* row rides the reserved sentinel, which matches no node — but it is
    a gateway lease, NOT an orphan. Only the age ceiling may retire it."""
    db = _db(tmp_path)
    cid = continuation_task_id("casehex", 1)
    db.enqueue_task(cid, None, CONTINUATION_MACHINE_SENTINEL, "claude", "manager_continuation", {})
    _backdate(db, cid, _iso(NOW - timedelta(seconds=GRACE + 600)))  # past grace, within max_age
    assert _stale(db) == {}


def test_sentinel_continuation_row_flagged_past_age_ceiling(tmp_path):
    db = _db(tmp_path)
    cid = continuation_task_id("casehex", 2)
    db.enqueue_task(cid, None, CONTINUATION_MACHINE_SENTINEL, "claude", "manager_continuation", {})
    _backdate(db, cid, _iso(NOW - timedelta(seconds=MAX_AGE + 1)))
    assert _stale(db).get(cid) == "age_exceeded"


def test_thresholds_zero_disable_reasons(tmp_path):
    db = _db(tmp_path)
    db.enqueue_task("task_u", None, "kanebra", "claude", "resume_session", {"prompt": "x"})
    _backdate(db, "task_u", _iso(NOW - timedelta(days=30)))
    # grace=0 disables node reasons; max_age=0 disables the age ceiling.
    rows = db.list_stale_pending_tasks(grace_sec=0, max_age_sec=0, now=NOW)
    assert rows == []


# ---------------------------------------------------------------------------
# cancel_task
# ---------------------------------------------------------------------------


def test_cancel_task_flips_pending_and_writes_event(tmp_path):
    db = _db(tmp_path)
    _seed_session(db, "sess_c", "closed")
    db.enqueue_task("task_c", "sess_c", None, "claude", "resume_session", {"prompt": "x"})
    assert db.cancel_task("task_c", "pending reaped: session_closed") is True
    row = db.get_task("task_c")
    assert row["status"] == "cancelled"
    assert row["error"] == "pending reaped: session_closed"
    events = db.get_events("sess_c")
    assert any(e["task_id"] == "task_c" and e["success"] == 0 for e in events)


def test_cancel_task_never_clobbers_a_completed_row(tmp_path):
    db = _db(tmp_path)
    db.enqueue_task("task_done", None, "kanebra", "claude", "run_oneoff", {"prompt": "x"})
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET status='completed' WHERE id='task_done'")
    assert db.cancel_task("task_done", "reaped") is False
    assert db.get_task("task_done")["status"] == "completed"


# ---------------------------------------------------------------------------
# Reaper sweep (_reap_stale_pending_once wiring)
# ---------------------------------------------------------------------------


def test_reaper_cancels_closed_session_pending(tmp_path, monkeypatch):
    import src.control.db as db_mod
    from src.control import task_server
    from config import config as cfg

    db = _db(tmp_path)
    _seed_session(db, "sess_r", "closed")
    db.enqueue_task("task_r", "sess_r", None, "claude", "resume_session", {"prompt": "x"})

    monkeypatch.setattr(db_mod, "_db_instance", db, raising=False)
    monkeypatch.setattr(cfg.mesh, "pending_reaper_enabled", True, raising=False)
    monkeypatch.setattr(cfg.mesh, "pending_reaper_grace_sec", GRACE, raising=False)
    monkeypatch.setattr(cfg.mesh, "pending_max_age_sec", MAX_AGE, raising=False)

    task_server._reap_stale_pending_once()

    assert db.get_task("task_r")["status"] == "cancelled"


def test_reaper_is_a_noop_when_disabled(tmp_path, monkeypatch):
    import src.control.db as db_mod
    from src.control import task_server
    from config import config as cfg

    db = _db(tmp_path)
    _seed_session(db, "sess_d", "closed")
    db.enqueue_task("task_d", "sess_d", None, "claude", "resume_session", {"prompt": "x"})

    monkeypatch.setattr(db_mod, "_db_instance", db, raising=False)
    monkeypatch.setattr(cfg.mesh, "pending_reaper_enabled", False, raising=False)

    task_server._reap_stale_pending_once()

    assert db.get_task("task_d")["status"] == "pending"  # untouched


# ---------------------------------------------------------------------------
# close_case cancels the Case's pending single-flight tokens
# ---------------------------------------------------------------------------


def test_close_case_cancels_pending_continuation_tokens(tmp_path):
    db = _db(tmp_path)
    case_id = db.create_flow_run(task_id="t_case", current_stage="execute")
    cont = continuation_task_id(case_id, 1)
    qresume = quota_resume_task_id(case_id, "task_paused")
    db.enqueue_task(cont, None, CONTINUATION_MACHINE_SENTINEL, "claude", "manager_continuation", {"case_id": case_id})
    db.enqueue_task(qresume, None, CONTINUATION_MACHINE_SENTINEL, "claude", "manager_quota_resume", {"case_id": case_id})

    assert db.close_case(case_id) is True

    assert db.get_task(cont)["status"] == "cancelled"
    assert db.get_task(cont)["error"] == "case closed"
    assert db.get_task(qresume)["status"] == "cancelled"


def test_close_case_leaves_other_cases_tokens_alone(tmp_path):
    db = _db(tmp_path)
    case_a = db.create_flow_run(task_id="t_a", current_stage="execute")
    case_b = db.create_flow_run(task_id="t_b", current_stage="execute")
    cont_b = continuation_task_id(case_b, 1)
    db.enqueue_task(cont_b, None, CONTINUATION_MACHINE_SENTINEL, "claude", "manager_continuation", {"case_id": case_b})

    db.close_case(case_a)

    assert db.get_task(cont_b)["status"] == "pending"  # untouched


# ---------------------------------------------------------------------------
# stats() honesty figure
# ---------------------------------------------------------------------------


def test_stats_reports_stale_pending_separately(tmp_path):
    db = _db(tmp_path)
    _seed_session(db, "sess_s", "closed")
    db.enqueue_task("task_orphan", "sess_s", None, "claude", "resume_session", {"prompt": "x"})
    _seed_session(db, "sess_ok", "awaiting_input")
    db.enqueue_task("task_live", "sess_ok", None, "claude", "resume_session", {"prompt": "x"})

    stats = db.stats()
    assert stats["tasks_pending"] == 2       # both still pending rows
    assert stats["tasks_stale_pending"] == 1  # only the closed-session orphan
