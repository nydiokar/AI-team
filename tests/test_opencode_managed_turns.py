"""[A82 Step 4b] OpenCode server backend implements the managed-turn contract.

Drives the REAL ``OpenCodeServerBackend`` code against a fake OpenCode HTTP +
SSE server (stdlib ``ThreadingHTTPServer``). The fake supports delayed / lost
prompt acks, foreign messages, busy status, server death and history
reconciliation. The stand-in "server process" is a real ``sleep`` child so the
backend's process identity / gone-proof is real. No opencode binary and no
model are ever invoked: ``_exe`` points at a nonexistent path so any accidental
spawn fails closed.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

import src.control.task_server as ts
from src.backends.opencode import OpenCodeBackend, OpenCodeServerBackend, managed_message_id
from src.control.turn_queue import ManagedTurnOwnership, OwnershipConflictError
from src.core.interfaces import Session, SessionStatus
from src.core.process_utils import process_gone_proof

# --------------------------------------------------------------------------- #
# Fake OpenCode server
# --------------------------------------------------------------------------- #
class FakeOpenCode:
    """In-process OpenCode 1.18-shaped HTTP+SSE server (only what the backend uses)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: Dict[str, List[Dict[str, Any]]] = {}
        self.busy: Dict[str, bool] = {}
        self.prompts: List[Dict[str, Any]] = []
        self.aborts: List[str] = []
        self.summaries: List[Dict[str, Any]] = []
        self.created: List[str] = []
        self.aborted_ids: set[str] = set()
        self.release = threading.Event()
        self.release.set()            # cleared ⇒ replies are held (turn keeps running)
        self.ack_delay = 0.0          # seconds to delay the prompt_async ack
        self.record_prompt = True     # False ⇒ the prompt is "lost" (never recorded)
        self.reply_text = "managed reply"
        self.summarize_error = False
        self.status_error = False
        self.idle_while_held = False  # status omits the session while the reply is held
        self._clock = 1_000
        self._n = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:
                return None

            def _send(self, code: int, body: Any = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def _body(self) -> Dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def do_GET(self) -> None:  # noqa: N802
                fake.handle(self, "GET")

            def do_POST(self) -> None:  # noqa: N802
                fake.handle(self, "POST")

            def do_DELETE(self) -> None:  # noqa: N802
                fake.handle(self, "DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.alive = True

    # -- state helpers ------------------------------------------------------ #
    def tick(self) -> int:
        with self.lock:
            self._clock += 1
            return self._clock

    def new_id(self, prefix: str) -> str:
        with self.lock:
            self._n += 1
            return f"{prefix}_fake{self._n:06d}"

    def add_session(self, oc_id: str) -> None:
        with self.lock:
            self.sessions.setdefault(oc_id, [])

    def add_user(self, oc_id: str, msg_id: str, *, compaction: bool = False) -> None:
        part = {"type": "compaction"} if compaction else {"type": "text", "text": "prompt"}
        msg = {"info": {"id": msg_id, "role": "user", "sessionID": oc_id,
                        "time": {"created": self.tick()}}, "parts": [part]}
        with self.lock:
            self.sessions[oc_id].append(msg)

    def add_assistant(self, oc_id: str, parent_id: str, text: str, *, error: Optional[str] = None) -> None:
        info: Dict[str, Any] = {"id": self.new_id("msg"), "role": "assistant", "sessionID": oc_id,
                                "parentID": parent_id, "time": {"created": self.tick(), "completed": self.tick()}}
        parts: List[Dict[str, Any]] = []
        if error:
            info["error"] = {"name": "MessageAbortedError", "data": {"message": error}}
        else:
            info["finish"] = "stop"
            parts = [{"type": "text", "text": text}, {"type": "step-finish", "reason": "stop"}]
        with self.lock:
            self.sessions[oc_id].append({"info": info, "parts": parts})

    def _run_reply(self, oc_id: str, msg_id: str) -> None:
        self.release.wait(30)
        with self.lock:
            aborted = msg_id in self.aborted_ids
        if not aborted:
            self.add_assistant(oc_id, msg_id, self.reply_text)
        with self.lock:
            self.busy.pop(oc_id, None)

    def handle(self, h: Any, method: str) -> None:
        if not self.alive:
            h.close_connection = True
            return
        parsed = urlparse(h.path)
        path, query = parsed.path, parsed.query
        parts = [p for p in path.split("/") if p]
        if method == "GET" and path == "/event":
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            try:
                h.wfile.write(b'data: {"payload":{"type":"server.connected","properties":{}}}\n')
                h.wfile.flush()
                while self.alive:
                    time.sleep(0.2)
                    h.wfile.write(b": hb\n")
                    h.wfile.flush()
            except OSError:
                pass
            h.close_connection = True
            return
        if method == "GET" and path == "/global/health":
            return h._send(200, {"healthy": True})
        if method == "GET" and path == "/session/status":
            if self.status_error:
                return h._send(500, {"name": "UnknownError"})
            with self.lock:
                return h._send(200, {k: {"type": "busy"} for k, v in self.busy.items() if v})
        if method == "POST" and path == "/session":
            body = h._body()
            oc_id = self.new_id("ses")
            self.add_session(oc_id)
            self.created.append(oc_id)
            return h._send(200, {"id": oc_id, "title": body.get("title")})
        if len(parts) >= 2 and parts[0] == "session":
            oc_id = parts[1]
            with self.lock:
                exists = oc_id in self.sessions
            if not exists:
                return h._send(404, {"name": "NotFoundError", "data": {"message": f"Session not found: {oc_id}"}})
            if method == "GET" and len(parts) == 2:
                return h._send(200, {"id": oc_id})
            if method == "GET" and len(parts) == 3 and parts[2] == "message":
                limit = int((query.split("limit=")[1] if "limit=" in query else "100").split("&")[0])
                with self.lock:
                    return h._send(200, list(self.sessions[oc_id][-limit:]))
            if method == "GET" and len(parts) == 4 and parts[2] == "message":
                with self.lock:
                    found = [m for m in self.sessions[oc_id] if m["info"]["id"] == parts[3]]
                if found:
                    return h._send(200, found[0])
                return h._send(404, {"name": "NotFoundError", "data": {"message": "Message not found"}})
            if method == "POST" and parts[2:] == ["prompt_async"]:
                body = h._body()
                msg_id = body.get("messageID") or self.new_id("msg")
                assert str(msg_id).startswith("msg"), "OpenCode rejects ids without the msg prefix"
                self.prompts.append({"session": oc_id, **body})
                if self.record_prompt:
                    self.add_user(oc_id, msg_id)
                    with self.lock:
                        self.busy[oc_id] = not self.idle_while_held
                    threading.Thread(target=self._run_reply, args=(oc_id, msg_id), daemon=True).start()
                if self.ack_delay:
                    time.sleep(self.ack_delay)
                return h._send(204)
            if method == "POST" and parts[2:] == ["abort"]:
                self.aborts.append(oc_id)
                with self.lock:
                    users = [m for m in self.sessions[oc_id] if m["info"]["role"] == "user"]
                    running = self.busy.get(oc_id)
                if running and users:
                    last = users[-1]["info"]["id"]
                    with self.lock:
                        self.aborted_ids.add(last)
                    self.add_assistant(oc_id, last, "", error="aborted")
                    with self.lock:
                        self.busy.pop(oc_id, None)
                return h._send(200, True)
            if method == "POST" and parts[2:] == ["summarize"]:
                body = h._body()
                self.summaries.append({"session": oc_id, **body})
                if self.summarize_error:
                    return h._send(500, {"name": "UnknownError"})
                cid = self.new_id("msg")
                self.add_user(oc_id, cid, compaction=True)
                with self.lock:
                    self.busy[oc_id] = True
                self.release.wait(30)
                with self.lock:
                    aborted = cid in self.aborted_ids
                if not aborted:
                    self.add_assistant(oc_id, cid, "summary")
                with self.lock:
                    self.busy.pop(oc_id, None)
                return h._send(200, True)
        return h._send(404, {"name": "NotFoundError", "data": {"message": path}})

    def die(self) -> None:
        self.alive = False
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake():
    f = FakeOpenCode()
    yield f
    if f.alive:
        f.release.set()
        f.die()


@pytest.fixture
def stand_in_proc():
    proc = subprocess.Popen(["sleep", "300"])
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


@pytest.fixture
def backend(fake, stand_in_proc, tmp_path, monkeypatch):
    import src.core.test_guard as tg

    monkeypatch.setattr(tg, "assert_live_calls_allowed", lambda _name: None)
    oc = OpenCodeServerBackend()
    oc._exe = str(tmp_path / "no-such-opencode-binary")  # any real spawn fails closed
    key = oc._server_key(str(tmp_path))
    oc._procs[key] = stand_in_proc
    oc._base_urls[key] = fake.url
    # Fast clocks for tests (production defaults are seconds/minutes).
    oc._MANAGED_POLL_SEC = 0.05
    oc._MANAGED_ACK_TIMEOUT_SEC = 1
    oc._MANAGED_RECONCILE_TRIES = 3
    oc._MANAGED_IDLE_GRACE_POLLS = 3
    oc._MANAGED_ABSENT_GRACE_SEC = 0.5
    oc._managed_deadline_sec = lambda: 20.0
    return oc


def _session(tmp_path, sid: str = "gw-1", native: str = "") -> Session:
    return Session(
        session_id=sid, backend="opencode-server", repo_path=str(tmp_path),
        status=SessionStatus.IDLE, created_at="2026-10-02T00:00:00",
        updated_at="2026-10-02T00:00:00", last_user_message="do it",
        backend_session_id=native,
    )


def _own(sid: str = "gw-1", turn: str = "turn-uuid-1") -> ManagedTurnOwnership:
    return ManagedTurnOwnership(task_id=f"t-{turn}", session_id=sid, node_id="n",
                                claim_token="tok", turn_uuid=turn)


def _bg(fn, *a, **k):
    out: Dict[str, Any] = {}

    def run() -> None:
        try:
            out["result"] = fn(*a, **k)
        except BaseException as e:  # noqa: BLE001
            out["exc"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, out


def _wait(pred, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


# --------------------------------------------------------------------------- #
# Capability / identity
# --------------------------------------------------------------------------- #
def test_capability_matrix_server_supports_cli_fails_closed():
    assert OpenCodeServerBackend().supports_managed_turns() is True
    cli = OpenCodeBackend()
    assert cli.supports_managed_turns() is False
    # Sender capability: OpenCode MCP config is per server process (shared by
    # every session in the repo) ⇒ never provisioned (fail closed).
    assert OpenCodeServerBackend().provision_sender_capability("gw-1", "secret") is False
    assert OpenCodeServerBackend().provision_sender_capability("gw-1", None) is False


def test_managed_message_id_is_deterministic_and_native_shaped():
    a = managed_message_id("2f1c-uuid")
    assert a == managed_message_id("2f1c-uuid")
    assert a != managed_message_id("other-uuid")
    # OpenCode Identifier: "msg_" + 12 hex (6 bytes) + 14 base62 chars.
    assert a.startswith("msg_") and len(a) == 30
    int(a[4:16], 16)
    assert a[16:].isalnum()


# --------------------------------------------------------------------------- #
# Happy path, attribution, process identity
# --------------------------------------------------------------------------- #
def test_managed_turn_creates_native_session_submits_deterministic_id(backend, fake, tmp_path, stand_in_proc):
    seen: List[Dict[str, Any]] = []

    def on_process(ident: Dict[str, Any]) -> None:
        seen.append(dict(ident))
        assert fake.prompts == [], "process identity must be reported BEFORE submit"

    s = _session(tmp_path)
    res = backend.run_managed_turn(s, "hello", _own(), on_process=on_process)
    assert res.success is True, res.errors
    assert res.output == "managed reply"
    assert res.backend_session_id == fake.created[0]
    assert fake.prompts[0]["messageID"] == managed_message_id("turn-uuid-1")
    assert seen and seen[0]["pid"] == stand_in_proc.pid
    assert fake.aborts == []
    assert backend.is_quiescent(s) is True


def test_ownership_mismatch_raises_typed_conflict(backend, tmp_path):
    with pytest.raises(OwnershipConflictError):
        backend.run_managed_turn(_session(tmp_path, sid="gw-1"), "x", _own(sid="gw-OTHER"))


def test_busy_native_session_is_conflict_and_never_interrupted(backend, fake, tmp_path):
    fake.add_session("ses_busy")
    fake.busy["ses_busy"] = True
    res = backend.run_managed_turn(_session(tmp_path, native="ses_busy"), "x", _own())
    assert res.success is False and res.error_class == "managed_conflict"
    assert fake.prompts == [] and fake.aborts == []
    assert backend.is_quiescent(_session(tmp_path, native="ses_busy")) is False


def test_local_active_turn_is_conflict(backend, fake, tmp_path):
    fake.add_session("ses_a")
    fake.release.clear()
    th, out = _bg(backend.run_managed_turn, _session(tmp_path, native="ses_a"), "first", _own(turn="u-a"))
    _wait(lambda: len(fake.prompts) == 1)
    res = backend.run_managed_turn(_session(tmp_path, native="ses_a"), "second", _own(turn="u-b"))
    assert res.error_class == "managed_conflict"
    assert len(fake.prompts) == 1 and fake.aborts == []
    fake.release.set()
    th.join(5)
    assert out["result"].success is True


def test_foreign_and_late_messages_are_never_our_result(backend, fake, tmp_path):
    fake.add_session("ses_f")
    fake.release.clear()
    fake.idle_while_held = True             # idle window: history is consulted
    backend._MANAGED_IDLE_GRACE_POLLS = 10_000
    th, out = _bg(backend.run_managed_turn, _session(tmp_path, native="ses_f"), "mine", _own(turn="u-f"))
    _wait(lambda: len(fake.prompts) == 1)
    # A late reply of an EARLIER turn and a foreign user/assistant pair land
    # while ours runs; then the session goes idle before our reply exists.
    fake.add_assistant("ses_f", "msg_earlier_turn", "LATE FOREIGN REPLY")
    fake.add_user("ses_f", "msg_foreign_user")
    fake.add_assistant("ses_f", "msg_foreign_user", "FOREIGN REPLY")
    time.sleep(0.3)
    assert "result" not in out, "a foreign reply must never complete our turn"
    fake.release.set()
    th.join(5)
    res = out["result"]
    assert res.success is True and res.output == "managed reply"
    assert "FOREIGN" not in res.output


def test_lost_ack_reconciled_by_our_message_id(backend, fake, tmp_path):
    fake.add_session("ses_l")
    fake.ack_delay = 2.0  # > ack timeout ⇒ the client never sees the 204
    res = backend.run_managed_turn(_session(tmp_path, native="ses_l"), "x", _own(turn="u-l"))
    assert res.success is True and res.output == "managed reply"
    assert len(fake.prompts) == 1, "never resubmitted"
    assert fake.aborts == [], "ambiguity is never resolved by aborting"


def test_lost_ack_never_recorded_is_recovery_required(backend, fake, tmp_path):
    fake.add_session("ses_r")
    fake.ack_delay = 2.0
    fake.record_prompt = False
    s = _session(tmp_path, native="ses_r")
    res = backend.run_managed_turn(s, "x", _own(turn="u-r"))
    assert res.success is False and res.error_class == "recovery_required"
    assert fake.aborts == []
    # held entry blocks a new turn on the session until resolved natively
    assert backend.is_quiescent(_session(tmp_path, native="ses_r")) in (True, False)
    time.sleep(0.6)  # past the absent grace: idle + never recorded ⇒ resolved
    assert backend.is_quiescent(_session(tmp_path, native="ses_r")) is True


def test_deadline_is_recovery_required_without_abort_and_late_reply_not_bound(backend, fake, tmp_path):
    fake.add_session("ses_d")
    fake.release.clear()
    backend._managed_deadline_sec = lambda: 0.5
    s = _session(tmp_path, native="ses_d")
    res = backend.run_managed_turn(s, "x", _own(turn="u-d"))
    assert res.error_class == "recovery_required"
    assert fake.aborts == []
    # carrier reconcile path: session rebuilt from the id only
    bare = Session(session_id="gw-1", backend="opencode-server", repo_path="", status=SessionStatus.IDLE,
                   created_at="x", updated_at="x")
    assert backend.is_quiescent(bare) is False, "native turn still running ⇒ not quiescent"
    # a NEW turn on the session is refused while the held one runs
    res2 = backend.run_managed_turn(s, "next", _own(turn="u-d2"))
    assert res2.error_class == "managed_conflict"
    fake.release.set()
    _wait(lambda: not fake.busy.get("ses_d"))
    assert backend.is_quiescent(bare) is True
    assert backend.forget_managed_turn(s, "u-d") is True
    # the late reply is in native history parented to OUR id only
    late = [m for m in fake.sessions["ses_d"] if m["info"].get("parentID") == managed_message_id("u-d")]
    assert late and late[0]["parts"][0]["text"] == "managed reply"


def test_carrier_crash_attribution_by_deterministic_id(fake, stand_in_proc, tmp_path, monkeypatch):
    """A previous carrier submitted under managed_message_id(turn_uuid) and died;
    a successor backend instance re-invoked for the SAME attempt binds to that
    native turn by id (no resubmit) and never to a foreign reply."""
    import src.core.test_guard as tg

    monkeypatch.setattr(tg, "assert_live_calls_allowed", lambda _name: None)
    mid = managed_message_id("u-crash")
    fake.add_session("ses_c")
    fake.add_user("ses_c", "msg_foreign_before")
    fake.add_assistant("ses_c", "msg_foreign_before", "FOREIGN")
    fake.add_user("ses_c", mid)
    fake.add_assistant("ses_c", mid, "reply of the crashed attempt")
    oc = OpenCodeServerBackend()
    oc._exe = str(tmp_path / "nope")
    key = oc._server_key(str(tmp_path))
    oc._procs[key] = stand_in_proc
    oc._base_urls[key] = fake.url
    oc._MANAGED_POLL_SEC = 0.05
    res = oc.run_managed_turn(_session(tmp_path, native="ses_c"), "x", _own(turn="u-crash"))
    assert res.success is True and res.output == "reply of the crashed attempt"
    assert fake.prompts == [], "the attempt's prompt is never resubmitted"


def test_server_death_mid_turn_is_recovery_with_process_gone_proof(backend, fake, tmp_path, stand_in_proc):
    fake.add_session("ses_x")
    fake.release.clear()
    idents: List[Dict[str, Any]] = []
    th, out = _bg(backend.run_managed_turn, _session(tmp_path, native="ses_x"), "x", _own(turn="u-x"),
                  on_process=idents.append)
    _wait(lambda: len(fake.prompts) == 1)
    assert process_gone_proof(idents[0]) is None, "server alive ⇒ no proof"
    stand_in_proc.kill()
    stand_in_proc.wait(5)
    fake.die()
    th.join(10)
    assert out["result"].error_class == "recovery_required"
    assert process_gone_proof(idents[0]) is not None, "dead shared server ⇒ successor has proof"


def test_native_session_lost_goes_to_recovery_not_recreated(backend, fake, tmp_path):
    res = backend.run_managed_turn(_session(tmp_path, native="ses_gone"), "x", _own(turn="u-g"))
    assert res.error_class == "recovery_required"
    assert fake.created == [] and fake.prompts == []


# --------------------------------------------------------------------------- #
# Quiescence
# --------------------------------------------------------------------------- #
def test_quiescence_unknown_or_busy_is_false(backend, fake, tmp_path):
    fake.add_session("ses_q")
    s = _session(tmp_path, native="ses_q")
    assert backend.is_quiescent(s) is True
    fake.busy["ses_q"] = True
    assert backend.is_quiescent(s) is False
    fake.busy.pop("ses_q")
    fake.status_error = True
    assert backend.is_quiescent(s) is False, "unknown ⇒ not quiescent"
    fake.status_error = False
    # no native session yet ⇒ nothing can be in flight
    assert backend.is_quiescent(_session(tmp_path, sid="gw-new", native="")) is True
    # unreachable server (no live server; spawn fails closed) ⇒ False
    other = OpenCodeServerBackend()
    other._exe = str(tmp_path / "nope")
    assert other.is_quiescent(_session(tmp_path, native="ses_q")) is False


# --------------------------------------------------------------------------- #
# Cancel
# --------------------------------------------------------------------------- #
def test_cancel_aborts_only_our_running_turn(backend, fake, tmp_path):
    fake.add_session("ses_k")
    fake.release.clear()
    th, out = _bg(backend.run_managed_turn, _session(tmp_path, native="ses_k"), "x", _own(turn="u-k"))
    _wait(lambda: len(fake.prompts) == 1)
    assert backend.cancel_managed_turn(_session(tmp_path), "u-k") is True
    th.join(5)
    assert fake.aborts == ["ses_k"]
    assert out["result"].success is False and out["result"].error_class == "cancelled"
    fake.release.set()


def test_cancel_armed_before_start_never_submits(backend, fake, tmp_path):
    fake.add_session("ses_p")
    assert backend.cancel_managed_turn(_session(tmp_path), "u-p") is True
    res = backend.run_managed_turn(_session(tmp_path, native="ses_p"), "x", _own(turn="u-p"))
    assert res.error_class == "cancelled"
    assert fake.prompts == [] and fake.aborts == []


def test_cancel_refused_when_another_message_runs(backend, fake, tmp_path):
    fake.add_session("ses_o")
    fake.release.clear()
    th, out = _bg(backend.run_managed_turn, _session(tmp_path, native="ses_o"), "x", _own(turn="u-o"))
    _wait(lambda: len(fake.prompts) == 1)
    fake.add_user("ses_o", "msg_someone_else")  # a foreign prompt is now the running one
    assert backend.cancel_managed_turn(_session(tmp_path), "u-o") is False
    assert fake.aborts == []
    fake.release.set()
    th.join(5)
    assert out["result"].success is True


def test_cancel_unknown_turn_never_aborts(backend, fake, tmp_path):
    fake.add_session("ses_u")
    fake.busy["ses_u"] = True
    backend.cancel_managed_turn(_session(tmp_path, native="ses_u"), "u-never-ran")
    assert fake.aborts == []
    assert backend.forget_managed_turn(_session(tmp_path), "u-never-ran") is True


# --------------------------------------------------------------------------- #
# Compaction
# --------------------------------------------------------------------------- #
def test_managed_compaction_native_summarize(backend, fake, tmp_path, monkeypatch):
    fake.add_session("ses_s")
    monkeypatch.setattr(backend, "_parse_model", lambda _m: ("model-x", "prov-x"))
    seen: List[Dict[str, Any]] = []
    res = backend.run_managed_compaction(_session(tmp_path, native="ses_s"), _own(turn="u-s"), on_process=seen.append)
    assert res.success is True, res.errors
    assert fake.summaries[0]["providerID"] == "prov-x" and fake.summaries[0]["modelID"] == "model-x"
    assert seen and fake.aborts == []


def test_managed_compaction_conflict_when_busy_and_recovery_when_ambiguous(backend, fake, tmp_path, monkeypatch):
    fake.add_session("ses_t")
    monkeypatch.setattr(backend, "_parse_model", lambda _m: ("m", "p"))
    fake.busy["ses_t"] = True
    res = backend.run_managed_compaction(_session(tmp_path, native="ses_t"), _own(turn="u-t1"))
    assert res.error_class == "managed_conflict" and fake.summaries == []
    fake.busy.pop("ses_t")
    fake.summarize_error = True
    res = backend.run_managed_compaction(_session(tmp_path, native="ses_t"), _own(turn="u-t2"))
    assert res.error_class == "recovery_required"
    assert fake.aborts == []


def test_cancel_managed_compaction_aborts_only_the_compaction(backend, fake, tmp_path, monkeypatch):
    fake.add_session("ses_v")
    monkeypatch.setattr(backend, "_parse_model", lambda _m: ("m", "p"))
    fake.release.clear()
    th, out = _bg(backend.run_managed_compaction, _session(tmp_path, native="ses_v"), _own(turn="u-v"))
    _wait(lambda: len(fake.summaries) == 1 and fake.busy.get("ses_v"))
    assert backend.cancel_managed_turn(_session(tmp_path), "u-v") is True
    assert fake.aborts == ["ses_v"]
    fake.release.set()
    th.join(5)
    assert out["result"].error_class == "cancelled"


# --------------------------------------------------------------------------- #
# Carrier integration: real worker _handle_task + in-process task server + MeshDB
# --------------------------------------------------------------------------- #
from tests.test_turn_queue_carrier_integration import (  # noqa: E402
    NODE, NOW, _ClientHTTP, _row, _run_one, _sess, _worker, db,  # noqa: F401  (db is a fixture)
)
from src.control.db import MeshDB  # noqa: E402


def _seed_oc_turn(db: MeshDB, task_id: str, sid: str, prompt: str, repo: str, native: str) -> None:
    db.upsert_session(Session(
        session_id=sid, backend="opencode-server", repo_path=repo, status=SessionStatus.IDLE,
        created_at=NOW, updated_at=NOW, machine_id=NODE,
    ))
    db.enroll_session(sid)
    db.enqueue_turn(
        task_id=task_id, session_id=sid, backend="opencode-server", action="resume_session",
        payload={"task_id": task_id, "prompt": prompt,
                 "session": {"session_id": sid, "backend": "opencode-server", "repo_path": repo,
                             "backend_session_id": native}},
        turn_source="human", turn_kind="instruction", machine_id=NODE,
    )
    db.activate_turn(task_id)


def _oc_worker(tmp_path, backend):
    w = _worker(tmp_path, _ClientHTTP(TestClient(ts.app)))
    w.cfg.backends = ["opencode-server"]
    w._backends = {"opencode-server": backend}
    w._register()
    return w


def test_INT_opencode_managed_turn_end_to_end(db, backend, fake, tmp_path):
    fake.add_session("ses_int")
    seen_uuids: List[str] = []
    real = backend.run_managed_turn

    def spy(session, message, ownership, **kw):
        seen_uuids.append(ownership.turn_uuid)
        return real(session, message, ownership, **kw)

    backend.run_managed_turn = spy
    w = _oc_worker(tmp_path, backend)
    _seed_oc_turn(db, "t-oc1", "sess-oc1", "do the work", str(tmp_path), "ses_int")
    _run_one(w, "t-oc1")
    row = _row(db, "t-oc1")
    assert row["status"] == "completed", row
    assert json.loads(row["result"])["output"] == "managed reply"
    assert _sess(db, "sess-oc1")["backend_session_id"] == "ses_int"
    assert db.get_active_turn("sess-oc1") is None
    assert seen_uuids and seen_uuids[0]
    assert fake.prompts[0]["messageID"] == managed_message_id(seen_uuids[0])
    assert fake.prompts[0]["parts"][0]["text"] == "do the work"
    assert fake.aborts == []


def test_INT_opencode_busy_native_session_is_released_not_failed(db, backend, fake, tmp_path):
    fake.add_session("ses_busy2")
    fake.busy["ses_busy2"] = True
    w = _oc_worker(tmp_path, backend)
    _seed_oc_turn(db, "t-oc2", "sess-oc2", "x", str(tmp_path), "ses_busy2")
    _run_one(w, "t-oc2")
    row = _row(db, "t-oc2")
    assert row["status"] == "pending", row
    assert fake.prompts == [] and fake.aborts == []


def test_INT_opencode_ambiguous_outcome_holds_recovery(db, backend, fake, tmp_path):
    fake.add_session("ses_amb")
    fake.ack_delay = 2.0
    fake.record_prompt = False
    w = _oc_worker(tmp_path, backend)
    _seed_oc_turn(db, "t-oc3", "sess-oc3", "x", str(tmp_path), "ses_amb")
    _run_one(w, "t-oc3")
    row = _row(db, "t-oc3")
    assert row["status"] == "recovery_required", row
    assert db.get_active_turn("sess-oc3")["id"] == "t-oc3"
    assert fake.aborts == []
    rec = w._managed_claims["t-oc3"]
    assert isinstance(rec.get("backend_identity"), dict) and rec["backend_identity"]["pid"] > 0
    # Same-incarnation reconcile: once the backend reports quiescence (idle and
    # the prompt never recorded past the grace), the hold resolves.
    time.sleep(0.6)
    asyncio.run(w._reconcile_managed_claims())
    assert _row(db, "t-oc3")["status"] in ("failed",), _row(db, "t-oc3")


# =========================================================================== #
# [A82 step 4 rework, review round 1] m4 late capture, m6 transport errors never
# kill the shared serve, m7 native session created only after the gates and
# persisted write-ahead.
# =========================================================================== #
@pytest.fixture(autouse=True)
def _native_store(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_STATE_DIR", str(tmp_path / "carrier-state"))


def test_m4_late_reply_is_captured_bound_to_the_turn_and_session_busy_until_delivered(backend, fake, tmp_path):
    fake.add_session("ses_late")
    fake.release.clear()
    backend._managed_deadline_sec = lambda: 0.5
    in_sink, release_sink = threading.Event(), threading.Event()
    got: List[Any] = []
    s = _session(tmp_path, native="ses_late")

    def sink(sid: str, outcome: Any) -> None:
        got.append((sid, outcome, backend.is_quiescent(s)))
        in_sink.set()
        release_sink.wait(5)

    backend.set_proactive_sink(sink)
    res = backend.run_managed_turn(s, "x", _own(turn="u-late"))
    assert res.error_class == "recovery_required"
    assert backend.is_quiescent(s) is False
    fake.release.set()
    assert in_sink.wait(10), "the late reply was dropped"
    sid, outcome, quiescent_during_delivery = got[0]
    assert sid == "gw-1" and outcome.late_managed is True
    assert outcome.managed_turn_uuid == "u-late" and outcome.output == "managed reply"
    assert outcome.is_error is False and outcome.backend_session_id == "ses_late"
    assert quiescent_during_delivery is False, "reconcile could resolve before the late reply was captured"
    release_sink.set()
    _wait(lambda: backend.is_quiescent(s))
    assert fake.aborts == []


def test_m4_forgotten_turn_gets_no_late_delivery(backend, fake, tmp_path):
    fake.add_session("ses_fg")
    fake.release.clear()
    backend._managed_deadline_sec = lambda: 0.5
    got: List[Any] = []
    backend.set_proactive_sink(lambda sid, outcome: got.append(outcome))
    s = _session(tmp_path, native="ses_fg")
    assert backend.run_managed_turn(s, "x", _own(turn="u-fg")).error_class == "recovery_required"
    assert backend.forget_managed_turn(s, "u-fg") is True
    fake.release.set()
    _wait(lambda: backend.is_quiescent(s))
    time.sleep(0.3)
    assert got == []


def test_m4_late_reply_completes_the_held_turn_through_the_carrier(db, backend, fake, tmp_path):
    fake.add_session("ses_lc")
    fake.release.clear()
    backend._managed_deadline_sec = lambda: 0.5
    w = _oc_worker(tmp_path, backend)
    w._setup_proactive_delivery()
    _seed_oc_turn(db, "t-oc-late", "sess-oc-late", "slow", str(tmp_path), "ses_lc")
    _run_one(w, "t-oc-late")
    assert _row(db, "t-oc-late")["status"] == "recovery_required"
    assert asyncio.run(w._reconcile_managed_claims()) == 0, "must not resolve while the reply is owed"
    fake.release.set()
    _wait(lambda: [tid for tid, _t, _e in w._result_spool.list_spooled()] == ["t-oc-late"])
    asyncio.run(w._redeliver_spooled_results())
    row = _row(db, "t-oc-late")
    assert row["status"] == "completed", row
    assert json.loads(row["result"])["output"] == "managed reply"


def _fail_status_once(monkeypatch, exc: BaseException) -> List[str]:
    import urllib.request

    import src.backends.opencode as oc_mod

    real = urllib.request.urlopen
    hits: List[str] = []

    def urlopen(req, *a, **k):
        if not hits and "/session/status" in req.full_url:
            hits.append(req.full_url)
            raise exc
        return real(req, *a, **k)

    monkeypatch.setattr(oc_mod.urllib.request, "urlopen", urlopen)
    return hits


@pytest.mark.parametrize("kind", ["connect_timeout", "reset"])
def test_m6_probe_transport_error_never_terminates_the_shared_server(backend, fake, tmp_path,
                                                                     stand_in_proc, monkeypatch, kind):
    import socket
    import urllib.error

    fake.add_session("ses_a")
    fake.add_session("ses_b")
    fake.release.clear()
    th, out = _bg(backend.run_managed_turn, _session(tmp_path, native="ses_a"), "a", _own(turn="u-a"))
    _wait(lambda: len(fake.prompts) == 1)
    exc = urllib.error.URLError(socket.timeout("timed out")) if kind == "connect_timeout" \
        else ConnectionResetError("reset by peer")
    hits = _fail_status_once(monkeypatch, exc)
    assert backend.is_quiescent(_session(tmp_path, sid="gw-b", native="ses_b")) is False, "unknown ⇒ busy"
    assert hits, "the probe hit the injected transport error"
    key = backend._server_key(str(tmp_path))
    assert stand_in_proc.poll() is None, "the shared serve was terminated"
    assert backend._procs.get(key) is stand_in_proc and backend._base_urls.get(key) == fake.url
    fake.release.set()
    th.join(10)
    assert out["result"].success is True, out["result"].errors


def test_m6_dead_server_process_is_still_cleaned_up(backend, fake, tmp_path, stand_in_proc):
    fake.add_session("ses_d6")
    stand_in_proc.kill()
    stand_in_proc.wait(5)
    fake.die()
    assert backend.is_quiescent(_session(tmp_path, native="ses_d6")) is False
    assert backend._server_key(str(tmp_path)) not in backend._base_urls, "proven death clears the reference"


def test_m7_first_turn_conflict_creates_no_native_session(backend, fake, tmp_path):
    from src.backends.opencode import _get_repo_lock

    lock = _get_repo_lock(str(tmp_path))
    assert lock.acquire(blocking=False)
    try:
        res = backend.run_managed_turn(_session(tmp_path, native=""), "x", _own(turn="u-first"))
    finally:
        lock.release()
    assert res.error_class == "managed_conflict"
    assert fake.created == [], "a refused first turn leaked an orphan native session"


def test_m7_first_turn_recovery_keeps_its_native_session_across_a_restart(backend, fake, tmp_path, stand_in_proc):
    fake.record_prompt = False  # the first prompt's acceptance is ambiguous ⇒ recovery
    first = backend.run_managed_turn(_session(tmp_path, native=""), "x", _own(turn="u-r1"))
    assert first.error_class == "recovery_required"
    assert len(fake.created) == 1
    native = fake.created[0]
    assert first.backend_session_id == native
    # A successor backend (carrier restart): the gateway never learned the id.
    fake.record_prompt = True
    successor = OpenCodeServerBackend()
    successor._exe = str(tmp_path / "no-such-opencode-binary")
    key = successor._server_key(str(tmp_path))
    successor._procs[key] = stand_in_proc
    successor._base_urls[key] = fake.url
    successor._MANAGED_POLL_SEC = 0.05
    nxt = successor.run_managed_turn(_session(tmp_path, native=""), "y", _own(turn="u-r2"))
    assert nxt.success is True, nxt.errors
    assert nxt.backend_session_id == native and fake.created == [native], "history lost: a second session"
    assert [p["session"] for p in fake.prompts] == [native, native]


# =========================================================================== #
# [A82 pre-cutover backend carries] m6 refusal streak decays; m7 write-ahead
# store under the carrier state dir + cleanup; close() stops the late watcher.
# =========================================================================== #
def _refuse_status(monkeypatch) -> None:
    import src.backends.opencode as oc_mod

    def urlopen(req, *a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(oc_mod.urllib.request, "urlopen", urlopen)


def test_m6_isolated_refusals_further_apart_than_the_window_never_terminate_the_serve(
        backend, fake, tmp_path, stand_in_proc, monkeypatch):
    backend._UNREACHABLE_TERMINATE_SEC = 0.6
    key = backend._server_key(str(tmp_path))
    _refuse_status(monkeypatch)
    _body, err = backend._http(key, "GET", "/session/status", timeout=1)
    assert "server kept" in err
    time.sleep(0.75)  # no refusal for longer than the window: the streak decays
    _body, err = backend._http(key, "GET", "/session/status", timeout=1)
    assert "server kept" in err, err
    assert stand_in_proc.poll() is None and backend._base_urls.get(key) == fake.url


def test_m6_a_continuous_refusal_streak_still_terminates_a_live_non_serving_process(
        backend, fake, tmp_path, stand_in_proc, monkeypatch):
    backend._UNREACHABLE_TERMINATE_SEC = 0.6
    key = backend._server_key(str(tmp_path))
    _refuse_status(monkeypatch)
    deadline = time.monotonic() + 3
    err = ""
    while "will restart" not in err and time.monotonic() < deadline:
        _body, err = backend._http(key, "GET", "/session/status", timeout=1)
        time.sleep(0.1)
    assert "will restart" in err
    assert key not in backend._base_urls
    stand_in_proc.wait(5)


def _stored_rows(tmp_path) -> List[tuple]:
    import sqlite3

    path = tmp_path / "carrier-state" / "opencode-native-sessions.sqlite3"
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    try:
        return list(conn.execute("SELECT session_id, native_id FROM native_sessions"))
    finally:
        conn.close()


def test_m7_write_ahead_store_lives_under_the_carrier_state_dir(monkeypatch, tmp_path):
    from pathlib import Path

    from src.backends.opencode import _native_store_path

    monkeypatch.setenv("AI_TEAM_OPENCODE_STATE_DIR", str(tmp_path / "ignored"))
    assert _native_store_path() == tmp_path / "carrier-state" / "opencode-native-sessions.sqlite3"
    monkeypatch.delenv("WORKER_STATE_DIR")
    assert _native_store_path() == Path("logs") / "carrier_state" / "opencode-native-sessions.sqlite3"


def test_m7_first_turn_resolved_by_late_capture_clears_its_write_ahead_row(backend, fake, tmp_path):
    fake.release.clear()
    backend._managed_deadline_sec = lambda: 0.5
    got: List[Any] = []
    backend.set_proactive_sink(lambda sid, outcome: got.append(outcome))
    res = backend.run_managed_turn(_session(tmp_path, native=""), "x", _own(turn="u-wa"))
    assert res.error_class == "recovery_required"
    native = fake.created[0]
    assert _stored_rows(tmp_path) == [("gw-1", native)]
    fake.release.set()
    _wait(lambda: got)
    assert got[0].backend_session_id == native
    _wait(lambda: _stored_rows(tmp_path) == [])


def test_m7_a_session_whose_native_id_is_known_drops_a_stale_write_ahead_row(backend, fake, tmp_path):
    from src.backends.opencode import _native_store

    key = backend._server_key(str(tmp_path))
    _native_store("INSERT OR REPLACE INTO native_sessions VALUES (?, ?, ?)", ("gw-1", key, "ses_stale"))
    fake.add_session("ses_known")
    res = backend.run_managed_turn(_session(tmp_path, native="ses_known"), "x", _own(turn="u-known"))
    assert res.success is True, res.errors
    assert _stored_rows(tmp_path) == []
