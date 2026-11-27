"""A81 event-driven read refresh — server read-path efficiency.

Two properties of the transcript LIST read (`GET /api/sessions/{id}/messages` →
`transcript.get_transcript` → `MeshDB.get_session_turns`):

  1. The projection is SLIM — the large `parsed_output_json`/`file_changes_json`
     blobs (and unused `return_code`/`session_id`) are NOT selected,
     while the fields the list renders (`reply_text`, `result`, `files_modified_json`,
     `usage_json`) still are, so the rendered transcript is byte-identical.
  2. `WHERE session_id=? ORDER BY created_at ASC` is served by the composite index
     `idx_mesh_tasks_session_created` as a covered range scan — no temp-b-tree sort.
"""
import json
from datetime import datetime, timezone

import pytest

from src.control.db import MeshDB
from src.control import transcript as transcript_mod
from src.core.interfaces import Session, SessionStatus
from tests.stage8a_legacy import enqueue_pre_cutover


def _session(session_id: str) -> Session:
    now = datetime.now(tz=timezone.utc).isoformat()
    return Session(
        session_id=session_id,
        backend="claude",
        repo_path="/tmp/repo",
        status=SessionStatus.AWAITING_INPUT,
        created_at=now,
        updated_at=now,
        machine_id="Horse",
    )


def test_get_session_turns_omits_unused_blob_columns(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_session("sess_slim"))
    enqueue_pre_cutover(db, task_id="task_1", session_id="sess_slim", machine_id="Horse",
                        backend="claude", action="resume_session", payload={"prompt": "hi"})
    with db._write() as conn:
        conn.execute(
            """UPDATE mesh_tasks SET created_at=?, completed_at=?, status='completed',
               reply_text=?, result=?, files_modified_json=?, usage_json=?,
               parsed_output_json=?, file_changes_json=?, error_class=?, return_code=?
               WHERE id=?""",
            ("2026-01-01T10:00:00+00:00", "2026-01-01T10:01:00+00:00",
             "the full reply", '{"output": "legacy"}', '["a.py", "b.py"]',
             '{"input_tokens": 10, "output_tokens": 5}',
             '{"big": "parsed blob"}', '[{"diff": "big change blob"}]',
             "some_error", 0, "task_1"),
        )

    rows = db.get_session_turns("sess_slim")
    assert len(rows) == 1
    row = rows[0]
    # Rendered fields are present…
    for kept in ("task_id", "prompt", "reply_text", "result",
                 "files_modified_json", "usage_json", "status", "action",
                 "created_at", "completed_at"):
        assert kept in row, f"expected {kept} in slim projection"
    # …and the unused blobs are NOT selected.
    for dropped in ("parsed_output_json", "file_changes_json",
                    "return_code", "session_id"):
        assert dropped not in row, f"{dropped} should be dropped from the projection"


def test_transcript_still_renders_full_content_after_slim(tmp_path):
    """The slim projection is I/O-only — the transcript the client sees is unchanged:
    full reply_text, correct file_count, usage summary."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_session("sess_render"))
    enqueue_pre_cutover(db, task_id="task_r", session_id="sess_render", machine_id="Horse",
                        backend="claude", action="resume_session", payload={"prompt": "do it"})
    with db._write() as conn:
        conn.execute(
            """UPDATE mesh_tasks SET created_at=?, completed_at=?, status='completed',
               reply_text=?, files_modified_json=?, usage_json=?,
               parsed_output_json=?, file_changes_json=? WHERE id=?""",
            ("2026-01-01T10:00:00+00:00", "2026-01-01T10:01:00+00:00",
             "done, all green", '["x.py", "y.py", "z.py"]',
             '{"input_tokens": 100, "output_tokens": 20}',
             '{"unused": "blob"}', '[{"unused": "blob"}]', "task_r"),
        )

    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = db
    try:
        turns = transcript_mod.get_transcript(tmp_path, tmp_path, "sess_render", limit=50)
    finally:
        db_mod._db_instance = old

    assert turns is not None and len(turns) == 1
    turn = turns[0]
    assert turn["instruction"] == "do it"
    assert turn["result"] == "done, all green"
    assert turn["file_count"] == 3
    assert turn["usage"] == {"input_tokens": 100, "output_tokens": 20}


def test_session_created_composite_index_serves_the_read(tmp_path):
    """The transcript read is a covered range scan on idx_mesh_tasks_session_created
    (no temp b-tree for the ORDER BY)."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    plan_rows = db._conn().execute(
        """EXPLAIN QUERY PLAN
           SELECT id FROM mesh_tasks WHERE session_id = ? ORDER BY created_at ASC LIMIT ?""",
        ("sess_x", 200),
    ).fetchall()
    plan = " | ".join(str(r["detail"]) for r in plan_rows)
    assert "idx_mesh_tasks_session_created" in plan, plan
    # The composite covers both the filter AND the order → no sort step.
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan.upper(), plan


def test_cancelled_transcript_uses_ledger_truth_and_excludes_control_receipts(tmp_path, monkeypatch):
    import src.control.db as db_mod
    from src.backends.claude_driver import SALVAGE_ERROR_BANNER

    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_session("sess_stop"))
    monkeypatch.setattr(db_mod, "_db_instance", db)
    for task_id, action, reply, status in (
        ("turn", "resume_session", SALVAGE_ERROR_BANNER + "\n\n---\n\nWork in progress", "cancelled"),
        ("cancel", "cancel_managed", "interrupt delivered or armed", "completed"),
        ("close", "close_session", "session closed", "completed"),
    ):
        enqueue_pre_cutover(db, task_id=task_id, session_id="sess_stop", machine_id="Horse",
                           backend="claude", action=action, payload={"prompt": "question"})
        with db._write() as conn:
            conn.execute("UPDATE mesh_tasks SET status=?, reply_text=? WHERE id=?", (status, reply, task_id))
    turns = transcript_mod.get_transcript(tmp_path, tmp_path, "sess_stop", limit=1)
    assert len(turns) == 1 and turns[0]["task_id"] == "turn"
    assert turns[0]["success"] is False
    assert "Stopped by you" in turns[0]["result"]
    assert "Work in progress" in turns[0]["result"]
    assert "backend error" not in turns[0]["result"]
    assert db.get_session_turns_tail("sess_stop") == []
    # Original evidence is retained; this is a read correction, no history rewrite.
    assert db.get_task("turn")["reply_text"].startswith(SALVAGE_ERROR_BANNER)


@pytest.mark.parametrize(("backend", "output", "stored_reply", "errors", "error", "message", "kept_reply"), [
    ("codex", "", "Claude failed", ["codex_turn_failed"], "codex_turn_failed",
     "Codex reported a failed turn without a provider reason.", ""),
    ("codex", "Partial reply.", None, ["codex_turn_failed"], None,
     "Codex reported a failed turn without a provider reason.", "Partial reply."),
    ("codex", "", "stdout_tail: hidden", ["Traceback (most recent call last):\n secret"], None,
     "The turn failed without a recorded reason.", ""),
    ("codex", "Partial reply.", "Partial reply.", ["cancelled"], "cancelled",
     "The backend interrupted the turn.", "Partial reply."),
    ("claude", "", "Claude session expired", ["no conversation found with session id"], None,
     "Claude session expired", ""),
])
def test_failed_rows_project_evidence_separately(tmp_path, monkeypatch, backend, output, stored_reply,
                                                 errors, error, message, kept_reply):
    import src.control.db as db_mod

    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_session("sess_history"))
    monkeypatch.setattr(db_mod, "_db_instance", db)
    enqueue_pre_cutover(db, task_id="history", session_id="sess_history", machine_id="Horse",
                       backend=backend, action="resume_session", payload={"prompt": "question"})
    with db._write() as conn:
        conn.execute(
            "UPDATE mesh_tasks SET status='failed', reply_text=?, result=?, error=? WHERE id='history'",
            (stored_reply, json.dumps({
                "output": output, "errors": errors,
                "error_detail": 'stdout_tail:\n{"type":"error","message":"borrowed reason"}',
            }), error),
        )
    [turn] = transcript_mod.get_transcript(tmp_path, tmp_path, "sess_history")
    assert turn["failure"] == {"message": message, "has_reply": bool(kept_reply)}
    assert turn["result"] == (kept_reply or message)
    assert turn["success"] is False


def test_transcript_boundary_rejects_fifth_read_and_allows_retry(monkeypatch):
    from fastapi.testclient import TestClient

    from src.control import control_api
    from src.control.routes.sessions import _TRANSCRIPT_READ_SLOTS
    from src.orchestrator import TaskOrchestrator

    monkeypatch.setattr(control_api, "_dashboard_token", lambda: "test")
    monkeypatch.setattr(transcript_mod, "get_transcript", lambda *args, **kwargs: [])
    client = TestClient(control_api.build_control_api(TaskOrchestrator.__new__(TaskOrchestrator)))
    headers = {"Authorization": "Bearer test"}
    route = "/api/sessions/sess_history/messages"
    for _ in range(4):
        assert _TRANSCRIPT_READ_SLOTS.acquire(blocking=False)
    try:
        busy = client.get(route, headers=headers)
        assert busy.status_code == 503
        assert busy.json()["detail"]["reason"] == "read_at_capacity"
    finally:
        for _ in range(4):
            _TRANSCRIPT_READ_SLOTS.release()
    assert client.get(route, headers=headers).json() == {"messages": []}
    assert client.get(route, headers=headers, params={"limit": 1001}).status_code == 422
