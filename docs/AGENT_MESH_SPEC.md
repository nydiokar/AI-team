# Agent Mesh Architecture — Specification v1.0

> **Status:** Design-complete. Approved for incremental implementation.  
> **Last updated:** 2026-06-04

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
       │  Tailscale mesh (private network, trusted layer)
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
- Worker nodes only connect **outward** to the VPS — no inbound ports required on personal machines.
- Tailscale is the trust boundary. Authentication inside the mesh relies on Tailscale identity as the primary layer; tokens are a secondary hardening measure, not the primary gate.
- Session state is canonical on the VPS. Workers receive everything they need in the dispatch payload.
- The existing `CodingBackend` interface, `Session`, `TaskResult`, and orchestrator abstractions are preserved and extended, not replaced.

---

## 3. Trust and Security Model

### 3.1 Tailscale as the Primary Trust Boundary

Tailscale membership is the primary security perimeter. A node that is not enrolled in the Tailscale network cannot reach any component of the mesh. ACLs enforce:

- Workers can reach the VPS task server port.
- VPS can push tasks to worker agent ports (for hybrid push/pull).
- Nothing is exposed on public interfaces — all mesh components bind to the Tailscale IP only.

This means: if you trust your Tailscale ACL, you trust the node. We do not need per-command HMAC signing, certificate chains, or elaborate capability matrices.

### 3.2 Token as Secondary Layer

A shared `WORKER_TOKEN` (long random secret, per-network not per-node) is required on all task server requests. This prevents accidental misuse (e.g., a mis-configured curl hitting the port) but is not the primary security mechanism. Tailscale is.

Per-node tokens are a future hardening step if the network grows to include untrusted contributors. Not needed now.

### 3.3 Command Security

The allowed task actions map directly to the existing `CodingBackend` interface methods:

| Action | Description |
|--------|-------------|
| `create_session` | Start a new agent session on a capable node |
| `resume_session` | Continue existing session (affinity-pinned to originating node) |
| `run_oneoff` | Stateless single-turn task, any capable node |
| `cancel` | Cancel a running task |
| `compact_session` | Compact context window on existing session |

No raw shell. No eval. No arbitrary subprocess. If a new action type is needed, it is added to the `CodingBackend` ABC first, then to the allowed action list — never the reverse.

---

## 4. Node Capabilities Model

Node capabilities are declared by the worker daemon at registration time and are intentionally coarse. The only meaningful capability is **which backends are installed and runnable on this node**. Fine-grained labels like "has browser" or "has GPU" are deliberately omitted — they would require maintaining a capability taxonomy that fights against the general-purpose nature of Claude, OpenCode, and Codex.

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

Routing logic uses only `backends` and `max_concurrent`. Everything else the agent can or cannot do is the agent's own business — the mesh does not gatekeep it.

---

## 5. Session Affinity

Sessions in the current system carry `backend_session_id` (native CLI session state) stored locally on whichever machine created the session. This state cannot be migrated. Therefore:

- `create_session` → dispatched to any node that advertises the required backend, least-loaded wins.
- `resume_session` → **must** go to the same node that owns `session.machine_id`. If that node is offline, the task fails immediately with a clear message. It does not queue and wait indefinitely.
- `run_oneoff` → any capable node, no affinity.

Session state (the `Session` object) is canonical on the VPS. The VPS serializes the full session into the dispatch payload. Workers do not maintain their own session store — they receive what they need, execute, and return the updated `ExecutionResult`.

---

## 6. Offline Node Behavior

When a session-pinned node is offline:

1. Task fails immediately.
2. Telegram message: `"Node {node_id} is offline. Task not queued. Reply /task_retry when the node is back."`
3. The task is persisted in the DB with `status=failed_node_offline`.
4. When that node comes back online (heartbeat received), a single Telegram notification is sent: `"Node {node_id} is back online. You have 1 pending task — use /retry {task_id} to requeue it."` This notification is only sent if there are pending retryable tasks for that node, not on every reconnect.

For session-less (`run_oneoff`) tasks: if no capable node is available, the task queues with a 10-minute timeout, then fails with notification if still unserviced.

No indefinite silent queuing.

---

## 7. Push vs. Pull — Hybrid Model

Workers primarily **poll** the VPS task server for work. Poll interval: 5 seconds when idle, backs off to 30 seconds after 5 consecutive empty polls, resets to 5 seconds when a task arrives.

The VPS **may also push** a task notification to the worker's local API port (if the worker is reachable via Tailscale). The push is a lightweight nudge: `POST /nudge` — it contains no task payload, just a signal to poll immediately. This eliminates the poll delay on interactive sessions without requiring workers to expose a full inbound API surface.

If the push fails (worker unreachable on its port), the VPS falls back silently to the worker picking it up on the next poll. No error, no retry — polls are the safety net.

This model:
- Works behind all NAT configurations (poll is always outbound).
- Has sub-second task pickup latency when both sides are reachable (nudge).
- Requires workers to bind a minimal HTTP endpoint to their Tailscale IP.

---

## 8. Telegram Notifications for Nodes

No automatic node lifecycle notifications. The mesh is silent by default about node state.

Commands that return node state on demand:

| Command | Response |
|---------|----------|
| `/nodes` | List of registered nodes, status, last seen |
| `/node {id}` | Detail for one node (status, backends, active tasks) |

The one exception is the "pending task" notification when an offline node reconnects (Section 6). This is task-driven, not lifecycle-driven.

---

## 9. Component Overview

### 9.1 VPS — New Components

**`src/control/task_server.py`** — FastAPI app, bound to `{VPS_TAILSCALE_IP}:9002`

Endpoints (all require `Authorization: Bearer {WORKER_TOKEN}`):
- `POST /nodes/register` — worker startup
- `POST /nodes/heartbeat` — keepalive (every 30s)
- `POST /nodes/deregister` — clean shutdown
- `GET /nodes` — list all nodes and status
- `POST /tasks/{task_id}/claim` — worker claims a task (optimistic lock)
- `GET /tasks/pending` — worker polls for claimable tasks
- `POST /tasks/{task_id}/result` — worker submits result
- `POST /nodes/{node_id}/nudge` — VPS nudges a worker to poll (internal, VPS-only)

**`src/control/node_registry.py`** — In-memory `{node_id → NodeInfo}`. Marks nodes offline after 90s with no heartbeat. Ephemeral — survives VPS restarts via the task DB (nodes re-register on their next poll cycle).

**`src/control/task_db.py`** — SQLite-backed task persistence for distributed tasks.

```sql
CREATE TABLE mesh_tasks (
    id           TEXT PRIMARY KEY,
    session_id   TEXT,
    machine_id   TEXT,          -- NULL = any capable node
    backend      TEXT,
    action       TEXT,          -- create_session | resume_session | run_oneoff | cancel
    payload      TEXT,          -- JSON: full Session + prompt, serialized by VPS
    status       TEXT DEFAULT 'pending',
    claimed_by   TEXT,
    claimed_at   TEXT,
    result       TEXT,          -- JSON: ExecutionResult, written by worker
    created_at   TEXT,
    updated_at   TEXT
);
```

### 9.2 Worker Node — New Components

**`src/worker/agent.py`** — Persistent daemon, one per participating machine.

Responsibilities:
- Register with VPS on startup
- Poll `GET /tasks/pending?node_id={self}&backends={list}` every 5–30s
- Accept `POST /nudge` on local Tailscale IP to trigger immediate poll
- Claim a matching task via `POST /tasks/{id}/claim`
- Instantiate the appropriate local backend (existing `ClaudeCodeBackend`, etc.)
- Execute using session payload from task record
- POST result back
- Send heartbeats every 30s
- Deregister on clean shutdown (SIGTERM handler)

**`src/worker/config.py`** — Worker env vars:

| Env var | Description |
|---------|-------------|
| `WORKER_NODE_ID` | Unique identifier for this machine |
| `WORKER_TOKEN` | Shared mesh auth token |
| `WORKER_TAILSCALE_IP` | This node's Tailscale IP |
| `WORKER_API_PORT` | Local port for nudge endpoint (default: 9001) |
| `WORKER_MAX_CONCURRENT` | Max parallel tasks (default: 2) |
| `CONTROLLER_URL` | VPS task server base URL |
| `WORKER_BACKENDS` | Comma-separated list of available backends |

### 9.3 VPS — Modified Components

**`src/orchestrator.py`** — `TaskOrchestrator._task_worker` extended:

```python
# Pseudocode — session-affine dispatch
if session and session.machine_id:
    node = node_registry.get(session.machine_id)
    if not node or node.status != "online":
        raise TaskError(f"Node {session.machine_id} is offline")
    dispatch_to_node(task, node, session)
else:
    node = node_registry.pick_capable(backend=task_backend)
    if not node:
        raise TaskError("No capable node available")
    dispatch_to_node(task, node, session)
```

The `Session` object is serialized into the task payload by the VPS before dispatch. Workers return the updated `backend_session_id` in `ExecutionResult`, which the VPS writes back to session state. Workers never own session state.

**`src/core/interfaces.py`** — No changes to existing interfaces. A new `IWorkerAgent` ABC may be added for the worker daemon, but it does not modify existing ABCs.

---

## 10. Migration Path — Phased Build

### Phase 0 — Network Layer (no code, prerequisite)

- [ ] Enroll VPS in Tailscale. Note its Tailscale IP.
- [ ] Enroll main PC. Note its Tailscale IP.
- [ ] Set Tailscale ACL: VPS port 9002 reachable from workers; worker port 9001 reachable from VPS. Nothing else.
- [ ] Generate `WORKER_TOKEN`: `openssl rand -hex 32`
- [ ] Validate: `curl http://{tailscale-ip-vps}:9002/health` from main PC (should fail — nothing running yet, but no network timeout).

### Phase 1 — Worker Agent (main PC side)

Build `src/worker/agent.py` and `src/worker/config.py`. The worker at this stage:
- Registers with a stub controller (can be a simple FastAPI echo server for testing).
- Instantiates local backends and runs a `run_oneoff` task received over HTTP.
- Returns `ExecutionResult`.

Validation: send a task directly to the worker's `/tasks/execute` endpoint from a curl command on the main PC. Confirm it runs Claude and returns output.

No VPS involvement yet.

### Phase 2 — Task Server (VPS side)

Build `src/control/task_server.py`, `node_registry.py`, `task_db.py`. Deploy on VPS bound to Tailscale IP.

At this stage the VPS task server is live but the main orchestrator still ignores it. Workers poll it and receive nothing — that is correct.

Validation:
- Worker starts on main PC, registers with VPS.
- `GET /nodes` from VPS shows main PC as online.
- Post a task manually to `task_db` via SQLite CLI. Worker picks it up within 5s, executes, posts result.

### Phase 3 — Orchestrator Integration (VPS side)

Modify `TaskOrchestrator` to check `node_registry` and dispatch to the task server instead of running locally when a node is registered.

**Critical:** during this phase the orchestrator must fall back to local execution if the node registry is empty or the target backend is available locally. This ensures zero regression if no workers are registered.

Fallback logic:

```python
if node_registry.is_empty() or (session.machine_id == local_node_id):
    # run locally as today
else:
    # dispatch to mesh
```

Validation: with worker running on main PC, send a task via Telegram. Confirm it executes on main PC and result arrives back in Telegram.

### Phase 4 — Migrate Gateway to VPS

Move the running gateway process from main PC to VPS. Main PC becomes worker-only. This is the phase where things can break — do it with main PC physically accessible.

Steps:
1. Copy `.env` with Telegram credentials to VPS.
2. Start gateway on VPS (Telegram bot + orchestrator).
3. Start worker daemon on main PC.
4. Stop gateway on main PC.
5. Confirm Telegram commands reach VPS and tasks execute on main PC.

### Phase 5 — Hardening

- Heartbeat timeout detection with pending-task notifications (Section 6).
- `/nodes` and `/node {id}` Telegram commands.
- Nudge endpoint on workers + VPS push on task arrival.
- Per-node concurrency enforcement.
- Worker SIGTERM handler for clean deregistration.
- Structured worker logs with `node_id` field.

### Phase 6 — Additional Nodes

Adding a new node requires:
1. Install Tailscale, enroll in network, add to ACL.
2. Install required backends (claude, opencode, etc.).
3. Copy `.env` with `WORKER_NODE_ID`, `WORKER_TOKEN`, `CONTROLLER_URL`, `WORKER_TAILSCALE_IP`, `WORKER_BACKENDS`.
4. Start worker daemon.

No VPS changes. No orchestrator changes. Node appears in `/nodes` within 30 seconds.

---

## 11. Open Questions Resolved

| Question | Decision |
|----------|----------|
| Push vs. pull | Hybrid: poll primary, nudge for latency |
| Node offline + session task | Fail immediately, notify, persist for manual retry |
| Offline node comes back | Notify only if pending retryable tasks exist |
| Automatic node lifecycle notifications | Off by default; on-demand via `/nodes` |
| Capability schema | Backend list only (`["claude","opencode","codex"]`); no fine-grained labels |
| Trust model | Tailscale-first; shared token as secondary layer |
| Session state ownership | VPS canonical; serialized into dispatch payload |
| SQLite vs. Postgres | SQLite until >5 nodes or observed contention |
| Per-node tokens | Future hardening; not needed now |
| Command signing | Future hardening; not needed now |

---

## 12. What Is Not in Scope

- Kubernetes, Docker Swarm, or any container orchestration.
- Distributed consensus (Raft, etcd).
- Pub/sub message bus (NATS, RabbitMQ, Kafka).
- Agent-to-agent direct communication — all coordination goes through the VPS.
- Capability labels beyond backend availability.
- Any GUI or web dashboard.
- Multi-user access control (the existing Telegram `allowed_users` filter is sufficient).

These may become relevant if the mesh grows beyond ~10 nodes or task throughput exceeds what SQLite can handle. They are not needed before that point.

---

## 13. Files To Create / Modify Summary

### New files
| File | Purpose |
|------|---------|
| `src/worker/__init__.py` | Package marker |
| `src/worker/agent.py` | Worker daemon — poll, claim, execute, report |
| `src/worker/config.py` | Worker env var config |
| `src/control/__init__.py` | Package marker |
| `src/control/task_server.py` | FastAPI task server (VPS, Tailscale-bound) |
| `src/control/node_registry.py` | In-memory node registry with heartbeat tracking |
| `src/control/task_db.py` | SQLite mesh task queue |

### Modified files
| File | Change |
|------|--------|
| `src/orchestrator.py` | Dispatch logic: check node registry, route to mesh or local |
| `src/core/interfaces.py` | Add `IWorkerAgent` ABC (additive, no changes to existing) |
| `config/settings.py` | Add `MeshConfig` dataclass for VPS-side mesh settings |
| `ecosystem.config.js` | Add worker daemon process entry |
| `.env.example` | Add worker and mesh env vars |

### Unchanged
Everything in `src/backends/`, `src/telegram/`, `src/bridges/`, `src/validation/`, `src/core/` (except interfaces.py additive change). The backends run on workers exactly as they run today.
