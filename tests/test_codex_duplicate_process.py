import pytest
from src.backends import codex as codex_module
from src.backends.codex import CodexBackend
from src.core.telemetry import TelemetryContext


class _Proc:
    def __init__(self, pid):
        self.pid = pid


def test_session_process_replacement_is_refused_without_termination(monkeypatch):
    backend = CodexBackend()
    first = TelemetryContext(
        turn_id="turn_duplicate",
        invocation_id="inv_first",
        node_id="worker-a",
        session_id="session-a",
        backend="codex",
    )
    second = TelemetryContext(
        turn_id="turn_duplicate",
        invocation_id="inv_second",
        node_id="worker-a",
        session_id="session-a",
        backend="codex",
    )
    emitted = []
    actions = []
    monkeypatch.setattr(
        codex_module,
        "terminate_many_popen",
        lambda procs: actions.append(("terminate", procs[0].pid)),
    )

    backend._register_process(
        _Proc(101),
        "session-a",
        telemetry_context=first,
        emit=lambda event: emitted.append(event),
    )
    with pytest.raises(RuntimeError, match="codex_thread_busy"):
        backend._register_process(
            _Proc(202), "session-a", telemetry_context=second, emit=emitted.append
        )
    assert actions == []
    assert emitted == []
    assert backend._session_procs["session-a"].pid == 101
