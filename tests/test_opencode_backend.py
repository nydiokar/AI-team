"""
Unit tests for OpenCodeServerBackend — HTTP transport behaviour, server
lifecycle, activity/telemetry emission, and the converged turn/compaction
entry points.

[A102 S1-OpenCode] The OpenCode CLI backend (``OpenCodeBackend``) was deleted
(R3), so its command-shape / stdout-parsing tests are gone. The server backend's
managed turn semantics (attribution, lost-ack reconcile, correlated reply,
expiry→recovery, late capture, cancel-only-ours, quiescence) are the safety net
in ``test_opencode_managed_turns.py``; this file covers the thin HTTP/lifecycle
seams. No real opencode binary is required; all transport is mocked.
"""
import json
import threading
from unittest.mock import patch

from src.backends.opencode import OpenCodeServerBackend
from src.core.interfaces import ExecutionResult
from src.core.telemetry import TelemetryContext
from src.core.turn_liveness import turn_control

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(
    session_id: str = "gw-session-1",
    repo_path: str = "/repo",
    backend_session_id: str = "",
    last_user_message: str = "do the thing",
):
    from unittest.mock import MagicMock

    s = MagicMock()
    s.session_id = session_id
    s.repo_path = repo_path
    s.backend_session_id = backend_session_id
    s.last_user_message = last_user_message
    s.task_history = []
    return s


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

def test_server_http_timeout_aborts_turn_and_preserves_server():
    b = OpenCodeServerBackend()
    key = "/repo"

    class _Proc:
        pid = 4242

    proc = _Proc()
    b._procs[key] = proc
    b._base_urls[key] = "http://127.0.0.1:4096"
    with (
        patch("src.backends.opencode.urllib.request.urlopen", side_effect=TimeoutError("timed out")),
        patch.object(b, "_http", wraps=b._http) as http_spy,
    ):
        response, err = b._http(key, "POST", "/session/ses_1/message", {"parts": []}, timeout=7)

    assert response == {}
    assert "timed out" in err
    # A timed-out /message POST aborts the running generation but keeps the
    # shared server + native session so the next turn can resume.
    assert b._procs[key] is proc
    assert b._base_urls[key] == "http://127.0.0.1:4096"
    assert http_spy.call_args_list[1].args[2] == "/session/ses_1/abort"


# ---------------------------------------------------------------------------
# Natural turn / compaction entry points (converged, turn-driven)
# ---------------------------------------------------------------------------

def test_server_resume_transport_failure_preserves_backend_session_id(tmp_path):
    b = OpenCodeServerBackend()
    session = _make_session(
        repo_path=str(tmp_path),
        backend_session_id="ses_old",
        last_user_message="previous",
    )
    # The single turn body reported a non-success result; the native identity
    # must be preserved (never silently blanked).
    with patch.object(b, "_send_message", return_value=ExecutionResult(
            False, "", backend_session_id="ses_old", errors=["recovery"], error_class="recovery_required")):
        result = b.resume_session(session, "continue", turn=turn_control("u-resume"))

    assert result.success is False
    assert session.backend_session_id == "ses_old"
    assert result.backend_session_id == "ses_old"


def test_server_resume_lost_native_session_is_server_unavailable_and_preserves_identity(tmp_path):
    backend = OpenCodeServerBackend()
    session = _make_session(repo_path=str(tmp_path), backend_session_id="ses_lost")
    # The server no longer knows the saved native id (lookup returns no id, not a
    # 404) ⇒ server_unavailable, and the id is NOT silently replaced.
    with (
        patch.object(backend, "_ensure_server", return_value=None),
        patch.object(backend, "_http", return_value=({}, None)),
    ):
        result = backend.resume_session(session, "continue", turn=turn_control("u-lost"))
    assert result.success is False
    assert result.error_class == "server_unavailable"
    assert session.backend_session_id == "ses_lost"
    assert result.backend_session_id == "ses_lost"


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def test_server_close_preserves_native_session_history():
    backend = OpenCodeServerBackend()
    session = _make_session(backend_session_id="ses_keep")
    with patch.object(backend, "_http") as http:
        backend.close(session)
    http.assert_not_called()


def test_server_startup_capacity_is_bounded(tmp_path):
    backend = OpenCodeServerBackend()
    backend._server_slots = threading.BoundedSemaphore(0)
    with (
        patch("src.core.test_guard.assert_live_calls_allowed", return_value=None),
        patch("src.backends.opencode.subprocess.Popen") as popen,
    ):
        error = backend._ensure_server(str(tmp_path), str(tmp_path))
    assert error and "capacity is full" in error
    popen.assert_not_called()


# ---------------------------------------------------------------------------
# Activity / telemetry emission (structural, redacted)
# ---------------------------------------------------------------------------

def test_server_activity_emits_safe_tool_label_without_arguments(monkeypatch):
    backend = OpenCodeServerBackend()
    stop = threading.Event()
    ready = threading.Event()
    session_id = "ses_live"
    event = {
        "payload": {
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {"type": "tool", "tool": "bash", "state": {
                    "status": "running", "input": {"command": "SECRET_COMMAND"},
                }},
            },
        },
    }
    unrecognized_tool_event = {
        "payload": {
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {"type": "tool", "tool": "bash SECRET_COMMAND", "state": {
                    "status": "running", "input": {"command": "SECRET_COMMAND"},
                }},
            },
        },
    }

    class _Stream:
        def __init__(self):
            self._lines = [
                b"data: " + json.dumps(event).encode() + b"\n",
                b"data: " + json.dumps(unrecognized_tool_event).encode() + b"\n",
            ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self, _limit):
            if self._lines:
                return self._lines.pop(0)
            stop.set()
            return b""

    emitted = []
    monkeypatch.setattr(backend, "_base_urls", {"/repo": "http://localhost"})
    monkeypatch.setattr("src.backends.opencode.urllib.request.urlopen", lambda *_a, **_k: _Stream())
    monkeypatch.setattr("src.core.observability.emit_event", lambda *a, **kw: emitted.append((a, kw)))
    context = TelemetryContext(
        turn_id="task-live", invocation_id="inv-live", node_id="worker", session_id="gateway-session"
    )
    turn = turn_control("u-activity")
    backend._read_activity_events("/repo", session_id, context, None, stop, ready, turn, {})
    assert emitted == [(('task_activity',), {
        'session_id': 'gateway-session', 'task_id': 'task-live',
        'turn_id': 'task-live', 'label': 'Using Bash',
    })]
    assert "SECRET_COMMAND" not in str(emitted)


def test_server_tool_telemetry_is_structural_and_redacts_arguments():
    context = TelemetryContext.create(turn_id="turn-1", node_id="worker", session_id="session-1")

    class _Sink:
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)

    sink = _Sink()
    progress = {}
    backend = OpenCodeServerBackend()
    backend._emit_tool_telemetry(context, sink, "bash", "call-1", "running", progress)
    backend._emit_tool_telemetry(context, sink, "bash", "call-1", "completed", progress)
    assert [event.event_name for event in sink.events] == ["tool.call.started", "tool.call.completed"]
    assert all("command" not in str(event.attributes).lower() for event in sink.events)
    assert sink.events[1].attributes["duration_ms"] >= 0


def test_server_malformed_sse_is_reported_as_transport_error(monkeypatch):
    backend = OpenCodeServerBackend()
    ready = threading.Event()
    stop = threading.Event()

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self, _limit):
            return b"data: {invalid-json}\n"

    reader_state = {}
    monkeypatch.setattr(backend, "_base_urls", {"/repo": "http://localhost"})
    monkeypatch.setattr("src.backends.opencode.urllib.request.urlopen", lambda *_a, **_k: _Stream())
    backend._read_activity_events("/repo", "ses_live", None, None, stop, ready, turn_control("u-mal"), reader_state)
    assert ready.is_set()
    assert "malformed" in reader_state["error"]
