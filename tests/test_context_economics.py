"""Recovery evidence traverses the actual durable producer/session/telemetry links."""
import json

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
