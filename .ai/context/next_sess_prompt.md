# Handoff prompt — next session (Phase 9 Step C: live two-machine test)

## What this project is

A Telegram-controlled gateway for local coding agents (Claude Code, Codex, OpenCode).
The user sends messages from their phone, tasks execute on their PC, results come back to Telegram.

Long-term direction: control plane on VPS, worker nodes (PC, laptop, etc.) pull tasks from
a central SQLite task DB. Full spec: `docs/AGENT_MESH_SPEC.md`.

**Read `.ai/CONTEXT.md` (Phase 9 sections) and `.ai/NEXT_TASKS.md` before doing anything.**

---

## Where things stand (all smoke-tested, working tree clean after Step B commit)

Phase 9 Steps 1–3 + Step B are **done, adversarially reviewed, and tested**:

- `scripts/test_mesh_local.py` — 18/18 (task server API cycle)
- `scripts/test_routing_integration.py` — 24/24 (routing wiring: process_task → remote dispatch)

**What Step B delivered:**
- `process_task` now routes tasks with `session.machine_id` set to the pinned remote node
  when `MESH_ENABLED=true`. Zero behavior change for sessions without `machine_id`.
- `_process_task_remote`: full bookkeeping (BUSY status, heartbeats, error classification,
  session status update). Fails loudly if node offline — no silent local fallback (affinity is hard).
- `_mesh_enqueue_task`: skips self-claim for machine-pinned tasks, leaving row `pending` for the worker.
- `backend_session_id` propagated end-to-end: worker result → task_server → DB → gateway → session record.
- Session never stuck as BUSY on unexpected dispatch exception.

---

## Your job this session: Phase 9 Step C — live two-machine test

The code is complete. This session is about **validation with real processes**, not new code.
Work through these in order — each step is a gate for the next.

### Step A — Shadow trial (single machine, two processes, isolated DB)

Proves the daemon lifecycle works without touching production state.

```bash
# Terminal 1 — trial task server (separate port + DB)
WORKER_TOKEN=<token> MESH_DB_PATH=state/mesh_trial.db \
uvicorn src.control.task_server:app --host 127.0.0.1 --port 9099

# Terminal 2 — trial worker daemon pointed at it
WORKER_NODE_ID=trial-node WORKER_TOKEN=<token> WORKER_TAILSCALE_IP=127.0.0.1 \
WORKER_API_PORT=9099 CONTROLLER_URL=http://127.0.0.1:9099 WORKER_BACKENDS=claude \
python -m src.worker.agent
```

Watch: register → heartbeat every 30s → poll `/tasks/pending` (empty). Stop with Ctrl+C,
confirm clean deregistration in logs. No tasks will route here — the live gateway is still
running with `MESH_ENABLED=false`.

### Step B — Single-session live trial (MESH_ENABLED=true, pinned session)

This is the first real end-to-end routing test. Use a **non-critical repo** and a **new session**.

1. Generate a worker token: `openssl rand -hex 32` → set as `WORKER_TOKEN` in both processes.
2. Start the trial task server on a separate port (e.g. 9099) with `MESH_DB_PATH=state/mesh_trial.db`.
3. Start the worker daemon pointed at it, with `WORKER_NODE_ID=trial-node`.
4. Create a new gateway session from Telegram, note its `session_id`.
5. Manually set `machine_id = "trial-node"` on that session's JSON file
   (`state/sessions/<session_id>.json`) and restart the gateway (`pm2 restart ai-team-gateway --update-env`)
   with `MESH_ENABLED=true MESH_DB_PATH=state/mesh_trial.db`.
6. Send a message from Telegram to that session and watch it route:
   - Gateway inserts pending row into `mesh_trial.db` (not self-claimed, since `machine_id` is set)
   - Worker polls, claims, executes locally, posts result
   - Gateway's `_dispatch_to_node` poll returns the result
   - Telegram gets the reply
7. Verify `session.backend_session_id` is updated (check the JSON file).
8. Send a second message — it should resume the backend session (not create a new one).

**Rollback:** set `MESH_ENABLED=false` and restart the gateway. The session's `machine_id` can be
cleared to return it to local execution. No data loss.

### Step C — Real two-machine test (after Step B passes)

Before starting:
- Both machines enrolled in Tailscale (or same LAN is fine for initial test)
- Record both IPs; confirm connectivity: `curl http://{worker-ip}:9001/health` (connection refused = good, timeout = bad ACL)
- Generate `WORKER_TOKEN`: `openssl rand -hex 32`
- Set Tailscale ACL: worker port 9001 reachable from gateway machine; gateway's task server port 9002 reachable from worker

Then:
1. Start the task server on the gateway machine: `uvicorn src.control.task_server:app --host {tailscale-ip} --port 9002`
2. Start the worker daemon on the second machine: `WORKER_NODE_ID=<machine-name> CONTROLLER_URL=http://{gateway-tailscale-ip}:9002 ...`
3. Pin a test session to `machine_id = <machine-name>` and send a Telegram message.
4. Watch it route: DB → worker on second machine → result → Telegram reply.

---

## What NOT to do this session

- **Do not change any production code.** The routing is complete. If you find a bug, fix it — but
  no new features, no refactors, no schema changes.
- **Do not run the trial with MESH_ENABLED=true pointing at state/mesh.db** — always use an
  isolated trial DB (`mesh_trial.db`) until you've confirmed the trial works.
- **Do not pin an existing active session to a remote node** without understanding that its
  `backend_session_id` is machine-local — the remote worker will try to resume a session that
  doesn't exist on the remote machine (it will get a "missing conversation" error and create a
  new session instead). Use a fresh session for the trial.

---

## Key files

| File | Why |
|------|-----|
| `.ai/CONTEXT.md` | Full Phase 9 build history including Step B details |
| `.ai/NEXT_TASKS.md` | Rollout sequence |
| `src/orchestrator.py` | `process_task`, `_process_task_remote`, `_dispatch_to_node`, `_mesh_enqueue_task` |
| `src/worker/agent.py` | Worker daemon — what it does when it claims a task |
| `src/control/task_server.py` | Task server API |
| `scripts/test_mesh_local.py` | Smoke test (18/18) |
| `scripts/test_routing_integration.py` | Routing integration test (24/24) |
| `docs/AGENT_MESH_SPEC.md` | Full mesh spec |
| `ecosystem.config.js` | PM2 config — task server and worker entries (disabled by default) |
