#!/usr/bin/env python3
"""Read-only worker workload and caller ancestry; never grants restart permission."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psutil
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

ROOT: Path = Path(__file__).resolve().parents[1]


class ActiveTask(BaseModel):
    task_id: str
    backend: str = "unknown"
    action: str = "unknown"
    phase: str = "unknown"
    started_at: str | None = None


class LiveState(BaseModel):
    active_tasks: list[str]
    active_task_details: dict[str, ActiveTask] = Field(default_factory=dict)
    slots_used: int = Field(ge=0)
    slots_total: int | None = None
    live_sessions: int | None = None
    build_sha: str | None = None


class WorkerNode(BaseModel):
    node_id: str
    live: bool
    heartbeat_age_sec: float | None = None
    last_heartbeat: str | None = None
    incarnation_id: str | None = None
    live_state: LiveState | str | None = None


class NodeList(BaseModel):
    nodes: list[WorkerNode]


class PM2Environment(BaseModel):
    name: str
    pm_cwd: str
    pm_exec_path: str
    WORKER_NODE_ID: str | None = None


class PM2Process(BaseModel):
    pid: int
    pm2_env: PM2Environment


class LocalWorker(BaseModel):
    name: str
    pid: int
    cwd: str
    script: str
    hosts_current_agent: bool
    active_login_pids: list[int] | None


class WorkerContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checked_at: str
    node_id: str
    activity: str
    heartbeat_age_sec: float | None
    last_heartbeat: str | None
    incarnation_id: str | None
    build_sha: str | None
    slots_used: int | None
    slots_total: int | None
    pooled_sessions: int | None
    tasks: list[ActiveTask]
    local_workers: list[LocalWorker]


def gateway_url(env: dict[str, str]) -> str:
    if env.get("DASHBOARD_URL"):
        return env["DASHBOARD_URL"].rstrip("/")
    controller = urlsplit(env.get("CONTROLLER_URL", "http://127.0.0.1"))
    host = controller.hostname or "127.0.0.1"
    host = f"[{host}]" if ":" in host else host
    return urlunsplit((controller.scheme or "http", f"{host}:{env.get('DASHBOARD_PORT') or '9003'}", "", "", ""))


def local_workers(node_id: str) -> list[LocalWorker]:
    # `pm2 jlist` otherwise starts a new daemon on machines without PM2 running.
    pm2_home: Path = Path(os.environ.get("PM2_HOME", str(Path.home() / ".pm2")))
    try:
        daemon_pid: int = int((pm2_home / "pm2.pid").read_text().strip())
    except (OSError, ValueError):
        return []
    if not psutil.pid_exists(daemon_pid):
        return []
    # PM2's full environment contains credentials; select fields before output.
    clean_env: dict[str, str] = {k: v for k, v in os.environ.items() if k not in ("NODE_CHANNEL_FD", "NODE_CHANNEL_SERIALIZATION_MODE")}
    result = subprocess.run(["pm2", "jlist"], capture_output=True, timeout=8, env=clean_env, check=True)
    parents: set[int] = {p.pid for p in psutil.Process().parents()}
    workers: list[LocalWorker] = []
    for raw in json.loads(result.stdout):
        process = PM2Process.model_validate(raw)
        item = process.pm2_env
        if item.WORKER_NODE_ID != node_id or process.pid <= 0:
            continue
        login_pids: list[int] | None = []
        try:
            for child in psutil.Process(process.pid).children(recursive=True):
                argv = child.cmdline()
                if argv and Path(argv[0]).name == "claude" and argv[1:3] == ["auth", "login"]:
                    login_pids.append(child.pid)
        except (psutil.Error, OSError):
            login_pids = None
        workers.append(LocalWorker(name=item.name, pid=process.pid, cwd=item.pm_cwd,
                                   script=item.pm_exec_path, hosts_current_agent=process.pid in parents,
                                   active_login_pids=login_pids))
    return workers


def summarize(node: WorkerNode, workers: list[LocalWorker]) -> WorkerContext:
    state: LiveState | None = node.live_state if isinstance(node.live_state, LiveState) else None
    if isinstance(node.live_state, str):
        state = LiveState.model_validate_json(node.live_state)
    fresh: bool = node.live and node.heartbeat_age_sec is not None and 0 <= node.heartbeat_age_sec < 60
    activity: str = "unknown"
    tasks: list[ActiveTask] = []
    if state is not None:
        ids: list[str] = list(dict.fromkeys([*state.active_tasks, *state.active_task_details]))
        tasks = [state.active_task_details.get(task_id, ActiveTask(task_id=task_id)) for task_id in ids]
        if fresh:
            activity = "busy" if tasks or state.slots_used else "idle"
    return WorkerContext(
        checked_at=datetime.now(UTC).isoformat(), node_id=node.node_id, activity=activity,
        heartbeat_age_sec=node.heartbeat_age_sec, last_heartbeat=node.last_heartbeat,
        incarnation_id=node.incarnation_id, build_sha=state.build_sha if state else None,
        slots_used=state.slots_used if state else None, slots_total=state.slots_total if state else None,
        pooled_sessions=state.live_sessions if state else None, tasks=tasks, local_workers=workers,
    )


def snapshot(node_id: str) -> WorkerContext:
    env: dict[str, str] = {k: v for k, v in dotenv_values(os.environ.get("AI_TEAM_ENV_FILE", str(ROOT / ".env"))).items() if v is not None}
    env.update(os.environ)
    token: str = env.get("DASHBOARD_TOKEN", "")
    if not token:
        raise RuntimeError("dashboard_token_unavailable")
    request = urllib.request.Request(f"{gateway_url(env)}/api/nodes", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=8) as response:
        raw: bytes = response.read(1_048_577)
    if len(raw) > 1_048_576:
        raise RuntimeError("node_response_too_large")
    nodes = NodeList.model_validate_json(raw).nodes
    node = next((n for n in nodes if n.node_id == node_id), None)
    if node is None:
        raise RuntimeError("node_not_found")
    return summarize(node, local_workers(node_id))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("node_id")
    args = parser.parse_args()
    try:
        context = snapshot(args.node_id)
        print(context.model_dump_json(indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, psutil.Error) as exc:
        # Validation/HTTP exceptions can contain raw payloads or URLs. Do not print them.
        print(json.dumps({"node_id": args.node_id, "activity": "unknown", "error": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
