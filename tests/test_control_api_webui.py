"""U5 — gateway serves the built Web UI with the token injected (no network).

The gateway serves web/dist at / and injects window.__DASHBOARD_TOKEN__ into
index.html so a tailnet device needs no token prompt, while /api/* still enforces
the token. Tests point _web_dist_dir at a temp dir so they don't depend on a real
frontend build.
"""
import pytest
from fastapi.testclient import TestClient

from src.control import control_api
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


TOKEN = "test-webui-token"


class _StubOrchestrator:
    def __init__(self):
        self.session_service = SessionService(SessionStore())


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


def test_index_injects_token(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "window.__DASHBOARD_TOKEN__" in r.text
    assert TOKEN in r.text
    # Injected before the app body so the global exists at boot.
    assert r.text.index("__DASHBOARD_TOKEN__") < r.text.index("app")


def test_spa_fallback_returns_index(client):
    # An unknown client-side route returns the SPA index (not 404).
    r = client.get("/sessions/abc123")
    assert r.status_code == 200
    assert "__DASHBOARD_TOKEN__" in r.text


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
        assert "__DASHBOARD_TOKEN__" in r.text


def test_no_dist_skips_mount(monkeypatch, tmp_path):
    # When there is no built UI (dev), / is not served by the gateway.
    monkeypatch.setattr(control_api, "_web_dist_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    c = TestClient(control_api.build_control_api(_StubOrchestrator()))
    # No SPA catch-all mounted → unknown path is a 404, and /health still works.
    assert c.get("/health").status_code == 200
    assert c.get("/some/spa/route").status_code == 404
