import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.control.db import MeshDB
from src.control.task_server import TelemetryBatchPayload, submit_telemetry_batch
from src.core.telemetry import build_event


def _request(content_length: int = 0) -> Request:
    headers = []
    if content_length:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return Request({"type": "http", "headers": headers})


def _event(node_id: str = "worker-a") -> dict:
    return build_event(
        "turn.started",
        turn_id="turn_ingest",
        node_id=node_id,
        emitter_process_instance_id="proc_ingest",
        source="worker",
    ).model_dump(mode="json")


def test_batch_validates_events_independently(tmp_path, monkeypatch):
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr("src.control.task_server.get_db", lambda: db)
    payload = TelemetryBatchPayload(
        batch_id="batch_1",
        node_id="worker-a",
        events=[
            _event(),
            {"event_id": "invalid"},
            _event(node_id="worker-b"),
        ],
    )

    result = submit_telemetry_batch(payload, _request())

    assert result["accepted"] == 1
    assert result["duplicates"] == 0
    assert result["rejected"] == 2
    assert result["rejections"] == [
        {"index": 1, "code": "schema_invalid"},
        {"index": 2, "code": "node_id_mismatch"},
    ]


def test_disabled_telemetry_accepts_no_writes(tmp_path, monkeypatch):
    from config import config

    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr("src.control.task_server.get_db", lambda: db)
    monkeypatch.setattr(config.telemetry, "enabled", False)
    payload = TelemetryBatchPayload(
        batch_id="batch_disabled",
        node_id="worker-a",
        events=[_event()],
    )

    result = submit_telemetry_batch(payload, _request())

    assert result["disabled"] is True
    assert result["accepted"] == 0
    count = db._conn().execute("SELECT COUNT(*) FROM llm_events").fetchone()[0]
    assert count == 0


def test_duplicate_batch_upload_does_not_duplicate_accounting(tmp_path, monkeypatch):
    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr("src.control.task_server.get_db", lambda: db)
    event = _event()
    payload = TelemetryBatchPayload(
        batch_id="batch_duplicate",
        node_id="worker-a",
        events=[event],
    )

    first = submit_telemetry_batch(payload, _request())
    second = submit_telemetry_batch(payload, _request())

    assert first["accepted"] == 1
    assert first["duplicates"] == 0
    assert second["accepted"] == 0
    assert second["duplicates"] == 1
    count = db._conn().execute("SELECT COUNT(*) FROM llm_events").fetchone()[0]
    assert count == 1


def test_encoded_payload_size_is_enforced_without_content_length(
    tmp_path, monkeypatch
):
    from config import config

    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr("src.control.task_server.get_db", lambda: db)
    monkeypatch.setattr(config.telemetry, "enabled", True)
    monkeypatch.setattr(config.telemetry, "upload_max_bytes", 100)
    payload = TelemetryBatchPayload(
        batch_id="batch_too_large",
        node_id="worker-a",
        events=[_event()],
    )

    with pytest.raises(HTTPException) as error:
        submit_telemetry_batch(payload, _request())

    assert error.value.status_code == 413


def test_batch_defers_turn_projection_to_flusher(tmp_path, monkeypatch):
    """Ingestion inserts raw events fast; projection is deferred to the flusher.

    This keeps the DB write lock off the worker request path — the turn is not
    projected inside the request, but its id is queued for the background
    flusher, and ``project_dirty`` builds it.
    """
    import src.control.task_server as ts
    from src.control.telemetry_store import TelemetryStore

    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(ts, "get_db", lambda: db)
    ts._drain_projection()  # clear any leaked global dirty state

    payload = TelemetryBatchPayload(
        batch_id="batch_defer",
        node_id="worker-a",
        events=[_event()],
    )
    result = submit_telemetry_batch(payload, _request())
    assert result["accepted"] == 1

    # Raw event landed synchronously; the turn projection did NOT run in-request.
    events = db._conn().execute("SELECT COUNT(*) FROM llm_events").fetchone()[0]
    turns_in_request = db._conn().execute("SELECT COUNT(*) FROM llm_turns").fetchone()[0]
    assert events == 1
    assert turns_in_request == 0

    # The turn is queued for the flusher; draining + projecting builds it.
    dirty_turns, dirty_sessions = ts._drain_projection()
    assert "turn_ingest" in dirty_turns
    TelemetryStore(db).project_dirty(dirty_turns, dirty_sessions, isolate=True)
    turns_after = db._conn().execute("SELECT COUNT(*) FROM llm_turns").fetchone()[0]
    assert turns_after == 1
