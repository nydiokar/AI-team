# Mesh Self-Awareness Plan

## Problem

The gateway's view of a worker is "online/offline + task claimed/done". It has no
visibility into what the worker is actually doing. If a Claude session is busy, the
gateway sees `claimed` and waits. The worker knows — it just never says.

The mesh needs a shared ledger where each node owns its slice of truth and reports it
honestly. The gateway aggregates slices into a network-wide view and acts on it.

```
Current                         Target
───────                         ──────
Worker knows: slots, sessions   Worker reports: slots, active tasks, session states
Gateway knows: online/offline   Gateway knows: rich live state per node
                                Network knows: itself
```

---

## Milestones

### M1 — Enriched Heartbeats (DONE)
**Goal:** Workers report live operational state with every heartbeat. Gateway stores
it and exposes it. No new protocol — extend the existing 30s heartbeat.

**What workers send:**
- `active_tasks: [task_id, ...]` — tasks currently executing
- `slots_used: int` — semaphore slots consumed (len of _active)
- `slots_total: int` — max_concurrent from config

**What gateway stores:**
- `live_state TEXT` column on nodes (migration 8, JSON blob)
- In-memory on NodeInfo for fast reads

**Unblocks:** dispatch logic can check slot availability, operators can see node load,
mesh health has real signal instead of just "online/offline".

---

### M2 — On-Demand Worker Pull
**Goal:** Gateway can query a worker's current state right now, not wait 30s for the
next heartbeat. Used before dispatch and during stuck-task investigation.

**What changes:**
- Worker's raw asyncio nudge server gains `GET /status` route alongside `POST /nudge`
- Returns same state snapshot as heartbeat live_state
- Gateway calls this from orchestrator before task dispatch and from mesh health checks

**Unblocks:** Gateway can make routing decisions on fresh state. Stuck task
investigation no longer requires reading 30s-stale data.

---

### M3 — Session State Reconciliation
**Goal:** Worker reports per-session backend states (busy/idle/error) with each
heartbeat. Gateway reconciles against its sessions table to detect divergence.

**What changes:**
- Worker backends expose a `get_session_states() -> dict[session_id, status]` method
- Worker includes `session_states: {session_id: busy|idle|error}` in heartbeat
- Gateway compares reported states against sessions table
- Divergence (worker says idle, gateway says busy) triggers a flag and optional
  auto-recovery (mark session error, release for reattach)

**Unblocks:** Catches stuck sessions without manual investigation. Foundation for
self-healing mesh where divergence resolves automatically.

---

### M4 — Network-Wide Dashboard
**Goal:** Full network observability from a single view. Operators and the gateway
itself can see every node's live state, session assignments, slot utilization, and
health trends.

**What changes:**
- `/nodes` endpoint enriched with live_state, session_states, slot utilization
- Telegram `/nodes` command shows rich per-node status (slots used, active tasks,
  session states, last heartbeat age)
- Aggregate mesh metrics: total slots used/available across all nodes, sessions
  per node, error rates
- Alerts on divergence patterns (node reporting idle but gateway shows 3 busy sessions)

**Unblocks:** True mesh self-awareness. Operators see the network as it sees itself.
Anomalies surface before they become incidents.

---

## Architecture Principle

Each node owns truth about itself. The gateway owns truth about the network.
Neither guesses about the other's domain.

| Truth domain       | Owner   | Mechanism                        |
|--------------------|---------|----------------------------------|
| Session busy/idle  | Worker  | Heartbeat (push) + /status (pull)|
| Slot utilization   | Worker  | Heartbeat live_state             |
| Task queue         | Gateway | mesh_tasks table                 |
| Node routing       | Gateway | NodeRegistry + live_state        |
| Network health     | Gateway | Aggregated from all node slices  |
