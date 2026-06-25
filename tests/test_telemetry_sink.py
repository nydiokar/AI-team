import json
import time

from src.control.telemetry_sink import BufferedHttpTelemetrySink
from src.core.telemetry import build_event


def _event(event_id_suffix="1"):
    event = build_event(
        "turn.started",
        turn_id="turn_sink",
        node_id="worker-a",
        emitter_process_instance_id="proc",
        source="worker",
    )
    return event.model_copy(update={"event_id": f"evt_{event_id_suffix}"})


def test_failed_upload_spools_and_replays_without_changing_event_ids(tmp_path, monkeypatch):
    sink = BufferedHttpTelemetrySink(
        "http://controller",
        "token",
        node_id="worker-a",
        spool_dir=tmp_path,
        batch_size=2,
    )
    calls = []
    monkeypatch.setattr(sink, "_post_batch", lambda body: calls.append(body) or False)
    sink.emit(_event("1"))
    sink.emit(_event("2"))

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    spooled = json.loads(files[0].read_text(encoding="utf-8"))
    assert [event["event_id"] for event in spooled["events"]] == ["evt_1", "evt_2"]

    monkeypatch.setattr(sink, "_post_batch", lambda body: True)
    assert sink.replay_spool() == 1
    assert list(tmp_path.glob("*.json")) == []


def test_flush_is_noop_for_empty_buffer(tmp_path, monkeypatch):
    sink = BufferedHttpTelemetrySink(
        "http://controller",
        "token",
        node_id="worker-a",
        spool_dir=tmp_path,
    )
    called = []
    monkeypatch.setattr(sink, "_post_batch", lambda body: called.append(body) or True)
    sink.flush()
    assert called == []


def test_timed_flush_delivers_small_batch(tmp_path, monkeypatch):
    sink = BufferedHttpTelemetrySink(
        "http://controller",
        "token",
        node_id="worker-a",
        spool_dir=tmp_path,
        batch_size=50,
        flush_interval_ms=100,
    )
    called = []
    monkeypatch.setattr(sink, "_post_batch", lambda body: called.append(body) or True)
    sink.emit(_event("timed"))
    deadline = time.time() + 1
    while not called and time.time() < deadline:
        time.sleep(0.02)
    assert len(called) == 1
    assert called[0]["events"][0]["event_id"] == "evt_timed"
