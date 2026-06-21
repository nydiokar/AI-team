# Agent Mesh Architecture — Specification v1.1

> **Status:** Design-complete. Approved for incremental implementation.  
> **Last updated:** 2026-06-04  
> **Changelog v1.1:** Corrected session serialization assumption; clarified backend session locality; added SQLite WAL requirement; hardened machine_id migration note; aligned with verified 2025 industry patterns.

---

## 1. Problem Statement

The gateway currently runs on the main PC. If that machine is unreachable or the session is killed, nothing works. The goal is to move the control plane to a VPS that is always on, while personal machines (main PC, laptops, other nodes) act as general-purpose worker nodes that join and leave the mesh voluntarily. The user's phone never touches any machine directly — only Telegram.

This must be achievable without disrupting the existing single-machine workflow during the transition.

---

## 2. Desired End State

```
[Telegram / Phone]
       │
       ▼
[VPS — always on]
  ├── Telegram bot (existing interface.py, unchanged)
  ├── TaskOrchestrator (extended, not replaced)
  ├── Central task DB (SQLite → Postgres when needed)
  ├── Node registry (which workers are alive, capabilities)
  └── Task server API (FastAPI, bound to Tailscale IP only)
       │
       │  Tailscale mesh (private network, primary trust layer)
       │
  ┌────┴──────────────┬──────────────────┐
  ▼                   ▼                  ▼
[Main PC]         [Laptop]          [Pi / other]
  WorkerAgent       WorkerAgent       WorkerAgent
  (persistent       (persistent       (persistent
   daemon)           daemon)           daemon)
  local backends    local backends    local backends
```

Key properties:
- VPS is the single entry point and the orchestration brain.
- Worker nodes only connect **outward** to the VPS task server — no inbound ports required (pull model is the baseline; push nudge is an optimization).
- Tailscale is the primary trust boundary. Shared token is a secondary hardening layer.
- Session state (the `Session` object) is canonical on the VPS. **Backend session state (`backend_session_id`) is local to the worker machine and cannot be migrated.** This constraint shapes all session routing decisions.
- Existing `CodingBackend`, `Session`, `TaskResult`, and orchestrator abstractions are preserved and extended, not replaced.

---

## 3. Hard Constraints From the Existing Codebase

These are not design preferences — they are facts about the current code that cannot be assumed away.

### 3.1 Backend Sessions Are Machine-Local

Claude Code stores its session state in `~/.claude/` on the machine where it runs. Codex is equivalent. `backend_session_id` is a handle into that local state. **You cannot resume a Claude Code session on a different machine by sending the `backend_session_id` over the network.** It will fail with "no conversation found with session id."

The only exception is OpenCode in server mode, which persists sessions in a local SQLite database and theoretically supports cross-machine resume if that database is accessible remotely — but this is not a path we build toward in this spec. Keep it as a future note.

**Consequence:** Session affinity (Section 5) is not a routing preference — it is a hard correctness requirement. A session tied to main-pc cannot be executed on any other node regardless of what capabilities that node advertises.

### 3.2 `machine_id` Is `socket.gethostname()`

In `src/core/session_store.py`, `machine_id` is set to `socket.gethostname()` at session creation time. This means sessions created while the gateway runs on the main PC carry the main PC's hostname as `machine_id`. Sessions created after migration to the VPS will carry the VPS hostname unless we intervene.

**Migration note (Phase 4):** Before migrating the gateway to the VPS, run a one-time script to tag existing sessions with a stable `WORKER_NODE_ID` matching the main PC's worker registration. The `machine_id` field in session JSON is writable — this is a one-time data migration, not a code change.

### 3.3 `SessionStore` Path Is Project-Root-Relative

`session_store.py` anchors `_PROJECT_ROOT` three levels up from `src/core/session_store.py`. Sessions are written to `{project_root}/state/sessions/`. On the VPS this resolves correctly as long as the repo is cloned to the same relative path. The VPS canonical session store and the worker's local copy must not be on the same filesystem — workers receive session data in the dispatch payload (read-only), the VPS owns writes.

### 3.4 SQLite WAL Mode Required for Concurrent Workers

The `mesh_tasks` table will have multiple workers polling and claiming simultaneously. SQLite's default journal mode serializes writes and will produce `database is locked` errors under concurrent load. **WAL (Write-Ahead Logging) mode must be enabled on first connection:**

```python
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA busy_timeout=5000;")
```

This is not optional.

---

## 4. Trust and Security Model

### 4.1 Tailscale as the Primary Trust Boundary

Tailscale membership is the primary security perimeter. A node not enrolled in the Tailscale network cannot reach any mesh component. ACLs enforce:

- Workers can reach the VPS task server port (9002).
- VPS can reach worker agent ports (9001) for nudge delivery.
- All mesh components bind to Tailscale IPs only — never `0.0.0.0`.

This is validated by current industry practice. Tailscale with ACLs is the established pattern for private agent-to-agent meshes (WireGuard-level encryption + zero-trust ACL enforcement, no coordination server required at enforcement time). If you trust your Tailscale ACL, you trust the node.

### 4.2 Token as Secondary Layer

A shared `WORKER_TOKEN` (long random secret, per-network not per-node) is required on all task server requests. This prevents accidental access (misconfigured curl, port scan that somehow reached the Tailscale IP) but is not the primary security mechanism. Tailscale is.

Per-node tokens are a future hardening step. Not needed now.

### 4.3 Command Security

Allowed task actions map directly to the existing `CodingBackend` interface methods. Nothing outside this list is accepted:

| Action | CodingBackend method |
|--------|----------------------|
| `create_session` | `create_session(session)` |
| `resume_session` | `resume_session(session, message)` |
| `run_oneoff` | `run_oneoff(cwd, message)` |
| `cancel` | `cancel(session)` |
| `compact_session` | `compact_session(session)` |

No raw shell. No eval. No arbitrary subprocess. Adding a new action requires adding it to `CodingBackend` ABC first, then to the allowed list — never ad hoc.

---

## 5. Node Capabilities

Node capabilities are declared at registration and are intentionally minimal. The routing decision is: **does this node have the required backend installed and runnable?** That is all.

Fine-grained labels (GPU, browser, ollama, etc.) are explicitly rejected. Claude Code, OpenCode, and Codex are general-purpose agents — attaching capability labels would create a gatekeeping taxonomy that fights their nature and requires constant maintenance as tools evolve.

```json
{
  "node_id": "main-pc",
  "tailscale_ip": "100.x.x.x",
  "api_port": 9001,
  "capabilities": {
    "backends": ["claude", "opencode", "opencode-server", "codex"],
    "max_concurrent": 2
  },
  "status": "online",
  "last_heartbeat": "2026-06-04T12:00:00Z"
}
```

Routing uses only `backends` (does this node have it?) and `max_concurrent` (is it under load?).

---

## 6. Session Affinity

- `create_session` → dispatched to any node that advertises the required backend; least-loaded by active task count wins. VPS writes `machine_id = node_id` to the session immediately after dispatch.
- `resume_session` → **must** route to the node matching `session.machine_id`. If that node is offline: fail immediately (Section 7). No exceptions.
- `run_oneoff` → any capable node, no affinity, no state written to session.

Session object (the `Session` dataclass) is read from the VPS store and included in the dispatch payload. Worker receives it as input, executes, returns `ExecutionResult`. VPS applies the result back to session state — specifically `backend_session_id`, `status`, `last_task_id`, `last_files_modified`. Worker never writes session state directly.

---

## 7. Offline Node Behavior

**Session-pinned task, node offline:**
1. Fail immediately. Do not queue.
2. Telegram message: `"Node {node_id} is offline. Task not sent. Use /retry {task_id} when it's back."`
3. Task persisted in DB with `status=failed_node_offline` and full payload retained for retry.
4. When that node reconnects (heartbeat received), a single Telegram notification fires **only if** there are tasks in `failed_node_offline` state for that node: `"Node {node_id} is back. 1 task waiting — /retry {task_id}"`

**Session-less task (`run_oneoff`), no capable node available:**
1. Queue with 10-minute timeout.
2. If still unserviced after timeout: fail with `"No capable node came online within 10 minutes."`

**No indefinite silent queuing.**

---

## 8. Push vs. Pull — Hybrid Model

**Baseline:** workers poll. Poll interval starts at 5 seconds, backs off to 30 seconds after 5 consecutive empty polls, resets to 5 seconds when a task arrives. This works behind all NAT configurations and survives any network hiccup without coordination.

**Optimization:** VPS sends a lightweight `POST /nudge` to the worker's Tailscale IP:port when a new task is queued for it. The nudge payload contains no task data — only `{"task_queued": true}`. Worker triggers an immediate poll on receipt. If nudge delivery fails, the worker picks it up on the next poll cycle. No retry on nudge failure.

This gives sub-second task pickup when both sides are reachable, with poll as the always-correct safety net.

Workers must bind a minimal HTTP listener on their Tailscale IP to receive nudges. This is the only inbound port required on worker machines.

---

## 9. Telegram Node Commands

No automatic node lifecycle notifications. The mesh is silent about node state unless asked.

| Command | Response |
|---------|----------|
| `/nodes` | List of registered nodes, status, last seen, backend list |
| `/node {id}` | Detail: status, backends, active task count, last heartbeat |

The one proactive notification is the pending-task nudge when an offline node reconnects (Section 7). This is task-driven, not lifecycle-driven, and only fires when there is actually something actionable for the user.

---

## 10. Component Overview

### 10.1 VPS — New Components

**`src/control/task_server.py`** — FastAPI app, bound to `{VPS_TAILSCALE_IP}:9002`

All endpoints require `Authorization: Bearer {WORKER_TOKEN}`.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/nodes/register` | Worker startup — accepts NodeInfo payload |
| POST | `/nodes/heartbeat` | Keepalive (every 30s) |
| POST | `/nodes/deregister` | Clean shutdown |
| GET | `/nodes` | List all nodes |
| GET | `/tasks/pending` | Worker polls; filters by `node_id` and `backends` query params |
| POST | `/tasks/{id}/claim` | Worker claims task (optimistic lock) |
| POST | `/tasks/{id}/result` | Worker submits `ExecutionResult` |
| POST | `/nodes/{id}/nudge` | VPS pushes nudge to worker (internal use only, not worker-facing) |

**`src/control/node_registry.py`** — In-memory `{node_id → NodeInfo}` dict.

- Marks nodes offline after 90s with no heartbeat.
- On node reconnect: checks `mesh_tasks` for `failed_node_offline` tasks and triggers Telegram notification if any exist.
- Ephemeral: nodes re-register on their next heartbeat cycle after VPS restart. No DB persistence needed — the task DB persists the tasks; node state is reconstructed from live heartbeats.

**`src/control/task_db.py`** — SQLite-backed task queue. WAL mode mandatory.

```sql
CREATE TABLE mesh_tasks (
    id           TEXT PRIMARY KEY,
    session_id   TEXT,
    machine_id   TEXT,          -- NULL = any capable node
    backend      TEXT,
    action       TEXT,          -- create_session|resume_session|run_oneoff|cancel|compact_session
    payload      TEXT,          -- JSON: Session (full) + prompt string
    status       TEXT DEFAULT 'pending',
    claimed_by   TEXT,          -- node_id that claimed this task
    claimed_at   TEXT,
    result       TEXT,          -- JSON: ExecutionResult, written on completion
    created_at   TEXT,
    updated_at   TEXT
);

-- Index for the common worker poll query
CREATE INDEX idx_mesh_tasks_status_machine
    ON mesh_tasks(status, machine_id);
```

### 10.2 Worker Node — New Components

**`src/worker/agent.py`** — Persistent daemon, one per participating machine. Managed by PM2 (added to `ecosystem.config.js`).

Lifecycle:
1. Read config from env.
2. Register with VPS: `POST /nodes/register` with NodeInfo.
3. Start nudge listener on `{WORKER_TAILSCALE_IP}:{WORKER_API_PORT}`.
4. Enter poll loop: `GET /tasks/pending?node_id=X&backends=claude,opencode`.
5. On task received: `POST /tasks/{id}/claim`, instantiate backend, execute, `POST /tasks/{id}/result`.
6. Continue heartbeats (30s interval) concurrently with execution.
7. On SIGTERM: `POST /nodes/deregister`, drain active tasks (best-effort, up to 30s), exit.

Concurrency: up to `WORKER_MAX_CONCURRENT` tasks run in parallel via asyncio. Each claims its own task row independently.

**`src/worker/config.py`** — Worker env vars:

| Env var | Purpose | Default |
|---------|---------|---------|
| `WORKER_NODE_ID` | Stable identifier for this machine | required |
| `WORKER_TOKEN` | Shared mesh auth token | required |
| `WORKER_TAILSCALE_IP` | This node's Tailscale IP | required |
| `WORKER_API_PORT` | Nudge listener port | `9001` |
| `WORKER_MAX_CONCURRENT` | Max parallel tasks | `2` |
| `CONTROLLER_URL` | VPS task server base URL | required |
| `WORKER_BACKENDS` | Comma-separated available backends | required |

### 10.3 VPS — Modified Components

**`src/orchestrator.py`**

`_task_worker` extended with mesh dispatch. Local execution is the fallback when no workers are registered, ensuring zero regression during migration:

```python
async def _dispatch_or_run_local(self, task, session, backend_name):
    if node_registry.is_empty():
        # Pre-mesh: run locally as today
        return await self._run_backend_local(task, session, backend_name)

    if session and session.machine_id:
        node = node_registry.get(session.machine_id)
        if not node or node.status != "online":
            raise TaskError(f"Node {session.machine_id} is offline")
    else:
        node = node_registry.pick_capable(backend=backend_name)
        if not node:
            raise TaskError("No capable node available")

    return await self._dispatch_to_node(task, session, node)
```

**`config/settings.py`** — New `MeshConfig` dataclass:

```python
@dataclass
class MeshConfig:
    enabled: bool = False                    # MESH_ENABLED env var
    tailscale_ip: str = ""                   # MESH_TAILSCALE_IP
    task_server_port: int = 9002             # MESH_TASK_SERVER_PORT
    worker_token: str = ""                   # WORKER_TOKEN
    node_heartbeat_timeout_sec: int = 90     # MESH_HEARTBEAT_TIMEOUT_SEC
    oneoff_queue_timeout_sec: int = 600      # MESH_ONEOFF_QUEUE_TIMEOUT_SEC
```

**`src/core/interfaces.py`** — Additive only. New `IWorkerAgent` ABC. Zero changes to existing interfaces.

**`ecosystem.config.js`** — New entry for the worker daemon process.

**`.env.example`** — Document all new env vars.

---

## 11. What Is Not Migrated

These components are **not** part of the mesh and remain entirely on each machine that runs them:

- `~/.claude/` — Claude Code's native session storage. Never touches the network.
- OpenCode's local SQLite session DB. Same.
- `state/sessions/` on the VPS — canonical session metadata (the `Session` object). Workers receive a read-only copy in the dispatch payload and never write to it directly.
- `results/` and `summaries/` artifact directories — written by the orchestrator on the VPS after receiving `ExecutionResult` from workers.

---

## 12. OpenCode Cross-Machine Sessions — Future Path

OpenCode stores sessions in a local SQLite database. If that database is mounted on a shared volume (NFS, JuiceFS, or similar), sessions *could* resume on any node that has access to the same DB file. This would eliminate the session-affinity hard requirement for OpenCode sessions specifically.

This is not built in this spec. It is noted because: (a) OpenCode is the only backend where this is architecturally possible, (b) it becomes relevant if you want to run the same session across e.g. main PC and laptop without caring which one picks it up. If pursued, it is an OpenCode-specific extension, not a general mesh change.

---

## 13. Migration Path — Phased Build

### Phase 0 — Network Layer (no code, prerequisite)

- [ ] Enroll VPS in Tailscale. Record its Tailscale IP.
- [ ] Enroll main PC. Record its Tailscale IP.
- [ ] Set Tailscale ACL: VPS port 9002 reachable from main PC; main PC port 9001 reachable from VPS.
- [ ] Generate `WORKER_TOKEN`: `openssl rand -hex 32`
- [ ] Validate connectivity: `curl -H "Authorization: Bearer $WORKER_TOKEN" http://{vps-tailscale-ip}:9002/health` from main PC. Should get a connection refused (nothing running yet) not a timeout — confirms routing works.

### Phase 1 — Worker Agent

Build `src/worker/agent.py` and `src/worker/config.py`. At this stage the worker can:
- Register with a stub controller (a 10-line FastAPI echo server).
- Instantiate local backends and execute a `run_oneoff` task received via HTTP.
- Return `ExecutionResult` as JSON.

**Validation:** curl a `run_oneoff` task directly to the worker's nudge port. Confirm it runs the local Claude backend and returns output. No VPS involvement.

### Phase 2 — Task Server (VPS)

Build `src/control/task_server.py`, `node_registry.py`, `task_db.py`. Deploy on VPS, bound to Tailscale IP. Enable WAL mode in `task_db.py` initialization.

At this stage the orchestrator still ignores the mesh. Workers poll and get nothing.

**Validation:**
- Worker starts on main PC, POSTs registration.
- `GET /nodes` from VPS returns main PC as online.
- Manually INSERT a row into `mesh_tasks` via SQLite CLI.
- Worker picks it up within the poll interval, executes, POSTs result.
- Confirm result row is written in DB.

### Phase 3 — Orchestrator Integration

Modify `TaskOrchestrator` with `_dispatch_or_run_local`. `MESH_ENABLED=false` by default — orchestrator behaves identically to today. Set `MESH_ENABLED=true` to activate routing.

**Validation:** Set `MESH_ENABLED=true` with worker running on main PC. Send a task via Telegram. Confirm it routes through the VPS task DB to the main PC worker and result arrives back in Telegram. Single-machine fallback (no worker) still works with `MESH_ENABLED=false`.

### Phase 4 — Migrate Gateway to VPS

This is the operationally risky phase. Do it with main PC accessible.

**Pre-migration:**
- Run the `machine_id` fix script: update existing sessions' `machine_id` from the VPS hostname (or empty) to `WORKER_NODE_ID` of the main PC worker.
- Confirm all active session `machine_id` values are set to the main PC's `WORKER_NODE_ID`.

**Migration steps:**
1. Clone repo to VPS, configure `.env` with Telegram credentials.
2. Start gateway on VPS (`python main.py` or PM2).
3. Start worker daemon on main PC.
4. Send a test task via Telegram. Confirm end-to-end.
5. Stop gateway on main PC.
6. Monitor for 30 minutes. Check `logs/orchestrator.log` on VPS for errors.

**Rollback:** if anything breaks, stop VPS gateway, restart main PC gateway. Sessions are in `state/sessions/` — copy them between machines if needed.

### Phase 5 — Hardening

- Heartbeat timeout detection with pending-task notifications (Section 7).
- `/nodes` and `/node {id}` Telegram commands.
- Per-node concurrency enforcement in worker claim logic.
- Worker SIGTERM handler: deregister, drain active tasks within 30s.
- Structured worker logs with `node_id` field in every log line.
- `MESH_ENABLED` flag wired to `MeshConfig`.

### Phase 6 — Additional Nodes

Adding a node:
1. Enroll in Tailscale, add to ACL.
2. Install required backends.
3. Create `.env` with `WORKER_NODE_ID`, `WORKER_TOKEN`, `CONTROLLER_URL`, `WORKER_TAILSCALE_IP`, `WORKER_BACKENDS`.
4. Start worker daemon (PM2 or systemd).

No VPS changes. Node appears in `/nodes` within 30 seconds.

---

## 14. Future Phases (Do Not Build Yet)

| Phase | Trigger to build |
|-------|-----------------|
| Postgres | >5 nodes or observed SQLite write contention |
| Per-node JWT tokens | Untrusted contributors added to mesh |
| Command signing (HMAC) | Security audit requirement |
| Redis Streams / NATS | Poll latency becomes a real problem (unlikely under 10 nodes) |
| OpenCode shared session DB | Want session portability across nodes for OpenCode backend specifically |
| Web dashboard | More than one operator managing the mesh |

---

## 15. Files To Create / Modify

### New files

| File | Purpose |
|------|---------|
| `src/worker/__init__.py` | Package marker |
| `src/worker/agent.py` | Worker daemon — poll, claim, execute, report |
| `src/worker/config.py` | Worker env var config |
| `src/control/__init__.py` | Package marker |
| `src/control/task_server.py` | FastAPI task server (VPS-side, Tailscale-bound) |
| `src/control/node_registry.py` | In-memory node registry with heartbeat tracking |
| `src/control/task_db.py` | SQLite mesh task queue (WAL mode required) |
| `scripts/fix_session_machine_ids.py` | One-time migration: update machine_id before Phase 4 |

### Modified files

| File | Change |
|------|--------|
| `src/orchestrator.py` | `_dispatch_or_run_local`: mesh routing with local fallback |
| `src/core/interfaces.py` | Add `IWorkerAgent` ABC (additive only) |
| `config/settings.py` | Add `MeshConfig` dataclass |
| `ecosystem.config.js` | Add worker daemon entry |
| `.env.example` | Document new mesh and worker env vars |

### Unchanged

Everything in `src/backends/`, `src/telegram/`, `src/bridges/`, `src/validation/`, `src/core/` (except additive interfaces.py change). The backends are instantiated on workers exactly as they are today on the single machine.

---

## 16. What Is Out of Scope

- Kubernetes, Docker Swarm, container orchestration.
- Distributed consensus (Raft, etcd).
- Pub/sub message bus (NATS, RabbitMQ, Kafka) — overkill until >10 nodes.
- Agent-to-agent direct communication — all coordination through VPS.
- Capability labels beyond backend list.
- GUI or web dashboard.
- Multi-user access control (existing Telegram `allowed_users` filter is sufficient).
- MCP server integration (relevant if you want to expose agent capabilities as MCP tools in the future — noted, not in scope).
