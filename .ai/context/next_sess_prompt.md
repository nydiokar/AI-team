# Handoff prompt — next session (Phase 9 Step C: real two-machine test)

## What this project is

A Telegram-controlled gateway for local coding agents (Claude Code, Codex, OpenCode).
User sends messages from phone → tasks execute on PC → results back to Telegram.

Long-term: control plane on VPS, worker nodes pull tasks from a central SQLite DB.
Full spec: `docs/AGENT_MESH_SPEC.md`.

**Read `.ai/CONTEXT.md` and `.ai/NEXT_TASKS.md` before doing anything.**

---

## Where things stand

Phase 9 Steps 1–3 + Step B are **done, tested, and live-validated**:

- `scripts/test_mesh_local.py` — 18/18
- `scripts/test_routing_integration.py` — 30/30
- **Live trial confirmed:** localhost two-process trial completed successfully.
  Full cycle: Telegram → gateway inserts pending row → trial-node worker claims
  → executes → posts result → gateway polls completion → Telegram reply delivered.

Bugs fixed during the session:
- Windows: `loop.add_signal_handler` not available → graceful fallback
- Gateway restart wipes in-memory NodeRegistry → added DB fallback for node liveness check
- `_mesh_enqueue_task` silent failure on remote tasks → escalated to `logger.error`
- `_dispatch_to_node` 600s spin on missing row → fast-fail on first poll

---

## Step C — Real two-machine test

This is purely operational — **no new code needed**. The routing is complete and proven.

### Prerequisites (user action, not code)

1. Second machine available (VPS, laptop, or another PC on same LAN)
2. Both machines on Tailscale OR same LAN (LAN is fine for first test)
3. Generate a permanent token: `openssl rand -hex 32`
4. Confirm network: from the worker machine, `curl http://{pc-ip}:9002/health` should
   get a response (not timeout). If timeout, check firewall on the PC.

### On the main PC — start the task server

```powershell
$env:WORKER_TOKEN="<your-token>"; python -m uvicorn src.control.task_server:app --host 0.0.0.0 --port 9002
```

Use `0.0.0.0` so it's reachable from the second machine (or bind to the Tailscale IP).

### On the second machine — start the worker

```bash
# Linux/Mac
WORKER_NODE_ID=remote-machine WORKER_TOKEN=<your-token> \
WORKER_TAILSCALE_IP=<second-machine-ip> WORKER_API_PORT=9001 \
CONTROLLER_URL=http://<pc-ip>:9002 WORKER_BACKENDS=claude \
python -m src.worker.agent
```

```powershell
# Windows PowerShell
$env:WORKER_NODE_ID="remote-machine"; $env:WORKER_TOKEN="<your-token>"; $env:WORKER_TAILSCALE_IP="<second-machine-ip>"; $env:WORKER_API_PORT="9001"; $env:CONTROLLER_URL="http://<pc-ip>:9002"; $env:WORKER_BACKENDS="claude"; python -m src.worker.agent
```

Watch Terminal 1 (task server) for `POST /nodes/register 200`.

### On the main PC — restart the gateway with mesh enabled

```powershell
$env:MESH_ENABLED="true"; $env:MESH_DB_PATH="state/mesh.db"; pm2 restart ai-team-gateway --update-env
```

Note: using `state/mesh.db` (production DB) now, not the trial DB.

### Pin a fresh session to the remote node

1. Create a new session from Telegram
2. Open `state/sessions/<session_id>.json`
3. Set `"machine_id": "remote-machine"` (matching WORKER_NODE_ID above)
4. The gateway reads this on the next message — no restart needed

### Send a message and verify

Send a Telegram message to that session. Watch:
- Task server Terminal 1: `POST /tasks/<id>/claim`, then `POST /tasks/<id>/result`
- Worker terminal: logs showing it picked up and executed the task
- Telegram: reply arrives

Check `state/sessions/<session_id>.json` — `backend_session_id` should be populated.
Send a second message — it should resume (not create fresh).

### Rollback

```powershell
pm2 restart ai-team-gateway --update-env
```
With `MESH_ENABLED` unset. Clear `machine_id` from the session JSON to return it to local.

---

## After Step C passes — VPS migration path

Per `docs/AGENT_MESH_SPEC.md` Phase 4:

1. Clone repo to VPS
2. Copy `state/` across (sessions, bindings, mesh.db)
3. Run `scripts/seed_db_from_json.py` on VPS to backfill any sessions not in DB
4. Write `scripts/fix_session_machine_ids.py` — updates existing sessions'
   `machine_id` to match the PC's `WORKER_NODE_ID` so they route correctly
5. Start gateway on VPS (control plane), worker on PC (execution)
6. Test Telegram → VPS → PC worker → result → Telegram
7. Stop gateway on PC

---

## Key files

| File | Why |
|------|-----|
| `src/orchestrator.py` | `process_task`, `_process_task_remote`, `_dispatch_to_node` |
| `src/worker/agent.py` | Worker daemon |
| `src/control/task_server.py` | Task server API |
| `src/control/node_registry.py` | Node registry (in-memory + DB fallback) |
| `ecosystem.config.js` | PM2 config — task server + worker entries (enable when ready) |
| `scripts/test_routing_integration.py` | 30/30 integration tests |
| `docs/AGENT_MESH_SPEC.md` | Full mesh spec |
