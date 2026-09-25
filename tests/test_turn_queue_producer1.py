"""A82 Stage 4a — producer 1 (web / Telegram / runtime session instructions).

Real bound orchestrator methods on a bare instance (``__new__``) with a real temp
MeshDB as ``get_db()``; stub control-API orchestrator for the HTTP seam. No
backend, network or CLI (autouse spawn guard).

P1-01 enrolled ⇒ one durable managed row; no BUSY / last_task_id / legacy queue
P1-02 unenrolled ⇒ legacy path byte-identical (legacy queue, no protocol-1 row)
P1-03 harness gate still refuses before any admission
P1-04 Case join lineage recorded ONCE; replay with the operation id creates nothing
P1-05 unconverted producers / file ingestion fail closed for an enrolled session
P1-06 enrollment marker unreadable ⇒ fail closed (no legacy fallback)
P1-07 end-to-end: admit → scheduler (real prepare) → carrier claim/complete → next head
P1-08 web route: enrolled 200 envelope, no mark_busy, typed 503 / 429 + Retry-After
P1-09 Telegram: enrolled reply is "Queued", session not set BUSY; refusal is honest
"""
import asyncio
import types
from datetime import datetime, timezone

import pytest

import src.control.db as db_mod
from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control import turn_scheduler as ts
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.core.session_task_queue import SessionTaskQueue
from src.orchestrator import HarnessAdmissionBlocked, TaskOrchestrator

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


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.delenv("HARNESS_FLOW_DRIVE", raising=False)
    monkeypatch.delenv("HARNESS_LEVEL3_GUARD", raising=False)


def _sess(sid="sess-1"):
    from src.services.session_store import SessionStore

    return SessionStore().get(sid)  # DB-canonical read (get_db patched)


def _setup(tmp_path, monkeypatch, *, enroll=True, machine="worker-a"):
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    db.upsert_session(Session(
        session_id="sess-1", backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id=machine,
    ))
    if enroll:
        db.enroll_session("sess-1")
    # A registered managed-capable carrier for the session's assignment.
    _register_carrier(db, "worker-a")
    o = TaskOrchestrator.__new__(TaskOrchestrator)
    from src.services.session_store import SessionStore

    o.session_store = SessionStore()
    o.task_queue = SessionTaskQueue(50, lambda _t: "")
    o.active_tasks = {}
    o._compact_injected_ids = set()
    o.events = []
    o._emit_event = lambda name, task, data=None: o.events.append(name)
    o._emit_turn_telemetry = lambda name, task, data=None, **k: o.events.append(name)
    return db, o


def _register_carrier(db, node_id, managed=("claude",)):
    db.upsert_node(node_id=node_id, tailscale_ip="", api_port=9001, backends=["claude"],
                   max_concurrent=2, managed_backends=list(managed))


def _submit(o, **kw):
    kw.setdefault("description", "please do the thing")
    kw.setdefault("session_id", "sess-1")
    kw.setdefault("cwd", "/tmp/repo")
    kw.setdefault("source", "web_session")
    return asyncio.run(o.submit_instruction(**kw))


def _managed_rows(db):
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE queue_protocol = 1 ORDER BY queue_sequence").fetchall()]


# P1-01 --------------------------------------------------------------------- #
def test_P1_01_enrolled_session_gets_one_durable_managed_turn(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    tid = _submit(o, operation_id="web-op-1")
    assert isinstance(tid, tq.TurnAdmission) and not tid.idempotent_replay
    rows = _managed_rows(db)
    assert [r["id"] for r in rows] == [tid]
    row = rows[0]
    assert row["status"] == "queued" and row["turn_source"] == "human"
    assert row["prompt"] == "please do the thing"
    assert row["idempotency_key"] == "web-op-1"
    assert o.task_queue.qsize() == 0 and not o.active_tasks
    s = _sess()
    assert s.status == SessionStatus.IDLE and not s.last_task_id  # queued is not BUSY
    assert "task_created" in o.events and "turn.accepted" in o.events


# P1-02 --------------------------------------------------------------------- #
def test_P1_02_unenrolled_session_keeps_legacy_path(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    tid = _submit(o, operation_id="web-op-1")
    assert type(tid) is str and tid.startswith("task_")
    assert o.task_queue.qsize() == 1 and tid in o.active_tasks
    assert _managed_rows(db) == []
    queued = o.task_queue.get_nowait()
    assert TaskOrchestrator._TURN_OPERATION_META_KEY not in queued.metadata


def test_P1_02b_no_mesh_db_is_legacy(tmp_path, monkeypatch):
    _db, o = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(db_mod, "get_db", lambda: None)
    tid = _submit(o)
    assert type(tid) is str and o.task_queue.qsize() == 1


# P1-03 --------------------------------------------------------------------- #
def test_P1_03_harness_gate_refuses_before_admission(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(TaskOrchestrator, "_harness_level3_allows_autopickup",
                        staticmethod(lambda task: False))
    with pytest.raises(HarnessAdmissionBlocked):
        _submit(o)
    assert _managed_rows(db) == []


# P1-04 --------------------------------------------------------------------- #
def test_P1_04_case_join_lineage_once_and_replay_is_side_effect_free(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    tid = _submit(o, join_case_id=case_id, operation_id="op-join")
    row = db.get_task(tid)
    assert row["flow_run_id"] == case_id
    links = [l for l in db.list_flow_links(flow_run_id=case_id) if l["entity_type"] == "task"]
    assert [l["entity_id"] for l in links] == [tid]
    runs_before = len(db.list_flow_runs())
    again = _submit(o, join_case_id=case_id, operation_id="op-join")
    assert again == tid and again.idempotent_replay
    links = [l for l in db.list_flow_links(flow_run_id=case_id) if l["entity_type"] == "task"]
    assert len(links) == 1 and len(db.list_flow_runs()) == runs_before
    with pytest.raises(tq.OwnershipConflictError):
        _submit(o, description="a different request", join_case_id=case_id,
                operation_id="op-join")


def test_P1_04b_open_case_attach_sets_membership(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    case_id = db.open_case(objective="obj", session_id="sess-1", role="manager")
    tid = _submit(o)
    assert db.get_task(tid)["flow_run_id"] == case_id


# P1-05 --------------------------------------------------------------------- #
@pytest.mark.parametrize("source", ["manager_continuation", "watched_job", "cache_heartbeat",
                                    "manager_respawn", "manager_quota_resume"])
def test_P1_05_unconverted_producers_fail_closed(tmp_path, monkeypatch, source):
    db, o = _setup(tmp_path, monkeypatch)
    with pytest.raises(tq.ManagedUnsupportedError):
        _submit(o, source=source)
    assert _managed_rows(db) == [] and o.task_queue.qsize() == 0


def test_P1_05b_file_ingestion_fails_closed(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    with pytest.raises(tq.ManagedUnsupportedError):
        _submit(o, extra_metadata={"staged_file": {"name": "a.txt"}})
    assert _managed_rows(db) == [] and o.task_queue.qsize() == 0


# P1-06 --------------------------------------------------------------------- #
def test_P1_06_unreadable_marker_fails_closed(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)

    def broken(_sid):
        raise RuntimeError("database disk image is malformed")

    monkeypatch.setattr(db, "is_session_enrolled", broken)
    with pytest.raises(tq.BackingStoreError):
        _submit(o)
    assert o.task_queue.qsize() == 0


# P1-07 --------------------------------------------------------------------- #
def test_P1_07_end_to_end_admit_schedule_claim_complete(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    t1 = _submit(o, description="first instruction", operation_id="a")
    t2 = _submit(o, description="second instruction", operation_id="b")
    shared = ta.SharedWaitingAllowance()
    res = asyncio.run(ts.run_scheduler_pass(db, o._prepare_managed_turn, allowance=shared))
    assert res.activated == 1
    pend = db.get_pending_managed_turns(node_id="worker-a", backends=["claude"])
    assert [p["id"] for p in pend] == [t1]
    import json
    payload = json.loads(db.get_task(t1)["payload"])
    assert payload["prompt"] == "first instruction" and payload["task_id"] == t1
    assert payload["action"] == "create_session"
    assert payload["session"]["last_user_message"] == "first instruction"
    assert db.get_task(t2)["status"] == "queued"
    tok = db.claim_turn(t1, "worker-a", "worker_daemon", "inc-1")
    db.start_turn(t1, tok, incarnation_id="inc-1")
    db.complete_turn(task_id=t1, claim_token=tok, result={"success": True, "output": "ok"},
                     status="completed", native_session_id="native-1")
    assert _sess().backend_session_id == "native-1"
    res = asyncio.run(ts.run_scheduler_pass(db, o._prepare_managed_turn, allowance=shared))
    assert res.activated == 1 and db.get_task(t2)["status"] == "pending"
    assert json.loads(db.get_task(t2)["payload"])["action"] == "resume_session"


def test_P1_07b_unpinned_session_assigned_to_configured_local_carrier(tmp_path, monkeypatch):
    """Adopted A87 finding 4: never the hostname — the registered local
    carrier id (WORKER_NODE_ID) that will actually claim."""
    import socket
    from config import config

    db, o = _setup(tmp_path, monkeypatch, machine=None)
    _register_carrier(db, "local-daemon")
    monkeypatch.setattr(config.mesh, "local_carrier_node_id", "local-daemon")
    t1 = _submit(o, operation_id="a")
    assert db.get_task(t1)["machine_id"] == "local-daemon"
    asyncio.run(ts.run_scheduler_pass(db, o._prepare_managed_turn,
                                      allowance=ta.SharedWaitingAllowance()))
    row = db.get_task(t1)
    assert row["status"] == "pending" and row["machine_id"] == "local-daemon"
    assert row["machine_id"] != socket.gethostname()


def test_P1_07c_no_claimable_carrier_refuses_admission(tmp_path, monkeypatch):
    """Adopted A87 finding 4: a turn nobody can claim is refused (typed 503),
    with no row and no Case lineage side effect."""
    import socket
    from config import config

    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch, machine=None)
    monkeypatch.setattr(config.mesh, "local_carrier_node_id", "")
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    with pytest.raises(tq.CarrierUnavailableError) as ei:
        _submit(o, join_case_id=case_id)
    assert ei.value.status_code == 503
    # host-pinned with no carrier registered under the hostname: refused too
    db._conn().execute("UPDATE sessions SET machine_id = ? WHERE session_id='sess-1'",
                       (socket.gethostname(),))
    with pytest.raises(tq.CarrierUnavailableError):
        _submit(o, join_case_id=case_id)
    # pinned to a node that registered legacy-only (no managed backends)
    _register_carrier(db, "legacy-node", managed=())
    db._conn().execute("UPDATE sessions SET machine_id = 'legacy-node' WHERE session_id='sess-1'")
    with pytest.raises(tq.CarrierUnavailableError):
        _submit(o, join_case_id=case_id)
    assert _managed_rows(db) == []
    assert [l for l in db.list_flow_links(flow_run_id=case_id) if l["entity_type"] == "task"] == []


def test_P1_07d_registration_persists_managed_capability(tmp_path, monkeypatch):
    from src.control.node_registry import NodeCapabilities, NodeInfo, NodeRegistry

    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    reg = NodeRegistry()
    reg.register(NodeInfo(node_id="n1", tailscale_ip="", api_port=9001, capabilities=NodeCapabilities(
        backends=["claude", "codex"], queue_protocols=[0, 1], managed_backends=["claude"])))
    reg.register(NodeInfo(node_id="n0", tailscale_ip="", api_port=9001, capabilities=NodeCapabilities(
        backends=["claude"], queue_protocols=[0], managed_backends=["claude"])))
    assert db.node_managed_backends("n1") == ["claude"]
    assert db.node_managed_backends("n0") == []  # not protocol-1 ⇒ not managed


# P1-08 --------------------------------------------------------------------- #
class _WebOrch:
    def __init__(self, db, outcome=None):
        from src.services.session_service import SessionService
        from src.services.session_store import SessionStore

        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)
        self._backends = {"claude": object()}
        self.db = db
        self.outcome = outcome
        self.calls = []
        self.busy_marks = []
        orig = self.session_service.mark_busy
        self.session_service.mark_busy = lambda sid, **k: (self.busy_marks.append(sid), orig(sid, **k))[1]

    async def submit_instruction(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return tq.TurnAdmission("turn_abc", status="queued", revision=1,
                                queue_sequence=1, idempotent_replay=False)


def _client(monkeypatch, orch):
    from fastapi.testclient import TestClient
    from src.control import control_api

    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "tok")
    return TestClient(control_api.build_control_api(orch))


def _web(tmp_path, monkeypatch, outcome=None):
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    orch = _WebOrch(db, outcome)
    s = Session(session_id="sess-1", backend="claude", repo_path="/tmp/repo",
                status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a")
    orch.session_service.store.save(s)
    db.upsert_session(s)
    db.enroll_session("sess-1")
    return orch, _client(monkeypatch, orch)


def test_P1_08_web_enrolled_envelope_and_no_busy(tmp_path, monkeypatch):
    orch, c = _web(tmp_path, monkeypatch)
    r = c.post("/api/instructions", headers={"Authorization": "Bearer tok", "Idempotency-Key": "k1"},
               json={"description": "hello", "session_id": "sess-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["task_id"] == "turn_abc" and "session" in body
    assert orch.busy_marks == []
    assert orch.calls[0]["operation_id"] == "k1"
    assert orch.session_service.store.get("sess-1").status == SessionStatus.IDLE


@pytest.mark.parametrize("err,status", [
    (tq.BackingStoreError("db down"), 503),
    (tq.CapacityError("full", retry_after=1), 429),
    (tq.ByteCapError("big"), 413),
])
def test_P1_08b_web_typed_refusals(tmp_path, monkeypatch, err, status):
    orch, c = _web(tmp_path, monkeypatch, outcome=err)
    r = c.post("/api/instructions", headers={"Authorization": "Bearer tok"},
               json={"description": "hello", "session_id": "sess-1"})
    assert r.status_code == status
    assert r.json().get("ok") is not True
    if status == 429:
        assert r.headers.get("retry-after") == "1"
    assert orch.busy_marks == []


def test_P1_08c_instructions_body_capped_before_parse(tmp_path, monkeypatch):
    from src.control import control_api

    orch, c = _web(tmp_path, monkeypatch)
    huge = b'{"description": "' + b"x" * (control_api._INSTRUCTIONS_MAX_REQUEST_BYTES + 10) + b'"}'
    r = c.post("/api/instructions", headers={"Authorization": "Bearer tok",
                                             "Content-Type": "application/json"}, content=huge)
    assert r.status_code == 413 and orch.calls == []


# P1-09 --------------------------------------------------------------------- #
def _tg(outcome):
    from src.telegram.interface import TelegramInterface

    tg = TelegramInterface.__new__(TelegramInterface)
    sess = Session(session_id="sess-1", backend="claude", repo_path="/tmp/repo",
                   status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW)
    sent = []
    saved = []

    async def send_message(chat_id, text, **k):
        sent.append(text)

    tg.app = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=send_message))
    tg.session_store = types.SimpleNamespace(get_active=lambda _c: sess, save=saved.append)
    tg._user_can_access_session = lambda *_a: True

    async def submit_instruction(**kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    tg.orchestrator = types.SimpleNamespace(submit_instruction=submit_instruction)
    return tg, sess, sent, saved


def test_P1_09_telegram_enrolled_is_queued_not_busy():
    adm = tq.TurnAdmission("turn_t", status="queued", revision=1, queue_sequence=3,
                           idempotent_replay=False)
    tg, sess, sent, saved = _tg(adm)
    asyncio.run(tg._submit_buffered_instruction(chat_id=1, user_id=1, message_text="do it now"))
    assert sess.status == SessionStatus.IDLE and not sess.last_task_id and saved == []
    assert sent and sent[0].startswith("📥 Queued #3")


def test_P1_09b_telegram_refusal_is_honest():
    tg, sess, sent, saved = _tg(tq.CapacityError("full"))
    asyncio.run(tg._submit_buffered_instruction(chat_id=1, user_id=1, message_text="do it now"))
    assert sess.status == SessionStatus.IDLE and saved == []
    assert sent[0].startswith("❌ Not queued (capacity)")


def test_P1_10_web_upload_into_enrolled_session_refused_before_side_effects(tmp_path, monkeypatch):
    orch, c = _web(tmp_path, monkeypatch)
    r = c.post("/api/sessions/sess-1/upload", headers={"Authorization": "Bearer tok"},
               files={"file": ("a.txt", b"hi")}, data={"instruction": "read it"})
    assert r.status_code == 422 and r.json()["detail"]["reason"] == "managed_unsupported"
    assert orch.busy_marks == [] and orch.calls == []


def test_P1_10b_telegram_document_into_enrolled_session_refused(tmp_path, monkeypatch):
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    db.upsert_session(Session(session_id="sess-1", backend="claude", repo_path="/tmp/repo",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW))
    db.enroll_session("sess-1")
    tg, sess, sent, saved = _tg(tq.TurnAdmission("x", status="queued", revision=1,
                                                 queue_sequence=1, idempotent_replay=False))
    replies = []

    async def reply_text(text, **k):
        replies.append(text)

    update = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=1), effective_user=types.SimpleNamespace(id=1),
        message=types.SimpleNamespace(reply_text=reply_text, document=None, photo=None, caption=""),
    )
    tg._check_user_permission = lambda *_a: True

    async def _no_flush(_chat):
        return None

    tg._flush_buffer = _no_flush
    asyncio.run(tg._handle_document(update, None))
    assert replies and "not supported yet" in replies[-1]
    assert sess.status == SessionStatus.IDLE and saved == []


# P1-11 (A87 rework probes L1/L2) ------------------------------------------ #
def test_P1_11_no_enrollment_anywhere_means_no_marker_read_and_legacy_survives_db_fault(
        tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    stmts = []
    db._conn().set_trace_callback(stmts.append)
    _submit(o)
    db._conn().set_trace_callback(None)
    assert not [s for s in stmts if "turn_queue_enrolled" in s], stmts
    assert o.task_queue.qsize() == 1

    def boom(_sid):
        raise RuntimeError("database disk image is malformed (injected)")

    monkeypatch.setattr(db, "is_session_enrolled", boom)
    tid = _submit(o)  # main behavior: legacy path unaffected
    assert type(tid) is str and o.task_queue.qsize() == 2


def test_P1_11b_web_unenrolled_marker_unreadable_is_legacy_200(tmp_path, monkeypatch):
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    orch = _WebOrch(db, None)

    async def legacy_submit(**kw):
        orch.calls.append(kw)
        return "task_legacy"

    orch.submit_instruction = legacy_submit
    s = Session(session_id="sess-1", backend="claude", repo_path="/tmp/repo",
                status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a")
    orch.session_service.store.save(s)
    db.upsert_session(s)
    monkeypatch.setattr(db, "is_session_enrolled",
                        lambda sid: (_ for _ in ()).throw(RuntimeError("x")))
    c = _client(monkeypatch, orch)
    r = c.post("/api/instructions", headers={"Authorization": "Bearer tok"},
               json={"description": "hello", "session_id": "sess-1"})
    assert r.status_code == 200 and r.json()["task_id"] == "task_legacy"
    assert "turn_queue_enrolled" not in orch.calls[0]  # byte-identical legacy call


def test_P1_11c_enrollment_exists_one_marker_read_per_web_request(tmp_path, monkeypatch):
    orch, c = _web(tmp_path, monkeypatch)  # sess-1 enrolled ⇒ presence True
    reads = []
    real = orch.db.is_session_enrolled
    monkeypatch.setattr(orch.db, "is_session_enrolled", lambda sid: (reads.append(sid), real(sid))[1])
    r = c.post("/api/instructions", headers={"Authorization": "Bearer tok"},
               json={"description": "hello", "session_id": "sess-1"})
    assert r.status_code == 200 and reads == ["sess-1"]
    assert orch.calls[0]["turn_queue_enrolled"] is True


def test_P1_11d_presence_flag_lifecycle(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    assert db.any_session_enrolled() is False
    db.upsert_session(Session(session_id="x", backend="claude", repo_path="/tmp/repo",
                              status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW))
    db.enroll_session("x")
    assert db.any_session_enrolled() is True
    assert MeshDB(str(tmp_path / "mesh.db")).any_session_enrolled() is True  # loaded at start
    db._conn().execute("UPDATE sessions SET turn_queue_enrolled = 0")
    assert db.refresh_enrollment_presence() is False  # cleared when none


def test_P1_07e_carrier_gone_before_activation_backs_off(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    t1 = _submit(o, operation_id="a")
    db._conn().execute("UPDATE nodes SET managed_backends = '[]' WHERE node_id = 'worker-a'")
    res = asyncio.run(ts.run_scheduler_pass(db, o._prepare_managed_turn,
                                            allowance=ta.SharedWaitingAllowance()))
    row = db.get_task(t1)
    assert res.activated == 0 and row["status"] == "queued"
    assert row["blocked_reason"].startswith("prepare_failed: CarrierUnavailableError")
    assert row["blocked_until"]


# P1-12 (A87 rework probes L3/L4) ------------------------------------------ #
def test_P1_12_concurrent_same_operation_writes_lineage_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")

    async def both():
        kw = dict(description="d", session_id="sess-1", cwd="/tmp/repo", source="runtime",
                  join_case_id=case_id, operation_id="dup")
        return await asyncio.gather(o.submit_instruction(**kw), o.submit_instruction(**kw))

    a, b = asyncio.run(both())
    assert a == b and len(_managed_rows(db)) == 1
    links = [l for l in db.list_flow_links(flow_run_id=case_id) if l["entity_type"] == "task"]
    assert [l["entity_id"] for l in links] == [a]
    assert o.events.count("task_created") == 1


def test_P1_12b_refused_admission_leaves_no_lineage(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch)
    case_id = db.open_case(objective="obj", session_id="mgr-x", role="manager")
    for i in range(20):
        _submit(o, description=f"m{i}", join_case_id=case_id, operation_id=f"op{i}")
    with pytest.raises(tq.CapacityError):
        _submit(o, description="m20", join_case_id=case_id, operation_id="op20")
    links = [l["entity_id"] for l in db.list_flow_links(flow_run_id=case_id)
             if l["entity_type"] == "task"]
    assert len(links) == 20 and all(db.get_task(x) is not None for x in links)


# P1-13 (A87 rework m7/m8) -------------------------------------------------- #
def test_P1_13_compat_cap_admits_worst_case_valid_request(tmp_path, monkeypatch):
    """A previously valid 262144-char prompt of non-BMP text, JSON ASCII-escaped
    (≈3 MiB), must not be refused by the pre-parse cap; a larger body is 413."""
    import json as _json
    from src.control import control_api

    orch, c = _web(tmp_path, monkeypatch)
    body = _json.dumps({"description": "😀" * control_api._MAX_INSTRUCTION_CHARS,
                        "session_id": "sess-1",
                        "continue_inline": "😀" * control_api._CONTINUE_INLINE_MAX},
                       ensure_ascii=True).encode()
    assert len(body) > 3 * 1024 * 1024
    r = c.post("/api/instructions", headers={"Authorization": "Bearer tok",
                                             "Content-Type": "application/json"}, content=body)
    assert r.status_code == 200, r.text[:200]
    over = b" " * (control_api._INSTRUCTIONS_MAX_REQUEST_BYTES + 1)
    r = c.post("/api/instructions", headers={"Authorization": "Bearer tok",
                                             "Content-Type": "application/json"}, content=over)
    assert r.status_code == 413


def test_P1_13b_stalled_body_read_times_out_408_before_route(monkeypatch):
    from src.control import control_api

    monkeypatch.setattr(control_api, "_BODY_READ_DEADLINE_SEC", 0.3)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "tok")

    class _Never:
        session_service = None

        async def submit_instruction(self, **kw):
            raise AssertionError("route reached")

    app = control_api.build_control_api(_Never())
    sent = []

    async def scenario():
        first = [True]

        async def receive():
            if first[0]:
                first[0] = False
                return {"type": "http.request", "body": b'{"desc', "more_body": True}
            await asyncio.sleep(3600)

        async def send(m):
            sent.append(m)

        scope = {"type": "http", "method": "POST", "path": "/api/instructions",
                 "raw_path": b"/api/instructions", "root_path": "", "scheme": "http",
                 "query_string": b"", "http_version": "1.1", "server": ("t", 80),
                 "client": ("c", 1),
                 "headers": [(b"authorization", b"Bearer tok"),
                             (b"content-type", b"application/json")]}
        t0 = asyncio.get_running_loop().time()
        await asyncio.wait_for(app(scope, receive, send), 5)
        return asyncio.get_running_loop().time() - t0

    elapsed = asyncio.run(scenario())
    assert elapsed < 2.0
    assert sent and sent[0]["status"] == 408
