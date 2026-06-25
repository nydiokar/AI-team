"""M3 — read-only web dashboard tests (no network, no paid backend).

Covers: auth enforcement, the read-model JSON endpoints fed by db.list_* +
SessionView, the events poll endpoint (cold tail + incremental since-offset),
and the observability.read_recent_events reader it sits on.
"""
import json

import pytest
from fastapi.testclient import TestClient

from src.control import dashboard
from src.core import observability


TOKEN = "test-dash-token"


@pytest.fixture
def client(monkeypatch):
    # Force a known token regardless of env/config.
    monkeypatch.setattr(dashboard, "_dashboard_token", lambda: TOKEN)
    return TestClient(dashboard.app)


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --- auth -------------------------------------------------------------------

def test_health_is_open(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_index_is_open_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "AI-Team Cockpit" in r.text


def test_api_requires_token(client):
    assert client.get("/api/sessions").status_code in (401, 403)  # no header


def test_api_rejects_bad_token(client):
    r = client.get("/api/sessions", headers=_auth("wrong"))
    assert r.status_code == 401


def test_missing_server_token_is_500(monkeypatch):
    monkeypatch.setattr(dashboard, "_dashboard_token", lambda: "")
    c = TestClient(dashboard.app)
    r = c.get("/api/sessions", headers=_auth("anything"))
    assert r.status_code == 500


# --- read-model endpoints ---------------------------------------------------

def test_sessions_endpoint_reflects_store(client, tmp_path):
    # Create a session through the real service/store (isolated DB via conftest).
    from src.services.session_store import SessionStore
    from src.services.session_service import SessionService
    from src.core.interfaces import SessionOrigin

    svc = SessionService(SessionStore())
    res = svc.create_session(
        backend="claude", repo_path=str(tmp_path), chat_id=1,
        origin=SessionOrigin("web", "user"),
    )
    assert res.ok

    r = client.get("/api/sessions", headers=_auth())
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    ids = {s["session_id"] for s in sessions}
    assert res.session.session_id in ids
    mine = next(s for s in sessions if s["session_id"] == res.session.session_id)
    # SessionView shape (M2): derived booleans + origin present.
    assert mine["backend"] == "claude"
    assert mine["is_active"] is True
    assert mine["origin_channel"] == "web"


def test_tasks_and_nodes_endpoints_return_lists(client):
    rt = client.get("/api/tasks", headers=_auth())
    rn = client.get("/api/nodes", headers=_auth())
    assert rt.status_code == 200 and isinstance(rt.json()["tasks"], list)
    assert rn.status_code == 200 and isinstance(rn.json()["nodes"], list)


def test_tasks_limit_validation(client):
    assert client.get("/api/tasks?limit=0", headers=_auth()).status_code == 422
    assert client.get("/api/tasks?limit=99999", headers=_auth()).status_code == 422


def test_turn_graph_diagnostics_and_timeline_endpoints(client):
    from datetime import timedelta
    from src.control.db import get_db
    from src.control.telemetry_store import TelemetryStore
    from src.core.telemetry import build_event, utc_now

    db = get_db()
    assert db is not None
    store = TelemetryStore(db)
    start = utc_now()
    common = {
        "turn_id": "turn_dashboard",
        "node_id": "gateway",
        "emitter_process_instance_id": "gateway_proc",
        "source": "gateway",
        "backend": "codex",
    }
    store.insert_events(
        [
            build_event("turn.started", event_time=start, observed_time=start, **common),
            build_event(
                "invocation.created",
                event_time=start,
                observed_time=start,
                invocation_id="inv_dashboard",
                attributes={
                    "attempt": 1,
                    "spawn_reason": "initial",
                    "action": "run_oneoff",
                },
                **common,
            ),
            build_event(
                "turn.completed",
                event_time=start + timedelta(seconds=1),
                observed_time=start + timedelta(seconds=1),
                invocation_id="inv_dashboard",
                attributes={
                    "status": "success",
                    "timeout_status": "none",
                    "exit_code": 0,
                },
                **common,
            ),
        ]
    )

    turns = client.get("/api/turns", headers=_auth())
    detail = client.get("/api/turns/turn_dashboard", headers=_auth())
    diagnostics = client.get(
        "/api/turns/turn_dashboard/diagnostics", headers=_auth()
    )
    graph = client.get("/api/turns/turn_dashboard/graph", headers=_auth())
    timeline = client.get("/api/turns/turn_dashboard/events", headers=_auth())

    assert turns.status_code == 200
    assert any(turn["turn_id"] == "turn_dashboard" for turn in turns.json()["turns"])
    assert detail.json()["final_status"] == "success"
    assert len(diagnostics.json()["invocations"]) == 1
    assert graph.json()["nodes"][0]["kind"] == "turn"
    assert len(timeline.json()["events"]) == 3


def test_sessions_limit_validation_and_bound(client, tmp_path):
    from src.services.session_store import SessionStore
    from src.services.session_service import SessionService

    svc = SessionService(SessionStore())
    for _ in range(5):
        svc.create_session(backend="claude", repo_path=str(tmp_path))

    # Out-of-range rejected like /api/tasks.
    assert client.get("/api/sessions?limit=0", headers=_auth()).status_code == 422
    assert client.get("/api/sessions?limit=99999", headers=_auth()).status_code == 422

    # Limit actually bounds the result set.
    r = client.get("/api/sessions?limit=2", headers=_auth())
    assert r.status_code == 200
    assert len(r.json()["sessions"]) == 2


# --- node liveness (derived, not the stale stored status) -------------------

def test_node_liveness_helper_fresh_vs_stale():
    from datetime import datetime, timedelta, timezone
    from src.control.dashboard import _annotate_node_liveness, _heartbeat_timeout_sec

    now = datetime.now(tz=timezone.utc)
    timeout = _heartbeat_timeout_sec()

    fresh = {"last_heartbeat": now.isoformat(), "status": "offline"}  # stale column!
    _annotate_node_liveness(fresh)
    assert fresh["live"] is True  # derived from timestamp, not the "offline" column
    assert fresh["heartbeat_age_sec"] is not None

    stale = {"last_heartbeat": (now - timedelta(seconds=timeout + 30)).isoformat(),
             "status": "online"}  # stale column says online
    _annotate_node_liveness(stale)
    assert stale["live"] is False  # heartbeat too old -> offline regardless of column


def test_node_liveness_helper_handles_missing_and_bad():
    from src.control.dashboard import _annotate_node_liveness
    for bad in ({}, {"last_heartbeat": None}, {"last_heartbeat": "not-a-date"}):
        _annotate_node_liveness(bad)
        assert bad["live"] is False
        assert bad["heartbeat_age_sec"] is None


def test_nodes_endpoint_annotates_live(client):
    db = client.app  # noqa: F841 — fetch db via the shared get_db
    from src.control.db import get_db
    d = get_db()
    d.upsert_node(node_id="N1", tailscale_ip="127.0.0.1", api_port=9001,
                  backends=["claude"], max_concurrent=1)  # last_heartbeat=now
    r = client.get("/api/nodes", headers=_auth())
    assert r.status_code == 200
    n1 = next(n for n in r.json()["nodes"] if n["node_id"] == "N1")
    assert n1["live"] is True
    assert "heartbeat_age_sec" in n1


# --- events endpoint + reader ----------------------------------------------

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


def test_events_endpoint(client, events_file):
    observability.emit_event("dispatch", task_id="tX")
    r = client.get("/api/events", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert any(e["event"] == "dispatch" for e in body["events"])
    assert body["offset"] > 0

    # Poll with the offset → no repeats.
    r2 = client.get(f"/api/events?since={body['offset']}", headers=_auth())
    assert r2.json()["events"] == []
