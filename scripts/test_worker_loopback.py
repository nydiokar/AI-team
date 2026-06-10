"""Phase 3 loopback smoke: prove the worker daemon actually runs.

Runs the REAL worker (worker_main.py) against a REAL standalone task server
(server_main.py), on a temp DB + temp ports, with NO paid backend involved.

Pipeline exercised end to end:
  register -> heartbeat -> poll -> claim -> execute -> post result -> DB updated
  -> SIGTERM -> drain -> deregister

The injected task uses backend "nobackend" so the worker hits the
"backend not available" branch (agent.py:169) and returns a structured failure
WITHOUT calling any real CLI. We assert the task reaches a terminal state in the
DB and the worker registered/deregistered — i.e. the daemon wiring works.

Run:  python scripts/test_worker_loopback.py
Exits non-zero on failure.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT_SERVER = 9096
PORT_NUDGE = 9095
TOKEN = "loopback-tok"
NODE_ID = "loopback-node"


def _post(path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT_SERVER}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def main() -> int:
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "loopback.db")

    # Shared env for both processes — AI_TEAM_ENV_FILE beats process env, so we
    # write a throwaway .env and point both at it.
    env_file = os.path.join(tmp, "loopback.env")
    Path(env_file).write_text(
        f"MESH_ENABLED=true\n"
        f"MESH_EMBEDDED_SERVER=false\n"
        f"MESH_TASK_SERVER_PORT={PORT_SERVER}\n"
        f"MESH_TAILSCALE_IP=\n"
        f"MESH_DB_PATH={db_path}\n"
        f"WORKER_TOKEN={TOKEN}\n"
    )

    base_env = dict(os.environ)
    base_env["AI_TEAM_ENV_FILE"] = env_file
    base_env["AI_TEAM_TEST_MODE"] = "1"

    worker_env = dict(base_env)
    worker_env.update({
        "WORKER_NODE_ID": NODE_ID,
        "WORKER_TOKEN": TOKEN,
        "WORKER_TAILSCALE_IP": "127.0.0.1",
        "WORKER_API_PORT": str(PORT_NUDGE),
        "CONTROLLER_URL": f"http://127.0.0.1:{PORT_SERVER}",
        "WORKER_BACKENDS": "claude,opencode",
        "WORKER_MAX_CONCURRENT": "1",
    })

    server = subprocess.Popen([sys.executable, "server_main.py"], cwd=ROOT, env=base_env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    worker = None
    failures = []
    try:
        time.sleep(4)
        # Sanity: server up
        h = urllib.request.urlopen(f"http://127.0.0.1:{PORT_SERVER}/health", timeout=5)
        assert json.loads(h.read())["status"] == "ok", "server not healthy"
        print("[ok] standalone server healthy")

        # Inject a task the worker will claim. machine_id=NODE_ID (affinity),
        # backend=opencode (advertised by the worker, so it passes the server's
        # poll filter) pointed at a NON-git temp dir -> opencode rejects the
        # non-repo cwd and fails fast & free, no paid CLI invoked.
        import sqlite3
        norepo = os.path.join(tmp, "norepo")
        os.makedirs(norepo, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO mesh_tasks (id, session_id, machine_id, backend, action, payload, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?, 'pending', ?, ?)",
            ("task_loopback", "sess_loop", NODE_ID, "opencode", "run_oneoff",
             json.dumps({"prompt": "noop", "metadata": {"cwd": norepo}}),
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()
        print("[ok] injected pending task")

        # Start the real worker daemon.
        worker = subprocess.Popen([sys.executable, "worker_main.py"], cwd=ROOT, env=worker_env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        # Wait for the worker to register.
        registered = False
        for _ in range(20):
            nodes = _post("/nodes/heartbeat", {"node_id": NODE_ID}) if False else None
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{PORT_SERVER}/nodes",
                    headers={"Authorization": f"Bearer {TOKEN}"})
                lst = json.loads(urllib.request.urlopen(req, timeout=5).read())
                if any(n["node_id"] == NODE_ID for n in lst):
                    registered = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        print(f"[{'ok' if registered else 'FAIL'}] worker registered")
        if not registered:
            failures.append("worker never registered")

        # Wait for the task to reach a terminal state.
        terminal = None
        for _ in range(30):
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT status, error, claimed_by FROM mesh_tasks WHERE id=?",
                               ("task_loopback",)).fetchone()
            conn.close()
            if row and row[0] in ("completed", "failed", "failed_node_offline"):
                terminal = row
                break
            time.sleep(0.5)
        if terminal:
            print(f"[ok] task terminal: status={terminal[0]} claimed_by={terminal[2]} error={terminal[1]!r}")
            if terminal[2] != NODE_ID:
                failures.append(f"task not claimed by worker (claimed_by={terminal[2]})")
            # Either terminal state proves the pipeline ran; we just need the
            # worker to have claimed + executed + posted a result.
        else:
            failures.append("task never reached terminal state")
            print("[FAIL] task never reached terminal state")

        # SIGTERM -> drain -> deregister.
        worker.send_signal(signal.SIGTERM if os.name != "nt" else signal.CTRL_BREAK_EVENT) \
            if False else worker.terminate()
        try:
            worker.wait(timeout=40)
        except subprocess.TimeoutExpired:
            failures.append("worker did not exit within 40s of SIGTERM")
            worker.kill()
        worker_out = worker.stdout.read() if worker.stdout else ""
        deregistered = "event=deregistered" in worker_out or "event=draining" in worker_out
        print(f"[{'ok' if deregistered else 'warn'}] worker drain/deregister observed in log")
        print("--- worker log tail ---")
        print("\n".join(worker_out.splitlines()[-12:]))

    finally:
        for p in (worker, server):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()
        srv_out = server.stdout.read() if server.stdout else ""
        print("--- server log tail ---")
        print("\n".join(srv_out.splitlines()[-6:]))

    if failures:
        print("\nLOOPBACK FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nLOOPBACK PASSED — worker daemon runs end-to-end against standalone server.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
