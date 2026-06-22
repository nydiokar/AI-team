"""observability.read_recent_events reader tests (no network, no paid backend).

These were extracted from the old test_dashboard.py when the standalone dashboard
was retired (U2 of docs/CONTROL_SURFACE_UNIFICATION.md). They cover the event
poll reader the control API sits on: cold tail + incremental since-offset,
byte-accurate offsets (multibyte/CRLF), rotation recovery, partial-line handling,
and the no-duplicate TOCTOU property. The HTTP-endpoint coverage now lives in
tests/test_control_api.py.
"""
import pytest

from src.core import observability


@pytest.fixture
def events_file(monkeypatch, tmp_path):
    """Point observability at a temp events.ndjson."""
    monkeypatch.setattr(observability, "_LOGS_DIR", tmp_path)
    return tmp_path / "events.ndjson"


def test_read_recent_events_empty(events_file):
    data = observability.read_recent_events()
    assert data["events"] == [] and data["offset"] == 0


def test_read_recent_events_tail_and_incremental(events_file):
    observability.emit_event("alpha", session_id="s1")
    first = observability.read_recent_events()
    assert [e["event"] for e in first["events"]] == ["alpha"]
    off = first["offset"]
    assert off > 0

    # Nothing new since the last offset.
    same = observability.read_recent_events(since_offset=off)
    assert same["events"] == []

    # Append more; only the delta comes back.
    observability.emit_event("beta", task_id="t1")
    delta = observability.read_recent_events(since_offset=off)
    assert [e["event"] for e in delta["events"]] == ["beta"]


def test_read_recent_events_recovers_after_rotation(events_file):
    observability.emit_event("a")
    observability.emit_event("b")
    off = observability.read_recent_events()["offset"]
    # Simulate rotation: file becomes smaller than the client's stale offset.
    events_file.write_text('{"event":"fresh"}\n', encoding="utf-8")
    data = observability.read_recent_events(since_offset=off)
    # Stale offset must NOT silence the stream — the tail comes back.
    assert [e["event"] for e in data["events"]] == ["fresh"]


def test_read_recent_events_skips_malformed(events_file):
    events_file.write_text(
        '{"event":"good"}\nnot json\n{"event":"good2"}\n', encoding="utf-8"
    )
    data = observability.read_recent_events()
    assert [e["event"] for e in data["events"]] == ["good", "good2"]


def test_offset_is_byte_accurate_with_multibyte_content(events_file):
    """Regression: text-mode seek corrupted offsets on multi-byte chars.

    Emit events containing non-ASCII (multi-byte UTF-8). The incremental delta
    must return exactly the new event and nothing duplicated/dropped — which only
    holds if the offset is a real byte count and the seek is binary.
    """
    observability.emit_event("café", session_id="naïve", detail="日本語")
    off = observability.read_recent_events()["offset"]
    # Offset must equal the real byte length of the file (binary), not char count.
    assert off == events_file.stat().st_size

    observability.emit_event("second", detail="emoji 🚀 tail")
    delta = observability.read_recent_events(since_offset=off)
    assert [e["event"] for e in delta["events"]] == ["second"]
    assert delta["events"][0]["detail"] == "emoji 🚀 tail"
    # And the new offset is again the full byte length — no drift accumulated.
    assert delta["offset"] == events_file.stat().st_size


def test_offset_byte_accurate_with_crlf(events_file):
    """Regression: CRLF line endings broke text-mode tell()/seek() on Windows."""
    events_file.write_bytes(
        b'{"event":"a"}\r\n{"event":"b"}\r\n'
    )
    first = observability.read_recent_events()
    assert [e["event"] for e in first["events"]] == ["a", "b"]
    off = first["offset"]
    assert off == events_file.stat().st_size
    # Append a CRLF line; incremental read returns only it.
    with events_file.open("ab") as f:
        f.write(b'{"event":"c"}\r\n')
    delta = observability.read_recent_events(since_offset=off)
    assert [e["event"] for e in delta["events"]] == ["c"]


def test_partial_trailing_line_not_consumed(events_file):
    """A writer mid-append (no trailing newline) must not be parsed or counted.

    The incomplete line's bytes are excluded from the offset so the next poll
    re-reads it once it's whole — no split-line corruption, no lost event.
    """
    events_file.write_bytes(b'{"event":"complete"}\n{"event":"partial"')  # no \n
    data = observability.read_recent_events()
    assert [e["event"] for e in data["events"]] == ["complete"]
    # Offset stops at the end of the complete line, not EOF.
    full_size = events_file.stat().st_size
    assert data["offset"] < full_size
    # Finish the partial line; the next poll picks it up exactly once.
    with events_file.open("ab") as f:
        f.write(b'}\n')
    delta = observability.read_recent_events(since_offset=data["offset"])
    assert [e["event"] for e in delta["events"]] == ["partial"]


def test_partial_only_line_reports_no_progress(events_file):
    """If the only content is an incomplete line, report no events and hold the
    offset at the start so the next poll re-reads from there."""
    events_file.write_bytes(b'{"event":"incomplete"')
    data = observability.read_recent_events()
    assert data["events"] == []
    assert data["offset"] == 0


def test_no_duplicate_when_event_appended_during_poll(events_file):
    """The offset comes from what was actually read, not a pre-read stat().

    Two back-to-back full reads with the carried offset must never replay an
    event — the property that breaks under a stat/read TOCTOU.
    """
    observability.emit_event("one")
    r1 = observability.read_recent_events(since_offset=0)
    assert [e["event"] for e in r1["events"]] == ["one"]
    # Simulate an append that lands right after r1's read, then poll with r1 offset.
    observability.emit_event("two")
    r2 = observability.read_recent_events(since_offset=r1["offset"])
    assert [e["event"] for e in r2["events"]] == ["two"]  # exactly once, no "one"
