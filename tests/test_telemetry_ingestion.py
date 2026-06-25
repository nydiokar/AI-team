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
