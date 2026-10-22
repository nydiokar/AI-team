"""[A62] `scripts/ops_flag.sh migrate` against a real Control API (in-process
uvicorn over the isolated test DB). Proves migrate never pins a null governor
knob — an explicit null row would silently override a cap set in .env later —
and that typed values still round-trip. No paid backend; needs bash + jq + curl.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

import src.control.db as db_mod
from src.control import control_api

REPO = Path(__file__).resolve().parents[1]
TOKEN = "a62-migrate-token"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "jq", "curl")), reason="needs bash, jq and curl"
)


@pytest.fixture
def gateway(monkeypatch):
    import uvicorn

    import config as _config_pkg

    for name in ("CLAUDE_SDK_MAX_TURNS", "CLAUDE_SDK_MAX_BUDGET_USD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_turns", None, raising=False)
    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_budget_usd", None, raising=False)
    monkeypatch.setattr(control_api, "_dashboard_token", lambda: TOKEN)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        control_api.build_control_api(type("_Orch", (), {})()),
        host="127.0.0.1", port=port, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _ops_flag(url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GATEWAY_URL": url, "DASHBOARD_TOKEN": TOKEN}
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "ops_flag.sh"), *args],
        env=env, capture_output=True, text=True, timeout=120,
    )


def test_migrate_leaves_null_governor_unpinned_and_round_trips_values(gateway, monkeypatch):
    # Env as seen by the (in-process) gateway: a budget cap, NO turn cap, a bool
    # from env; the str overlay stays at its "" default.
    monkeypatch.setenv("CLAUDE_SDK_MAX_BUDGET_USD", "2.5")
    monkeypatch.setenv("CASE_CONTINUATION_ENABLED", "1")

    run = _ops_flag(gateway, "migrate")
    assert run.returncode == 0, run.stdout + run.stderr

    db = db_mod.get_db()
    assert db.get_runtime_flag("CLAUDE_SDK_MAX_TURNS") is None  # null ⇒ never pinned
    assert "left unpinned" in run.stdout and "CLAUDE_SDK_MAX_TURNS" in run.stdout
    assert db.get_runtime_flag("CLAUDE_SDK_MAX_BUDGET_USD")["value"] == "2.5"
    assert db.get_runtime_flag("CASE_CONTINUATION_ENABLED")["value"] == "1"
    assert db.get_runtime_flag("TOOL_PROFILES_OVERLAY")["value"] == ""

    # The point of not pinning: a cap set in env later still takes effect.
    import config as _config_pkg
    monkeypatch.setattr(_config_pkg.config.claude, "sdk_max_turns", 30, raising=False)
    assert db_mod.sdk_governor_ceilings()["sdk_max_turns"] == 30


def test_migrate_skips_env_zero_cap_without_failing(gateway, monkeypatch):
    # Env keeps A53's "0 = no cap"; the registry refuses <= 0, so migrate must
    # leave it on env (not pin "no cap", not fail the run).
    monkeypatch.setenv("CLAUDE_SDK_MAX_TURNS", "0")

    run = _ops_flag(gateway, "migrate")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "SKIP  CLAUDE_SDK_MAX_TURNS" in run.stdout
    assert db_mod.get_db().get_runtime_flag("CLAUDE_SDK_MAX_TURNS") is None
