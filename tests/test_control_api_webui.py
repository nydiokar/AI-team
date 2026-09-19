"""U5 — gateway serves the built Web UI WITHOUT the token (no network).

The gateway serves web/dist at / as a plain static page. The dashboard token is
NEVER embedded in HTML — anything that can fetch / (a crawler, any tailnet peer)
would otherwise get full control-API access. A device pairs once by opening
``/#token=...`` (fragment, never sent to the server) or typing it in the TokenGate.
Tests point _web_dist_dir at a temp dir so they don't depend on a real frontend build.
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-webui-token"


class _StubOrchestrator:
    def __init__(self):
        self.session_service = SessionService(SessionStore(), repo_path_validator=lambda _p: None)


@pytest.fixture
def fake_dist(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><title>UI</title></head><body>app</body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    (dist / "favicon.ico").write_text("icon", encoding="utf-8")
    monkeypatch.setattr(control_api, "_web_dist_dir", lambda: dist)
    return dist


@pytest.fixture
def client(monkeypatch, fake_dist):
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(control_api.build_control_api(_StubOrchestrator()))


def test_index_never_embeds_token(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "app" in r.text  # the real index is served
    assert "__DASHBOARD_TOKEN__" not in r.text
    assert TOKEN not in r.text


def test_spa_fallback_returns_index(client):
    # An unknown client-side route returns the SPA index (not 404).
    r = client.get("/sessions/abc123")
    assert r.status_code == 200
    assert "app" in r.text
    assert "__DASHBOARD_TOKEN__" not in r.text
    assert TOKEN not in r.text


def test_api_still_requires_token(client):
    assert client.get("/api/sessions").status_code == 401
    r = client.get("/api/sessions", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_unknown_api_get_returns_404_not_spa(client):
    # DX-1: an unmatched GET under /api/ must 404, not fall through to the SPA
    # index (which would 200 with HTML and hide the missing endpoint).
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert "__DASHBOARD_TOKEN__" not in r.text


def test_real_static_file_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.text == "icon"


def test_assets_mounted(client):
    r = client.get("/assets/app.js")
    assert r.status_code == 200 and "console.log" in r.text


def test_api_still_requires_token_even_with_ui(client):
    # Serving the UI must not weaken API auth.
    assert client.get("/api/sessions").status_code in (401, 403)


def test_health_still_open(client):
    assert client.get("/health").json()["status"] == "ok"


@pytest.mark.parametrize("attack", [
    "/%2e%2e/%2e%2e/secret.txt",          # percent-encoded ../../
    "/..%2f..%2fsecret.txt",               # mixed-encoded ../../
    "/assets/%2e%2e/%2e%2e/secret.txt",    # escape from under /assets
])
def test_spa_fallback_blocks_path_traversal(monkeypatch, tmp_path, attack):
    """The SPA file resolver must confine reads to web/dist. A traversal payload
    (which the router does NOT normalize) must fall through to the SPA index, never
    serve a file outside dist. Regression for the unauthenticated arbitrary-file-read
    fixed in control_api._web_spa."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><head></head><body>app</body>", encoding="utf-8")
    # A secret sibling of dist (stands in for .env / config) that must never leak.
    (tmp_path / "secret.txt").write_text("TOP_SECRET_TOKEN", encoding="utf-8")
    monkeypatch.setattr(control_api, "_web_dist_dir", lambda: dist)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    c = TestClient(control_api.build_control_api(_StubOrchestrator()))
    r = c.get(attack)
    # The only hard guarantee: the secret outside dist is never served.
    assert "TOP_SECRET_TOKEN" not in r.text
    # Escapes routed to the SPA handler fall through to the index; escapes under
    # the /assets StaticFiles mount are blocked by Starlette with a 404 — both safe.
    if attack.startswith("/assets/"):
        assert r.status_code == 404
    else:
        assert r.status_code == 200 and "<body>app</body>" in r.text
        assert "__DASHBOARD_TOKEN__" not in r.text


def test_no_dist_skips_mount(monkeypatch, tmp_path):
    # When there is no built UI (dev), / is not served by the gateway.
    monkeypatch.setattr(control_api, "_web_dist_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    c = TestClient(control_api.build_control_api(_StubOrchestrator()))
    # No SPA catch-all mounted → unknown path is a 404, and /health still works.
    assert c.get("/health").status_code == 200
    assert c.get("/some/spa/route").status_code == 404


# --- Trusted-host token injection (no pairing on the operator's tailnet devices) ---
# In prod, `tailscale serve` proxies from 127.0.0.1 and uvicorn's proxy-headers
# middleware replaces the peer with the remote tailnet IP (X-Forwarded-For), so the
# app sees the real device IP; TestClient's ``client=`` models that post-middleware
# view directly.

def _client_from(monkeypatch, ip: str, base_url: str = "http://testserver") -> TestClient:
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    return TestClient(
        control_api.build_control_api(_StubOrchestrator()),
        base_url=base_url, client=(ip, 50000),
    )


@pytest.mark.parametrize("host,ip", [
    ("gateway.example-tailnet.ts.net", "100.101.1.2"),   # via `tailscale serve` (XFF peer)
    ("100.64.0.10:9003", "100.101.1.2"),            # direct tailnet bind, tailnet peer
    ("100.99.1.1:9003", "100.101.1.2"),              # tailnet IP literal, bind host unset
    ("[fd7a:115c:a1e0::1]:9003", "fd7a:115c:a1e0::5"),
])
def test_trusted_request_gets_token_injected(monkeypatch, fake_dist, host, ip):
    monkeypatch.setattr(control_api, "_control_api_bind_host", lambda: "100.64.0.10")
    c = _client_from(monkeypatch, ip)
    for path in ("/", "/sessions/abc"):
        r = c.get(path, headers={"Host": host})
        assert r.status_code == 200
        assert f'window.__DASHBOARD_TOKEN__ = "{TOKEN}"' in r.text
        assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("host,ip", [
    ("evil.example.com", "100.101.1.2"),        # DNS rebinding: attacker-controlled name
    ("100.64.0.10", "203.0.113.9"),            # trusted name, non-tailnet client
    ("gateway.example-tailnet.ts.net", "192.168.1.20"),
    ("192.168.1.5:9003", "100.101.1.2"),        # non-tailnet IP literal
    # Loopback peer = a local process (e.g. a host-networked container or an SSRF
    # through a local service), never a remote device: never trusted.
    ("127.0.0.1:9003", "127.0.0.1"),
    ("localhost:9003", "127.0.0.1"),
    ("gateway.example-tailnet.ts.net", "127.0.0.1"),
])
def test_untrusted_request_never_gets_token(monkeypatch, fake_dist, host, ip):
    monkeypatch.setattr(control_api, "_control_api_bind_host", lambda: "100.64.0.10")
    c = _client_from(monkeypatch, ip)
    r = c.get("/", headers={"Host": host})
    assert r.status_code == 200
    assert "__DASHBOARD_TOKEN__" not in r.text
    assert TOKEN not in r.text


@pytest.mark.parametrize("host,base_url", [
    ("100.64.0.10:9003", "http://100.64.0.10:9003"),        # dialing our tailnet IP
    ("gateway.example-tailnet.ts.net", "http://testserver"),       # via serve: XFF = our own IP
])
def test_self_originated_request_never_gets_token(monkeypatch, fake_dist, host, base_url):
    # A process ON the gateway host (host-networked container, local SSRF) shows up
    # with one of our own addresses as the peer — local, not a remote device.
    monkeypatch.setattr(control_api, "_is_local_address", lambda ip: ip == "100.64.0.10")
    c = _client_from(monkeypatch, "100.64.0.10", base_url=base_url)
    r = c.get("/", headers={"Host": host})
    assert TOKEN not in r.text


def test_is_local_address():
    assert control_api._is_local_address("127.0.0.1")
    assert not control_api._is_local_address("203.0.113.9")   # TEST-NET-3, never local
    assert not control_api._is_local_address("not-an-ip")


def test_index_forbids_framing(monkeypatch, fake_dist):
    # The auto-authenticated dashboard must not be frameable (clickjacking).
    c = _client_from(monkeypatch, "100.101.1.2")
    for host in ("gateway.example-tailnet.ts.net", "evil.example.com"):
        r = c.get("/", headers={"Host": host})
        assert r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
