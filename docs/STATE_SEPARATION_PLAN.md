# State Separation & Mesh Migration Plan

> **Status:** ACTIVE — plan of record (supersedes the standalone "VPS migration
> Phase 4"). VPS cutover is the end-state of Phases 2–3; runbook in
> `docs/PHASE_4_RUNBOOK.md`.
> **Progress (2026-06-10):** Phase 0 partly done (2 orphan tasks + DB/JSON count
> mismatch outstanding); **Phase 1 DONE** (DB-first reads + DB-aware recovery
> already in code); Phases 2–4 not started.
> **Target:** Three-process architecture with clear state boundaries and a management-plane fallback

---

## 1. Why

The gateway currently runs orchestrator, task server, and worker coroutines in a single process. Restarting the gateway kills in-flight tasks, corrupts session state, and the recovery logic (`_recover_stale_busy_sessions`) has to pessimistically mark everything as ERROR because it has no durable way to check what actually completed.

Three separate processes with clear state boundaries fix this:

| Process | Role | Restart-safe? |
|---------|------|--------------|
| **Gateway** | Telegram bot, instruction submission, result relay | Yes — stateless by design |
| **Task Server** | Durable task queue, node registry, session state authority | Yes — survives gateway restarts |
| **Worker** | Backend execution (Claude Code, OpenCode) | Yes — survives gateway restarts |

**Why not full separation:** A standalone task server or remote worker might be unreachable (process crash, network partition, misconfiguration). If the gateway cannot dispatch work, it cannot fix itself. Therefore the gateway must retain a **last-resort embedded worker** — one local in-process worker that activates only when the mesh is broken, capable of running recovery tasks.

---

## 2. Current State (Factual)

```
One process (ai-team-gateway, PID 40040):
├── Orchestrator
│   ├── 3× in-process _task_worker coroutines (always local)
│   └── SessionStore → JSON files (authoritative)
│                      └── SQLite mesh.db (shadow mirror, WAL mode)
├── Embedded Task Server (FastAPI via embedded_server.py, port 9002)
│   └── NodeRegistry (in-memory, shares process with orchestrator)
├── Telegram gateway
└── Main/CLI entry point
```

**Key facts:**
- `MESH_ENABLED=true`, `MESH_SHADOW_WRITE=true` — shadow writes to DB are active
- `ai-team-worker` PM2 entry **exists but is disabled** — never run
- All sessions have `machine_id = socket.gethostname()` → all tasks run locally
- `_recover_stale_busy_sessions` reads **only from JSON files**, never from DB
- DB has 5 tables (`sessions`, `mesh_tasks`, `task_events`, `nodes`, `schema_version`) with full schema
- Worker agent (`src/worker/agent.py`) is fully implemented (poll, claim, execute, result) but unused
- Task server (`src/control/task_server.py`) has all endpoints but runs embedded

---

## 3. End Goal

```
Three processes + one fallback:

┌─────────────────────────────────────────────────┐
│ 1. ai-team-gateway (stateless relay)            │
│    ├── Telegram bot                             │
│    ├── Instruction → Task (writes to task server)│
│    ├── Status polls (reads from task server)     │
│    └── Fallback worker (1 slot, mesh-broken only)│
└─────────────────────────────────────────────────┘
                       │ HTTP (localhost:9002)
                       ▼
┌─────────────────────────────────────────────────┐
│ 2. ai-team-server (durable state)               │
│    ├── FastAPI task server (port 9002)          │
│    ├── NodeRegistry (in-memory + DB backed)     │
│    ├── SQLite mesh.db (CANONICAL state store)   │
│    └── SessionStore reads from DB, writes JSON  │
│                    as legacy fallback            │
└─────────────────────────────────────────────────┘
                       │ HTTP (localhost:9002)
                       ▼
┌─────────────────────────────────────────────────┐
│ 3. ai-team-worker (backend execution)           │
│    ├── Polls /tasks/pending                     │
│    ├── Claims, executes, posts results          │
│    └── Heartbeat every 30s                      │
└─────────────────────────────────────────────────┘
```

**State ownership:**

| State | Owned by | Storage | Read by |
|-------|----------|---------|---------|
| Session (status, repo, metadata) | Task Server | DB canonical, JSON fallback | Gateway, Worker |
| Task queue | Task Server | `mesh_tasks` table | Gateway (enqueue), Worker (claim) |
| Task result | Task Server | `mesh_tasks.result`, `task_events` | Gateway (relay to Telegram) |
| Node registry | Task Server | In-memory + DB mirror | Gateway (dispatch), Worker (registration) |
| Backend session (Claude Code state) | Worker machine | Local filesystem (`~/.claude/`) | Only the owning worker |
| Telegram bindings | Gateway | `state/telegram/active_bindings.json` | Gateway only |

**Fallback worker rule:**
- The gateway keeps **exactly 1** in-process worker coroutine
- This worker **never activates** when the task server is healthy and workers are online
- It activates only when: task server unreachable, or no worker available to claim a task after N seconds
- It can run any task, including recovery/repair tasks ("restart the task server", "debug worker connectivity")
- JSON file writes continue as a legacy state path so the gateway can operate independently of the DB during fallback mode

---

## 4. Migration Phases

### Phase 0 — Prerequisites (no code changes)

- [ ] Confirm `mesh.db` is in WAL mode and `busy_timeout=5000` (check `PRAGMA journal_mode` and `PRAGMA busy_timeout`)
- [ ] Count sessions: `SELECT COUNT(*) FROM sessions` vs `ls state/sessions/*.json | wc -l` — verify they match
- [ ] Check for any sessions in `mesh_tasks` with `status=pending` or `status=claimed` that might be orphaned
- [ ] Verify `WORKER_TOKEN` is set in `.env` (it is — 7b54e516...)
- [ ] Verify `MESH_TAILSCALE_IP` is correct (100.112.245.29)

---

### Phase 1 — DB as Canonical State Source

**Goal:** SessionStore reads from DB first, falls back to JSON. Recovery logic uses DB + worker liveness instead of assuming ERROR.

**Steps:**

1. **Add a `_read_from_db` path to `SessionStore`**
   - `get(session_id)` tries `db.get_session(session_id)` first, falls back to JSON file
   - `list_all()` reads from DB, falls back to JSON directory scan
   - `save()` continues dual-write (DB + JSON)
   - This is safe: JSON and DB are already in sync via shadow writes

2. **Fix `_recover_stale_busy_sessions`**
   - Check worker liveness before marking ERROR:
     - Is the backend subprocess still alive? (check PID)
     - Is there a completed result in `mesh_tasks` for `session.last_task_id`?
     - Is the task still pending/claimed in `mesh_tasks`? (worker will finish it)
   - Only mark ERROR if: no worker is running the task **AND** no result exists
   - If result exists in DB: restore session to IDLE, propagate the result

3. **Add a `db.get_task(session_id, last_task_id)` query**
   - Returns task status and result if completed
   - Used by recovery logic to distinguish "interrupted" from "finished but not propagated"

4. **Verify parity**
   - Deploy, monitor for a few days
   - No behavioral change yet (tasks still run locally)
   - But recovery should correctly restore finished tasks instead of ERROR-marking them

**Risk:** Low — reads are additive, writes unchanged. JSON files remain as fallback.

---

### Phase 2 — Standalone Task Server

**Goal:** Task server runs as its own PM2 process. Gateway connects to it as an HTTP client instead of embedding it.

**Steps:**

1. **Create `ai-team-server` PM2 entry in `ecosystem.config.js`**
   - Script: `python -m uvicorn src.control.task_server:app --host $MESH_TAILSCALE_IP --port $MESH_TASK_SERVER_PORT`
   - Or a thin `server_main.py` entry point
   - Same `.env`, same `mesh.db` file
   - Kill timeout: 10s (no active work to drain)

2. **Remove embedding from the gateway**
   - Delete `src/control/embedded_server.py`
   - Remove `_start_embedded_task_server()` / `_stop_embedded_task_server()` from orchestrator
   - Gateway no longer starts or stops the task server

3. **Add HTTP client in the gateway for task server interaction**
   - Wrap task server endpoints in a `TaskServerClient` class (stdlib `urllib`, same pattern as worker agent)
   - Methods: `enqueue_task()`, `get_task_status()`, `get_node_status()`, `get_health()`
   - Auth: Bearer token via `WORKER_TOKEN`
   - Base URL: `http://{MESH_TAILSCALE_IP}:{MESH_TASK_SERVER_PORT}`

4. **Gateway health check uses `TaskServerClient.get_health()`** instead of checking embedded server state

5. **Recovery flows through the client** — `_recover_stale_busy_sessions` queries the task server via HTTP

**Deployment order:**
1. Start `ai-team-server` (it binds port 9002)
2. Restart `ai-team-gateway` (it no longer binds port 9002, connects as client instead)
3. If gateway fails to connect → gateway falls back to embedded mode? **No.** If task server is down, the gateway enters **fallback mode** (see Phase 4).

**Risk:** Medium — gateway loses the in-process NodeRegistry singleton. Node discovery now requires HTTP round-trips through the task server. This was the original reason for embedding (commit history). Mitigation: the task server's NodeRegistry already persists to DB, and the gateway's `TaskServerClient` can cache node status with a short TTL.

---

### Phase 3 — Standalone Workers

**Goal:** The `ai-team-worker` PM2 process runs and executes tasks. Gateway's in-process workers become the fallback (Phase 4).

**Steps:**

1. **Enable `ai-team-worker` PM2 process**
   - Set `WORKER_NODE_ID` to this machine's hostname
   - Set `CONTROLLER_URL` to `http://{MESH_TAILSCALE_IP}:{MESH_TASK_SERVER_PORT}`
   - Start with `pm2 start ecosystem.config.js --only ai-team-worker`

2. **Reduce gateway's in-process workers to 1** (from 3)
   - Change `max_concurrent_tasks` default or add a `MIN_WORKERS` config
   - The single remaining worker is the fallback (Phase 4)

3. **Update `_dispatch_or_run_local()` to prefer mesh dispatch**
   - Current: if `MESH_ENABLED=false` or registry empty → local
   - New: always try mesh first (enqueue to task server), fall back to local worker only on failure
   - Session affinity still respected: if `session.machine_id` matches this host, route locally

4. **Test the flow end-to-end:**
   - Gateway receives instruction → enqueues to task server
   - Worker polls → claims → executes → posts result
   - Gateway polls for result → relays to Telegram
   - Restart gateway during task → worker continues, task completes, result stored in DB
   - Gateway recovers → reads completed result from DB → relays to Telegram

**Risk:** Medium — the worker agent code exists but has never been exercised in production. Expect:
   - Token/permission mismatches (fix: check `.env`)
   - Backend session ID routing issues (worker receives session data, executes, but `backend_session_id` is local)
   - Nudge listener binding issues (port 9001, Tailscale IP)

**Mitigation:** Run worker in `--no-daemon` mode first to observe logs. Keep the embedded fallback worker so the system is never completely stuck.

---

### Phase 4 — Fallback Worker + Graceful Degradation

**Goal:** If the task server or mesh workers are unavailable, the gateway degrades gracefully: it uses its one embedded worker and JSON file state to keep operating.

**Steps:**

1. **Define "mesh healthy" criteria in the gateway:**
   - Task server responds to `/health` within 5s
   - At least one worker has heartbeated within the last `heartbeat_timeout_sec`
   - If either condition fails → enter fallback mode

2. **Implement fallback mode:**
   - The 1 remaining in-process worker activates
   - Tasks are written to JSON files directly (`logs/fallback_tasks/` or existing `state/sessions/` path)
   - Session state reads/writes go to JSON files (DB is optional)
   - Telegram notifications work normally
   - A periodic health-check task retries the task server connection every 30s
   - When the task server is healthy again → flush any completed fallback results to DB → exit fallback mode

3. **Fallback worker can run recovery tasks:**
   - If the task server is down, the user can send `/task restart task server` or similar
   - The fallback worker runs it locally (e.g., `pm2 restart ai-team-server`)
   - Result: "Task server restarted, mesh healthy again"

4. **When mesh recovers from fallback:**
   - Gateway syncs any fallback-completed tasks to the task server DB
   - New tasks go through mesh dispatch again
   - User gets a notification: "Mesh restored — workers back online"

**Risk:** Low — this is additive, the embedded worker already exists (just reduced from 3→1). The fallback logic is new but has no effect when the mesh is healthy.

---

## 5. What Does NOT Change

These stay the same through all phases:
- Telegram interface (`src/telegram/interface.py`) — unchanged
- `CodingBackend` ABC and all backends — unchanged
- `TaskResult`, `ExecutionResult`, `Session` dataclasses — unchanged
- Session affinity rules (`create_session` any node, `resume_session` pinned) — unchanged
- `.env` file format — additive only
- Worker agent's execution logic (`_execute_task` in `src/worker/agent.py`) — unchanged

---

## 6. Rollback Plan

Each phase is independently rollbackable:

| Phase | Rollback |
|-------|----------|
| 1 (DB canonical) | Revert `SessionStore.get()` to JSON-only. JSON files were never deleted. |
| 2 (standalone server) | Revert to embedding: re-enable `embedded_server.py`, stop `ai-team-server` PM2. |
| 3 (standalone workers) | Revert gateway workers to 3 in-process, disable `ai-team-worker` PM2. |
| 4 (fallback) | Revert: set `MIN_WORKERS=3`, fallback never activates. |

The JSON file write path is never removed — it remains as the ultimate fallback state store throughout all phases.
