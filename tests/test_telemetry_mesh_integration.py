import asyncio
import socket

from src.control.db import MeshDB
from src.control.telemetry_sink import DatabaseTelemetrySink
from src.control.telemetry_store import TelemetryStore
from src.core.interfaces import ExecutionResult
from src.core.telemetry import (
    EMITTER_PROCESS_INSTANCE_ID,
    build_event,
    new_telemetry_id,
)
from src.worker import agent as worker_agent


class TelemetryAwareCodexBackend:
    def run_oneoff(
        self,
        cwd,
        prompt,
        *,
        telemetry_context=None,
        telemetry_sink=None,
    ):
        assert cwd == "/repo"
        assert prompt == "do the work"
        process_instance_id = new_telemetry_id("proc")
        common = {
            "turn_id": telemetry_context.turn_id,
            "session_id": telemetry_context.session_id,
            "node_id": telemetry_context.node_id,
            "emitter_process_instance_id": EMITTER_PROCESS_INSTANCE_ID,
            "source": "backend",
            "invocation_id": telemetry_context.invocation_id,
            "backend": "codex",
            "model": telemetry_context.model,
        }
        telemetry_sink.emit(
            build_event(
                "process.spawned",
                pid=4321,
                attributes={
                    "process_instance_id": process_instance_id,
                    "process_role": "agent",
                    "executable_name": "codex",
                },
                **common,
            )
        )
        telemetry_sink.emit(
            build_event(
                "telemetry.coverage",
                attributes={
                    "area": "usage",
                    "coverage": "aggregate_only",
                    "reason_code": "codex_turn_total_only",
                },
                **common,
            )
        )
        telemetry_sink.emit(
            build_event(
                "model.request.usage",
                attributes={
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_tokens": 50,
                    "input_token_semantics": "includes_cache",
                    "usage_granularity": "invocation_total",
                    "usage_source": "turn.completed.usage",
                    "usage_coverage": "aggregate_only",
                    "work_category": "primary",
                },
                **common,
            )
        )
        telemetry_sink.emit(
            build_event(
                "process.exited",
                pid=4321,
                attributes={
                    "process_instance_id": process_instance_id,
                    "exit_code": 0,
                    "signal": None,
                    "duration_ms": 10,
                },
                **common,
            )
        )
        return ExecutionResult(
            success=True,
            output="done",
            execution_time=0.01,
            return_code=0,
        )


def test_remote_worker_turn_is_correlated_across_gateway_worker_and_backend(
    tmp_path, monkeypatch
):
    db = MeshDB(str(tmp_path / "mesh.db"))
    store = TelemetryStore(db)
    sink = DatabaseTelemetrySink(store)
    gateway_node = socket.gethostname()
    turn_id = "turn_mesh_integration"
    gateway_common = {
        "turn_id": turn_id,
        "node_id": gateway_node,
        "emitter_process_instance_id": "gateway_proc",
        "source": "gateway",
        "backend": "codex",
    }
    sink.emit(
        build_event(
            "turn.accepted",
            attributes={"task_id": turn_id, "source": "test"},
            **gateway_common,
        )
    )
    sink.emit(build_event("turn.started", **gateway_common))

    async def direct_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(worker_agent.asyncio, "to_thread", direct_to_thread)
    result = asyncio.run(
        worker_agent._execute_task(
            {
                "id": turn_id,
                "backend": "codex",
                "action": "run_oneoff",
                "claimed_by": "worker-a",
                "payload": {
                    "prompt": "do the work",
                    "metadata": {"cwd": "/repo"},
                    "telemetry": {
                        "schema_version": 1,
                        "turn_id": turn_id,
                        "session_id": None,
                        "gateway_node_id": gateway_node,
                        "attempt": 1,
                        "spawn_reason": "initial",
                    },
                },
            },
            {"codex": TelemetryAwareCodexBackend()},
            telemetry_sink=sink,
            node_id="worker-a",
        )
    )
    sink.emit(
        build_event(
            "turn.result_recorded",
            invocation_id=result["telemetry_invocation_id"],
            attributes={"status": "success", "error_code": None},
            **gateway_common,
        )
    )
    sink.emit(
        build_event(
            "turn.completed",
            invocation_id=result["telemetry_invocation_id"],
            attributes={
                "status": "success",
                "timeout_status": "none",
                "exit_code": result["return_code"],
            },
            **gateway_common,
        )
    )

    diagnostics = store.diagnostics(turn_id)
    events = store.list_events(turn_id)

    assert result["success"] is True
    assert diagnostics["turn"]["gateway_node_id"] == gateway_node
    assert diagnostics["turn"]["execution_node_id"] == "worker-a"
    assert diagnostics["turn"]["metrics"]["total_token_work"] == 110
    assert diagnostics["turn"]["metrics"]["model_request_count"] is None
    assert len(diagnostics["invocations"]) == 1
    assert diagnostics["invocations"][0]["node_id"] == "worker-a"
    assert diagnostics["processes"][0]["pid"] == 4321
    assert {event["source"] for event in events} == {
        "gateway",
        "worker",
        "backend",
    }
