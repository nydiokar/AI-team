from src.backends import codex as codex_module
from src.backends.codex import CodexBackend
from src.core.telemetry import TelemetryContext


class _Proc:
    def __init__(self, pid):
        self.pid = pid


def test_session_process_replacement_emits_duplicate_before_termination(monkeypatch):
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
    backend._register_process(
        _Proc(202),
        "session-a",
        telemetry_context=second,
        emit=lambda event: (
            emitted.append(event),
            actions.append(("emit", event.event_name)),
        ),
    )

    assert actions == [
        ("emit", "invocation.duplicate_detected"),
        ("terminate", 101),
    ]
    assert emitted[0].invocation_id == "inv_second"
    assert emitted[0].attributes == {
        "duplicate_of_invocation_id": "inv_first",
        "confidence": "probable",
        "rule": "session_process_replacement",
    }
