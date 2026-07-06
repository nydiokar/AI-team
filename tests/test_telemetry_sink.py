import json
import os
import time
import urllib.error

import src.control.telemetry_sink as sink_mod
from src.control.telemetry_sink import (
    BufferedHttpTelemetrySink,
    FanOutTelemetrySink,
    build_runtime_telemetry_sink,
)
from src.core.telemetry import build_event


class _RecordingSink:
    def __init__(self):
        self.events = []
        self.flushed = 0

    def emit(self, event):
        self.events.append(event)

    def emit_many(self, events):
        self.events.extend(events)

    def flush(self):
        self.flushed += 1


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


def test_batches_are_split_below_encoded_byte_limit(tmp_path):
    sink = BufferedHttpTelemetrySink(
        "http://controller",
        "token",
        node_id="worker-a",
        spool_dir=tmp_path,
        batch_size=200,
        upload_max_bytes=65_536,
    )
    events = [
        _event(f"{index:03d}_" + "x" * 70)
        for index in range(200)
    ]

    batches = sink._split_batches(events)

    assert len(batches) > 1
    assert sum(len(batch) for batch in batches) == 200
    for batch in batches:
        encoded = json.dumps(
            sink._batch_body(batch),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        assert len(encoded) <= sink.upload_max_bytes


def test_retryable_upload_failure_uses_bounded_backoff(tmp_path, monkeypatch):
    sink = BufferedHttpTelemetrySink(
        "http://controller",
        "token",
        node_id="worker-a",
        spool_dir=tmp_path,
        upload_max_attempts=3,
        retry_backoff_seconds=0.01,
    )
    attempts = []
    sleeps = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def urlopen(request, timeout):
        attempts.append((request, timeout))
        if len(attempts) < 3:
            raise urllib.error.HTTPError(
                request.full_url, 503, "unavailable", {}, None
            )
        return Response()

    monkeypatch.setattr("src.control.telemetry_sink.urllib.request.urlopen", urlopen)
    monkeypatch.setattr("src.control.telemetry_sink.time.sleep", sleeps.append)

    assert sink._post_batch(sink._batch_body([_event("retry")])) is True
    assert len(attempts) == 3
    assert sleeps == [0.01, 0.02]


def test_schema_rejection_is_not_retried_immediately(tmp_path, monkeypatch):
    sink = BufferedHttpTelemetrySink(
        "http://controller",
        "token",
        node_id="worker-a",
        spool_dir=tmp_path,
        upload_max_attempts=3,
    )
    attempts = []

    def urlopen(request, timeout):
        attempts.append((request, timeout))
        raise urllib.error.HTTPError(request.full_url, 422, "invalid", {}, None)

    monkeypatch.setattr("src.control.telemetry_sink.urllib.request.urlopen", urlopen)

    assert sink._post_batch(sink._batch_body([_event("invalid")])) is False
    assert len(attempts) == 1


def test_schema_rejection_is_not_spooled_for_future_retry(tmp_path, monkeypatch):
    sink = BufferedHttpTelemetrySink(
        "http://controller",
        "token",
        node_id="worker-a",
        spool_dir=tmp_path,
        batch_size=1,
    )

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 422, "invalid", {}, None)

    monkeypatch.setattr("src.control.telemetry_sink.urllib.request.urlopen", urlopen)

    sink.emit(_event("permanent"))

    assert list(tmp_path.glob("*.json")) == []


def test_gateway_writes_local_db_and_never_ships(monkeypatch):
    """The gateway owns the store — it must write locally, not HTTP-ship to itself."""
    local = _RecordingSink()
    monkeypatch.setattr(sink_mod, "_build_local_db_sink", lambda: local)
    sink = build_runtime_telemetry_sink(node_id="kanebra", is_gateway=True)
    assert sink is local


def test_worker_without_local_db_ships_over_http(monkeypatch):
    monkeypatch.setattr(sink_mod, "_build_local_db_sink", lambda: None)
    sink = build_runtime_telemetry_sink(
        node_id="Horse", base_url="http://gateway:9001", token="tok", is_gateway=False
    )
    assert isinstance(sink, BufferedHttpTelemetrySink)


def test_worker_with_shadow_db_fans_out_and_still_ships(monkeypatch):
    """The blindness fix: a worker with a local shadow DB must STILL ship to the
    gateway (HTTP) while also mirroring to its local ledger."""
    local = _RecordingSink()
    monkeypatch.setattr(sink_mod, "_build_local_db_sink", lambda: local)
    sink = build_runtime_telemetry_sink(
        node_id="Horse", base_url="http://gateway:9001", token="tok", is_gateway=False
    )
    assert isinstance(sink, FanOutTelemetrySink)
    assert local in sink._sinks
    assert any(isinstance(s, BufferedHttpTelemetrySink) for s in sink._sinks)


def test_fanout_isolates_a_failing_sink(tmp_path):
    class _Boom:
        def emit(self, event):
            raise RuntimeError("boom")

        def emit_many(self, events):
            raise RuntimeError("boom")

        def flush(self):
            raise RuntimeError("boom")

    good = _RecordingSink()
    fan = FanOutTelemetrySink([_Boom(), good])
    event = _event("fan")
    fan.emit(event)
    fan.emit_many([event])
    fan.flush()
    assert good.events == [event, event]
    assert good.flushed == 1


def test_expired_spool_files_are_removed_before_replay(tmp_path, monkeypatch):
    sink = BufferedHttpTelemetrySink(
        "http://controller",
        "token",
        node_id="worker-a",
        spool_dir=tmp_path,
        spool_max_age_days=7,
    )
    expired = tmp_path / "expired.json"
    expired.write_text(
        json.dumps(sink._batch_body([_event("expired")])),
        encoding="utf-8",
    )
    old = time.time() - 8 * 86400
    os.utime(expired, (old, old))
    monkeypatch.setattr(
        sink,
        "_post_batch",
        lambda _body: (_ for _ in ()).throw(
            AssertionError("expired batches must not be uploaded")
        ),
    )

    assert sink.replay_spool() == 0
    assert not expired.exists()
