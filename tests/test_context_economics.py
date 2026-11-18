"""Recovery evidence traverses the actual durable producer/session/telemetry links."""
import json
import pytest

from src.control.db import MeshDB
from src.control.session_timeline import build_session_timeline
from src.control.telemetry_store import TelemetryStore
from src.core.interfaces import Session, SessionStatus
from src.core.telemetry import TelemetryContext, build_event
from src.core.telemetry_adapters.claude_stream_json import ClaudeStreamJsonAdapter


def test_respawn_cost_is_linked_on_both_sessions_and_session_cost_is_complete(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    for sid, parent in (("old", None), ("new", "old")):
        db.upsert_session(Session(session_id=sid, backend="claude", repo_path="/tmp",
            status=SessionStatus.AWAITING_INPUT, created_at="2026-11-16T10:00:00Z",
            updated_at="2026-11-16T10:00:00Z", continued_from=parent))
    estimate = {"known": True, "usd": 1.0, "cache_creation_tokens": 160000}
    db.enqueue_task("token", session_id=None, machine_id="__local__", backend="claude",
                    action="manager_respawn", payload={"estimate": estimate})
    db.enqueue_turn("recovery", session_id="new", machine_id="__local__", backend="claude",
                    action="create_session", payload={}, turn_kind="respawn", operation_id="token",
                    producer_token="token", producer_meta={"estimate": estimate})
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET producer_turn_id='recovery',status='completed' WHERE id='token'")
        conn.execute("UPDATE mesh_tasks SET turn_kind='respawn',idempotency_key='token',status='completed' WHERE id='recovery'")
    adapter = ClaudeStreamJsonAdapter(TelemetryContext(
        turn_id="recovery", invocation_id="inv", node_id="node", session_id="new", backend="claude", model="opus",
    ), emitter_process_instance_id="proc")
    events = adapter.consume_line(json.dumps({"type": "result", "total_cost_usd": .47,
        "modelUsage": {"claude-opus-5-5": {"contextWindow": 200000}},
        "usage": {"input_tokens": 100, "output_tokens": 1000,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 10000}}))
    store = TelemetryStore(db)
    store.insert_events(events)
    for sid in ("old", "new"):
        evidence = build_session_timeline(db=db, telemetry_store=store, session_id=sid).context_economics
        row = evidence["recoveries"][0]
        assert row["prior_session_id"] == "old"
        assert row["session_id"] == "new"
        assert row["estimate"]["usd"] == 1.0
        assert row["observed_usd"] == .47
        assert row["cost_basis"] == "provider_reported"
    assert db.session_provider_cost("new") == {"usd": .47, "priced_turns": 1, "turns": 1, "coverage": "complete"}


def test_malformed_compaction_event_cannot_break_timeline(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    store = TelemetryStore(db)
    event = build_event("context.compacted", turn_id="bad", session_id="s", node_id="n",
        emitter_process_instance_id="p", source="backend",
        attributes={"before_tokens": "bad", "after_tokens": -10, "trigger": "secret"})
    store.insert_events([event])
    response = build_session_timeline(db=db, telemetry_store=store, session_id="s")
    assert response.context_economics["compactions"] == []
    assert "invalid_compaction_context" in store.get_turn("bad")["data_quality"]


@pytest.mark.parametrize("after_tokens,native_id,next_compaction,expected", [
    (40000, "native", False, 140000),
    (None, "native", False, None),
    (40000, "replacement", False, None),
    (40000, "native", True, None),
])
@pytest.mark.parametrize("task_status", ["completed", "failed"])
def test_manual_native_boundary_joins_only_unambiguous_next_context(
    after_tokens: int | None, native_id: str, next_compaction: bool, expected: int | None, task_status: str,
) -> None:
    from src.control.session_timeline import _session_context_economics

    tasks = [{"id": "compact", "action": "compact_session", "status": task_status,
              "started_at": "2026-11-18T10:01:00Z", "completed_at": "2026-11-18T10:02:00Z"}]
    turns = [
        {"turn_id": "before", "started_at": "2026-11-18T10:00:00Z", "ended_at": "2026-11-18T10:00:30Z",
         "backend_session_id_end": "native", "metrics": {"turn_exit_context_tokens": 160000}},
        {"turn_id": "compact", "started_at": "2026-11-18T10:01:00Z", "ended_at": "2026-11-18T10:02:00Z",
         "backend_session_id_start": "native", "backend_session_id_end": "native", "observed_models": ["opus"],
         "metrics": {"provider_reported_cost_usd": 0, "compactions": [
             {"event_id": "boundary", "before_tokens": 180000, "after_tokens": None}]}},
        {"turn_id": "after", "started_at": "2026-11-18T10:03:00Z", "backend_session_id_start": native_id,
         "metrics": {"turn_entry_context_tokens": after_tokens}},
    ]
    if next_compaction:
        tasks.append({"id": "intervening", "action": "compact_session", "status": "completed",
                      "started_at": "2026-11-18T10:02:30Z", "completed_at": "2026-11-18T10:02:45Z"})
    outcomes = _session_context_economics(turns, tasks, [])["compactions"]
    outcome = next(row for row in outcomes if row["task_id"] == "boundary")
    assert outcome["reclaimed_tokens"] == (expected if task_status == "completed" else None)
    assert outcome["observed_compaction_usd"] == 0
    assert len([row for row in outcomes if row["task_id"] in ("boundary", "compact")]) == 1


def test_unknown_compaction_sources_remain_visibly_unavailable() -> None:
    response = build_session_timeline(db=None, telemetry_store=None, session_id="missing")
    assert response.context_economics["recovery_coverage"] == "unavailable"
    assert response.context_economics["compaction_coverage"] == "unavailable"


def test_foreign_cache_writes_cannot_borrow_claude_pricing() -> None:
    from src.services.context_economics import observed_turn_cost

    turn = {"model": "gpt-6-sol", "final_status": "completed", "metrics": {
        "input_tokens": 100, "uncached_input_tokens": 100, "output_tokens": 10,
        "cache_read_tokens": 0, "cache_creation_tokens": 1000,
    }}
    assert observed_turn_cost(turn) is None
    turn["metrics"]["provider_reported_cost_usd"] = .03
    assert observed_turn_cost(turn) == .03


def test_legacy_claude_context_is_unavailable_without_rewriting_receipts(tmp_path) -> None:
    from src.services.context_economics import observed_turn_cost

    db = MeshDB(str(tmp_path / "legacy.db"))
    adapter = ClaudeStreamJsonAdapter(TelemetryContext(
        turn_id="legacy", invocation_id="inv", node_id="node", session_id="s", backend="claude",
    ), emitter_process_instance_id="proc")
    store = TelemetryStore(db)
    store.insert_events(adapter.consume_line('{"type":"result","usage":{"input_tokens":100}}'))
    legacy_metrics = {"turn_exit_context_tokens": 100, "context_window_tokens": 200000,
                      "context_used_ratio": .0005, "input_tokens": 100}
    with db._write() as conn:
        conn.execute("UPDATE llm_turns SET metrics_json=?,projection_version=3 WHERE turn_id='legacy'",
                     (json.dumps(legacy_metrics),))
    assert db.latest_context_observation("s")["context_tokens"] is None
    response = build_session_timeline(db=db, telemetry_store=store, session_id="s")
    assert response.context_fill["context_used_ratio"] is None
    assert response.context_economics["context_tokens"] is None
    assert observed_turn_cost(store.get_turn("legacy")) is None
    # Read-time compatibility must neither reproject history nor alter spend.
    assert json.loads(db._conn().execute("SELECT metrics_json FROM llm_turns WHERE turn_id='legacy'").fetchone()[0]) == legacy_metrics
    assert store.get_turn("legacy")["projection_version"] == 3


def test_maximum_context_evidence_cardinality_is_bounded() -> None:
    import time
    import tracemalloc
    from src.control.session_timeline import _session_context_economics

    turns = [{"turn_id": f"turn-{i}", "started_at": f"2026-11-18T10:{i:03d}:00Z",
              "observed_models": ["opus"], "metrics": {"compactions": [
                  {"event_id": f"boundary-{i}-{j}", "before_tokens": 180000, "after_tokens": 40000}
                  for j in range(32)]}} for i in range(500)]
    tasks = [{"id": f"task-{i}", "turn_kind": "compaction", "status": "completed"} for i in range(500)]
    _session_context_economics([], [], [])  # exclude module imports from incremental allocation
    tracemalloc.start()
    started = time.perf_counter()
    result = _session_context_economics(turns, tasks, [])
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(result["compactions"]) == 20
    print(json.dumps({"projection_ms": round(elapsed * 1000, 3), "peak_python_bytes": peak,
                      "json_bytes": len(json.dumps(result)), "turns": 500, "tasks": 500,
                      "native_boundaries": 16000}))


def test_recovery_query_plan_uses_existing_indexes(tmp_path) -> None:
    db = MeshDB(str(tmp_path / "plans.db"))
    statements: list[str] = []
    conn = db._conn()
    conn.set_trace_callback(statements.append)
    try:
        db.session_recovery_evidence("s")
        db.session_provider_cost("s")
    finally:
        conn.set_trace_callback(None)
    plans = [str(row[3]) for statement in statements
             for row in conn.execute("EXPLAIN QUERY PLAN " + statement).fetchall()]
    assert any("idx_sessions_continued_from" in plan for plan in plans)
    assert any("idx_mesh_tasks_session_created" in plan for plan in plans)
    assert any("idx_llm_turns_session" in plan for plan in plans)
    assert not any("AUTOMATIC" in plan for plan in plans)
