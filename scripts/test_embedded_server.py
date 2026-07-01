"""
Smoke test for the embedded task server (D1).

Verifies that EmbeddedTaskServer starts uvicorn on the *current* asyncio event
loop, that the in-process NodeRegistry singleton is shared between the HTTP
handlers and direct get_registry() access (the whole point of D1), and that the
server stops cleanly.

Uses an isolated temp DB and token so it never touches state/mesh.db.

Run: python scripts/test_embedded_server.py
"""
import asyncio
import sys
import urllib.request
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._test_env import cleanup_test_environment, configure_test_environment

TEST_DB = configure_test_environment("embedded_server", worker_token="embed-test-token")

from config import config
from src.control.embedded_server import EmbeddedTaskServer
from src.control.node_registry import get_registry

FAILURES = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def _post(url, token, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read().decode())


async def main():
    print("=== Embedded task server smoke test ===\n")
    host = "127.0.0.1"
    # Ask the OS for a free ephemeral port to avoid clashing with a running
    # gateway or a stale uvicorn left over from a prior run.
    import socket as _socket
    _s = _socket.socket()
    _s.bind((host, 0))
    port = _s.getsockname()[1]
    _s.close()
    token = config.mesh.worker_token
    if not token:
        print("[SKIP] no WORKER_TOKEN configured; cannot exercise authed endpoints")
        return 0

    server = EmbeddedTaskServer(host=host, port=port)
    await server.start()
    base = f"http://{host}:{port}"

    try:
        # blocking urllib calls must run off-loop or they starve uvicorn,
        # which is serving on this same event loop.
        # 1. Server is up on this loop
        st, body = await asyncio.to_thread(_get, f"{base}/health", token)
        check("health returns ok", st == 200 and body.get("status") == "ok", str(body))

        # 2. Register a node over HTTP
        st, body = await asyncio.to_thread(_post, f"{base}/nodes/register", token, {
            "node_id": "embed-test-node",
            "tailscale_ip": "127.0.0.1",
            "api_port": 9001,
            "capabilities": {"backends": ["claude"], "max_concurrent": 1},
        })
        check("node registers over HTTP", st == 200 and body.get("status") == "registered", str(body))

        # 3. The key D1 guarantee: the SAME in-process registry singleton the
        #    orchestrator would call sees the node the HTTP handler registered.
        node = get_registry().get("embed-test-node")
        check("in-process registry shares HTTP-registered node (D1 core)",
              node is not None and node.status == "online",
              "registry singleton did NOT see the node")

        # 4. Heartbeat path
        st, body = await asyncio.to_thread(_post, f"{base}/nodes/heartbeat", token, {"node_id": "embed-test-node"})
        check("heartbeat accepted", st == 200, str(body))

        # 5. Deregister cleans up in-process state too
        st, body = await asyncio.to_thread(_post, f"{base}/nodes/deregister", token, {"node_id": "embed-test-node"})
        check("deregister over HTTP", st == 200, str(body))
        check("registry singleton drops node after deregister",
              get_registry().get("embed-test-node") is None)
    finally:
        await server.stop()

    # 6. Server stopped — connection should now refuse
    refused = False
    try:
        await asyncio.to_thread(_get, f"{base}/health", token)
    except Exception:
        refused = True
    check("server stops cleanly (port closed)", refused)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("All embedded-server checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    finally:
        cleanup_test_environment(TEST_DB)
