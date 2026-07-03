"""Web Push (#21) tests — DB layer, control API validation, and service boundary.

No network, no paid backend, no pywebpush required: the delivery send is monkey-
patched so we assert the fan-out contract (bounded, non-blocking, gone→disable,
transient→error) without an outbound push service.
"""
import asyncio
import types

import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.control.db import MeshDB, _CURRENT_VERSION
from src.services.push_service import PushService, build_task_payload, push_available


TOKEN = "test-push-token"


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    d = MeshDB(str(tmp_path / "push.db"))
    yield d
    d.close()


def test_migration_present():
    assert _CURRENT_VERSION >= 20


def test_upsert_is_idempotent_and_refreshes(db):
    db.upsert_push_subscription("https://e/1", "p1", "a1", "chrome")
    db.upsert_push_subscription("https://e/1", "p2", "a2", "chrome-2")
    subs = db.list_push_subscriptions()
    assert len(subs) == 1
    assert subs[0]["p256dh_key"] == "p2"
    assert subs[0]["label"] == "chrome-2"


def test_disable_and_enabled_filter(db):
    db.upsert_push_subscription("https://e/1", "p", "a")
    db.upsert_push_subscription("https://e/2", "p", "a")
    db.disable_push_subscription("https://e/2")
    assert len(db.list_push_subscriptions(enabled_only=True)) == 1
    assert len(db.list_push_subscriptions(enabled_only=False)) == 2


def test_resubscribe_reenables(db):
    db.upsert_push_subscription("https://e/1", "p", "a")
    db.disable_push_subscription("https://e/1")
    db.upsert_push_subscription("https://e/1", "p", "a")  # browser re-subscribes
    assert len(db.list_push_subscriptions(enabled_only=True)) == 1


def test_mark_error_keeps_subscription(db):
    db.upsert_push_subscription("https://e/1", "p", "a")
    db.mark_push_error("https://e/1", "boom")
    subs = db.list_push_subscriptions(enabled_only=True)
    assert len(subs) == 1
    assert subs[0]["last_error"] == "boom"


# ---------------------------------------------------------------------------
# Service boundary
# ---------------------------------------------------------------------------

def _push_cfg(configured=True):
    push = types.SimpleNamespace(
        vapid_public_key="pub" if configured else "",
        vapid_private_key="priv" if configured else "",
        vapid_subject="mailto:x@y" if configured else "",
        fanout_concurrency=4,
        send_timeout_sec=0.3,
        max_subscribe_bytes=4096,
    )
    push.configured = bool(push.vapid_public_key and push.vapid_private_key and push.vapid_subject)
    return types.SimpleNamespace(push=push)


def test_payload_is_bounded_and_sanitized():
    p = build_task_payload(
        title="T" * 500, body="B" * 500, task_id="t", session_id="s", url="/sessions/s"
    )
    assert len(p["title"]) <= 120
    assert len(p["body"]) <= 240
    assert p["url"] == "/sessions/s"
    # only the whitelisted keys are present — no prompt/output/etc.
    assert set(p) == {"title", "body", "task_id", "session_id", "url"}


def test_push_unavailable_without_vapid(db):
    ok, reason = push_available(_push_cfg(configured=False), db)
    assert not ok
    assert reason == "vapid_not_configured"


def test_push_rejects_malformed_public_key(db):
    # A 32-byte (43-char) key is the common wrong-format mistake; the browser
    # rejects it as "applicationServerKey is not valid". Catch it server-side.
    cfg = _push_cfg(configured=True)
    cfg.push.vapid_public_key = "YJIP-sRQ1mAzoTTowe63PyX_jjrEr0iFmUrtKt53pX0"  # 43 chars
    ok, reason = push_available(cfg, db)
    assert not ok
    assert reason == "vapid_public_key_malformed"


def test_valid_vapid_public_key_accepts_65_byte_point():
    import base64
    from src.services.push_service import _valid_vapid_public_key

    raw = bytes([0x04]) + bytes(64)  # 65 bytes, uncompressed point marker
    key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    assert _valid_vapid_public_key(key) is True
    assert _valid_vapid_public_key("") is False
    assert _valid_vapid_public_key("short") is False


def test_fanout_disables_gone_and_records_timeout(db):
    db.upsert_push_subscription("https://e/ok", "p", "a")
    db.upsert_push_subscription("https://e/gone", "p", "a")
    db.upsert_push_subscription("https://e/slow", "p", "a")

    svc = PushService(_push_cfg(), db)
    svc.available = lambda: (True, None)  # bypass pywebpush import for the boundary test

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    class _Err(Exception):
        def __init__(self, code):
            self.response = _Resp(code)

    import time

    def fake_send(sub, data):
        ep = sub["endpoint"]
        if ep.endswith("/gone"):
            raise _Err(410)
        if ep.endswith("/slow"):
            time.sleep(0.6)  # exceeds the 0.3s per-send timeout below
        return None

    svc._send_blocking = fake_send
    # Tight per-send timeout so the slow sender is cut off well before it returns.
    svc._cfg.push.send_timeout_sec = 0.3

    t0 = time.time()
    asyncio.run(svc.fanout({"title": "t", "body": "b", "url": "/"}))
    elapsed = time.time() - t0

    # The awaited fan-out resolves at the timeout boundary, not the 0.6s send.
    # (A small teardown tail from the still-running thread is tolerated.)
    assert elapsed < 0.9
    remaining = db.list_push_subscriptions(enabled_only=True)
    endpoints = {s["endpoint"] for s in remaining}
    assert "https://e/gone" not in endpoints          # 410 → disabled
    assert "https://e/slow" in endpoints              # transient → kept
    slow = next(s for s in remaining if s["endpoint"] == "https://e/slow")
    assert slow["last_error"] == "timeout"


def test_send_blocking_raises_malformed_on_missing_key(db):
    # The real _send_blocking must classify a row missing keys as permanent so the
    # fan-out disables it rather than retrying it every outcome.
    from src.services.push_service import _MalformedSubscription

    svc = PushService(_push_cfg(), db)
    # No pywebpush needed: the KeyError happens before webpush() is reached.
    with pytest.raises(_MalformedSubscription):
        svc._send_blocking({"endpoint": "https://e/x"}, "{}")


def test_fanout_disables_malformed_row(db):
    from src.services.push_service import _MalformedSubscription

    db.upsert_push_subscription("https://e/bad", "p", "a")
    svc = PushService(_push_cfg(), db)
    svc.available = lambda: (True, None)

    def malformed_send(sub, data):
        raise _MalformedSubscription("p256dh_key")

    svc._send_blocking = malformed_send
    asyncio.run(svc.fanout({"title": "t", "body": "b", "url": "/"}))
    assert len(db.list_push_subscriptions(enabled_only=True)) == 0


def test_fanout_noop_when_unavailable(db):
    db.upsert_push_subscription("https://e/1", "p", "a")
    svc = PushService(_push_cfg(configured=False), db)
    # Must not raise and must not disable/error anything.
    asyncio.run(svc.fanout({"title": "t", "body": "b", "url": "/"}))
    assert len(db.list_push_subscriptions(enabled_only=True)) == 1


# ---------------------------------------------------------------------------
# Control API
# ---------------------------------------------------------------------------

class _StubOrchestrator:
    pass


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    test_db = MeshDB(str(tmp_path / "api_push.db"))
    monkeypatch.setattr(control_api, "_db", lambda: test_db)
    c = TestClient(control_api.build_control_api(_StubOrchestrator()))
    c._test_db = test_db  # type: ignore[attr-defined]
    yield c
    test_db.close()


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_subscribe_requires_auth(client):
    r = client.post("/api/push/subscribe", json={"endpoint": "x", "keys": {"p256dh": "p", "auth": "a"}})
    assert r.status_code in (401, 403)


def test_subscribe_and_status(client):
    r = client.post(
        "/api/push/subscribe",
        headers=_auth(),
        json={"endpoint": "https://e/1", "keys": {"p256dh": "p", "auth": "a"}, "label": "chrome"},
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(client._test_db.list_push_subscriptions()) == 1


def test_subscribe_rejects_malformed(client):
    r = client.post("/api/push/subscribe", headers=_auth(), json={"endpoint": "https://e/1"})
    assert r.status_code == 422
    assert len(client._test_db.list_push_subscriptions()) == 0


def test_subscribe_rejects_oversized_body(client):
    # Real oversized body: caught by the Content-Length pre-check before buffering,
    # and never written to the DB.
    big = {"endpoint": "https://e/1", "keys": {"p256dh": "p", "auth": "a"}, "label": "x" * 9000}
    r = client.post("/api/push/subscribe", headers=_auth(), json=big)
    assert r.status_code == 413
    assert len(client._test_db.list_push_subscriptions()) == 0


def test_subscribe_rejects_oversized_content_length_header(client):
    # A hostile declared Content-Length must be rejected up front even if the
    # actual (small) body would parse — the memory-abuse guard.
    small = '{"endpoint":"https://e/1","keys":{"p256dh":"p","auth":"a"}}'
    r = client.post(
        "/api/push/subscribe",
        headers={**_auth(), "Content-Length": "999999", "Content-Type": "application/json"},
        content=small,
    )
    assert r.status_code == 413
    assert len(client._test_db.list_push_subscriptions()) == 0


def test_unsubscribe_disables(client):
    client.post(
        "/api/push/subscribe",
        headers=_auth(),
        json={"endpoint": "https://e/1", "keys": {"p256dh": "p", "auth": "a"}},
    )
    r = client.post("/api/push/unsubscribe", headers=_auth(), json={"endpoint": "https://e/1"})
    assert r.status_code == 200
    assert len(client._test_db.list_push_subscriptions(enabled_only=True)) == 0


def test_status_reports_unavailable_without_vapid(client):
    r = client.get("/api/push/status", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["vapid_public_key"] == ""
    # Operator diagnostics name the missing env var(s). (The ambient test .env may
    # have some VAPID vars set; VAPID_SUBJECT is the one this suite never sets.)
    assert "VAPID_SUBJECT" in body["missing_env"]
    assert body["enabled_subscriptions"] == 0


def test_fanout_warns_when_subscribers_exist_but_misconfigured(db, caplog):
    import logging

    db.upsert_push_subscription("https://e/1", "p", "a")  # a real subscriber
    svc = PushService(_push_cfg(configured=False), db)     # but VAPID not configured
    # Give the config a missing_config() like the real PushConfig.
    svc._cfg.push.missing_config = lambda: ["VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"]
    with caplog.at_level(logging.WARNING):
        asyncio.run(svc.fanout({"title": "t", "body": "b", "url": "/"}))
    assert any("push_fanout_skipped" in r.message and "subscribers=1" in r.message
               for r in caplog.records)
