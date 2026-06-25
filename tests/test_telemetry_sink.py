import json
import os
import time
import urllib.error

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
