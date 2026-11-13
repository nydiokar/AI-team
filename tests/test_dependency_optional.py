"""Optional dependency integrations against local files and a local push relay."""

import asyncio
import base64
import queue
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import http_ece
import jwt
import pandas as pd
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import BaseModel
from pywebpush import webpush_async

from scripts.dispatch import dispatch_state
from src.services.push_service import PushService


def test_dispatch_parquet_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parquet = tmp_path / "state.parquet"
    markdown = tmp_path / "state.md"
    monkeypatch.setattr(dispatch_state, "STATE_PARQUET", parquet)
    monkeypatch.setattr(dispatch_state, "STATE_MD", markdown)
    job = dispatch_state.JobState(
        "offline", "offline.md", "ready", "2026-11-13T00:00:00Z", "2026-11-13T00:00:00Z",
    )
    dispatch_state.render([job])
    result = pd.read_parquet(parquet)
    assert result["job_id"].tolist() == ["offline"]
    assert result["status"].tolist() == ["ready"]
    assert pd.to_datetime(result["created_at"], utc=True).dt.year.tolist() == [2026]
    assert "offline" in markdown.read_text()


class _PushCapture(BaseModel):
    body: bytes
    authorization: str


@pytest.mark.parametrize("transport", ["application", "aiohttp"])
def test_push_adapter_encrypts_and_signs_to_local_relay(tmp_path: Path, transport: str) -> None:
    received: queue.Queue[_PushCapture] = queue.Queue(maxsize=1)

    class Relay(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            assert 0 < length < 65536
            received.put_nowait(_PushCapture(
                body=self.rfile.read(length), authorization=self.headers["Authorization"],
            ))
            self.send_response(201)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    receiver = ec.generate_private_key(ec.SECP256R1())
    signing = ec.generate_private_key(ec.SECP256R1())
    private_key = tmp_path / "vapid.pem"
    private_key.write_bytes(signing.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    public = receiver.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )
    auth = b"isolated-auth-key"
    with HTTPServer(("127.0.0.1", 0), Relay) as relay:
        thread = threading.Thread(target=relay.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        endpoint = f"http://127.0.0.1:{relay.server_port}"
        service = PushService(SimpleNamespace(push=SimpleNamespace(
            vapid_private_key=str(private_key), vapid_subject="mailto:offline@example.test", send_timeout_sec=3,
        )), None)
        try:
            subscription = {
                "endpoint": endpoint,
                "p256dh_key": base64.urlsafe_b64encode(public).decode().rstrip("="),
                "auth_key": base64.urlsafe_b64encode(auth).decode().rstrip("="),
            }
            data = '{"body":"offline notification"}'
            if transport == "application":
                service._send_blocking(subscription, data)
            else:
                response = asyncio.run(webpush_async(
                    subscription_info={"endpoint": endpoint, "keys": {
                        "p256dh": subscription["p256dh_key"], "auth": subscription["auth_key"],
                    }}, data=data, vapid_private_key=str(private_key),
                    vapid_claims={"sub": "mailto:offline@example.test"}, timeout=3,
                ))
                assert not isinstance(response, str)
                assert response.status == 201
            capture = received.get(timeout=3)
            assert http_ece.decrypt(capture.body, private_key=receiver, auth_secret=auth) == b'{"body":"offline notification"}'
            token = capture.authorization.split("t=", 1)[1].split(",", 1)[0]
            claims = jwt.decode(token, signing.public_key(), algorithms=["ES256"], audience=endpoint)
            assert claims["sub"] == "mailto:offline@example.test"
        finally:
            relay.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
