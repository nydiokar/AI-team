"""A82 Stage 8a — cutover: born-managed sessions, unconditional protocol-0
fences, migration 43 + activation drain, OpenCode CLI retirement, carrier
routing / coverage, mixed-version safety and the closed carries.

Real pieces: file-backed ``MeshDB`` as ``get_db()``, the REAL bound orchestrator
methods on a bare instance, the REAL control API / task-server apps
(TestClient), the REAL scheduler pass and the REAL A84 effects drain. No CLI /
network (producer-1 autouse spawn guard).

S8-01  every session-creation surface writes the marker at INSERT (born
       managed) and the first turn is protocol 1
S8-02  the conflict UPDATE never touches the marker (stale whole-row save)
S8-03  protocol-0 session EXECUTION rows refused at insert AND claim
       unconditionally (marker 0, presence cache False); control rows and
       session-less one-offs untouched
S8-04  migration 43: enrolls every session, fails PENDING legacy execution,
       leaves CLAIMED legacy rows + control rows, idempotent, fast on a large DB
S8-05  activation drain: a claimed legacy row blocks the session's managed
       head (visible reason) until it is terminal; index-served (EXPLAIN)
S8-06  OpenCode CLI retired: create refused, admission/compaction 410 BEFORE
       any carrier lookup, reads/close keep working, Telegram + web surfaces
S8-07  routing: MESH_ENABLED=false ⇒ typed ``carrier_required`` for session
       turns (one-offs unaffected); coverage check + /health field
S8-08  mixed version: old worker (no managed registration) ⇒ typed refusal,
       never queued; only control rows / one-offs reach ``/tasks/pending``;
       ``close_session`` still completes on the old worker
S8-09  carries: unenroll F2/F3, Telegram stop off-loop F4, Codex close N-A,
       refused-invoke teardown N-B, Level-3 invoke abandon, session badge from
       the managed result, ``effects_state='failed'`` operator surface
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import types
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import src.control.db as db_mod
from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.orchestrator import HarnessAdmissionBlocked, TaskOrchestrator
from tests.test_turn_queue_4b import _pass, _run, _wire
from tests.test_turn_queue_producer1 import (  # noqa: F401  (autouse fixtures)
    NOW, _flags, _managed_rows, _no_cli_spawn, _register_carrier, _sess, _setup, _submit,
)

LEGACY_ACTIONS = ("create_session", "resume_session", "compact_session")


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


def _marker(db: MeshDB, sid: str) -> int:
    return int(db._conn().execute(
        "SELECT turn_queue_enrolled FROM sessions WHERE session_id = ?", (sid,)).fetchone()[0])


def _raw_legacy_row(db: MeshDB, tid: str, sid: str, *, status: str = "pending",
                    action: str = "resume_session", claimed_by: str | None = None) -> None:
    """A protocol-0 row as it exists in a pre-cutover DB (written before the
    insert fence existed) — raw SQL, the only way such a row can appear now."""
    db._conn().execute(
        "INSERT INTO mesh_tasks (id, session_id, machine_id, backend, action, payload, prompt, "
        "status, claimed_by, created_at, updated_at) VALUES (?, ?, 'worker-a', 'claude', ?, '{}', "
        "'old', ?, ?, ?, ?)",
        (tid, sid, action, status, claimed_by, NOW, NOW),
    )
    db._conn().commit()


# --------------------------------------------------------------------------- #
# S8-01 / S8-02 — born managed on every creation surface
# --------------------------------------------------------------------------- #
def _api(monkeypatch: pytest.MonkeyPatch, orch: Any) -> TestClient:
    from src.control import control_api

    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "tok")
    return TestClient(control_api.build_control_api(orch))


def _created_via_web(o: Any, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> str:
    r = _api(monkeypatch, o).post(
        "/api/sessions", headers={"Authorization": "Bearer tok"},
        json={"backend": "claude", "repo_path": "/tmp/repo", "node_id": "worker-a", **extra},
    )
    assert r.status_code == 200, r.text
    return str(r.json()["session"]["session_id"])


def _surface_web_create(o: Any, db: MeshDB, mp: pytest.MonkeyPatch) -> tuple[str, str]:
    return _created_via_web(o, mp), "web_session"


def _surface_web_fork(o: Any, db: MeshDB, mp: pytest.MonkeyPatch) -> tuple[str, str]:
    return _created_via_web(o, mp, continued_from="sess-1"), "web_session"


def _surface_dispatch_worker(o: Any, db: MeshDB, mp: pytest.MonkeyPatch) -> tuple[str, str]:
    # The Manager MCP ``dispatch_worker`` opens its observable worker session
    # through this same route (role_boot=worker), then sends automation turns.
    return _created_via_web(o, mp, role_boot="worker"), "automation_session"


def _surface_telegram(o: Any, db: MeshDB, mp: pytest.MonkeyPatch) -> tuple[str, str]:
    from src.telegram.interface import TelegramInterface

    tg = TelegramInterface.__new__(TelegramInterface)
    tg.orchestrator = o
    mp.setattr(o.session_service.store, "bind", lambda *_a, **_k: None)  # no bindings file
    s = asyncio.run(tg._create_and_bind_session(
        chat_id=7, user_id=7, backend="claude", repo_path="/tmp/repo", node_id="worker-a"))
    return s.session_id, "telegram_session"


@pytest.mark.parametrize("surface", [
    _surface_web_create, _surface_web_fork, _surface_dispatch_worker, _surface_telegram,
], ids=["web_create", "web_fork", "dispatch_worker", "telegram"])
def test_S8_01_every_creation_surface_is_born_managed(tmp_path, monkeypatch, surface):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    sid, source = surface(o, db, monkeypatch)
    assert sid != "sess-1" and _marker(db, sid) == 1
    tid = asyncio.run(o.submit_instruction(
        description="first", session_id=sid, cwd="/tmp/repo", source=source))
    assert isinstance(tid, tq.TurnAdmission)
    assert db.get_task(str(tid))["queue_protocol"] == 1
    assert o.task_queue.qsize() == 0  # never the legacy in-memory queue


def test_S8_01b_api_manager_invoke_is_born_managed(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    o._manager_role_enabled = lambda: True
    res = asyncio.run(o.invoke_manager("ship X", repo_path="/tmp/repo", node_id="worker-a"))
    assert res["ok"] is True, res
    assert _marker(db, res["session_id"]) == 1
    assert db.get_task(str(res["task_id"]))["queue_protocol"] == 1


@pytest.mark.parametrize("enrolled_dead", [True, False], ids=["enrolled_dead", "legacy_born_dead"])
def test_S8_01c_both_respawns_are_born_managed(tmp_path, monkeypatch, enrolled_dead):
    from tests.test_turn_queue_respawn_crash_safety import DEAD, _env

    db, o, case_id = _env(tmp_path, monkeypatch)
    _register_carrier(db, "Horse")  # the dead Manager's node, managed-capable
    if not enrolled_dead:  # a dead Manager born before the cutover (marker 0)
        db._conn().execute("UPDATE sessions SET turn_queue_enrolled = 0 WHERE session_id = ?", (DEAD,))
        db._conn().commit()
    # The tick entry (legacy routing fork) — both shapes reach the managed respawn.
    assert asyncio.run(o._do_respawn_manager_for_case(db, case_id, 1, DEAD)) is True
    turns = [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE turn_kind = 'respawn'").fetchall()]
    assert len(turns) == 1 and turns[0]["queue_protocol"] == 1
    new_sid = str(turns[0]["session_id"])
    assert new_sid != DEAD and _marker(db, new_sid) == 1
    assert db.get_task(db_mod.respawn_task_id(case_id, 1))["producer_turn_id"] == turns[0]["id"]


def test_S8_02_conflict_update_never_touches_the_marker(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    s = _sess()
    assert _marker(db, "sess-1") == 1 and db.any_session_enrolled() is True
    db._conn().execute("UPDATE sessions SET turn_queue_enrolled = 0 WHERE session_id = 'sess-1'")
    db._conn().commit()
    s.last_summary = "a stale whole-row save"
    db.upsert_session(s)  # ON CONFLICT DO UPDATE
    assert _marker(db, "sess-1") == 0
    db._conn().execute("UPDATE sessions SET turn_queue_enrolled = 1 WHERE session_id = 'sess-1'")
    db._conn().commit()
    db.upsert_session(s)
    assert _marker(db, "sess-1") == 1


# --------------------------------------------------------------------------- #
# S8-03 — unconditional protocol-0 fences
# --------------------------------------------------------------------------- #
def test_S8_03_legacy_execution_refused_at_insert_and_claim_unconditionally(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    assert _marker(db, "sess-1") == 0
    db._any_enrolled = False  # a stale presence cache cannot open the fence (F5)
    for action in LEGACY_ACTIONS:
        with pytest.raises(tq.LegacyExecutionRefusedError):
            db.enqueue_task(task_id=f"ins-{action}", session_id="sess-1", machine_id="worker-a",
                            backend="claude", action=action, payload={})
        assert db.get_task(f"ins-{action}") is None
    # A pre-cutover pending row (raw) is failed at claim — never claimed.
    _raw_legacy_row(db, "old-1", "sess-1")
    db._any_enrolled = False
    with pytest.raises(tq.LegacyExecutionRefusedError):
        db.claim_task("old-1", "worker-a")
    row = db.get_task("old-1")
    assert row["status"] == "failed" and row["claimed_by"] is None
    assert "legacy_execution_refused" in row["error"]
    # Control rows and session-less one-offs insert and claim exactly as before.
    db.enqueue_task(task_id="close-1", session_id="sess-1", machine_id="worker-a",
                    backend="claude", action="close_session", payload={})
    db.enqueue_task(task_id="one-1", session_id=None, machine_id="worker-a",
                    backend="claude", action="run_oneoff", payload={})
    assert db.claim_task("close-1", "worker-a") and db.claim_task("one-1", "worker-a")
    # Terminal records (spool replay / reconcile) stay insertable.
    db.enqueue_task(task_id="rec-1", session_id="sess-1", machine_id="worker-a",
                    backend="claude", action="resume_session", payload={}, status="completed")
    assert db.get_task("rec-1")["status"] == "completed"


# --------------------------------------------------------------------------- #
# S8-04 — migration 43
# --------------------------------------------------------------------------- #
def _rewind_to_42(path: str) -> None:
    """Make a migrated DB look exactly like schema 42 again (pre-cutover)."""
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM schema_version WHERE version >= 43")
    conn.execute("DROP INDEX IF EXISTS idx_mesh_tasks_legacy_exec_live")
    conn.execute("DROP INDEX IF EXISTS idx_mesh_tasks_effects_failed")
    conn.execute("UPDATE sessions SET turn_queue_enrolled = 0")
    conn.commit()
    conn.close()


def _seed_pre_cutover(db: MeshDB) -> None:
    for sid, status in (("s-idle", "idle"), ("s-busy", "busy"), ("s-closed", "closed")):
        db.upsert_session(Session(session_id=sid, backend="claude", repo_path="/tmp/repo",
                                  status=SessionStatus(status), created_at=NOW, updated_at=NOW,
                                  machine_id="worker-a"))
    _raw_legacy_row(db, "pend-resume", "s-idle")
    _raw_legacy_row(db, "pend-create", "s-idle", action="create_session")
    _raw_legacy_row(db, "pend-compact", "s-busy", action="compact_session")
    _raw_legacy_row(db, "claimed-1", "s-busy", status="claimed", claimed_by="worker-a")
    _raw_legacy_row(db, "running-1", "s-idle", status="running", claimed_by="worker-a")
    _raw_legacy_row(db, "close-1", "s-closed", action="close_session")
    _raw_legacy_row(db, "cancel-1", "s-busy", action="cancel_turn")
    db._conn().execute(
        "INSERT INTO mesh_tasks (id, session_id, machine_id, backend, action, payload, status, "
        "created_at, updated_at) VALUES ('oneoff-1', NULL, 'worker-a', 'claude', 'run_oneoff', "
        "'{}', 'pending', ?, ?)", (NOW, NOW))
    db._conn().commit()


def test_S8_04_migration_43_cutover(tmp_path):
    path = str(tmp_path / "mesh.db")
    db = MeshDB(path)
    _seed_pre_cutover(db)
    db.close()
    _rewind_to_42(path)
    db = MeshDB(path)  # applies 43
    assert db._conn().execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 43
    marks = dict(db._conn().execute("SELECT session_id, turn_queue_enrolled FROM sessions").fetchall())
    assert marks == {"s-idle": 1, "s-busy": 1, "s-closed": 1}
    for tid in ("pend-resume", "pend-create", "pend-compact"):
        row = db.get_task(tid)
        assert row["status"] == "failed" and row["completed_at"], row
        assert row["error"].startswith("legacy_execution_retired")
    assert db.get_task("claimed-1")["status"] == "claimed"
    assert db.get_task("running-1")["status"] == "running"
    for tid in ("close-1", "cancel-1", "oneoff-1"):
        assert db.get_task(tid)["status"] == "pending", tid
    # Idempotent: re-applying the migration body changes nothing.
    before = [tuple(r) for r in db._conn().execute("SELECT id, status, error, updated_at FROM mesh_tasks ORDER BY id")]
    sql = dict(db_mod._get_migrations())[43]
    with db._write() as conn:
        for statement in filter(None, (s.strip() for s in sql.split(";"))):
            conn.execute(statement)
    after = [tuple(r) for r in db._conn().execute("SELECT id, status, error, updated_at FROM mesh_tasks ORDER BY id")]
    assert before == after


def test_S8_04b_migration_43_is_fast_on_a_large_db(tmp_path):
    path = str(tmp_path / "mesh.db")
    db = MeshDB(path)
    conn = db._conn()
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO sessions (session_id, backend, repo_path, status, created_at, updated_at) "
        "VALUES (?, 'claude', '/r', 'idle', ?, ?)", [(f"s{i}", NOW, NOW) for i in range(5000)])
    statuses = ("completed", "completed", "failed", "pending", "claimed")
    conn.executemany(
        "INSERT INTO mesh_tasks (id, session_id, machine_id, backend, action, payload, status, "
        "created_at, updated_at) VALUES (?, ?, 'w', 'claude', ?, '{}', ?, ?, ?)",
        [(f"t{i}", f"s{i % 5000}", LEGACY_ACTIONS[i % 3] if i % 7 else "close_session",
          statuses[i % 5], NOW, NOW) for i in range(60000)])
    conn.execute("COMMIT")
    db.close()
    _rewind_to_42(path)
    started = time.monotonic()
    db = MeshDB(path)
    elapsed = time.monotonic() - started
    assert db._conn().execute("SELECT COUNT(*) FROM sessions WHERE turn_queue_enrolled = 0").fetchone()[0] == 0
    left = db._conn().execute(
        "SELECT COUNT(*) FROM mesh_tasks WHERE status = 'pending' AND action != 'close_session'"
    ).fetchone()[0]
    assert left == 0
    print(f"migration 43 on 5000 sessions / 60000 tasks: {elapsed:.3f}s")
    assert elapsed < 10.0


# --------------------------------------------------------------------------- #
# S8-05 — activation drain predicate
# --------------------------------------------------------------------------- #
def test_S8_05_claimed_legacy_row_blocks_activation_until_terminal(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    _raw_legacy_row(db, "legacy-live", "sess-1", status="claimed", claimed_by="worker-a")
    tid = str(_submit(o, operation_id="after-cutover"))
    res = _pass(db, o)
    row = db.get_task(tid)
    assert res.activated == 0 and row["status"] == "queued"
    assert row["blocked_reason"] == "legacy_work_draining: legacy-live" and row["blocked_until"]
    db.complete_task("legacy-live", result={"success": True})  # the old turn finishes
    db._conn().execute("UPDATE mesh_tasks SET blocked_until = NULL WHERE id = ?", (tid,))
    db._conn().commit()
    assert _pass(db, o).activated == 1
    assert db.get_task(tid)["status"] == "pending"


def test_S8_05b_drain_read_is_index_served(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    plan = " ".join(str(r[-1]) for r in db._conn().execute(
        "EXPLAIN QUERY PLAN " + db_mod._LEGACY_EXEC_LIVE_SQL, ("s",)).fetchall())
    assert "idx_mesh_tasks_legacy_exec_live" in plan and "SCAN mesh_tasks" not in plan


# --------------------------------------------------------------------------- #
# S8-06 — OpenCode CLI retired
# --------------------------------------------------------------------------- #
def test_S8_06_new_opencode_cli_session_refused_everywhere(tmp_path, monkeypatch):
    from src.backends.registry import active_backend_names, is_retired_backend

    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    assert is_retired_backend("opencode") and "opencode" not in active_backend_names()
    assert "opencode-server" in active_backend_names()
    res = o.session_service.create_session(backend="opencode", repo_path="/tmp/repo")
    assert not res.ok and res.reason == "backend_retired" and "opencode-server" in res.detail
    r = _api(monkeypatch, o).post("/api/sessions", headers={"Authorization": "Bearer tok"},
                                  json={"backend": "opencode", "repo_path": "/tmp/repo"})
    assert r.status_code == 410 and r.json()["detail"]["reason"] == "backend_retired"
    assert o.session_service.create_session(backend="opencode-server", repo_path="/tmp/repo").ok


def _opencode_session(db: MeshDB) -> None:
    db.upsert_session(Session(session_id="oc-1", backend="opencode", repo_path="/tmp/repo",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a", backend_session_id="ses_native"))


def test_S8_06b_existing_retired_session_refuses_turns_before_carrier_lookup(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    _opencode_session(db)
    looked_up: List[str] = []
    o._managed_carrier_assignment = lambda s, b: looked_up.append(b) or "worker-a"
    with pytest.raises(tq.BackendRetiredError) as ei:
        _submit(o, session_id="oc-1")
    assert ei.value.status_code == 410 and ei.value.code == "backend_retired"
    with pytest.raises(tq.BackendRetiredError):
        asyncio.run(o._admit_managed_compaction(o.session_store.get("oc-1"), "op-c"))
    assert looked_up == [] and _managed_rows(db) == []
    # Reads and close keep working.
    c = _api(monkeypatch, o)
    h = {"Authorization": "Bearer tok"}
    assert c.get("/api/sessions/oc-1/turn-requests", headers=h).status_code == 200
    r = c.post("/api/sessions/oc-1/turn-requests", headers=h, json={"body": "hi", "operation_id": "x"})
    assert r.status_code == 410, r.text
    assert o.session_service.close_session("oc-1").ok
    assert db.get_session("oc-1")["status"] == "closed"


def test_S8_06c_telegram_picker_and_command_refuse_the_cli_backend(tmp_path, monkeypatch):
    from src.telegram.interface import TELEGRAM_AVAILABLE, TelegramInterface

    tg = TelegramInterface.__new__(TelegramInterface)
    if TELEGRAM_AVAILABLE:
        data = [b.callback_data for row in tg._build_session_backend_markup().inline_keyboard for b in row]
        assert "session_new_backend:opencode" not in data
        assert "session_new_backend:opencode-server" in data
    replies: List[str] = []

    async def reply_text(text: str, **_k: Any) -> None:
        replies.append(text)

    upd = types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=1), effective_chat=types.SimpleNamespace(id=1),
        message=types.SimpleNamespace(reply_text=reply_text))
    tg._check_user_permission = lambda _u: True
    asyncio.run(tg._handle_session_new(upd, types.SimpleNamespace(args=["opencode", "repo"])))
    assert replies and "retired" in replies[-1] and "opencode-server" in replies[-1]


# --------------------------------------------------------------------------- #
# S8-07 — routing without a carrier, coverage
# --------------------------------------------------------------------------- #
def test_S8_07_mesh_off_session_turns_are_carrier_required_one_offs_run(tmp_path, monkeypatch):
    from config import config

    db, o = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(TaskOrchestrator, "_REFUSE_SESSION_TURNS_WITHOUT_MESH", True)
    monkeypatch.setattr(config.mesh, "enabled", False)
    for sid in ("sess-1",):
        with pytest.raises(tq.CarrierRequiredError) as ei:
            _submit(o, session_id=sid)
        assert ei.value.code == "carrier_required" and ei.value.status_code == 503
    db.unenroll_session_drained("sess-1")  # unenrolled: still refused (no legacy fallback)
    with pytest.raises(tq.CarrierRequiredError):
        _submit(o)
    with pytest.raises(tq.CarrierRequiredError):
        asyncio.run(o._admit_managed_compaction(o.session_store.get("sess-1"), "op"))
    assert _managed_rows(db) == [] and o.task_queue.qsize() == 0
    one = asyncio.run(o.submit_instruction(description="one-off", cwd="/tmp/repo", source="web_oneoff"))
    assert isinstance(one, str) and o.task_queue.qsize() == 1
    # Mesh on: the same session turn is admitted to its carrier.
    monkeypatch.setattr(config.mesh, "enabled", True)
    db.enroll_session("sess-1")
    assert isinstance(_submit(o, operation_id="mesh-on"), tq.TurnAdmission)


def test_S8_07b_carrier_coverage_check_and_health(tmp_path, monkeypatch, caplog):
    from config import config

    db, o = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config.mesh, "local_carrier_node_id", "local-daemon")
    db.upsert_session(Session(session_id="cx-1", backend="codex", repo_path="/r",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    db.upsert_session(Session(session_id="loc-1", backend="claude", repo_path="/r",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id=""))
    db.upsert_session(Session(session_id="gone-1", backend="codex", repo_path="/r",
                              status=SessionStatus.CLOSED, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    _opencode_session(db)
    with caplog.at_level("WARNING"):
        missing = o.check_managed_carrier_coverage()
    pairs = {(m["carrier"], m["backend"]): m["sessions"] for m in missing}
    # worker-a covers claude (sess-1) but not codex; no local-daemon registered.
    assert pairs == {("worker-a", "codex"): 1, ("local-daemon", "claude"): 1}
    assert o._retired_backend_sessions == 1
    assert "event=managed_carrier_missing" in caplog.text
    body = _api(monkeypatch, o).get("/api/turn-queue/coverage",
                                    headers={"Authorization": "Bearer tok"}).json()
    assert sorted((m["carrier"], m["backend"]) for m in body["managed_carrier_missing"]) == \
        sorted(pairs)
    _register_carrier(db, "local-daemon")
    _register_carrier(db, "worker-a", managed=("claude", "codex"))
    assert o.check_managed_carrier_coverage() == []


# --------------------------------------------------------------------------- #
# S8-08 — mixed version
# --------------------------------------------------------------------------- #
def test_S8_08_old_worker_gets_typed_refusal_and_only_control_rows(tmp_path, monkeypatch):
    import src.control.node_registry as nr_mod
    import src.control.task_server as ts_mod

    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    monkeypatch.setattr(ts_mod, "get_db", lambda: db)
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    monkeypatch.setattr(ts_mod, "_worker_token", lambda: "tok")
    client = TestClient(ts_mod.app)
    h = {"Authorization": "Bearer tok"}
    # The OLD binary re-registers worker-a without managed capability.
    assert client.post("/nodes/register", json={
        "node_id": "worker-a", "tailscale_ip": "127.0.0.1", "api_port": 0,
        "incarnation_id": "old-binary", "capabilities": {"backends": ["claude"]},
    }, headers=h).status_code == 200
    assert db.node_managed_backends("worker-a", live=False) == []
    err = pytest.raises(tq.CarrierUnavailableError, _submit, o).value
    assert err.code == "carrier_unavailable" and _managed_rows(db) == []  # never queued
    # Nothing can put session execution on the legacy routes for it ...
    with pytest.raises(tq.LegacyExecutionRefusedError):
        db.enqueue_task(task_id="leg-1", session_id="sess-1", machine_id="worker-a",
                        backend="claude", action="resume_session", payload={})
    # ... while its control rows and one-offs still flow, and close completes.
    db.update_session_fields("sess-1", native_session_id="native-1")  # a process may exist there
    assert o.session_service.close_session("sess-1").ok is True
    db.enqueue_task(task_id="one-1", session_id=None, machine_id="worker-a",
                    backend="claude", action="run_oneoff", payload={})
    rows = client.get("/tasks/pending", params={"node_id": "worker-a"}, headers=h).json()
    rows = rows if isinstance(rows, list) else rows.get("tasks", [])
    assert sorted(r["action"] for r in rows) == ["close_session", "run_oneoff"]
    for r in rows:
        assert client.post(f"/tasks/{r['id']}/claim", json={"node_id": "worker-a"}, headers=h).status_code == 200
        assert client.post(f"/tasks/{r['id']}/result", json={
            "node_id": "worker-a", "success": True, "output": "ok"}, headers=h).status_code == 200
        assert db.get_task(r["id"])["status"] == "completed"


def test_S8_08b_registered_managed_carrier_offline_queues(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    db._conn().execute("UPDATE nodes SET status = 'offline' WHERE node_id = 'worker-a'")
    db._conn().commit()
    adm = _submit(o, operation_id="offline")
    row = db.get_task(str(adm))
    assert row["status"] == "queued" and row["blocked_reason"] == "carrier_offline: worker-a"


# --------------------------------------------------------------------------- #
# S8-09 — carries
# --------------------------------------------------------------------------- #
def test_S8_09a_unenroll_clears_pause_and_hold_F2(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    db._conn().execute("UPDATE sessions SET turn_queue_paused = 1, turn_queue_hold = 'operator_stop' "
                       "WHERE session_id = 'sess-1'")
    db._conn().commit()
    assert db.unenroll_session_drained("sess-1") is True
    row = db.get_session("sess-1")
    assert row["turn_queue_enrolled"] == 0 and row["turn_queue_paused"] == 0
    assert row["turn_queue_hold"] is None


def test_S8_09b_unenroll_refused_with_retry_pause_or_producer_link_F3(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    tid = str(_submit(o, operation_id="a"))
    _pass(db, o)
    tok = _run(db, tid)
    db.complete_turn(tid, tok, {"success": False}, status="failed")
    db._conn().execute("UPDATE mesh_tasks SET retry_pause_state = 'pending' WHERE id = ?", (tid,))
    db._conn().commit()
    err = pytest.raises(tq.EnrollmentRefusedError, db.unenroll_session_drained, "sess-1").value
    assert err.code == "managed_obligation_remaining" and err.context["retry_pauses"] == 1
    db._conn().execute("UPDATE mesh_tasks SET retry_pause_state = 'done' WHERE id = ?", (tid,))
    db.enqueue_task(task_id="cont-tok", session_id=None, machine_id="__continuation__",
                    backend="claude", action="case_continuation", payload={})
    db._conn().execute("UPDATE mesh_tasks SET status = 'claimed', producer_turn_id = ? "
                       "WHERE id = 'cont-tok'", (tid,))
    db._conn().commit()
    err = pytest.raises(tq.EnrollmentRefusedError, db.unenroll_session_drained, "sess-1").value
    assert err.context["producer_links"] == 1
    db._conn().execute("UPDATE mesh_tasks SET status = 'completed' WHERE id = 'cont-tok'")
    db._conn().commit()
    assert db.unenroll_session_drained("sess-1") is True


def test_S8_09c_telegram_stop_runs_off_the_event_loop_F4(tmp_path, monkeypatch):
    from tests.test_turn_queue_4b import _bot, _Ctx, _Upd

    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    seen: List[bool] = []
    real = o.stop_managed_session_turn

    def spy(session: Any, **kw: Any) -> Any:
        try:
            asyncio.get_running_loop()
            seen.append(True)   # called ON the event loop thread
        except RuntimeError:
            seen.append(False)  # called in a worker thread
        return real(session, **kw)

    o.stop_managed_session_turn = spy
    bot = _bot(o)
    asyncio.run(bot._handle_session_cancel(_Upd(), _Ctx(["sess-1"])))
    monkeypatch.setattr(o.session_store, "get_active", lambda _chat: _sess())
    asyncio.run(bot._handle_cancel_command(_Upd(), _Ctx([])))
    assert seen == [False, False]


def test_S8_09d_codex_close_keeps_a_held_or_forgotten_thread_loaded_N_A(monkeypatch):
    from src.backends import codex_native as cn

    class _Client:
        def __init__(self) -> None:
            self.unloaded: List[str] = []

        def unload(self, thread_id: str) -> None:
            self.unloaded.append(thread_id)

    be = cn.CodexBackend()
    client = _Client()
    be._client = client  # type: ignore[assignment]
    s = Session(session_id="k1", backend="codex", repo_path="/r", status=SessionStatus.IDLE,
                created_at=NOW, updated_at=NOW, backend_session_id="thr-1")
    for slot in ("_held", "_unanswered"):
        be._loaded["thr-1"] = "x"
        getattr(be, slot)["k1"] = object()
        be.close(s)
        assert client.unloaded == [] and "thr-1" in be._loaded, slot
        getattr(be, slot).pop("k1")
    be.close(s)
    assert client.unloaded == ["thr-1"] and "thr-1" not in be._loaded


def test_S8_09e_close_of_a_never_run_session_leaves_no_teardown_row_N_B(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    o._manager_role_enabled = lambda: True
    db._conn().execute("DELETE FROM nodes")  # the pinned node is unknown: refused invoke
    db._conn().commit()
    with pytest.raises(tq.CarrierUnavailableError):
        asyncio.run(o.invoke_manager("ship X", repo_path="/tmp/repo", node_id="ghost-node"))
    assert db._conn().execute(
        "SELECT COUNT(*) FROM mesh_tasks WHERE action = 'close_session'").fetchone()[0] == 0
    # A registered carrier that ran a turn of the session still gets its teardown.
    _register_carrier(db, "worker-a")
    tid = str(_submit(o, operation_id="ran"))
    _pass(db, o)
    tok = _run(db, tid)
    db.complete_turn(tid, tok, {"success": True}, status="completed", native_session_id="n-1")
    assert o.session_service.close_session("sess-1").ok
    rows = db._conn().execute(
        "SELECT machine_id FROM mesh_tasks WHERE action = 'close_session'").fetchall()
    assert [r[0] for r in rows] == ["worker-a"]


def test_S8_09f_level3_blocked_invoke_abandons_the_boot(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    o._manager_role_enabled = lambda: True
    o._harness_level3_allows_autopickup = lambda _t: False
    with pytest.raises(HarnessAdmissionBlocked):
        asyncio.run(o.invoke_manager("ship X", repo_path="/tmp/repo", node_id="worker-a"))
    managers = [dict(r) for r in db._conn().execute(
        "SELECT * FROM sessions WHERE session_id != 'sess-1'").fetchall()]
    assert len(managers) == 1 and managers[0]["status"] == "closed"
    cases = [dict(r) for r in db._conn().execute("SELECT status FROM flow_runs").fetchall()]
    assert cases and all(c["status"] == "cancelled" for c in cases)


def test_S8_09g_session_badge_from_the_managed_result(tmp_path, monkeypatch):
    from tests.test_turn_queue_a84_effects import _make_env

    env = _make_env(tmp_path, monkeypatch, chat_id=None)
    tid = env.run_turn("break", "op-f", success=False, errors=["boom"])
    env.drain()
    assert env.session_row()["status"] == "error"  # needs attention
    env.run_turn("fix", "op-s")
    env.drain()
    assert env.session_row()["status"] == "awaiting_input"
    assert env.session_row()["status"] != "busy"
    # A closed session stays closed whatever a late result says.
    env.gw._conn().execute("UPDATE sessions SET status = 'closed' WHERE session_id = ?", ("sess-a84",))
    env.gw._conn().commit()
    env.gw.project_turn_session("sess-a84", tid, entry={"task_id": tid}, summary="", files_modified=[],
                                session_status="error")
    assert env.session_row()["status"] == "closed"


def test_S8_09h_failed_effects_are_visible_to_the_operator(tmp_path, monkeypatch):
    from src.orchestrator import MANAGED_EFFECTS_MAX_ATTEMPTS
    from tests.test_turn_queue_a84_effects import AUTH, _make_env

    env = _make_env(tmp_path, monkeypatch, chat_id=None)
    tid = env.run_turn("notifier down", "op-n")
    env.notifier.fail = 10_000
    for _ in range(MANAGED_EFFECTS_MAX_ATTEMPTS + 2):
        env.drain()
    assert env.row(tid)["effects_state"] == "failed"
    detail = env.api.get(f"/api/turn-requests/{tid}", headers=AUTH).json()
    assert detail["effects_state"] == "failed" and "notify_failed" in detail["effects_error"]
    page = env.api.get("/api/sessions/sess-a84/turn-requests", headers=AUTH).json()
    assert page["effects_failed"] == 1 and page["effects_failed_turn_id"] == tid
    plan = " ".join(str(r[-1]) for r in env.gw._conn().execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*), MAX(id) FROM mesh_tasks INDEXED BY "
        "idx_mesh_tasks_effects_failed WHERE session_id = ? AND effects_state = 'failed'",
        ("sess-a84",)).fetchall())
    assert "idx_mesh_tasks_effects_failed" in plan


# --------------------------------------------------------------------------- #
# S8-10 — the respawn oracles of the (now unreachable) legacy respawn branch,
# re-proven on the managed path every dead Manager takes after the cutover.
# Real orchestrator + DB (the crash-safety harness); converted from
# tests/test_case_respawn.py (exactly-one under concurrent ticks, same Case /
# no new Case / objective intact, spawn failure ⇒ not owned, then converges).
# --------------------------------------------------------------------------- #
def test_S8_10_concurrent_ticks_respawn_exactly_one_manager_on_the_same_case(tmp_path, monkeypatch):
    from tests.test_turn_queue_respawn_crash_safety import DEAD, _env

    db, o, case_id = _env(tmp_path, monkeypatch)
    _register_carrier(db, "Horse")
    open_before = {c["flow_run_id"] for c in db.list_open_cases()}
    sessions_before = db._conn().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    async def _race() -> List[bool]:
        return list(await asyncio.gather(
            o._do_respawn_manager_for_case(db, case_id, 1, DEAD),
            o._do_respawn_manager_for_case(db, case_id, 1, DEAD),
        ))

    assert asyncio.run(_race()) == [True, True]
    assert asyncio.run(o._do_respawn_manager_for_case(db, case_id, 1, DEAD)) is True  # a later tick
    turns = [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE turn_kind = 'respawn'").fetchall()]
    assert len(turns) == 1
    new_sid = str(turns[0]["session_id"])
    assert db._conn().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == sessions_before + 1
    assert db.case_manager_session_id(case_id) == new_sid
    row = db.get_session(new_sid)
    assert row["current_case_id"] == case_id and row["case_role"] == "manager"
    events = [e for e in db.list_flow_events(case_id) if e["event_type"] == "case.manager_respawned"]
    assert len(events) == 1
    # Same Case, same objective, no new Case.
    assert {c["flow_run_id"] for c in db.list_open_cases()} == open_before
    assert db.get_case_brief(case_id)["objective"] == "ship X"
    prompt = str(turns[0]["prompt"])
    assert case_id in prompt and "ship X" in prompt and "get_case" in prompt
    assert json.loads(turns[0]["payload"])["metadata"]["source"] == "manager_respawn"


def test_S8_10b_spawn_failure_is_not_owned_and_a_later_tick_converges(tmp_path, monkeypatch):
    from tests.test_turn_queue_respawn_crash_safety import DEAD, _env

    db, o, case_id = _env(tmp_path, monkeypatch)
    _register_carrier(db, "Horse")
    real = o.session_service.create_session
    o.session_service.create_session = lambda **_k: types.SimpleNamespace(ok=False, session=None,
                                                                          reason="boom")
    assert asyncio.run(o._do_respawn_manager_for_case(db, case_id, 1, DEAD)) is False
    assert db._conn().execute(
        "SELECT COUNT(*) FROM mesh_tasks WHERE turn_kind = 'respawn'").fetchone()[0] == 0
    o.session_service.create_session = real
    assert asyncio.run(o._do_respawn_manager_for_case(db, case_id, 1, DEAD)) is True
    assert db._conn().execute(
        "SELECT COUNT(*) FROM mesh_tasks WHERE turn_kind = 'respawn'").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Review minors (PR #185)
# --------------------------------------------------------------------------- #
def test_S8_11_operator_unenroll_is_refused_after_the_cutover_F2(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    monkeypatch.setattr(TaskOrchestrator, "_LEGACY_SESSION_EXECUTION_RETIRED", True)
    r = _api(monkeypatch, o).post("/api/sessions/sess-1/turn-requests/unenroll",
                                  headers={"Authorization": "Bearer tok"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "legacy_execution_retired"
    assert _marker(db, "sess-1") == 1


def test_S8_11b_a_non_enrolled_session_turn_is_refused_up_front_F2(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    monkeypatch.setattr(TaskOrchestrator, "_LEGACY_SESSION_EXECUTION_RETIRED", True)
    db.unenroll_session_drained("sess-1")  # forced (raw DB): the route refuses it
    db.refresh_enrollment_presence()
    db._conn().execute("UPDATE sessions SET status = 'awaiting_input' WHERE session_id = 'sess-1'")
    db._conn().commit()
    with pytest.raises(tq.LegacyExecutionRetiredError) as ei:
        _submit(o, operation_id="direct")
    assert ei.value.status_code == 409 and ei.value.code == "legacy_execution_retired"
    r = _api(monkeypatch, o).post("/api/instructions", headers={"Authorization": "Bearer tok"},
                                  json={"description": "hello", "session_id": "sess-1"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "legacy_execution_retired"
    s = _sess()
    assert s.status == SessionStatus.AWAITING_INPUT and not s.last_task_id  # no BUSY/ERROR flip
    assert o.task_queue.qsize() == 0 and db._conn().execute(
        "SELECT COUNT(*) FROM mesh_tasks").fetchone()[0] == 0


def test_S8_12_draining_backoff_is_capped_F3(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    _raw_legacy_row(db, "legacy-live", "sess-1", status="claimed", claimed_by="worker-a")
    tid = str(_submit(o, operation_id="after-cutover"))
    for _ in range(12):  # well past where the generic 3 s·2^n backoff reaches 300 s
        db._conn().execute("UPDATE mesh_tasks SET blocked_until = NULL WHERE id = ?", (tid,))
        db._conn().commit()
        _pass(db, o)
    row = db.get_task(tid)
    assert row["blocked_reason"] == "legacy_work_draining: legacy-live"
    wait = (datetime.fromisoformat(row["blocked_until"]) - datetime.now(tz=timezone.utc)).total_seconds()
    assert 0 < wait <= 15.0, wait  # resumes within ~15 s of the legacy row finishing


def test_S8_13_health_shows_only_a_coverage_boolean_details_need_auth_F4(tmp_path, monkeypatch):
    from config import config

    db, o = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config.mesh, "local_carrier_node_id", "local-daemon")
    db.upsert_session(Session(session_id="cx-1", backend="codex", repo_path="/r",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW,
                              machine_id="worker-a"))
    c = _api(monkeypatch, o)
    assert c.get("/health").json()["turn_queue"] == {"coverage_ok": None}  # not checked yet
    o.check_managed_carrier_coverage()
    health = c.get("/health")
    assert health.json()["turn_queue"] == {"coverage_ok": False}
    for secret in ("worker-a", "local-daemon", "codex", "sessions"):
        assert secret not in health.text
    assert c.get("/api/turn-queue/coverage").status_code == 401
    detail = c.get("/api/turn-queue/coverage", headers={"Authorization": "Bearer tok"}).json()
    assert detail["checked"] is True and detail["coverage_ok"] is False
    assert {"carrier": "worker-a", "pin": "worker-a", "backend": "codex", "sessions": 1} in \
        detail["managed_carrier_missing"]
    _register_carrier(db, "worker-a", managed=("claude", "codex"))
    _register_carrier(db, "local-daemon")
    o.check_managed_carrier_coverage()
    assert c.get("/health").json()["turn_queue"] == {"coverage_ok": True}


def test_S8_14_effects_failed_banner_clears_after_a_delivered_turn_F5(tmp_path, monkeypatch):
    from src.orchestrator import MANAGED_EFFECTS_MAX_ATTEMPTS
    from tests.test_turn_queue_a84_effects import AUTH, _make_env

    env = _make_env(tmp_path, monkeypatch, chat_id=None)

    def page() -> Dict[str, Any]:
        return env.api.get("/api/sessions/sess-a84/turn-requests", headers=AUTH).json()

    def failing_turn(op: str) -> str:
        tid = env.run_turn("notifier down", op)
        env.notifier.fail = 10_000
        for _ in range(MANAGED_EFFECTS_MAX_ATTEMPTS + 2):
            env.drain()
        env.notifier.fail = 0
        assert env.row(tid)["effects_state"] == "failed"
        return tid

    first = failing_turn("op-f1")
    assert (page()["effects_failed"], page()["effects_failed_turn_id"]) == (1, first)
    ok = env.run_turn("fine", "op-ok")
    env.drain()
    assert env.row(ok)["effects_state"] == "done"
    assert (page()["effects_failed"], page()["effects_failed_turn_id"]) == (0, None)
    again = failing_turn("op-f2")
    assert (page()["effects_failed"], page()["effects_failed_turn_id"]) == (1, again)


def test_S8_15_coverage_recovers_within_a_minute_and_logs_on_change_F6(tmp_path, monkeypatch, caplog):
    """Registration happens in the task-server process (separate in Docker), so
    the gateway cannot be hinted: the periodic check runs every 60 s (was 600)
    and logs only when the uncovered set changes."""
    from config import config

    db, o = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config.mesh, "local_carrier_node_id", "local-daemon")
    db.upsert_session(Session(session_id="loc-1", backend="claude", repo_path="/r",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id=""))
    assert TaskOrchestrator._CARRIER_COVERAGE_INTERVAL_SEC <= 60.0
    with caplog.at_level("WARNING"):
        o.check_managed_carrier_coverage()
        o.check_managed_carrier_coverage()  # unchanged ⇒ not logged again
    assert caplog.text.count("event=managed_carrier_missing") == 1

    async def run_loop() -> None:
        o.running = True
        loop_task = asyncio.create_task(o._carrier_coverage_loop(0.01))
        await asyncio.sleep(0.05)
        assert o._managed_carrier_missing  # a stale/absent carrier at gateway start
        _register_carrier(db, "local-daemon")  # the worker (re)registers elsewhere
        for _ in range(200):
            if o._managed_carrier_missing == []:
                break
            await asyncio.sleep(0.01)
        o.running = False
        loop_task.cancel()

    asyncio.run(run_loop())
    assert o._managed_carrier_missing == []
