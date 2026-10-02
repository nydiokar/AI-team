"""A82 Stage 8 preconditions — pre-cutover producers + routing.

Real pieces: a file-backed ``MeshDB`` as ``get_db()``, REAL bound orchestrator
methods on a bare instance, the REAL control API app (TestClient), the REAL
scheduler pass, the REAL task server + carrier ``_handle_task`` /
``_execute_task`` with a fake backend. No CLI / network (autouse spawn guard).

PC01  P1  ``manager_invoke`` (first turn of /api/manager) on a born-managed
          session is ONE managed automation turn, idempotent on the Case,
          Case lineage intact, runs to completion.
PC02  routing: Docker-like config (mesh on, gateway local execution off) — an
          UNPINNED enrolled session turn goes to the managed carrier; one-offs
          and unenrolled sessions are still refused; no local carrier ⇒ typed 503.
PC03  offline-carrier admission policy: registered-but-offline carrier ⇒
          queued with ``carrier_offline: <node>`` and activated on return;
          policy off / never-registered / unknown ⇒ 503.
PC04-08 P2 staged-file ingestion: attached managed turn (orchestrator, web,
          Telegram), file-only delivery as a protocol-0 control row, malformed
          refs refused, carrier fetches BEFORE invoke and a fetch failure is a
          not-invoked release (never a turn without its file).
"""
import asyncio
import json
import types
from datetime import datetime, timedelta, timezone

import pytest

import src.control.db as db_mod
from src.control import turn_admission as ta
from src.control import turn_queue as tq
from src.control import turn_scheduler as ts
from src.core.interfaces import ExecutionResult, Session, SessionStatus
from src.orchestrator import HarnessAdmissionBlocked, TaskOrchestrator
from tests.test_turn_queue_producer1 import (  # noqa: F401
    NOW, _client, _flags, _managed_rows, _no_cli_spawn, _register_carrier, _sess, _setup,
    _submit,
)

PAST = (datetime.now(tz=timezone.utc) - timedelta(seconds=1)).isoformat()
STAGED = {"file_id": "0123456789abcdef", "filename": "spec.md"}


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch):
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


def _pass(db, o):
    return asyncio.run(ts.run_scheduler_pass(
        db, o._prepare_managed_turn, allowance=ta.SharedWaitingAllowance(),
        recover_lineage=o._recover_managed_lineage,
        void_lineage=o._void_withdrawn_lineage_async,
    ))


def _protocol0_rows(db, action):
    return [dict(r) for r in db._conn().execute(
        "SELECT * FROM mesh_tasks WHERE COALESCE(queue_protocol, 0) = 0 AND action = ?",
        (action,)).fetchall()]


def _unblock(db, tid):
    db._conn().execute("UPDATE mesh_tasks SET blocked_until = ? WHERE id = ?", (PAST, tid))


# --------------------------------------------------------------------------- #
# PC01 — P1: manager_invoke first turn
# --------------------------------------------------------------------------- #
def _born_managed_service(db, o, sid="mgr-1", machine="worker-a"):
    """Stage-8 shape: the session is enrolled AT creation (born managed)."""
    def create_session(**kw):
        s = Session(session_id=sid, backend=kw.get("backend") or "claude",
                    repo_path=kw["repo_path"], status=SessionStatus.IDLE,
                    created_at=NOW, updated_at=NOW, machine_id=machine)
        db.upsert_session(s)
        db.enroll_session(sid)
        return types.SimpleNamespace(ok=True, session=o.session_store.get(sid))

    o.session_service = types.SimpleNamespace(create_session=create_session)


def test_PC01_manager_invoke_first_turn_is_one_managed_automation_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FLOW_DRIVE", "1")
    db, o = _setup(tmp_path, monkeypatch, enroll=False)
    _born_managed_service(db, o)
    o._manager_role_enabled = lambda: True
    res = asyncio.run(o.invoke_manager("ship X", repo_path="/tmp/repo", node_id="worker-a"))
    assert res["ok"] is True, res
    case_id, tid = res["case_id"], res["task_id"]
    assert isinstance(tid, tq.TurnAdmission) and not tid.idempotent_replay
    json.dumps(res)  # the /api/manager envelope stays serializable
    row = db.get_task(tid)
    # Durable trigger identity: the invoke's Case, under the automation principal.
    assert row["queue_protocol"] == 1 and row["status"] == "queued"
    assert row["turn_source"] == "system" and row["turn_kind"] == "instruction"
    assert row["idempotency_scope"] == "automation:mgr-1:instruction"
    assert row["idempotency_key"] == f"manager_invoke:{case_id}"
    assert json.loads(row["payload"])["metadata"]["source"] == "manager_invoke"
    # Case / role-boot lineage: the first turn is attached to the invoke's Case.
    assert row["flow_run_id"] == case_id
    links = [l for l in db.list_flow_links(flow_run_id=case_id) if l["entity_type"] == "task"]
    assert [l["entity_id"] for l in links] == [tid]
    # Queued is not BUSY; no legacy queue.
    assert o.task_queue.qsize() == 0 and not o.active_tasks
    s = _sess("mgr-1")
    assert s.status == SessionStatus.IDLE and not s.last_task_id
    # Replay of the SAME invoke identity collapses (no second turn / link).
    again = asyncio.run(o.submit_instruction(
        description=row["prompt"], session_id="mgr-1", cwd="/tmp/repo",
        source="manager_invoke", operation_id=f"manager_invoke:{case_id}"))
    assert again == tid and again.idempotent_replay
    links = [l for l in db.list_flow_links(flow_run_id=case_id) if l["entity_type"] == "task"]
    assert len(links) == 1 and len(_managed_rows(db)) == 1
    # trigger → turn → completion: activation carries the assignment as the
    # Manager's first message; a carrier completion commits the native id.
    assert _pass(db, o).activated == 1
    payload = json.loads(db.get_task(tid)["payload"])
    assert payload["action"] == "create_session"
    assert payload["session"]["last_user_message"] == row["prompt"]
    tok = db.claim_turn(tid, "worker-a", "worker_daemon", "inc-1")
    db.start_turn(tid, tok, incarnation_id="inc-1")
    db.complete_turn(task_id=tid, claim_token=tok, result={"success": True, "output": "booted"},
                     status="completed", native_session_id="native-mgr")
    assert db.get_task(tid)["status"] == "completed"
    assert _sess("mgr-1").backend_session_id == "native-mgr"


# --------------------------------------------------------------------------- #
# PC02 — Docker routing (mesh on, gateway local execution off)
# --------------------------------------------------------------------------- #
def _docker(monkeypatch, local_carrier="local-daemon"):
    from config import config

    monkeypatch.setattr(config.mesh, "enabled", True)
    monkeypatch.setattr(config.system, "local_execution_enabled", False, raising=False)
    monkeypatch.setattr(config.mesh, "local_carrier_node_id", local_carrier)


def test_PC02_docker_unpinned_enrolled_turn_routes_to_local_carrier(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch, machine=None)
    _register_carrier(db, "local-daemon")
    _docker(monkeypatch)
    tid = _submit(o, operation_id="op-docker")
    assert isinstance(tid, tq.TurnAdmission)
    assert db.get_task(tid)["machine_id"] == "local-daemon"
    assert o.task_queue.qsize() == 0
    assert _pass(db, o).activated == 1
    assert db.get_task(tid)["status"] == "pending"


def test_PC02b_docker_one_off_and_unenrolled_session_still_refused(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch, enroll=False, machine=None)
    _register_carrier(db, "local-daemon")
    _docker(monkeypatch)
    with pytest.raises(HarnessAdmissionBlocked, match="local_execution_disabled"):
        _submit(o)  # unenrolled (legacy) unpinned session
    with pytest.raises(HarnessAdmissionBlocked, match="local_execution_disabled"):
        _submit(o, session_id=None, source="web_oneoff")  # one-off
    assert _managed_rows(db) == [] and o.task_queue.qsize() == 0


def test_PC02c_docker_unpinned_enrolled_without_local_carrier_is_typed_503(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch, machine=None)
    _docker(monkeypatch, local_carrier="")
    with pytest.raises(tq.CarrierUnavailableError):
        _submit(o)
    assert _managed_rows(db) == [] and o.task_queue.qsize() == 0


# --------------------------------------------------------------------------- #
# PC03 — offline-carrier admission policy
# --------------------------------------------------------------------------- #
def _offline(db, node="worker-a"):
    db._conn().execute("UPDATE nodes SET status = 'offline' WHERE node_id = ?", (node,))


def test_PC03_registered_offline_carrier_admits_queued_and_activates_on_return(
        tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _offline(db)
    tid = _submit(o, operation_id="op-off")
    assert isinstance(tid, tq.TurnAdmission) and tid.status == "queued"
    row = db.get_task(tid)
    assert row["status"] == "queued" and row["machine_id"] == "worker-a"
    assert row["blocked_reason"] == "carrier_offline: worker-a"
    # The scheduler keeps it queued with the SAME visible reason while offline.
    _unblock(db, tid)
    assert _pass(db, o).activated == 0
    row = db.get_task(tid)
    assert row["status"] == "queued" and row["blocked_reason"] == "carrier_offline: worker-a"
    # Carrier returns (re-registers) ⇒ activates on THAT node (never relocated).
    _register_carrier(db, "worker-a")
    _unblock(db, tid)
    assert _pass(db, o).activated == 1
    row = db.get_task(tid)
    assert row["status"] == "pending" and row["machine_id"] == "worker-a"


def test_PC03b_stale_heartbeat_counts_as_offline_registered(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    db._conn().execute("UPDATE nodes SET last_heartbeat = '2000-01-01T00:00:00+00:00'")
    tid = _submit(o, operation_id="op-stale")
    assert db.get_task(tid)["blocked_reason"] == "carrier_offline: worker-a"


def test_PC03c_policy_off_refuses_offline_carrier_503(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(TaskOrchestrator, "_QUEUE_TURNS_FOR_OFFLINE_CARRIER", False)
    _offline(db)
    with pytest.raises(tq.CarrierUnavailableError):
        _submit(o)
    assert _managed_rows(db) == []


@pytest.mark.parametrize("node,managed", [("ghost", None), ("legacy-node", ())],
                         ids=["unknown-node", "never-registered-managed"])
def test_PC03d_unknown_or_unmanaged_node_still_503(tmp_path, monkeypatch, node, managed):
    db, o = _setup(tmp_path, monkeypatch, machine=node)
    if managed is not None:
        _register_carrier(db, node, managed=managed)
        _offline(db, node)
    with pytest.raises(tq.CarrierUnavailableError):
        _submit(o)
    assert _managed_rows(db) == []


# --------------------------------------------------------------------------- #
# PC04 / PC05 — P2 at the orchestrator seam
# --------------------------------------------------------------------------- #
def test_PC04_staged_file_instruction_is_one_managed_turn(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    tid = _submit(o, description="read it\n\n📎 File: `uploads/spec.md`",
                  extra_metadata={"staged_file": dict(STAGED)}, operation_id="op-file")
    assert isinstance(tid, tq.TurnAdmission)
    assert json.loads(db.get_task(tid)["payload"])["metadata"]["staged_file"] == STAGED
    assert _pass(db, o).activated == 1
    payload = json.loads(db.get_task(tid)["payload"])
    assert payload["metadata"]["staged_file"] == STAGED  # the carrier fetches it
    assert payload["action"] in ("create_session", "resume_session")
    assert _sess().status == SessionStatus.IDLE


@pytest.mark.parametrize("bad", [
    {"file_id": "../etc", "filename": "a.txt"},
    {"file_id": "0123456789abcdef", "filename": "../../.bashrc"},
    {"file_id": "0123456789abcdef", "filename": ".."},
    {"file_id": "0123456789abcdef"},
], ids=["id-traversal", "name-traversal", "name-dots", "name-missing"])
def test_PC04b_malformed_staged_ref_refused_422(tmp_path, monkeypatch, bad):
    db, o = _setup(tmp_path, monkeypatch)
    with pytest.raises(tq.MalformedTurnError):
        _submit(o, extra_metadata={"staged_file": bad})
    assert _managed_rows(db) == [] and _protocol0_rows(db, "fetch_staged_file") == []


def test_PC05_file_only_delivery_is_protocol0_control_row(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    meta = {"staged_file": dict(STAGED), "task_type": "fetch_staged_file"}
    tid = _submit(o, description="File `uploads/spec.md` delivered to session.",
                  source="telegram_session", extra_metadata=meta)
    assert type(tid) is str and not isinstance(tid, tq.TurnAdmission)
    assert _managed_rows(db) == [] and o.task_queue.qsize() == 0
    rows = _protocol0_rows(db, "fetch_staged_file")
    assert [r["id"] for r in rows] == [tid]
    row = rows[0]
    assert row["status"] == "pending" and row["machine_id"] == "worker-a"
    payload = json.loads(row["payload"])
    assert payload["action"] == "fetch_staged_file"
    assert payload["metadata"]["staged_file"] == STAGED
    assert payload["session"]["repo_path"] == "/tmp/repo"
    s = _sess()
    assert s.status == SessionStatus.IDLE and not s.last_task_id
    # Durable identity = the staged file id: a replay inserts nothing.
    again = _submit(o, description="x", source="telegram_session", extra_metadata=dict(meta))
    assert again == tid and len(_protocol0_rows(db, "fetch_staged_file")) == 1


# --------------------------------------------------------------------------- #
# PC06 — P2 through the real web app
# --------------------------------------------------------------------------- #
def _web_app(tmp_path, monkeypatch, *, machine="worker-a"):
    from src.control import control_api
    from tests.test_turn_queue_4b import _wire

    repo = tmp_path / "repo"
    repo.mkdir()
    db, o = _setup(tmp_path, monkeypatch, machine=machine)
    _wire(o)
    s = _sess()
    s.repo_path = str(repo)
    o.session_store.save(s)
    stage_root = tmp_path / "state" / "uploads"
    monkeypatch.setattr(control_api, "_upload_staging_root", lambda: stage_root)
    return db, o, repo, stage_root, _client(monkeypatch, o)


@pytest.mark.parametrize("machine", ["worker-a", None], ids=["pinned", "unpinned-local"])
def test_PC06_web_upload_with_instruction_is_managed_attached_turn(tmp_path, monkeypatch, machine):
    from config import config

    if machine is None:
        monkeypatch.setattr(config.mesh, "local_carrier_node_id", "worker-a")
    db, o, repo, stage_root, c = _web_app(tmp_path, monkeypatch, machine=machine)
    r = c.post("/api/sessions/sess-1/upload", headers={"Authorization": "Bearer tok"},
               files={"file": ("spec.md", b"# spec")}, data={"instruction": "read it"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivery"] == "attached" and body["staged_file"]["filename"] == "spec.md"
    rows = _managed_rows(db)
    assert [row["id"] for row in rows] == [body["task_id"]]
    meta = json.loads(rows[0]["payload"])["metadata"]
    assert meta["staged_file"] == body["staged_file"]
    assert rows[0]["prompt"] == "read it\n\n📎 File: `uploads/spec.md`"
    # Staged for the carrier; never written into the repo by the gateway.
    staged = list(stage_root.glob("*/spec.md"))
    assert len(staged) == 1 and staged[0].read_bytes() == b"# spec"
    assert not (repo / "uploads").exists()
    s = _sess()
    assert s.status == SessionStatus.IDLE and s.last_task_id != body["task_id"]


def test_PC06b_web_upload_without_instruction_carries_to_next_instruction(tmp_path, monkeypatch):
    db, o, repo, stage_root, c = _web_app(tmp_path, monkeypatch)
    r = c.post("/api/sessions/sess-1/upload", headers={"Authorization": "Bearer tok"},
               files={"file": ("spec.md", b"# spec")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivery"] == "pending_instruction" and _managed_rows(db) == []
    r = c.post("/api/instructions", headers={"Authorization": "Bearer tok"}, json={
        "description": "now read it", "session_id": "sess-1",
        "upload_attachment": {"filename": "spec.md", "path": "uploads/spec.md",
                              "staged_file": body["staged_file"]},
    })
    assert r.status_code == 200, r.text
    rows = _managed_rows(db)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["metadata"]["staged_file"] == body["staged_file"]
    assert _sess().status == SessionStatus.IDLE


def test_PC06c_web_upload_refused_admission_cleans_stage_and_keeps_status(tmp_path, monkeypatch):
    db, o, repo, stage_root, c = _web_app(tmp_path, monkeypatch)
    db._conn().execute("UPDATE nodes SET managed_backends = '[]'")
    r = c.post("/api/sessions/sess-1/upload", headers={"Authorization": "Bearer tok"},
               files={"file": ("spec.md", b"# spec")}, data={"instruction": "read it"})
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "carrier_unavailable"
    assert list(stage_root.glob("*/spec.md")) == []
    assert _managed_rows(db) == [] and _sess().status == SessionStatus.IDLE


# --------------------------------------------------------------------------- #
# PC07 — P2 through the Telegram document handler
# --------------------------------------------------------------------------- #
def _tg_document(o, tmp_path, monkeypatch, caption):
    from src.telegram import interface as tg_mod
    from src.telegram.interface import TelegramInterface

    stage_root = tmp_path / "state" / "uploads"
    monkeypatch.setattr(tg_mod, "_upload_staging_root", lambda: stage_root)
    tg = TelegramInterface.__new__(TelegramInterface)
    sess = o.session_store.get("sess-1")
    saved = []
    tg.session_store = types.SimpleNamespace(get_active=lambda _c: sess, save=saved.append)
    tg._user_can_access_session = lambda *_a: True
    tg._check_user_permission = lambda *_a: True
    tg.orchestrator = o

    async def _no_flush(_chat):
        return None

    tg._flush_buffer = _no_flush
    replies = []

    async def reply_text(text, **k):
        replies.append(text)

    async def download_to_drive(custom_path):
        from pathlib import Path

        Path(custom_path).write_bytes(b"# spec")

    async def get_file(_fid):
        return types.SimpleNamespace(download_to_drive=download_to_drive)

    doc = types.SimpleNamespace(file_id="tg-file-1", file_name="spec.md", file_size=6)
    update = types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=1), effective_user=types.SimpleNamespace(id=1),
        message=types.SimpleNamespace(reply_text=reply_text, document=doc, photo=None,
                                      caption=caption),
    )
    context = types.SimpleNamespace(bot=types.SimpleNamespace(get_file=get_file))
    asyncio.run(tg._handle_document(update, context))
    return sess, saved, replies, stage_root


def test_PC07_telegram_document_with_caption_is_queued_attached_turn(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    sess, saved, replies, stage_root = _tg_document(o, tmp_path, monkeypatch, "read it")
    rows = _managed_rows(db)
    assert len(rows) == 1, replies
    meta = json.loads(rows[0]["payload"])["metadata"]
    assert meta["staged_file"]["filename"] == "spec.md"
    assert rows[0]["prompt"] == "read it\n\n📎 File: `uploads/spec.md`"
    assert rows[0]["turn_source"] == "human"
    assert len(list(stage_root.glob("*/spec.md"))) == 1
    assert sess.status == SessionStatus.IDLE and saved == []
    assert replies and "📥 Queued" in replies[-1]


def test_PC07b_telegram_document_without_caption_is_file_delivery_control_row(
        tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    sess, saved, replies, stage_root = _tg_document(o, tmp_path, monkeypatch, "")
    assert _managed_rows(db) == []
    rows = _protocol0_rows(db, "fetch_staged_file")
    assert len(rows) == 1 and rows[0]["machine_id"] == "worker-a"
    assert sess.status == SessionStatus.IDLE and saved == []
    assert replies and "being delivered" in replies[-1]


def test_PC07c_telegram_refusal_cleans_stage_and_is_honest(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    db._conn().execute("UPDATE nodes SET managed_backends = '[]'")
    sess, saved, replies, stage_root = _tg_document(o, tmp_path, monkeypatch, "read it")
    assert _managed_rows(db) == [] and list(stage_root.glob("*/spec.md")) == []
    assert sess.status == SessionStatus.IDLE and saved == []
    assert replies and replies[-1].startswith("❌ Not queued (carrier_unavailable)")


# --------------------------------------------------------------------------- #
# PC08 — carrier: fetch BEFORE invoke; fetch failure ⇒ not-invoked release
# --------------------------------------------------------------------------- #
class _StagedBackend:
    def __init__(self, repo, log=None):
        self.repo = repo
        self.seen = []
        self.log = log

    def supports_managed_turns(self) -> bool:
        return True

    def is_quiescent(self, session) -> bool:
        return True

    def run_managed_turn(self, session, prompt, ownership, *, on_process=None, **_k):
        f = self.repo / "uploads" / "spec.md"
        self.seen.append(f.read_bytes() if f.exists() else None)
        if self.log is not None:
            self.log.append(("INVOKE",))
        return ExecutionResult(success=True, output="read", errors=[],
                               backend_session_id="native-1")


class _StageHTTP:
    def __init__(self, data=b"# spec", fail=False):
        self.data, self.fail, self.calls = data, fail, []

    def get_bytes(self, path, timeout=60):
        self.calls.append(("GET", path))
        if self.fail:
            raise OSError("404 Staged file not found")
        return self.data

    def delete(self, path, timeout=10):
        self.calls.append(("DELETE", path))
        return {"status": "deleted"}


def _staged_row(repo):
    return {"id": "t-1", "backend": "claude", "action": "resume_session", "payload": {
        "prompt": "read it", "metadata": {"staged_file": dict(STAGED)},
        "session": {"session_id": "s", "backend": "claude", "backend_session_id": "n",
                    "repo_path": str(repo)}}}


def test_PC08_carrier_fetches_staged_file_before_managed_invoke(tmp_path):
    from src.worker import agent as agent_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    http = _StageHTTP()
    b = _StagedBackend(repo, log=http.calls)
    own = tq.ManagedTurnOwnership(task_id="t-1", session_id="s", node_id="n", claim_token="tok")
    out = asyncio.run(agent_mod._execute_task(_staged_row(repo), {"claude": b}, http, ownership=own))
    assert out["success"] is True and b.seen == [b"# spec"]
    # The staged copy is removed only AFTER the prompt was submitted.
    assert http.calls == [("GET", f"/files/{STAGED['file_id']}"), ("INVOKE",),
                          ("DELETE", f"/files/{STAGED['file_id']}")]


def test_PC08b_carrier_fetch_failure_never_invokes_backend(tmp_path):
    from src.worker import agent as agent_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    b = _StagedBackend(repo)
    http = _StageHTTP(fail=True)
    own = tq.ManagedTurnOwnership(task_id="t-1", session_id="s", node_id="n", claim_token="tok")
    out = asyncio.run(agent_mod._execute_task(_staged_row(repo), {"claude": b}, http, ownership=own))
    assert b.seen == [] and out["success"] is False
    assert out["error_class"] == "managed_conflict"
    assert out["errors"][0].startswith("staged_file_unavailable")
    assert ("DELETE", f"/files/{STAGED['file_id']}") not in http.calls
    # Legacy (no ownership) keeps its behavior: runs without the file.
    out = asyncio.run(agent_mod._execute_task(_staged_row(repo), {"claude": _LegacyRun()}, http))
    assert out["success"] is True


class _LegacyRun:
    def resume_session(self, session, message, **_k):
        return ExecutionResult(success=True, output="legacy", errors=[])


def test_PC08c_fetch_failure_through_real_carrier_is_not_invoked_release(tmp_path, monkeypatch):
    """Real task server + carrier ``_handle_task``: a staged turn whose file
    cannot be fetched is released NOT invoked (back to pending, visible
    reason), its prompt kept — never run, never failed."""
    from fastapi.testclient import TestClient

    import src.control.node_registry as nr_mod
    from src.control import task_server as tsrv
    from tests.test_turn_queue_carrier_integration import (
        NODE, TOKEN, _ClientHTTP, _worker,
    )

    mdb = db_mod.MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(tsrv, "get_db", lambda: mdb)
    monkeypatch.setattr(db_mod, "get_db", lambda: mdb)
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    monkeypatch.setattr(tsrv, "_worker_token", lambda: TOKEN)

    class _HTTP(_ClientHTTP):
        def get_bytes(self, path, timeout=60):
            self.calls.append(("GET", path, None))
            resp = self.client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
            if resp.status_code >= 400:
                raise OSError(f"{resp.status_code} {resp.text}")
            return resp.content

        def delete(self, path, timeout=10):
            self.calls.append(("DELETE", path, None))
            return self.client.delete(path, headers={"Authorization": f"Bearer {TOKEN}"}).json()

    monkeypatch.setattr(tsrv, "_STAGING_ROOT", tmp_path / "state" / "uploads")
    http = _HTTP(TestClient(tsrv.app))
    w = _worker(tmp_path, http)
    repo = tmp_path / "repo"
    repo.mkdir()
    b = _StagedBackend(repo)
    w._backends = {"claude": b}
    mdb.upsert_session(Session(session_id="sess-1", backend="claude", repo_path=str(repo),
                               status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW,
                               machine_id=NODE, backend_session_id="n"))
    mdb.enroll_session("sess-1")
    payload = _staged_row(repo)["payload"]
    payload["task_id"] = "t-1"
    mdb.enqueue_turn(task_id="t-1", session_id="sess-1", backend="claude",
                     action="resume_session", payload=payload, turn_source="human",
                     turn_kind="instruction", machine_id=NODE)
    mdb.activate_turn("t-1")

    async def scenario():
        rows = [r for r in await w._fetch_pending() if r["id"] == "t-1"]
        assert rows
        await w._handle_task(rows[0])

    asyncio.run(scenario())
    row = dict(mdb._conn().execute("SELECT * FROM mesh_tasks WHERE id='t-1'").fetchone())
    assert b.seen == []  # the backend was never invoked
    assert row["status"] == "pending" and row["prompt"] == "read it"
    assert "staged_file_unavailable" in (row["blocked_reason"] or "")
    assert not row["result"]
    posted = [c[1] for c in http.calls if c[0] == "POST"]
    assert "/tasks/t-1/release-managed" in posted and "/tasks/t-1/result-managed" not in posted
    # Once the file is staged, the next attempt fetches it and runs the turn.
    stage = tmp_path / "state" / "uploads" / STAGED["file_id"]
    stage.mkdir(parents=True)
    (stage / "spec.md").write_bytes(b"# spec")

    async def again():
        rows = [r for r in await w._fetch_pending() if r["id"] == "t-1"]
        assert rows
        await w._handle_task(rows[0])

    asyncio.run(again())
    assert b.seen == [b"# spec"]
    assert mdb.get_task("t-1")["status"] == "completed"
    assert not stage.exists()  # removed after the turn ran
