import json

from src.control.db import MeshDB
from src.control.telemetry_sink import BufferedHttpTelemetrySink
from src.control.telemetry_store import TelemetryStore
from src.core.telemetry import TelemetryContext
from src.core.telemetry_adapters.codex import CodexTelemetryAdapter


SENTINELS = (
    "PROMPT_SECRET_123",
    "SOURCE_SECRET_123",
    "TOOL_ARG_SECRET_123",
    "TOOL_RESULT_SECRET_123",
    "MODEL_RESPONSE_SECRET_123",
    "API_KEY_SECRET_123",
)


def test_backend_payload_sentinels_never_reach_db_or_spool(tmp_path, monkeypatch):
    context = TelemetryContext(
        turn_id="turn_private",
        invocation_id="inv_private",
        node_id="worker-private",
        backend="codex",
    )
    adapter = CodexTelemetryAdapter(
        context, emitter_process_instance_id="worker_process"
    )
    lines = [
        json.dumps(
            {
                "type": "item.started",
                "item": {
                    "id": "tool_1",
                    "type": "command_execution",
                    "command": SENTINELS[2],
                    "aggregated_output": "",
                    "status": "in_progress",
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "tool_1",
                    "type": "command_execution",
                    "command": SENTINELS[2],
                    "aggregated_output": SENTINELS[3],
                    "status": "completed",
                    "exit_code": 0,
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "agent_1",
                    "type": "agent_message",
                    "text": SENTINELS[4],
                },
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 5,
                    "output_tokens": 2,
                },
            }
        ),
    ]
    events = adapter.coverage_events()
    for line in lines:
        events.extend(adapter.consume_line(line))

    db_path = tmp_path / "mesh.db"
    store = TelemetryStore(MeshDB(str(db_path)))
    store.insert_events(events)

    sink = BufferedHttpTelemetrySink(
        "http://unreachable",
        "token",
        node_id="worker-private",
        spool_dir=tmp_path / "spool",
        batch_size=50,
    )
    monkeypatch.setattr(sink, "_post_batch", lambda body: False)
    sink.emit_many(events)
    sink.flush()

    persisted = db_path.read_bytes()
    spooled = b"".join(path.read_bytes() for path in (tmp_path / "spool").glob("*.json"))
    for sentinel in SENTINELS:
        encoded = sentinel.encode()
        assert encoded not in persisted
        assert encoded not in spooled
