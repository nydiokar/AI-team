from datetime import timedelta

from src.control.db import MeshDB
from src.control.telemetry_store import TelemetryStore
from src.core.telemetry import build_event, utc_now


def _events():
    start = utc_now()
    common = {
        "turn_id": "turn_store",
        "session_id": "session_store",
        "node_id": "gateway",
        "emitter_process_instance_id": "gateway_proc",
        "source": "gateway",
        "backend": "codex",
    }
    return [
        build_event("turn.accepted", event_time=start, observed_time=start,
                    attributes={"task_id": "turn_store", "source": "test"}, **common),
        build_event("turn.started", event_time=start, observed_time=start,
                    attributes={}, **common),
        build_event(
            "invocation.created",
            event_time=start,
            observed_time=start,
            invocation_id="inv_store",
            attributes={"attempt": 1, "spawn_reason": "initial", "action": "run_oneoff"},
            **common,
        ),
        build_event(
            "model.request.usage",
            event_time=start + timedelta(seconds=1),
            observed_time=start + timedelta(seconds=1),
            invocation_id="inv_store",
            attributes={
                "input_tokens": 50,
                "output_tokens": 5,
                "input_token_semantics": "includes_cache",
                "usage_granularity": "invocation_total",
                "usage_source": "fixture",
                "usage_coverage": "aggregate_only",
            },
            **common,
        ),
        build_event(
            "turn.completed",
            event_time=start + timedelta(seconds=2),
            observed_time=start + timedelta(seconds=2),
            invocation_id="inv_store",
            attributes={"status": "success", "timeout_status": "none", "exit_code": 0},
            **common,
        ),
    ]


def test_schema_migration_creates_telemetry_tables(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    names = {
        row[0]
        for row in db._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "llm_turns",
        "llm_invocations",
        "llm_processes",
        "llm_invocation_processes",
        "llm_model_requests",
        "llm_events",
    }.issubset(names)


def test_event_insert_is_idempotent_and_projection_is_queryable(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    store = TelemetryStore(db)
    events = _events()
    first = store.insert_events(events)
    second = store.insert_events(events)
    assert first["accepted"] == len(events)
    assert first["duplicates"] == 0
    assert second["accepted"] == 0
    assert second["duplicates"] == len(events)

    turn = store.get_turn("turn_store")
    assert turn is not None
    assert turn["final_status"] == "success"
    assert turn["metrics"]["input_tokens"] == 50
    assert turn["metrics"]["model_request_count"] is None
    assert len(store.list_events("turn_store")) == len(events)


def test_late_event_rebuild_is_deterministic(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    store = TelemetryStore(db)
    events = _events()
    store.insert_events(events)
    before = store.get_turn("turn_store")

    start = utc_now()
    late = build_event(
        "telemetry.coverage",
        turn_id="turn_store",
        session_id="session_store",
        node_id="worker",
        emitter_process_instance_id="worker_proc",
        source="worker",
        backend="codex",
        event_time=start,
        observed_time=start,
        attributes={"area": "usage", "coverage": "aggregate_only"},
    )
    store.insert_events([late])
    after = store.get_turn("turn_store")
    before_accounting = dict(before["metrics"])
    after_accounting = dict(after["metrics"])
    before_event_count = before_accounting.pop("telemetry_event_count")
    after_event_count = after_accounting.pop("telemetry_event_count")
    assert before_accounting == after_accounting
    assert after_event_count == before_event_count + 1
    assert after["coverage"]["usage"]["coverage"] == "aggregate_only"
