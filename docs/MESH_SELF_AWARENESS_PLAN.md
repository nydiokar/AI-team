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
Worker knows: slots, tasks      Worker reports: slots and active task ids
Gateway knows: online/offline   Gateway knows: rich live state per node
                                Network knows: itself
```

---

## Milestones

### M1 — Enriched Heartbeats (DONE)
**Goal:** Workers report live operational state with every heartbeat. Gateway stores
it and exposes it. No new protocol — extend the existing 30s heartbeat.

**What workers send:**
- `active_tasks: [task_id, ...]` — tasks currently queued or executing in the worker process
- `slots_used: int` — semaphore slots consumed by tasks that have entered execution
- `slots_total: int` — max_concurrent from config

**What gateway stores:**
- `live_state TEXT` column on nodes (migration 8, JSON blob)
- In-memory on NodeInfo for fast reads

**Unblocks:** dispatch logic can check slot availability, operators can see node load,
mesh health has real signal instead of just "online/offline".

---

### M2 — On-Demand Worker Pull
**Goal:** Gateway can obtain a worker's current state right now, not wait 30s for the
next heartbeat. Used before dispatch and during stuck-task investigation.

**What changes:**
- On nudge receipt, worker immediately sends a heartbeat (in addition to waking the
  poll loop). Gateway gets fresh live_state within ~1s instead of up to 30s.
- Gateway orchestrator can send a nudge before dispatch and wait for a newer
  `live_state_updated_at` before making freshness-sensitive routing decisions.

**What does NOT change:**
- The nudge server remains a minimal raw asyncio socket — no GET /status route.
  Adding HTTP routing to a hand-rolled 512-byte server is fragile. The nudge →
  immediate-heartbeat pattern achieves the same freshness without a new protocol.

**Unblocks:** Gateway can make routing decisions on near-fresh state. Stuck task
investigation no longer requires reading 30s-stale data.

---

### M3 — Session State Reconciliation
**Goal:** Detect divergence between what the gateway thinks a session's state is and
what the task record says. Catch stuck sessions without manual investigation.

**Design note:** Workers do NOT report session states. Session state (busy/idle/error)
is owned by the gateway's `sessions` table. Workers only know which tasks they are
currently executing — they have no persistent view of session state between tasks.
Asking workers to report session states would invert the ownership model and create a
circular dependency (worker importing gateway session models). Reconciliation belongs
entirely on the gateway side.

**What changes:**
- Gateway-side reconciliation job: find sessions with `status='busy'` that have no
  corresponding active task (`mesh_tasks WHERE session_id=X AND status IN
  ('pending','claimed')`). These are stale-busy sessions — the task completed or was
  lost without updating the session.
- On divergence: mark session `error`, emit an event, surface in /nodes dashboard.
- Optional auto-recovery: re-enqueue or mark the session idle for reattach.

**What does NOT change:**
- Worker heartbeat payload — no session_states field added.
- Worker backends — no get_session_states() method added.

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
| Session busy/idle  | Gateway | sessions table + mesh_tasks reconciliation |
| Slot utilization   | Worker  | Heartbeat live_state             |
| Task queue         | Gateway | mesh_tasks table                 |
| Node routing       | Gateway | NodeRegistry + live_state        |
| Network health     | Gateway | Aggregated from all node slices  |
