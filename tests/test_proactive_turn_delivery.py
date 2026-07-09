"""Proactive turn delivery: worker report -> DB turn -> gateway notify hook.

Covers the persistence + endpoint layer added so an autonomous (background-job
continuation) turn becomes a first-class conversation turn and reaches the user.
The driver-side detection is covered by test_sdk_driver_proactive.py.
"""
from datetime import datetime, timezone

import pytest

from src.core.interfaces import Session, SessionStatus
from src.control.db import MeshDB
from src.control import transcript as transcript_mod


def _session(session_id: str = "sess_pro") -> Session:
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


def test_record_proactive_turn_persists_a_completed_assistant_turn(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_session())

    db.record_proactive_turn(
        task_id="proactive_abc123",
        session_id="sess_pro",
        backend="claude",
        machine_id="Horse",
        reply_text="The background job finished — all green.",
        usage={"input_tokens": 10, "output_tokens": 5},
    )

    rows = db.get_session_turns("sess_pro")
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "proactive_turn"
    assert row["status"] == "completed"
    assert (row["prompt"] or "") == ""
    assert row["reply_text"] == "The background job finished — all green."


def test_transcript_flags_proactive_turn_with_no_user_message(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_session())
    db.record_proactive_turn(
        task_id="proactive_xyz",
        session_id="sess_pro",
        backend="claude",
        machine_id="Horse",
        reply_text="Autonomous update text.",
    )

    # Point the DB singleton the transcript reader uses at our temp DB.
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = db
    try:
        turns = transcript_mod.get_transcript(tmp_path, tmp_path, "sess_pro", limit=50)
    finally:
        db_mod._db_instance = old

    assert turns is not None and len(turns) == 1
    turn = turns[0]
    assert turn["proactive"] is True
    assert turn["instruction"] == ""            # no fake user message
    assert turn["result"] == "Autonomous update text."


def test_proactive_endpoint_persists_and_invokes_hook(tmp_path):
    from config import config as cfg
    cfg.mesh.db_path = str(tmp_path / "mesh_ep.db")
    cfg.mesh.worker_token = "tok"
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old is not None:
        old.close()

    try:
        db = db_mod.get_db()
        db.upsert_session(_session("sess_ep"))

        import src.control.task_server as ts
        called = {}
        ts.bind_proactive_hook(lambda sid, tid, text, bsid: called.update(
            session_id=sid, task_id=tid, text=text, backend_session_id=bsid))

        try:
            resp = ts.report_proactive_turn("sess_ep", ts.ProactiveTurnPayload(
                node_id="Horse",
                session_id="sess_ep",
                output="Reaching back: the job is done.",
                backend_session_id="bk-999",
            ))
            assert resp["status"] == "accepted"
            task_id = resp["task_id"]

            # Persisted as a turn
            rows = db.get_session_turns("sess_ep")
            assert any(r["task_id"] == task_id and r["action"] == "proactive_turn" for r in rows)

            # Hook fired with the right payload (the "reach back")
            assert called["session_id"] == "sess_ep"
            assert called["text"] == "Reaching back: the job is done."
            assert called["backend_session_id"] == "bk-999"

            # Empty output is acknowledged without creating a turn
            resp2 = ts.report_proactive_turn("sess_ep", ts.ProactiveTurnPayload(
                node_id="Horse", session_id="sess_ep", output="   "))
            assert resp2["status"] == "empty"
        finally:
            ts.bind_proactive_hook(None)
    finally:
        db_mod._db_instance = None
        if db_mod._db_instance is None and old is not None:
            old.close()
        db_mod._db_instance = old


def test_proactive_endpoint_404_for_unknown_session(tmp_path):
    from config import config as cfg
    cfg.mesh.db_path = str(tmp_path / "mesh_404.db")
    cfg.mesh.worker_token = "tok"
    import src.control.db as db_mod
    old = db_mod._db_instance
    db_mod._db_instance = None
    if old is not None:
        old.close()
    try:
        db_mod.get_db()  # ensure schema exists
        import src.control.task_server as ts
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            ts.report_proactive_turn("nope", ts.ProactiveTurnPayload(
                node_id="Horse", session_id="nope", output="hi"))
        assert exc.value.status_code == 404
    finally:
        db_mod._db_instance = None
        db_mod._db_instance = old
