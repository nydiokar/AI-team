"""
Local smoke test for the mesh task server — exercises the full server-side
cycle in-process using FastAPI's TestClient: register -> enqueue -> poll ->
claim -> result -> verify DB state. No live worker or gateway required.

Run: python scripts/test_mesh_local.py
"""
import os
import sys
from pathlib import Path

# Use an isolated test DB so this never touches state/mesh.db
TEST_DB = Path(__file__).resolve().parent.parent / "state" / "test_mesh_local.db"
for ext in ("", "-wal", "-shm"):
    p = Path(str(TEST_DB) + ext)
    if p.exists():
        p.unlink()

os.environ["WORKER_TOKEN"] = "test-token-12345"
os.environ["MESH_DB_PATH"] = str(TEST_DB)
os.environ["MESH_SHADOW_WRITE"] = "true"
os.environ["MESH_ENABLED"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from src.control.task_server import app
from src.control.db import get_db

client = TestClient(app)
HEADERS = {"Authorization": "Bearer test-token-12345"}

def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)

FAILURES = []

print("=== Mesh local smoke test ===\n")

# 1. Health check (no auth)
r = client.get("/health")
check("health check returns 200", r.status_code == 200, r.text)

# 2. Auth rejection
r = client.get("/nodes")
check("unauthenticated request rejected", r.status_code in (401, 403), f"got {r.status_code}")

# 3. Register node
r = client.post("/nodes/register", headers=HEADERS, json={
    "node_id": "test-node",
    "tailscale_ip": "127.0.0.1",
    "api_port": 9001,
    "capabilities": {"backends": ["claude"], "max_concurrent": 2},
})
check("node registration succeeds", r.status_code == 200, r.text)

# 4. List nodes
r = client.get("/nodes", headers=HEADERS)
nodes = r.json()
check("registered node appears in list", any(n["node_id"] == "test-node" for n in nodes), str(nodes))

# 5. Heartbeat
r = client.post("/nodes/heartbeat", headers=HEADERS, json={"node_id": "test-node"})
check("heartbeat accepted for known node", r.status_code == 200, r.text)

r = client.post("/nodes/heartbeat", headers=HEADERS, json={"node_id": "ghost-node"})
check("heartbeat rejected for unknown node (404)", r.status_code == 404, f"got {r.status_code}")

# 6. Enqueue a fake task directly via DB (simulating _mesh_enqueue_task with
#    machine_id=None so it's pollable by any node — this is what a real
#    mesh-routed dispatch would look like)
db = get_db()
db.enqueue_task(
    task_id="task_smoketest01",
    session_id=None,
    machine_id=None,
    backend="claude",
    action="run_oneoff",
    payload={"prompt": "echo hello", "task_id": "task_smoketest01", "action": "run_oneoff", "metadata": {}},
)
row = db.get_task("task_smoketest01")
check("task enqueued as pending", row is not None and row["status"] == "pending", str(row))

# 7. Poll for pending tasks
r = client.get("/tasks/pending", headers=HEADERS, params={"node_id": "test-node", "backends": "claude"})
pending = r.json()
check("pending poll returns the task", any(t["id"] == "task_smoketest01" for t in pending), str(pending))
check("pending task payload deserialized to dict", isinstance(pending[0]["payload"], dict) if pending else False)

# 8. Claim the task
r = client.post("/tasks/task_smoketest01/claim", headers=HEADERS, json={"node_id": "test-node"})
check("claim succeeds", r.status_code == 200, r.text)
claimed_task = r.json().get("task", {})
check("claimed task has status=claimed", claimed_task.get("status") == "claimed", str(claimed_task))

# 9. Double-claim should fail (optimistic lock)
r = client.post("/tasks/task_smoketest01/claim", headers=HEADERS, json={"node_id": "other-node"})
check("double-claim rejected (409)", r.status_code == 409, f"got {r.status_code}")

# 10. Wrong-node result submission should be rejected (claim verification fix)
r = client.post("/tasks/task_smoketest01/result", headers=HEADERS, json={
    "node_id": "other-node",
    "success": True,
    "output": "fraudulent result",
    "execution_time": 1.0,
    "timestamp": "2026-06-06T00:00:00",
})
check("result from non-claiming node rejected (403)", r.status_code == 403, f"got {r.status_code}")

# 11. Correct-node result submission succeeds
r = client.post("/tasks/task_smoketest01/result", headers=HEADERS, json={
    "node_id": "test-node",
    "success": True,
    "output": "hello",
    "execution_time": 1.23,
    "timestamp": "2026-06-06T00:00:01",
    "return_code": 0,
})
check("result from claiming node accepted", r.status_code == 200, r.text)

row = db.get_task("task_smoketest01")
check("task marked completed in DB", row["status"] == "completed", str(row))

# 12. Pending poll no longer returns completed task
r = client.get("/tasks/pending", headers=HEADERS, params={"node_id": "test-node", "backends": "claude"})
pending2 = r.json()
check("completed task no longer pollable", not any(t["id"] == "task_smoketest01" for t in pending2), str(pending2))

# 13. Deregister
r = client.post("/nodes/deregister", headers=HEADERS, json={"node_id": "test-node"})
check("deregister succeeds", r.status_code == 200, r.text)
r = client.get("/nodes", headers=HEADERS)
nodes_after = r.json()
check("deregistered node removed from list", not any(n["node_id"] == "test-node" for n in nodes_after), str(nodes_after))

print()
if FAILURES:
    print(f"=== {len(FAILURES)} CHECK(S) FAILED: {FAILURES} ===")
    sys.exit(1)
else:
    print("=== All checks passed ===")

# Cleanup (best-effort — Windows may hold the file lock briefly after close)
db.close()
for ext in ("", "-wal", "-shm"):
    p = Path(str(TEST_DB) + ext)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
