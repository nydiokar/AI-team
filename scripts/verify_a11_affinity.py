#!/usr/bin/env python3
"""
End-to-end A11 session-affinity smoke test.

Proves that a session pinned to a remote node (Horse) is actually executed
on that node — not silently run locally — after the A11 fix
(src/orchestrator.py: affinity_unrouted guard, commit f434a6a).

Pulls WORKER_TOKEN/DASHBOARD_TOKEN from the app's own config loader (reads
.env internally, value never printed). Talks to the real control API on
DASHBOARD_PORT (default 9003) bound at MESH_TAILSCALE_IP, submits one turn
with a unique sentinel, polls to a terminal state, then cross-checks
state/mesh.db and the gateway pm2 log for the affinity signals.

Usage:
    python scripts/verify_a11_affinity.py --node Horse --repo-path <path_on_horse>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config  # noqa: E402


def api_base() -> str:
    # Dashboard API (9003) binds 127.0.0.1; only the mesh task server (9002)
    # binds the Tailscale IP.
    return f"http://127.0.0.1:{config.mesh.dashboard_port}"


def token() -> str:
    t = config.mesh.dashboard_token or config.mesh.worker_token
    if not t:
        print("FAIL: no DASHBOARD_TOKEN/WORKER_TOKEN configured", file=sys.stderr)
        sys.exit(1)
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="Horse")
    ap.add_argument("--repo-path", required=True)
    ap.add_argument("--gateway-proc", default="ai-team-gateway")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    base = api_base()
    hdrs = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    sentinel = f"PROMPT_SECRET_LLMOBS_GWMESH_{uuid.uuid4().hex[:12]}"

    print(f"=== A11 AFFINITY VERIFY (node={args.node}) ===")

    # 1. Mesh health
    health_url = f"http://{config.mesh.tailscale_ip or '127.0.0.1'}:{config.mesh.task_server_port}/health"
    r = requests.get(health_url, timeout=5)
    r.raise_for_status()
    h = r.json()
    nodes_online = h.get("db", {}).get("nodes_online", 0)
    print(f"mesh health: nodes_online={nodes_online} degraded={h.get('mesh_health', {}).get('degraded')}")
    if nodes_online < 2:
        print("FAIL: fewer than 2 nodes online")
        sys.exit(1)

    # 2. Create session pinned to remote node
    r = requests.post(f"{base}/api/sessions", headers=hdrs, json={
        "backend": "codex", "node_id": args.node, "repo_path": args.repo_path,
    }, timeout=15)
    r.raise_for_status()
    sess = r.json()
    session_id = sess["session"]["session_id"] if "session" in sess else sess.get("session_id")
    print(f"session created: session_id={session_id}")

    # 3. Submit one turn with sentinel
    r = requests.post(f"{base}/api/instructions", headers=hdrs, json={
        "session_id": session_id,
        "description": f"Reply with only: GWMESH_CODEX_SMOKE {sentinel}",
    }, timeout=60)
    r.raise_for_status()
    task_id = r.json()["task_id"]
    print(f"task submitted: task_id={task_id}")

    # 4. Poll to terminal
    deadline = time.time() + args.timeout
    final_status = None
    while time.time() < deadline:
        r = requests.get(f"{base}/api/tasks", headers=hdrs, params={"limit": 200}, timeout=15)
        r.raise_for_status()
        tasks = {t["id"]: t for t in r.json().get("tasks", [])}
        t = tasks.get(task_id)
        if t and t.get("status") in ("completed", "failed"):
            final_status = t["status"]
            break
        time.sleep(3)
    print(f"terminal task status: {final_status}")

    # 5. Gate query against mesh.db
    db_path = config.mesh.db_path if hasattr(config.mesh, "db_path") else "state/mesh.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    turn_row = conn.execute(
        "SELECT turn_id, gateway_node_id, execution_node_id, final_status "
        "FROM llm_turns WHERE task_id = ? ORDER BY rowid DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    print(f"llm_turns row: {dict(turn_row) if turn_row else None}")

    invocation_nodes = []
    if turn_row:
        rows = conn.execute(
            "SELECT node_id FROM llm_invocations WHERE turn_id = ?", (turn_row["turn_id"],)
        ).fetchall()
        invocation_nodes = [row["node_id"] for row in rows]
    print(f"llm_invocations.node_id: {invocation_nodes}")

    dump_hits = sum(
        line.count(sentinel)
        for line in subprocess.run(
            ["sqlite3", db_path, ".dump"], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    )
    print(f"sentinel .dump hits: {dump_hits}")
    conn.close()

    # 6. Gateway log check
    log_out = subprocess.run(
        ["pm2", "logs", args.gateway_proc, "--nostream", "--lines", "500"],
        capture_output=True, text=True,
    ).stdout
    relevant = [
        line for line in log_out.splitlines()
        if task_id in line or "affinity_unrouted" in line or "_process_task_remote" in line or "codex_started" in line
    ]
    print("log lines:")
    for line in relevant[-20:]:
        print(f"  {line}")
    affinity_unrouted = any("affinity_unrouted" in line and task_id in line for line in relevant)

    # 7. Close session
    r = requests.post(f"{base}/api/sessions/{session_id}/close", headers=hdrs, timeout=15)
    print(f"close response: {r.status_code} {r.text}")

    # Verdict
    print("\n=== VERDICT ===")
    ok = True
    if not turn_row:
        print("FAIL: no llm_turns row found for task_id"); ok = False
    else:
        if turn_row["gateway_node_id"] == turn_row["execution_node_id"]:
            print(f"FAIL: gateway_node_id == execution_node_id ({turn_row['gateway_node_id']}) — not routed remotely"); ok = False
        if args.node not in invocation_nodes:
            print(f"FAIL: {args.node} not in llm_invocations.node_id {invocation_nodes}"); ok = False
    if affinity_unrouted:
        print("FAIL: affinity_unrouted logged for this task_id — node unreachable for remote dispatch"); ok = False
    if dump_hits == 0:
        print("WARN: sentinel not found in db dump at all — turn may not have completed/persisted")
    print("PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
