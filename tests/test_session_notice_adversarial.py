"""Regression witnesses recovered from the session-notice follow-up."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from src.control import db as db_module, transcript
from src.control.db import MeshDB
from src.control.telemetry_store import TelemetryStore
from src.core.telemetry import TelemetryContext, build_event
from src.core.telemetry_adapters.claude_stream_json import ClaudeStreamJsonAdapter
from tests.stage8a_legacy import enqueue_pre_cutover
from tests.test_control_api import _auth, client, orch  # noqa: F401
from tests.test_transcript_read_a81 import _session


def _event(name: str, turn_id: str = "covered", **attributes):
    return build_event(
        name, turn_id=turn_id, session_id="coverage", invocation_id=f"inv-{turn_id}",
        node_id="worker", emitter_process_instance_id="test", source="worker",
        backend="claude", attributes=attributes,
    )



def _adapter() -> ClaudeStreamJsonAdapter:
    return ClaudeStreamJsonAdapter(
        TelemetryContext(turn_id="covered", invocation_id="inv-covered", session_id="coverage", node_id="worker", backend="claude"),
        emitter_process_instance_id="adapter",
    )



def test_corrupt_notice_does_not_discard_valid_db_conversation(tmp_path, monkeypatch):
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_session("recovery"))
    monkeypatch.setattr(db_module, "_db_instance", db)
    for task, action in [("task_valid", "resume_session"), ("job_bad", "watched_job")]:
        enqueue_pre_cutover(db, task_id=task, session_id="recovery", machine_id="Horse", backend="claude", action=action, payload={"prompt": "real instruction"})
    with db._write() as conn:
        conn.execute("UPDATE mesh_tasks SET status='completed',reply_text='real response' WHERE id='task_valid'")
        conn.execute("UPDATE mesh_tasks SET status='completed',payload='{' WHERE id='job_bad'")
    result = transcript.get_transcript(tmp_path, tmp_path, "recovery")
    assert any(turn["task_id"] == "task_valid" and turn["result"] == "real response" for turn in result)



@pytest.mark.parametrize("gap", ["missing_turn", "partial_counter"])
def test_completed_usage_gaps_do_not_look_complete(client, monkeypatch, gap):
    db = db_module.get_db()
    store = TelemetryStore(db)
    events = [_event("invocation.started")]
    if gap == "partial_counter":
        events += _adapter().observe_assistant_usage({"input_tokens": 10, "output_tokens": 2}, "m1")
    else:
        events += _adapter().consume_line(json.dumps({"type": "result", "usage": {"input_tokens": 10, "output_tokens": 2}}))
        events += [_event("invocation.started", "missing"), _event("turn.completed", "missing", status="success")]
    events += [_event("turn.completed", status="success")]
    store.insert_events(events)
    monkeypatch.setattr("src.control.control_api._db", lambda: db)
    response = client.get("/api/sessions/coverage/usage", headers=_auth())
    assert response.status_code == 200
    assert response.json()["tokens"]["total"] == 12
    assert response.json()["usage_state"] == "partial"
    assert response.json()["usage_reason"] == "incomplete_coverage"



def test_late_live_snapshot_cannot_overwrite_final_when_clock_moves_backward(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    store = TelemetryStore(db)
    adapter = _adapter()
    interim = adapter.observe_assistant_usage({"input_tokens": 10, "output_tokens": 2}, "m1")[0]
    final = adapter.consume_line(json.dumps({"type": "result", "usage": {"input_tokens": 100, "output_tokens": 20}}))
    # A distinct, buffered interim event appears newer by wall clock and is
    # delivered after the final result. Its authority is still only interim.
    interim = interim.model_copy(update={"event_time": final[0].event_time + timedelta(seconds=1)})
    store.insert_events(final)
    store.insert_events([interim])
    assert db.get_session_token_totals(["coverage"])["coverage"]["total"] == 120
