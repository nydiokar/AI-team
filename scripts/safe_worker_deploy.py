#!/usr/bin/env python3
"""Safely promote a new worker checkout through a PM2 canary.

Run this on the worker machine from the AI-team repo. The active worker is not
stopped until a canary worker has started from the current checkout and the
controller reports it online.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    env_file = os.environ.get("AI_TEAM_ENV_FILE") or str(ROOT / ".env")
    os.environ["AI_TEAM_ENV_FILE"] = env_file
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    except Exception:
        pass


def _run(cmd: List[str], *, env: Dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd))
    cp = subprocess.run(cmd, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if cp.stdout:
        print(cp.stdout.rstrip())
    if check and cp.returncode != 0:
        raise RuntimeError(f"command failed ({cp.returncode}): {' '.join(cmd)}")
    return cp


def _required_env(names: List[str]) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing required env: {', '.join(missing)}")


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['WORKER_TOKEN']}",
        "Content-Type": "application/json",
    }


def _http_get(path: str, params: Dict[str, str] | None = None, timeout: int = 10) -> Any:
    base = os.environ["CONTROLLER_URL"].rstrip("/")
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _http_post(path: str, body: Dict[str, Any], timeout: int = 10) -> Any:
    base = os.environ["CONTROLLER_URL"].rstrip("/")
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode(),
        headers=_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _node_row(node_id: str) -> Dict[str, Any] | None:
    try:
        nodes = _http_get("/nodes")
    except Exception:
        return None
    for row in nodes or []:
        if row.get("node_id") == node_id:
            return row
    return None


def _check_tools(backends: List[str]) -> None:
    problems: List[str] = []
    if "codex" in backends:
        node_hint = os.environ.get("CODEX_NODE_PATH") or os.environ.get("NODE_EXE")
        path_value = os.environ.get("PATH") or os.environ.get("Path") or ""
        if node_hint:
            node_ok = Path(node_hint).exists()
        else:
            node_ok = shutil.which("node", path=path_value) is not None
        if not node_ok:
            problems.append("codex requested but node.exe/node is not available; set CODEX_NODE_PATH")
        if shutil.which("codex", path=path_value) is None:
            problems.append("codex requested but codex executable is not on PATH")
    if "claude" in backends and shutil.which("claude") is None:
        problems.append("claude requested but claude executable is not on PATH")
    if "opencode" in backends and shutil.which("opencode") is None:
        problems.append("opencode requested but opencode executable is not on PATH")
    if "opencode-server" in backends and shutil.which("opencode") is None:
        problems.append("opencode-server requested but opencode executable is not on PATH")
    if problems:
        raise RuntimeError("; ".join(problems))


def _preflight(backends: List[str]) -> None:
    _required_env(["WORKER_NODE_ID", "WORKER_TOKEN", "WORKER_TAILSCALE_IP", "CONTROLLER_URL", "WORKER_BACKENDS"])
    _run([sys.executable, "-m", "py_compile", "worker_main.py", "src/worker/agent.py", "src/worker/config.py"])
    _run([sys.executable, "-m", "py_compile", "src/backends/codex.py", "src/backends/claude_code.py", "src/backends/opencode.py"])
    _http_get("/health")
    _check_tools(backends)


def _pm2_delete(app: str) -> None:
    _run(["pm2", "delete", app], check=False)


def _start_canary(app: str, node_id: str, port: int) -> None:
    env = os.environ.copy()
    env.update(
        {
            "WORKER_NODE_ID": node_id,
            "WORKER_API_PORT": str(port),
            "WORKER_BACKENDS": "",
            "WORKER_MAX_CONCURRENT": "0",
            "WORKER_CANARY": "true",
            "PYTHONUNBUFFERED": "1",
        }
    )
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    _pm2_delete(app)
    _run(
        [
            "pm2",
            "start",
            "worker_main.py",
            "--name",
            app,
            "--interpreter",
            sys.executable,
            "--time",
            "--output",
            str(log_dir / "pm2-worker-canary-out.log"),
            "--error",
            str(log_dir / "pm2-worker-canary-error.log"),
            "--merge-logs",
        ],
        env=env,
    )


def _wait_for_node(node_id: str, timeout_sec: int) -> Dict[str, Any]:
    deadline = time.time() + timeout_sec
    last: Dict[str, Any] | None = None
    while time.time() < deadline:
        row = _node_row(node_id)
        if row:
            last = row
            live = row.get("live_state") or {}
            if row.get("status") == "online" and isinstance(live, dict) and live.get("canary") is True:
                return row
        time.sleep(2)
    raise RuntimeError(f"canary {node_id!r} did not become healthy; last={last}")


def _promote(real_app: str, canary_app: str, canary_node: str, timeout_sec: int) -> None:
    _run(["pm2", "restart", real_app, "--update-env"])
    real_node = os.environ["WORKER_NODE_ID"]
    deadline = time.time() + timeout_sec
    last = None
    while time.time() < deadline:
        row = _node_row(real_node)
        if row:
            last = row
            if row.get("status") == "online":
                _pm2_delete(canary_app)
                try:
                    _http_post("/nodes/deregister", {"node_id": canary_node}, timeout=5)
                except Exception:
                    pass
                print(f"promoted {real_app}; {real_node} is online")
                return
        time.sleep(2)
    raise RuntimeError(f"real worker did not return online after promotion; last={last}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely deploy a worker through a PM2 canary")
    parser.add_argument("--real-app", default="ai-team-worker")
    parser.add_argument("--canary-app", default="ai-team-worker-canary")
    parser.add_argument("--canary-suffix", default="-canary")
    parser.add_argument("--canary-port-offset", type=int, default=100)
    parser.add_argument("--health-timeout", type=int, default=90)
    parser.add_argument("--no-promote", action="store_true", help="start and verify canary, then leave real worker untouched")
    args = parser.parse_args()

    _load_env()
    backends = [b.strip() for b in os.environ.get("WORKER_BACKENDS", "").split(",") if b.strip()]
    _preflight(backends)

    real_node = os.environ["WORKER_NODE_ID"]
    canary_node = f"{real_node}{args.canary_suffix}"
    canary_port = int(os.environ.get("WORKER_API_PORT") or 9001) + args.canary_port_offset

    try:
        _start_canary(args.canary_app, canary_node, canary_port)
        row = _wait_for_node(canary_node, args.health_timeout)
        print(f"canary healthy: {row.get('node_id')} live_state={row.get('live_state')}")
        if args.no_promote:
            print("no-promote requested; leaving real worker untouched")
            return 0
        _promote(args.real_app, args.canary_app, canary_node, args.health_timeout)
        return 0
    except Exception as exc:
        print(f"SAFE DEPLOY FAILED: {exc}", file=sys.stderr)
        _pm2_delete(args.canary_app)
        try:
            _http_post("/nodes/deregister", {"node_id": canary_node}, timeout=5)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
