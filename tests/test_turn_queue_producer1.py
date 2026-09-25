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


def test_P1_07b_unpinned_session_assigned_to_this_host(tmp_path, monkeypatch):
    import socket

    db, o = _setup(tmp_path, monkeypatch, machine=None)
    t1 = _submit(o, operation_id="a")
    asyncio.run(ts.run_scheduler_pass(db, o._prepare_managed_turn,
                                      allowance=ta.SharedWaitingAllowance()))
    assert db.get_task(t1)["machine_id"] == socket.gethostname()
    assert db.get_pending_managed_turns(node_id="some-remote-node", accept_unpinned=True) == []


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
