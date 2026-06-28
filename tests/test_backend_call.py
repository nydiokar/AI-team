from src.core.backend_call import call_backend
from src.core.interfaces import ExecutionResult
from src.core.telemetry import TelemetryContext


def test_old_backend_signature_is_called_without_telemetry_keywords():
    calls = []

    def old_method(value):
        calls.append(value)
        return "ok"

    assert call_backend(
        old_method,
        "value",
        telemetry_context=object(),
        telemetry_sink=object(),
    ) == "ok"
    assert calls == ["value"]


def test_new_backend_signature_receives_telemetry_keywords():
    received = {}

    def new_method(value, *, telemetry_context=None, telemetry_sink=None):
        received.update(
            value=value,
            telemetry_context=telemetry_context,
            telemetry_sink=telemetry_sink,
        )
        return "ok"

    context = object()
    sink = object()
    assert call_backend(
        new_method,
        "value",
        telemetry_context=context,
        telemetry_sink=sink,
    ) == "ok"
    assert received == {
        "value": "value",
        "telemetry_context": context,
        "telemetry_sink": sink,
    }


def test_execution_result_gets_bounded_telemetry_summary():
    context = TelemetryContext.create(
        turn_id="turn_backend_call",
        node_id="node-a",
        backend="codex",
    )

    def backend(*, telemetry_context=None):
        assert telemetry_context == context
        return ExecutionResult(success=True, output="ok")

    result = call_backend(backend, telemetry_context=context)

    assert result.telemetry is not None
    assert result.telemetry.invocation_id == context.invocation_id
    assert result.telemetry.events == []
    assert result.telemetry.coverage == {}
