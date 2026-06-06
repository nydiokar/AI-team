# Handoff prompt — next session (Phase 9 Steps 1–3 complete)

## What this project is

A Telegram-controlled gateway for local coding agents (Claude Code, Codex, OpenCode).
The user sends messages from their phone, tasks execute on their PC, results come back to Telegram.

Long-term direction: move the control plane to a VPS, with worker nodes (PC, laptop, etc.)
pulling tasks from a central SQLite task DB. Full spec: `docs/AGENT_MESH_SPEC.md`.

Read `.ai/CONTEXT.md` for full current state before doing anything.

---

## What was just completed (Phase 9 Steps 1–3)

All Phase 9 Steps 1–3 are complete. The following files were built:

- `src/control/task_server.py` — FastAPI app, all 9 endpoints, Bearer auth, MeshDB-backed
- `src/control/node_registry.py` — in-memory NodeRegistry with heartbeat expiry, offline task failover, DB persistence
- `src/worker/__init__.py`, `src/worker/config.py`, `src/worker/agent.py` — full worker daemon
- `src/orchestrator.py` — `_run_backend_local`, `_dispatch_to_node`, `_dispatch_or_run_local` added
- `ecosystem.config.js` — PM2 entries for task server and worker (disabled by default)

`MESH_ENABLED=false` (default) → gateway unchanged.

---

## What to do next (Phase 9 Step 4 — local end-to-end test)

Test the full cycle on a single machine (no Tailscale needed):

```bash
# 1. Generate and set WORKER_TOKEN in .env
openssl rand -hex 32

# 2. Start task server
uvicorn src.control.task_server:app --host 127.0.0.1 --port 9002

# 3. Start worker daemon (separate terminal)
WORKER_NODE_ID=main-pc WORKER_TOKEN=<token> WORKER_TAILSCALE_IP=127.0.0.1 \
CONTROLLER_URL=http://127.0.0.1:9002 WORKER_BACKENDS=claude,opencode \
python -m src.worker.agent

# 4. Set MESH_ENABLED=true in .env, restart gateway
pm2 restart ai-team-gateway --update-env

# 5. Send a Telegram message — should route through DB → worker → result
```

After local testing passes: proceed to VPS migration (see AGENT_MESH_SPEC.md Phase 4).

---

## Important constraints

- **Do not require Tailscale or VPS for any of this.** Tasks 1-3 are fully testable
  on a single machine with `WORKER_TAILSCALE_IP=127.0.0.1` and `CONTROLLER_URL=http://127.0.0.1:9002`.
- **Do not change the gateway's current behaviour.** `MESH_ENABLED=false` must leave
  everything working exactly as it does today.
- **Do not add new DB tables or change the schema** unless strictly required.
  All needed methods already exist in `src/control/db.py`.
- **WSL is available** on this machine if a Linux environment is needed for testing FastAPI.
- The gateway runs under PM2 as `ai-team-gateway`. Restart with:
  `pm2 restart ai-team-gateway --update-env`

---

## Key files to read before starting

| File | Why |
|------|-----|
| `.ai/CONTEXT.md` | Full project state and architecture |
| `docs/AGENT_MESH_SPEC.md` | Full mesh spec — read Sections 5, 6, 7, 8, 10 |
| `src/control/db.py` | All DB methods available — understand before writing task server |
| `src/orchestrator.py` | Understand `_task_worker` and existing local execution path |
| `config/settings.py` | MeshConfig fields and env vars |
