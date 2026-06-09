# AI-Team Gateway — Project Context

**Last Updated:** 2026-06-09
**Branch:** `main`
**Status:** File upload support + observability crash fix shipped. Next: State Separation Phase 0 (prerequisite checks) followed by Phase 1 (DB as canonical state source). Full plan in `docs/STATE_SEPARATION_PLAN.md`.

---

## What this project is

A Telegram-controlled remote gateway for local coding agents (Claude Code, Codex, OpenCode CLI, OpenCode server).

Primary runtime flow:
- open a session from Telegram
- route follow-up messages to the active session
- resume the native backend session on each turn
- keep state file-backed and inspectable

Long-term direction: move the control plane to a VPS, worker nodes (PC, laptop, etc.) pull tasks from a central task DB and execute them locally. Spec: `docs/AGENT_MESH_SPEC.md`.

Canonical product intent: `.ai/context/production_vision.md`.

---

## Architecture — current state

```
[Telegram / Phone]
      │
      ▼
[Gateway — runs on main PC via PM2]
  ├── src/telegram/interface.py     Telegram command surface
  ├── src/orchestrator.py           Task queue, workers, session routing
  ├── src/core/session_store.py     File-backed session CRUD + shadow-write to DB
  ├── src/control/db.py             SQLite mesh DB (WAL, versioned migrations)
  └── src/backends/                 claude_code, codex, opencode, opencode-server
```

State layout:
```
state/sessions/<session_id>.json      authoritative session records
state/telegram/active_bindings.json   chat_id → session_id
state/summaries/<session_id>.md       compact per-session summary
state/mesh.db                         SQLite mirror (shadow copy of all of the above)
results/<task_id>.json                full task artifact (stdout, parsed output, diffs)
results/sessions/<session_id>/        per-session ordered artifact history
logs/session_events/<session_id>.log  append-only NDJSON event trail
logs/events.ndjson                    system-wide event log
```

---

## Phase completion status

### Phase 1 — Session foundation ✅
File-backed session CRUD, Telegram bindings, session picker, `#s_` / `#t_` refs.

### Phase 2 — Backend session support ✅
Claude, Codex, OpenCode CLI, OpenCode server — all with native create/resume.
`backend_session_id` persisted and used for resume on every turn.

### Phase 3 — Session execution flow ✅
Telegram plain text and `/task` queue tasks directly. Artifacts written for every turn.

### Phase 4 — Observability ✅
Per-session event logs, system event log, session summaries, result artifacts, path resolver.

### Phase 5 — Compatibility and cleanup ✅
`.task.md` watcher still supported as compatibility lane. Legacy bridge code present but off the primary path. Telegram session commands, inline pickers, buffered message debounce.

### Phase 6 — Operations and persistence ✅
PM2 supervision, health command, single-instance takeover, cross-platform process utilities.

### Phase 7 — OpenCode backends ✅
`OpenCodeBackend` (CLI, `opencode run`) and `OpenCodeServerBackend` (HTTP, `opencode serve`).
Session picker exposes both. Auto-commit after each run. Inactivity timeout, truncation detection.
Documented in `docs/OPENCODE_SERVER_CONTEXT.md`.

### Phase 8 — Agent mesh DB foundation ✅
**What was built:**
- `src/control/__init__.py` — package marker
- `src/control/db.py` — `MeshDB` class: SQLite WAL, thread-safe write lock, per-thread connection cache, versioned migration runner, full public API
- `schema.prisma` — documentation-only schema in Prisma DSL (no Node dependency; reference only)
- `config/settings.py` — `MeshConfig` dataclass added (`MESH_ENABLED`, `MESH_DB_PATH`, `WORKER_TOKEN`, `MESH_SHADOW_WRITE`, etc.)
- `src/core/session_store.py` — `_shadow_write()` hook: every `save()` mirrors to DB silently
- `src/orchestrator.py` — `_mesh_enqueue_task()` and `_mesh_complete_task()` helpers wired into `_task_worker`
- `scripts/seed_db_from_json.py` — one-shot backfill; already run: **149 sessions, 794 tasks, 799 events** seeded
- `state/mesh.db` — live SQLite DB, verified shadow-writing on every session save and task completion

**DB tables:**
| Table | Purpose |
|-------|---------|
| `sessions` | mirror of `state/sessions/*.json` |
| `mesh_tasks` | dispatch queue + historical task record |
| `task_events` | append-only event log per session |
| `nodes` | registered worker nodes (ephemeral, rebuilt from heartbeats) |

**Key design decisions:**
- JSON files remain authoritative. DB is a queryable shadow copy.
- `MESH_SHADOW_WRITE=true` by default — DB is always warm.
- `MESH_ENABLED=false` by default — no routing change to the running gateway.
- Migrations: append `(version, "ALTER TABLE ...")` to `_get_migrations()` in `db.py`. Auto-applied on startup.
- No ORM. Raw `sqlite3` (stdlib). Simple schema, no maintenance burden. Postgres swap = change connection factory + RETURNING syntax.

**Verified live:** session `f6e22e5df521`, task `task_e2f65d7d` — both written to DB automatically after gateway restart.

---

### Phase 9 — Agent mesh worker + task server ✅  ← completed + adversarially reviewed + fixed this session

**What was built:**
- `src/control/task_server.py` — FastAPI app, 9 endpoints, Bearer auth, MeshDB-backed
- `src/control/node_registry.py` — in-memory NodeRegistry, heartbeat expiry, offline task failover, DB persistence
- `src/worker/__init__.py`, `src/worker/config.py`, `src/worker/agent.py` — full worker daemon (register, poll+backoff, nudge listener, heartbeat, SIGTERM drain)
- `src/orchestrator.py` — `_run_backend_local`, `_dispatch_to_node`, `_dispatch_or_run_local` added
- `ecosystem.config.js` — `ai-team-task-server` and `ai-team-worker` PM2 entries (disabled by default)
- `scripts/test_mesh_local.py` — in-process FastAPI TestClient smoke test (18 checks, all passing)

**Adversarial review found 14 issues; the following were fixed:**
1. **Double-execution bug (critical)** — `_mesh_enqueue_task` wrote every task as `pending`, claimable by any worker, while `process_task` *also* always ran it locally. Fixed: the gateway now self-claims its own shadow-written rows immediately after insert (`db.claim_task(task_id, hostname)`), making them invisible to `get_pending_tasks` for every node including itself. DB stays a faithful historical mirror with zero claimable duplicate work. `_dispatch_or_run_local` remains defined but is intentionally NOT wired into `process_task` yet — that's a separate, larger rewrite (would need to absorb retry/timeout/heartbeat machinery into the remote path) reserved for when real multi-node routing is rolled out.
2. **Session payload missing (critical)** — `_mesh_enqueue_task` now embeds the full session dict in the payload so `_make_session_from_payload` on a worker can reconstruct it.
3. **Worker drain no-op (critical)** — `asyncio.create_task` results are now stored in `self._active` with a done-callback cleanup, so SIGTERM drain actually waits up to 30s.
4. **No claim verification on result submission (critical)** — `submit_result` now checks `payload.node_id == task.claimed_by` and returns 403 on mismatch.
5. **Blocking DB scan in async expiry loop (high)** — `_fail_offline_tasks` now runs via `asyncio.to_thread`.
6. **Worker never re-registers after server restart (high)** — heartbeat 404 now triggers automatic re-registration.
7. **`RuntimeError` would kill worker loop (high)** — routing failures now return structured failed `TaskResult` instead of raising.
8. **Nudge listener accepted any TCP probe as a nudge (high)** — now validates `POST /nudge` prefix before setting the poll event.
9. **`_fire_nudge` built invalid URL for empty `tailscale_ip`** — now skips with a debug log.
10. **Wrong `action` label for new sessions** — `_mesh_enqueue_task` now correctly labels `create_session` vs `resume_session`.

**Pre-existing bug found and fixed (not introduced this session):**
- `MeshDB._run_migrations` called `executescript()` even for the empty baseline marker `(1, "")`. `executescript()` issues an implicit `COMMIT`, killing the `BEGIN IMMEDIATE` transaction and causing `cannot commit - no transaction is active` on every **fresh** DB (the live `state/mesh.db` was already at version 1 so this was invisible). Fixed by skipping `executescript` for empty SQL and using plain `execute()` per-statement for real migrations.
- Found and cleaned up 2 orphaned `pending` rows in the live `mesh_tasks` table (`task_61e2816b`, `task_7259a339`) — both were cancelled/interrupted mid-run before Phase 8's `_mesh_complete_task` could finalize them. Marked `failed` so no worker could claim and re-execute them.

**Verified:** `python scripts/test_mesh_local.py` — 18/18 checks pass (register, heartbeat, 404-unknown-node, enqueue, poll, claim, double-claim rejection, claim-mismatch result rejection, completion, deregistration).

**Status:** `MESH_ENABLED=false` (default) → gateway behavior is provably unchanged; shadow-write is safe and self-contained. Ready for a controlled `MESH_ENABLED=true` trial — see NEXT_TASKS.md for the recommended rollout sequence.

---

### Phase 9 Step C — Real two-machine test ✅  ← completed 2026-06-07

**What was built/fixed:**
- Worker advertises `projects_root` + `repos` at register time; stored in DB (migrations v2/v3 added to `db.py`)
- `_mesh_online_nodes()` reads shared SQLite DB — works cross-process (gateway ≠ task server process)
- `_repo_choices_for_node()` and `_handle_session_new` text command also read from DB
- Telegram node picker: backend → node → repo button flow; `session_new_node:` callback step added
- `/session_new claude LP-1 AI-team` text fallback: detects node_id from known online nodes
- Session `machine_id` set correctly at creation time via `_create_and_bind_session(node_id=...)`
- `route_remote` fix: only true when `session.machine_id != socket.gethostname()` — local sessions never routed remotely
- FastAPI `@app.on_event` → `lifespan` context manager (deprecation fix)
- Worker loads `.env` via dotenv on startup (`main()` in `agent.py`)
- `WORKER_PROJECTS_ROOT` + `list_repos()` in `WorkerConfig`
- `upsert_node` stores `projects_root` + `repos`; `NodeCapabilities` extended to match
- Unused variable fixes: `resp =` on claim removed, `done, pending` → `_, pending`

**Known issue going into Step D:** gateway and task server are separate processes — the gateway's in-memory `NodeRegistry` is always empty. Fixed by reading from DB, but the real fix is process consolidation (D1).

**→ Resolved in Step D1 (2026-06-07):** task server is now embedded in the gateway via `src/control/embedded_server.py`, started by `TaskOrchestrator._start_embedded_task_server()` on the gateway's event loop when `MESH_ENABLED=true`. The `get_registry()` singleton is shared in-process; the DB path remains only as a post-restart safety net. The separate `ai-team-task-server` PM2 entry was removed.

---

### Phase 9 Step B — Wire `_dispatch_or_run_local` into `process_task` ✅  ← completed + adversarially reviewed + tested this session

**What was built:**

`process_task` in `src/orchestrator.py` now routes tasks to remote workers when `MESH_ENABLED=true` AND `session.machine_id` is set. Zero behavior change for all other sessions.

**Exact changes:**

1. **`src/orchestrator.py`**
   - `process_task`: resolves `session`/`session_id` once before the retry loop; sets `route_remote = bool(MESH_ENABLED and session and session.machine_id)`; if True, delegates to new `_process_task_remote` and skips the local retry loop entirely (`while not route_remote:`).
   - New `_process_task_remote`: sets session BUSY, verifies the pinned node is online (fails loudly — no silent local fallback, which would corrupt `backend_session_id` continuity), runs Telegram heartbeats, calls `_dispatch_to_node`, catches unexpected dispatch exceptions and converts them to failure results (so session never gets stuck as BUSY), sets session status (AWAITING_INPUT / CANCELLED / ERROR), classifies error.
   - `_mesh_enqueue_task`: skips self-claim when `machine_id` is set — leaves row `pending` so the pinned remote worker can claim it via `get_pending_tasks`. Local tasks (no `machine_id`) still self-claim immediately as before.
   - `_dispatch_to_node`: fails loudly when DB is unavailable (previously fell back silently to local — wrong for session-affinity tasks); propagates `backend_session_id` from the worker's result dict back to `session.backend_session_id` and saves it, so the next turn can resume the remote-side backend session.

2. **`src/control/task_server.py`**: added `backend_session_id: str = ""` to `ExecutionResultPayload`; included in `result_dict` stored to DB so the gateway can read it back.

3. **`src/worker/agent.py`**: `_execute_task` now includes `backend_session_id` in the result dict it posts to `/tasks/{id}/result`.

**Adversarial review findings — all addressed:**
- Session stuck as BUSY on unexpected dispatch exception → fixed with try/except in `_process_task_remote` that converts to failure result
- Silent local fallback in `_dispatch_to_node` when DB unavailable → fixed to fail loudly
- `backend_session_id` not propagated from worker result → fixed end-to-end (worker → task_server → DB → gateway → session)
- `_mesh_enqueue_task` self-claim for remote tasks → fixed with `if not machine_id:` guard

**Verified:** `python scripts/test_mesh_local.py` — 18/18; `python scripts/test_routing_integration.py` — 24/24.

**Correctness guarantees:**
- `MESH_ENABLED=false` (default) → identical to pre-mesh behavior. `route_remote` is always False; `while not route_remote:` runs exactly as the old `while True:` did.
- Sessions without `machine_id` → local path unchanged even with `MESH_ENABLED=true`.
- Sessions with `machine_id` + `MESH_ENABLED=true` → remote dispatch only; fail loudly if node offline; no silent local fallback.

---

## Phase 9 architecture — now built

### Step 1 — Task server (VPS-side) `src/control/task_server.py` ✅
FastAPI app bound to `{MESH_TAILSCALE_IP}:9002`. Endpoints:
- `POST /nodes/register` — worker startup
- `POST /nodes/heartbeat` — keepalive every 30s
- `POST /nodes/deregister` — clean shutdown
- `GET /nodes` — list nodes
- `GET /tasks/pending` — worker poll (filter by `node_id`, `backends`)
- `POST /tasks/{id}/claim` — optimistic lock claim
- `POST /tasks/{id}/result` — worker posts ExecutionResult
- `POST /nodes/{id}/nudge` — VPS pushes nudge to worker (internal)

All endpoints require `Authorization: Bearer {WORKER_TOKEN}`.

### Step 2 — Worker daemon `src/worker/agent.py`
Persistent daemon, one per participating machine, managed by PM2.
- Registers with VPS on startup
- Polls `GET /tasks/pending` with backoff (5s → 30s on empty)
- Claims task, instantiates local backend, executes, posts result
- Sends heartbeats every 30s concurrently
- On SIGTERM: deregisters, drains active tasks (up to 30s)

### Step 3 — Orchestrator mesh routing `_dispatch_or_run_local`
Add to `src/orchestrator.py`. `MESH_ENABLED=false` = local execution as today. `MESH_ENABLED=true` = route through node registry. Zero regression.

### Step 4 — `src/worker/config.py`
Worker env vars: `WORKER_NODE_ID`, `WORKER_TOKEN`, `WORKER_TAILSCALE_IP`, `WORKER_API_PORT`, `WORKER_MAX_CONCURRENT`, `CONTROLLER_URL`, `WORKER_BACKENDS`.

### Prerequisite (your action, not code)
- Confirm both VPS and main PC are enrolled in Tailscale
- Record both Tailscale IPs
- Generate `WORKER_TOKEN`: `openssl rand -hex 32`
- Set Tailscale ACL: VPS port 9002 reachable from PC; PC port 9001 reachable from VPS

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | Main runtime, task queue, workers, session routing, mesh shadow-write hooks |
| `src/telegram/interface.py` | Telegram command surface |
| `src/core/session_store.py` | File-backed session store + DB shadow-write |
| `src/control/db.py` | SQLite mesh DB — the canonical database layer |
| `src/control/__init__.py` | Package marker |
| `schema.prisma` | Schema documentation in Prisma DSL (read-only reference) |
| `scripts/seed_db_from_json.py` | Backfill historical JSON data into DB |
| `config/settings.py` | All config including new MeshConfig |
| `src/core/interfaces.py` | Session/Task/TaskResult dataclasses |
| `src/backends/claude_code.py` | Claude native backend |
| `src/backends/codex.py` | Codex native backend |
| `src/backends/opencode.py` | OpenCode CLI + server backends |
| `docs/AGENT_MESH_SPEC.md` | Full mesh architecture spec |
| `docs/OPENCODE_SERVER_CONTEXT.md` | OpenCode server backend context |
| `ecosystem.config.js` | PM2 supervisor config |
| `docs/OPERATIONS_PM2.md` | PM2 operator runbook |

---

## Backend hooks strategy

We evaluated whether backend lifecycle hooks (Claude Code, Codex CLI, OpenCode) can replace our current agent state management. Full analysis: `docs/BACKEND_HOOKS_STRATEGY.md`.

**Bottom line:** Our external orchestration is correct for gateway-level concerns (Telegram, state persistence, mesh routing). But hooks can replace 3 fragile things the backends currently do via stdout regex parsing, and add security guardrails we currently lack entirely.

**Tasks (when time allows):**

- **A. SessionStart hook for session ID detection** — replace fragile stdout regex parsing of `session_id`/`thread_id` with a deterministic `SessionStart` hook that writes the native session ID to a known file. All 3 backends support this.
- **B. PreToolUse security guardrails** — block dangerous commands (`rm -rf`, `DROP TABLE`, etc.) via `PreToolUse` exit-code-2 blocking. Claude Code (full), Codex CLI (shell only), OpenCode (plugin).
- **C. PostToolUse code quality gates** — run linters/tests after every `Write`/`Edit` tool call deterministically, instead of relying on the LLM to remember. Claude Code (full), OpenCode (plugin).

See `docs/BACKEND_HOOKS_STRATEGY.md` for event matrices, implementation order, and what NOT to do.

---

## Architecture rules

- DB is canonical state source (migrating from JSON). JSON files remain as legacy fallback — never deleted.
- Gateway keeps exactly 1 embedded fallback worker that activates only when the mesh is broken (task server unreachable, no workers online). This preserves the ability to run recovery tasks.
- `MESH_ENABLED=false` by default. Gateway behaves identically to pre-mesh with it off.
- Session affinity is a hard correctness requirement, not a preference. A session tied to a machine must execute on that machine.
- Backend session state (`backend_session_id`) is machine-local and cannot be migrated across nodes (except OpenCode server with shared DB — future).
- No uncontrolled autonomous behavior.
- Ollama remains optional and helper-only.
- Artifacts remain mandatory for audit purposes.

---

## State Separation Phases

Detailed plan: `docs/STATE_SEPARATION_PLAN.md`

### Phase 0 — Prerequisites (manual checks, no code)

- [ ] Verify `mesh.db` is in WAL mode and `busy_timeout=5000`:
      ```python
      import sqlite3; c = sqlite3.connect("state/mesh.db")
      print(c.execute("PRAGMA journal_mode").fetchone())     # must be 'wal'
      print(c.execute("PRAGMA busy_timeout").fetchone())     # must be 5000
      ```
- [ ] Verify session counts match: `SELECT COUNT(*) FROM sessions` vs `ls state/sessions/*.json | wc -l`
- [ ] Check `mesh_tasks` for orphaned `pending` or `claimed` rows; mark them `failed` if the task is dead
- [ ] Confirm `WORKER_TOKEN` is set in `.env`
- [ ] Confirm `MESH_TAILSCALE_IP` in `.env` is a real IP (not a comment string or empty)
- [ ] Run `scripts/test_mesh_local.py` and confirm 18/18 pass

### Phase 1 — DB as canonical state source

**Goal:** `SessionStore` reads from DB first, falls back to JSON. Recovery logic uses DB + worker liveness instead of blindly marking ERROR.

**Tasks:**

1. **`src/core/session_store.py` — add DB read path**
   - `get(session_id)`: try `db.get_session(session_id)` first, fall back to JSON file
   - `list_all()`: read from DB, fall back to JSON directory scan
   - `save()`: unchanged — continues dual-write (DB + JSON)
   - JSON files remain written and untouched; they just become the fallback read source
   - Files: `src/core/session_store.py`
   - Test: create session, verify it reads back from DB after cache clear; delete DB row, verify it falls back to JSON

2. **`src/orchestrator.py` — fix `_recover_stale_busy_sessions`**
   - Current: marks every BUSY session as ERROR unconditionally
   - New: before marking ERROR, check:
     - Is the task still running? (check `active_tasks` dict, check subprocess PID)
     - Is there a completed result in `mesh_tasks` for `session.last_task_id`?
     - Is the task still `pending` or `claimed` in `mesh_tasks`? (worker will finish it)
   - Only mark ERROR if: no worker is running the task AND no result exists in DB
   - If result exists in DB: restore session to IDLE, propagate the completion
   - Test: simulate a stale BUSY session with a completed DB entry → verify recovery sets IDLE, not ERROR

3. **`src/control/db.py` — add `get_task_by_session()` query**
   - `get_task_by_session(session_id, task_id)` → returns task status + result dict if completed
   - Used by recovery logic to distinguish "interrupted" from "finished but not propagated"
   - Test: enqueue a task, complete it, call `get_task_by_session` → verify it returns the result

4. **Deploy and monitor**
   - Restart gateway, observe recovery behavior
   - No behavioral change yet (tasks still run locally in-process)
   - But recovery should correctly restore finished tasks instead of ERROR-marking them

**Verification:** Create a session, submit a task, wait for it to complete, kill the gateway (`pm2 restart`), gateway comes back → session should be IDLE, not ERROR.

### Phase 2 — Standalone task server

**Goal:** Task server runs as its own PM2 process (`ai-team-server`). Gateway connects to it as an HTTP client instead of embedding it.

**Tasks:**

1. **`ecosystem.config.js` — add `ai-team-server` PM2 entry**
   - Script: `worker_main.py` or a new `server_main.py` (thin entry that starts uvicorn on `task_server:app`)
   - Env: `MESH_TAILSCALE_IP`, `MESH_TASK_SERVER_PORT`, `WORKER_TOKEN`
   - No autorestart limit (task server should always be up)
   - Kill timeout: 10s
   - Logs: `logs/pm2-server-out.log`, `logs/pm2-server-error.log`

2. **Create `server_main.py`** (thin PM2 entry point)
   - Loads `.env`, runs `uvicorn.run(src.control.task_server:app, host=..., port=...)`
   - Or use `python -m uvicorn src.control.task_server:app --host ... --port ...` directly in PM2

3. **Delete `src/control/embedded_server.py`**
   - Remove the file
   - No longer needed — task server is a separate process

4. **`src/orchestrator.py` — remove embedding, add HTTP client**
   - Remove `_embedded_task_server` field, `_start_embedded_task_server()`, `_stop_embedded_task_server()`
   - Remove calls to these from `start()` and `stop()`
   - Add `TaskServerClient` class (stdlib `urllib`, same pattern as `src/worker/agent.py` `_HTTP`)
   - Methods: `enqueue_task(payload)`, `get_task_status(task_id)`, `get_health()`, `list_nodes()`
   - Auth: Bearer `WORKER_TOKEN`
   - Base URL: `http://{MESH_TAILSCALE_IP}:{MESH_TASK_SERVER_PORT}`

5. **Recovery goes through the client**
   - `_recover_stale_busy_sessions` queries the task server via `TaskServerClient` instead of direct DB access
   - Health check uses `TaskServerClient.get_health()` instead of checking embedded server state

6. **Remove wiring from `_dispatch_to_node` that assumed in-process registry**
   - Current: reads in-process `get_registry()` singleton (same process, shared memory)
   - New: reads from `TaskServerClient.list_nodes()` (HTTP round-trip)
   - Cache node list with a short TTL (e.g., 5s) to avoid hammering on every task dispatch

**Deployment order:**
1. Start `ai-team-server` first (binds port 9002)
2. Restart `ai-team-gateway` (no longer binds port 9002, connects as client)
3. If gateway cannot reach task server → enter fallback mode (see Phase 4)

**Risk:** Gateway loses in-process NodeRegistry — node discovery now requires HTTP. Mitigated by client-side caching.

### Phase 3 — Standalone workers

**Goal:** The `ai-team-worker` PM2 process runs and executes tasks. Gateway's in-process workers become the fallback (Phase 4).

**Tasks:**

1. **`ecosystem.config.js` — enable `ai-team-worker` entry**
   - Set `WORKER_NODE_ID` to this machine's hostname (from `socket.gethostname()`)
   - Set `CONTROLLER_URL` to `http://{MESH_TAILSCALE_IP}:{MESH_TASK_SERVER_PORT}`
   - Set `WORKER_PROJECTS_ROOT` to the projects directory
   - Set `WORKER_BACKENDS` to the installed backends (e.g., `claude,opencode,opencode-server`)
   - Kill timeout: 35s (matches 30s drain window)
   - Enable: add `pm2 start ecosystem.config.js --only ai-team-worker` to deploy script

2. **`src/orchestrator.py` — reduce in-process workers to 1**
   - Change `max_concurrent_tasks` from 3 to 1 for the gateway's own worker pool
   - Or add a `MIN_WORKERS` config: 1 for embedded fallback, 0 for pure mesh
   - Default: `min_workers = 1` (always have the fallback)

3. **`src/orchestrator.py` — update `process_task` dispatch**
   - Current: if `MESH_ENABLED=true` and `session.machine_id != local` → route remote; else → run locally
   - New: always try mesh first via `TaskServerClient.enqueue_task()`, fall back to embedded worker only on failure
   - Session affinity still respected: if `session.machine_id` matches this host, route to embedded worker directly (no mesh round-trip)
   - Add a timeout for mesh dispatch (e.g., 5s) — if the task server doesn't respond, fall back

4. **Test the full flow end-to-end:**
   - Gateway receives instruction → enqueues to task server via HTTP
   - Worker polls → claims → executes → posts result via HTTP
   - Gateway polls for result → relays to Telegram
   - Kill gateway during task → worker continues, task completes, result in DB
   - Restart gateway → reads completed result from DB → relays to Telegram (`notify_completion`)

5. **Run worker in foreground first to observe logs:**
   ```bash
   pm2 start ecosystem.config.js --only ai-team-worker --no-daemon
   ```
   Watch for: token mismatch, backend session ID routing issues, nudge listener binding

**Risk:** Worker agent code exists but has never been exercised in production. Mitigation: keep the embedded fallback worker so the system is never completely stuck.

### Phase 4 — Fallback worker + graceful degradation

**Goal:** If the task server or mesh workers are unavailable, the gateway uses its 1 embedded worker and JSON files to keep operating. The fallback can run recovery tasks (e.g., "restart the task server").

**Tasks:**

1. **Define mesh health criteria (config or constants):**
   - Task server responds to `/health` within 5s → healthy
   - At least 1 worker has heartbeated within `heartbeat_timeout_sec` (90s) → healthy
   - Both must be true for "mesh healthy" — otherwise → fallback mode

2. **Implement fallback mode in `src/orchestrator.py`:**
   - When a task arrives and mesh is unhealthy:
     - Route to the 1 embedded in-process worker
     - Write task state to JSON files directly (existing `state/sessions/` path, no DB dependency)
     - Session reads fall back to JSON (Phase 1 already implemented this)
     - Telegram notifications work normally (gateway is fully local)
   - While in fallback mode, run a periodic health check every 30s:
     - Try `TaskServerClient.get_health()` again
     - If task server responds, check for worker heartbeats
     - If both pass → exit fallback mode

3. **Make the fallback worker capable of running recovery tasks:**
   - If task server is down, the user can send a message like "restart the task server"
   - The fallback worker executes: `pm2 restart ai-team-server` (local shell)
   - Result: "Task server restarted, mesh healthy again"
   - This requires the fallback worker to have access to shell commands (it already does via the backend interface)

4. **Sync completed fallback tasks to DB when mesh recovers:**
   - After exiting fallback mode, read any completed results from JSON files
   - Write them to the task server DB via `TaskServerClient`
   - This ensures the task server has a complete history even after fallback periods
   - Mark synced tasks in JSON to avoid double-sync on next recovery

5. **Test:**
   - Stop the task server → send a task → verify it runs via fallback worker
   - Restart the task server → verify the gateway detects it and exits fallback mode
   - Verify completed tasks during fallback are synced to DB after recovery

**Risk:** Low — this is additive. The embedded worker already exists. The fallback logic is new but has no effect when the mesh is healthy.
