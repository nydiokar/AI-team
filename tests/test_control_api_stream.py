"""U4 — SSE event-stream tests (no network, no paid backend).

Auth rejection is checked through the app (returns before streaming, so no hang).
The frame logic is tested by driving the extracted async generator directly —
Starlette's TestClient buffers streaming responses and cannot drive an endless
SSE stream, so we test event_stream_frames() with a bounded max_iterations.
"""
import json

import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.control.control_api import event_stream_frames
from src.core import observability
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-stream-token"


class _StubOrchestrator:
    def __init__(self):
        self.session_service = SessionService(SessionStore())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(_StubOrchestrator()))


@pytest.fixture
def events_file(monkeypatch, tmp_path):
    monkeypatch.setattr(observability, "_LOGS_DIR", tmp_path)
    return tmp_path / "events.ndjson"


# --- auth (returns before any streaming, safe through TestClient) -----------

def test_stream_rejects_bad_token(client):
    with client.stream("GET", "/api/events/stream?token=wrong") as r:
        assert r.status_code == 401


def test_stream_rejects_missing_token(client):
    with client.stream("GET", "/api/events/stream") as r:
        assert r.status_code == 401


def test_bearer_header_parsing():
    class _Req:
        def __init__(self, h):
            self.headers = h
    assert control_api._bearer_from_header(_Req({"Authorization": "Bearer abc"})) == "abc"
    assert control_api._bearer_from_header(_Req({})) is None


# --- frame generation (drive the async generator directly) ------------------

async def _collect(gen):
    return [frame async for frame in gen]


@pytest.mark.asyncio
async def test_stream_primes_with_existing_events(events_file):
    observability.emit_event("hello_stream", session_id="s1")
    frames = await _collect(event_stream_frames(max_iterations=0))
    # First frame is the priming tail and must contain our event.
    assert frames[0].startswith("data:")
    payload = json.loads(frames[0][len("data:"):].strip())
    assert "hello_stream" in [e["event"] for e in payload["events"]]


@pytest.mark.asyncio
async def test_stream_connect_comment_when_empty(events_file):
    frames = await _collect(event_stream_frames(max_iterations=0))
    assert frames == [": connected\n\n"]


@pytest.mark.asyncio
async def test_stream_pushes_new_event_then_keepalive(events_file):
    # No events at connect → ": connected". Emit one, then poll loop should push it.
    pushed = []

    async def never_disconnect():
        return False

    async def noop_sleep():
        return None

    gen = event_stream_frames(
        is_disconnected=never_disconnect, sleep=noop_sleep, max_iterations=2,
    )
    # frame 0 = connect comment (empty tail)
    it = gen.__aiter__()
    first = await it.__anext__()
    assert first == ": connected\n\n"
    # Now an event lands; the next loop iteration should emit it as data.
    observability.emit_event("late_event", task_id="t9")
    second = await it.__anext__()
    assert second.startswith("data:")
    payload = json.loads(second[len("data:"):].strip())
    assert "late_event" in [e["event"] for e in payload["events"]]
    # Third iteration: nothing new → keep-alive comment.
    third = await it.__anext__()
    assert third == ": keep-alive\n\n"
