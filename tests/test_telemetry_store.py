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
    event_columns = {
        row[1]
        for row in db._conn().execute("PRAGMA table_info(llm_events)").fetchall()
    }
    assert "clock_quality" in event_columns


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
    before_accounting.pop("coverage_score")
    after_accounting.pop("coverage_score")
    assert before_accounting == after_accounting
    assert after_event_count == before_event_count + 1
    assert after["coverage"]["usage"]["coverage"] == "aggregate_only"


def test_reconcile_closes_stale_turn_from_terminal_mesh_task(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    store = TelemetryStore(db)
    start = utc_now()
    common = {
        "turn_id": "turn_reconcile",
        "session_id": "session_reconcile",
        "node_id": "worker-a",
        "emitter_process_instance_id": "worker_proc",
        "source": "worker",
        "backend": "codex",
    }
    store.insert_events(
        [
            build_event(
                "turn.started", event_time=start, observed_time=start, **common
            ),
            build_event(
                "invocation.created",
                event_time=start,
                observed_time=start,
                invocation_id="inv_reconcile",
                attributes={
                    "attempt": 1,
                    "spawn_reason": "initial",
                    "action": "run_oneoff",
                },
                **common,
            ),
            build_event(
                "process.spawned",
                event_time=start,
                observed_time=start,
                invocation_id="inv_reconcile",
                pid=123,
                attributes={
                    "process_instance_id": "proc_reconcile",
                    "process_role": "agent",
                    "executable_name": "codex",
                },
                **common,
            ),
        ]
    )
    db.enqueue_task(
        "turn_reconcile",
        None,
        "worker-a",
        "codex",
        "run_oneoff",
        {"prompt": "not read by reconciliation"},
    )
    db.complete_task(
        "turn_reconcile",
        {
            "success": True,
            "return_code": 0,
            "telemetry_invocation_id": "inv_reconcile",
        },
    )

    result = store.reconcile(turn_id="turn_reconcile", since_hours=0)

    assert result["reconciled"] == ["turn_reconcile"]
    turn = store.get_turn("turn_reconcile")
    assert turn["final_status"] == "success"
    assert turn["final_invocation_id"] == "inv_reconcile"
    processes = store.get_processes("turn_reconcile")
    assert processes[0]["status"] == "unknown"
    names = [event["event_name"] for event in store.list_events("turn_reconcile")]
    assert "telemetry.reconciled" in names
    assert "process.exit_unknown" in names


def test_context_growth_between_turns_requires_backend_session_continuity(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    store = TelemetryStore(db)
    start = utc_now()

    def turn_events(turn_id, invocation_id, at, context_tokens, session_start, session_end):
        common = {
            "turn_id": turn_id,
            "session_id": "session_context",
            "node_id": "worker-a",
            "emitter_process_instance_id": "worker_proc",
            "source": "worker",
            "backend": "codex",
        }
        return [
            build_event(
                "turn.started",
                event_time=at,
                observed_time=at,
                attributes={"backend_session_id_start": session_start},
                **common,
            ),
            build_event(
                "invocation.created",
                event_time=at,
                observed_time=at,
                invocation_id=invocation_id,
                attributes={
                    "attempt": 1,
                    "spawn_reason": "initial",
                    "action": "resume_session",
                },
                **common,
            ),
            build_event(
                "model.request.usage",
                event_time=at + timedelta(seconds=1),
                observed_time=at + timedelta(seconds=1),
                invocation_id=invocation_id,
                model_request_id=f"{invocation_id}:mr:1",
                attributes={
                    "sequence": 1,
                    "input_tokens": context_tokens,
                    "output_tokens": 5,
                    "input_token_semantics": "includes_cache",
                    "usage_granularity": "request",
                    "usage_source": "fixture",
                    "usage_coverage": "complete",
                    "work_category": "primary",
                },
                **common,
            ),
            build_event(
                "turn.completed",
                event_time=at + timedelta(seconds=2),
                observed_time=at + timedelta(seconds=2),
                invocation_id=invocation_id,
                attributes={
                    "status": "success",
                    "timeout_status": "none",
                    "exit_code": 0,
                    "backend_session_id_end": session_end,
                },
                **common,
            ),
        ]

    store.insert_events(
        turn_events(
            "turn_context_1",
            "inv_context_1",
            start,
            100,
            "backend-session-a",
            "backend-session-a",
        )
    )
    store.insert_events(
        turn_events(
            "turn_context_2",
            "inv_context_2",
            start + timedelta(seconds=10),
            130,
            "backend-session-a",
            "backend-session-a",
        )
    )

    second = store.get_turn("turn_context_2")
    assert second["metrics"]["context_growth_between_turns"] == 30
    assert second["metrics"]["context_discontinuity_reason"] is None

    store.insert_events(
        turn_events(
            "turn_context_3",
            "inv_context_3",
            start + timedelta(seconds=20),
            50,
            "backend-session-b",
            "backend-session-b",
        )
    )
    third = store.get_turn("turn_context_3")
    assert third["metrics"]["context_growth_between_turns"] is None
    assert (
        third["metrics"]["context_discontinuity_reason"]
        == "backend_session_changed"
    )


def test_retention_prunes_events_then_deletes_expired_summaries(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    store = TelemetryStore(db)
    now = utc_now()

    def terminal_events(turn_id, at):
        common = {
            "turn_id": turn_id,
            "node_id": "worker-a",
            "emitter_process_instance_id": "worker_proc",
            "source": "worker",
            "backend": "codex",
        }
        return [
            build_event(
                "turn.started", event_time=at, observed_time=at, **common
            ),
            build_event(
                "turn.completed",
                event_time=at + timedelta(seconds=1),
                observed_time=at + timedelta(seconds=1),
                attributes={
                    "status": "success",
                    "timeout_status": "none",
                    "exit_code": 0,
                },
                **common,
            ),
        ]

    store.insert_events(
        terminal_events("turn_prune_events", now - timedelta(days=40))
    )
    store.insert_events(
        terminal_events("turn_delete_summary", now - timedelta(days=200))
    )
    preserved_metrics = store.get_turn("turn_prune_events")["metrics"]

    result = store.cleanup(
        event_retention_days=30,
        summary_retention_days=180,
        now=now,
    )

    assert result["event_turns_pruned"] == 1
    assert result["summaries_deleted"] == 1
    retained = store.get_turn("turn_prune_events")
    assert retained is not None
    assert retained["metrics"] == preserved_metrics
    assert retained["events_pruned_at"] is not None
    assert "detailed_events_pruned" in retained["data_quality"]
    assert store.list_events("turn_prune_events") == []
    assert store.get_turn("turn_delete_summary") is None
    assert store.list_events("turn_delete_summary") == []

    store.insert_events(
        [
            build_event(
                "telemetry.coverage",
                turn_id="turn_prune_events",
                node_id="worker-a",
                emitter_process_instance_id="worker_proc",
                source="worker",
                backend="codex",
                attributes={"area": "usage", "coverage": "partial"},
            )
        ]
    )
    after_late = store.get_turn("turn_prune_events")
    assert after_late["metrics"] == preserved_metrics
    assert "late_event_after_retention" in after_late["data_quality"]
