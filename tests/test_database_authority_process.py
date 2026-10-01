"""[A88] Real process boundary: a task-server subprocess owns its own controller
mesh.db; the in-test "worker" has a different DB root and must observe controller
flag changes over HTTP without ever creating a mesh.db of its own.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

import src.control.db as db_mod
from src.control import controller_state
from src.control.db import MeshDB
from src.worker.agent import _HTTP
from src.worker.controller_state_client import RemoteControllerState

REPO = Path(__file__).resolve().parent.parent
TOKEN = "a88-process-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def controller(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = tmp_path / "controller"
    root.mkdir()
    db_path = root / "state" / "mesh.db"
    port = _free_port()
    env_file = root / "controller.env"
    env_file.write_text(
        "MESH_ENABLED=true\nMESH_EMBEDDED_SERVER=false\nGATEWAY_LOCAL_EXECUTION_ENABLED=false\n"
        f"MESH_TASK_SERVER_PORT={port}\nMESH_BIND_HOST=127.0.0.1\nMESH_TAILSCALE_IP=\n"
        f"MESH_DB_PATH={db_path}\nMESH_SHADOW_WRITE=true\nWORKER_TOKEN={TOKEN}\n"
        # Pin the flags asserted below so a developer .env cannot leak in.
        "QUOTA_PREWARM_ENABLED=0\nDURABLE_RELAY_ENABLED=0\nMANAGER_ROLE_ENABLED=0\n"
    )
    env = dict(os.environ, AI_TEAM_ENV_FILE=str(env_file), AI_TEAM_TEST_MODE="1")
    stderr_path = root / "server.stderr"
    stderr_file = stderr_path.open("wb")  # a file, not a pipe: a chatty child can't block
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "server_main.py")],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=stderr_file,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=1) as r:
                    json.loads(r.read())
                break
            except (OSError, ValueError):
                if proc.poll() is not None or time.monotonic() > deadline:
                    err = stderr_path.read_bytes().decode(errors="replace")[-2000:]
                    pytest.fail(f"task-server did not start: {err}")
                time.sleep(0.2)
        yield base, db_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stderr_file.close()


@pytest.fixture
def worker_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from config import config

    for name in ("QUOTA_PREWARM_ENABLED", "MANAGER_ROLE_ENABLED", "DURABLE_RELAY_ENABLED"):
        monkeypatch.delenv(name, raising=False)  # the worker's env fallback must not see a dev .env
    root = tmp_path / "worker"
    config.mesh.db_path = str(root / "state" / "mesh.db")
    db_mod._db_instance = None
    yield root
    controller_state.uninstall()


def test_worker_reads_controller_state_across_processes(controller: tuple[str, Path], worker_root: Path) -> None:
    base, controller_db_path = controller
    operator_db = MeshDB(str(controller_db_path))  # the operator's /api/flags write, on the controller DB
    try:
        remote = RemoteControllerState(_HTTP(base, TOKEN))
        controller_state.install(remote)

        assert remote.refresh() is True
        assert db_mod.runtime_flag_enabled("QUOTA_PREWARM_ENABLED") is False

        operator_db.set_runtime_flag("QUOTA_PREWARM_ENABLED", True)
        assert remote.refresh() is True  # one refresh interval later
        assert db_mod.runtime_flag_enabled("QUOTA_PREWARM_ENABLED") is True

        # Manager boot reconcile lands in the controller ledger, not the worker's.
        assert remote.boot_reconcile_case("case_missing") == {"ok": False, "reason": "unknown_case"}
        case_id = operator_db.open_case("objective", "sess_proc")
        operator_db.set_runtime_flag("DURABLE_RELAY_ENABLED", True)
        assert remote.boot_reconcile_case(case_id)["ok"] is True

        # Wrong credential is refused and does not clobber the last-known-good snapshot.
        intruder = RemoteControllerState(_HTTP(base, "wrong"))
        assert intruder.refresh() is False
        assert remote.runtime_flag_row("QUOTA_PREWARM_ENABLED") == {
            "flag_name": "QUOTA_PREWARM_ENABLED", "value": "1",
            "set_at": operator_db.get_runtime_flag("QUOTA_PREWARM_ENABLED")["set_at"],  # type: ignore[index]
        }

        assert db_mod.get_db() is None
        assert not worker_root.exists(), "the worker must never create a mesh.db"
    finally:
        operator_db.close()


def test_worker_keeps_last_known_good_when_controller_dies(controller: tuple[str, Path], worker_root: Path) -> None:
    base, controller_db_path = controller
    operator_db = MeshDB(str(controller_db_path))
    try:
        operator_db.set_runtime_flag("MANAGER_ROLE_ENABLED", True)
    finally:
        operator_db.close()
    remote = RemoteControllerState(_HTTP(base, TOKEN))
    controller_state.install(remote)
    assert remote.refresh() is True

    dead = RemoteControllerState(_HTTP("http://127.0.0.1:1", TOKEN))  # nothing listens
    assert dead.refresh() is False
    assert db_mod.runtime_flag_enabled("MANAGER_ROLE_ENABLED") is True
    assert not worker_root.exists()
