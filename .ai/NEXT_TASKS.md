# Next Tasks

**Current priority:** Phase 9 Step D — D1 (process consolidation) COMPLETE.
Next up: D2 (worker execution logging). Then D3 (/nodes), D4 (status/session UX).

---

## ✅ Completed

- Steps 1–3: Task server, worker daemon, orchestrator routing — done, adversarially reviewed, tested
- Step B: `_dispatch_or_run_local` wired into `process_task` — done, live tested
- Step C: Real two-machine test — **DONE** (2026-06-07)
  - LP-1 worker registers via Tailscale, claims tasks, executes, returns results
  - Node picker in Telegram: backend → node → repo buttons, fully DB-backed (cross-process)
  - `/session_new claude LP-1 AI-team` text command fallback works
  - Worker advertises `projects_root` + `repos` at register time; stored in DB (migration v2/v3)
  - `_mesh_online_nodes()` reads shared DB — works across gateway and task server processes
  - Routing fix: `route_remote` only true when `session.machine_id != local hostname`
  - FastAPI `on_event` → `lifespan` (deprecation fix)
  - Worker loads `.env` via dotenv on startup
  - `WORKER_PROJECTS_ROOT` env var wired end-to-end

---

## Step D — Next (new session starts here)

### D1. Process consolidation — ✅ DONE (2026-06-07)
The task server now runs **embedded inside the gateway process**, on the gateway's
own asyncio event loop (not a separate thread — same loop so the registry's
expiry task and the orchestrator share one loop).

**What was built:**
- `src/control/embedded_server.py` — `EmbeddedTaskServer`: runs `uvicorn.Server.serve()`
  as an asyncio task on the current loop. Suppresses uvicorn's own signal handlers
  (gateway owns SIGINT/SIGTERM). Waits up to ~5s for `started`, surfaces early-exit
  exceptions. Clean `stop()` via `should_exit` + bounded await with cancel fallback.
- `src/orchestrator.py` — `_embedded_task_server` field; `_start_embedded_task_server()`
  (no-op unless `MESH_ENABLED`; binds `MESH_TAILSCALE_IP or 127.0.0.1` : `MESH_TASK_SERVER_PORT`;
  failure logs loudly but doesn't crash the gateway) and `_stop_embedded_task_server()`.
  Wired into `start()` (after workers spawn) and `stop()` (before worker cancel).
- `ecosystem.config.js` — removed the `ai-team-task-server` PM2 entry; replaced with
  a note. One process now: `ai-team-gateway` hosts the task server when mesh is on.
- `scripts/test_embedded_server.py` — 7/7 checks pass. Core check proven: a node
  registered over HTTP is immediately visible via the in-process `get_registry()`
  singleton — the cross-process gap that forced the DB-only workaround is closed.

**Result:** the gateway's `get_registry()` is no longer always empty. The DB
fallback in `_process_task_remote` stays as a safety net (survives gateway restart
before worker re-registration) but is no longer the primary discovery path.

**⚠ Operator action before flipping `MESH_ENABLED=true` on this PC:** the live
`.env` has `MESH_TAILSCALE_IP` set to a literal comment string (dotenv parsed
`"# this PC's Tailscale IP..."` as the value) and `MESH_TASK_SERVER_PORT=9002`.
Set `MESH_TASK_SERVER_PORT` and a real `MESH_TAILSCALE_IP` (or leave it blank to
bind 127.0.0.1) before enabling. Note: dotenv loads with `override=True`, so `.env`
beats process env vars — `scripts/test_mesh_local.py` currently fails for this
reason (its hardcoded test token loses to the real `.env` WORKER_TOKEN), unrelated
to D1.

### D2. Worker execution logging — HIGH PRIORITY  ← start here next
Currently when a task fails you see `success=False elapsed=0.0s` and nothing else. The error string from the backend exception never surfaces in Telegram or logs.

**What to build:**
- Worker: catch exception in `_execute_task`, include full traceback in `errors` list of the result dict
- Gateway: when `_process_task_remote` receives a failed result, log `errors` and include first error line in the Telegram failure message (currently shows nothing useful)
- Worker logs: add `node_id` field to every log line format string so you can grep by node

### D3. `/nodes` Telegram command — MEDIUM PRIORITY
Per spec Section 9. Read from DB (`db.list_nodes()`), format compactly.

**Format:**
```
Nodes (2 online)
• LP-1 — claude — 100.x.x.x — last seen 12s ago
• main-pc (this server) — claude,codex — local — last seen 2s ago
```

Also: `/node LP-1` for detail (backends, repos, active tasks, last heartbeat).

### D4. Status + session list UX overhaul — MEDIUM PRIORITY
Current `/status` output is too verbose — walls of text nobody reads.

**Target format for `/status`:**
```
✅ Gateway running — 3 workers, 1 active session

Session: b52d0b06 | claude | LP-1 | awaiting_input
Path: AI-team
```

**Target format for `/session_list`:**
```
Sessions (3)
• b52d0b06 — claude — LP-1 — awaiting_input — AI-team
• ae01d054 — claude — this server — idle — narrative-engine
• [closed] f6e22e5d — claude — main-pc
```

Node column only shown when mesh is enabled and workers exist. Closed sessions collapsed to one line.

### D5. Fix `scripts/fix_session_machine_ids.py` — MEDIUM PRIORITY
Per spec Section 3.2 — needed before VPS migration (Phase 4).
Script reads all session JSON files, finds sessions where `machine_id == socket.gethostname()` (the old server hostname), rewrites them to the correct `WORKER_NODE_ID`.
Already noted as needed in the spec, not yet written.

### D6. PM2 ecosystem update — LOW PRIORITY
- `ai-team-task-server` entry: either remove (if D1 embedded) or enable properly with correct env
- `ai-team-worker` entry: enable with `WORKER_PROJECTS_ROOT`, `WORKER_NODE_ID` etc wired in
- Both should have `restart_delay`, `max_restarts`, `error_file` / `out_file` set

---

## Phase 4 — VPS migration (after D1–D3 are solid)

Per spec Section 13 Phase 4. Pre-migration checklist:
- [ ] Run `scripts/fix_session_machine_ids.py` to retag existing sessions
- [ ] Clone repo to VPS
- [ ] Copy `state/` to VPS
- [ ] Start gateway on VPS, worker on main PC
- [ ] Test end-to-end
- [ ] Stop gateway on main PC

---

## Deferred (valid but lower priority)

- Backend hooks (Session start ID detection, PreToolUse security, PostToolUse quality gates) — see `docs/BACKEND_HOOKS_STRATEGY.md`
- Codex end-to-end validation
- OpenCode server cross-machine sessions (requires shared DB mount — future)
- Postgres migration trigger: >5 nodes or observed SQLite write contention
