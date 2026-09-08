"""Contract/failure tests for the candidate adapter, with no provider access."""
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.backends.codex_app_server import CodexAppServerClient, CodexProtocolError, CodexRPCError
from src.backends.codex import CodexBackend
from src.core.interfaces import CodingBackend
from src.core.telemetry import TelemetryContext


class Channel:
    def __init__(self, runtime):
        self.runtime = runtime
        self.queue = queue.Queue()

    def get(self):
        self.runtime.check()
        return self.queue.get(timeout=0.01)


class Runtime(CodexAppServerClient):
    def __init__(self):
        self.calls = []
        self.channels = {}
        self.threads = {}
        self.active = {}
        self.hold = False
        self.dead = False
        self.reject = ""
        self.status = "completed"
        self.process = SimpleNamespace(pid=1)
        self.started = threading.Event()

    def check(self):
        if self.dead:
            raise CodexProtocolError("codex_runtime_lost")

    def subscribe(self, thread_id):
        assert thread_id not in self.channels
        channel = Channel(self)
        self.channels[thread_id] = channel
        return channel

    def unsubscribe(self, thread_id):
        self.channels.pop(thread_id, None)

    def close(self):
        self.dead = True

    def emit(self, thread_id, method, **params):
        self.channels[thread_id].queue.put({"method": method, "params": {"threadId": thread_id, **params}})

    def complete(self, thread_id, status=None):
        turn = self.active[thread_id]
        self.emit(thread_id, "turn/completed", turn={"id": turn, "items": [],
                  "status": status or self.status, "error": {"message": "failed"} if self.status == "failed" else None})

    def request(self, method, params, timeout=30):
        self.check()
        self.calls.append((method, params))
        if method == self.reject:
            raise CodexRPCError({"code": -32000, "message": "native rejected", "data": {"reason": "test"}})
        if method in ("thread/start", "thread/resume"):
            tid = params.get("threadId", "native-" + str(len(self.threads)))
            self.threads[tid] = params["cwd"]
            return {"thread": {"id": tid, "cwd": params["cwd"], "status": {"type": "idle"}}}
        tid = params["threadId"]
        if method == "turn/start":
            turn_id = f"turn-{len(self.calls)}"
            self.active[tid] = turn_id
            self.emit(tid, "turn/started", turn={"id": turn_id, "status": "inProgress"})
            self.emit(tid, "item/completed", turnId=turn_id,
                      item={"id": "message", "type": "agentMessage", "text": "native answer"})
            self.started.set()
            if not self.hold:
                self.complete(tid)
            return {"turn": {"id": turn_id, "items": [], "status": "inProgress"}}
        if method == "turn/interrupt":
            assert params["turnId"] == self.active[tid]
            self.complete(tid, "interrupted")
            return {}
        if method == "thread/unsubscribe":
            return {"status": "unsubscribed"}
        raise AssertionError(method)


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("src.core.test_guard.assert_live_calls_allowed", lambda _: None)
    runtime = Runtime()
    backend = CodexBackend()
    monkeypatch.setattr(backend, "_runtime", lambda: runtime)
    return backend, runtime, str(tmp_path)


def run(backend, cwd, key="gateway", native_id=None, task="task"):
    return backend._run(cwd, "hello", native_id, key,
                        telemetry_context=TelemetryContext(turn_id=task, invocation_id="inv-" + task,
                                                           node_id="node", session_id=key, backend="codex"))


def test_generic_contract_and_exact_continuation(native):
    backend, runtime, cwd = native
    assert isinstance(backend, CodingBackend)
    first = run(backend, cwd)
    second = run(backend, cwd, native_id=first.backend_session_id, task="second")
    assert first.success and second.success
    assert first.backend_session_id == second.backend_session_id == "native-0"
    assert second.output == "native answer"
    assert [method for method, _ in runtime.calls] == ["thread/start", "turn/start", "turn/start"]
    assert all(params["cwd"] == cwd for method, params in runtime.calls if method == "turn/start")


def test_restart_reattaches_only_exact_persisted_id(native, monkeypatch):
    backend, runtime, cwd = native
    first = run(backend, cwd)
    restarted = CodexBackend()
    replacement = Runtime()
    monkeypatch.setattr(restarted, "_runtime", lambda: replacement)
    result = run(restarted, cwd, native_id=first.backend_session_id)
    assert result.success
    assert replacement.calls[0][0] == "thread/resume"
    assert replacement.calls[0][1]["threadId"] == first.backend_session_id
    assert replacement.calls[0][1]["excludeTurns"] is True


def test_first_turn_id_survives_failure_before_result(native):
    backend, runtime, cwd = native
    runtime.reject = "turn/start"
    first = run(backend, cwd)
    assert not first.success and first.backend_session_id == "native-0"
    assert first.parsed_output["error"]["code"] == -32000


def test_resume_failure_never_creates_fresh_thread(native):
    backend, runtime, cwd = native
    runtime.reject = "thread/resume"
    result = run(backend, cwd, native_id="exact-old")
    assert not result.success and result.backend_session_id == "exact-old"
    assert [method for method, _ in runtime.calls] == ["thread/resume"]


def test_native_failure_is_not_process_exit_success(native):
    backend, runtime, cwd = native
    runtime.status = "failed"
    result = run(backend, cwd)
    assert not result.success and result.return_code == 1
    assert result.output == "native answer"
    assert result.parsed_output["error"] == {"message": "failed"}


def test_cancel_interrupts_exact_turn_and_does_not_poison_followup(native):
    backend, runtime, cwd = native
    runtime.hold = True
    with ThreadPoolExecutor() as pool:
        future = pool.submit(run, backend, cwd)
        assert runtime.started.wait(2)
        backend.cancel_execution("task")
        result = future.result(timeout=3)
    assert result.errors == ["cancelled"]
    assert ("turn/interrupt", {"threadId": "native-0", "turnId": runtime.active["native-0"]}) in runtime.calls
    backend.cancel_execution("task")
    runtime.hold = False
    assert run(backend, cwd, task="later").success


def test_completion_wins_late_interrupt(native):
    backend, runtime, cwd = native
    original = runtime.request
    def request(method, params, timeout=30):
        reply = original(method, params, timeout)
        if method == "turn/start":
            backend.cancel_execution("task")
        return reply
    runtime.request = request
    assert run(backend, cwd).success


def test_same_session_rejected_without_disrupting_active_turn(native):
    backend, runtime, cwd = native
    runtime.hold = True
    with ThreadPoolExecutor() as pool:
        first = pool.submit(run, backend, cwd)
        assert runtime.started.wait(2)
        other = run(backend, cwd, task="other")
        assert not other.success and "thread_busy" in other.errors[0]
        assert not first.done()
        backend.cancel_execution("task")
        assert first.result(timeout=3).errors == ["cancelled"]


def test_distinct_sessions_run_concurrently(native):
    backend, runtime, cwd = native
    runtime.hold = True
    with ThreadPoolExecutor() as pool:
        first = pool.submit(run, backend, cwd, "first", None, "first")
        assert runtime.started.wait(2)
        runtime.started.clear()
        second = pool.submit(run, backend, cwd, "second", None, "second")
        assert runtime.started.wait(2)
        assert not first.done() and not second.done()
        backend.cancel_execution("first")
        backend.cancel_execution("second")
        assert first.result(timeout=3).backend_session_id != second.result(timeout=3).backend_session_id


def test_death_during_turn_fails_without_replaying(native):
    backend, runtime, cwd = native
    runtime.hold = True
    with ThreadPoolExecutor() as pool:
        future = pool.submit(run, backend, cwd)
        assert runtime.started.wait(2)
        runtime.dead = True
        result = future.result(timeout=3)
    assert not result.success
    assert result.backend_session_id == "native-0"
    assert len([method for method, _ in runtime.calls if method == "turn/start"]) == 1


def test_wrong_turn_event_fails_closed(native):
    backend, runtime, cwd = native
    def complete(thread_id, status=None):
        runtime.emit(thread_id, "turn/completed", turn={"id": "foreign", "status": "completed", "items": []})
    runtime.complete = complete
    result = run(backend, cwd)
    assert not result.success and "identity_mismatch" in result.errors[0]
    assert runtime.dead


def test_workspace_change_rejected_before_turn(native):
    backend, runtime, cwd = native
    first = run(backend, cwd)
    result = run(backend, str(Path(cwd).parent), native_id=first.backend_session_id)
    assert not result.success and "workspace_mismatch" in result.errors[0]
    assert len([method for method, _ in runtime.calls if method == "turn/start"]) == 1


def test_thread_identity_in_tool_configuration_is_scoped(native):
    backend, runtime, cwd = native
    run(backend, cwd, key="first")
    run(backend, cwd, key="second")
    configs = [params["config"] for method, params in runtime.calls if method == "thread/start"]
    assert configs[0]["shell_environment_policy.set"]["SESSION_ID"] == "first"
    assert configs[1]["shell_environment_policy.set"]["SESSION_ID"] == "second"
