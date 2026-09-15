"""A81 event-driven read refresh — server read-path efficiency.

Two properties of the transcript LIST read (`GET /api/sessions/{id}/messages` →
`transcript.get_transcript` → `MeshDB.get_session_turns`):

  1. The projection is SLIM — the large `parsed_output_json`/`file_changes_json`
     blobs (and unused `error_class`/`return_code`/`session_id`) are NOT selected,
     while the fields the list renders (`reply_text`, `result`, `files_modified_json`,
     `usage_json`) still are, so the rendered transcript is byte-identical.
  2. `WHERE session_id=? ORDER BY created_at ASC` is served by the composite index
     `idx_mesh_tasks_session_created` as a covered range scan — no temp-b-tree sort.
"""
from datetime import datetime, timezone

from src.control.db import MeshDB
from src.control import transcript as transcript_mod
from src.core.interfaces import Session, SessionStatus


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
    db.enqueue_task(task_id="task_1", session_id="sess_slim", machine_id="Horse",
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
                    "error_class", "return_code", "session_id"):
        assert dropped not in row, f"{dropped} should be dropped from the projection"


def test_transcript_still_renders_full_content_after_slim(tmp_path):
    """The slim projection is I/O-only — the transcript the client sees is unchanged:
    full reply_text, correct file_count, usage summary."""
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_session("sess_render"))
    db.enqueue_task(task_id="task_r", session_id="sess_render", machine_id="Horse",
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
