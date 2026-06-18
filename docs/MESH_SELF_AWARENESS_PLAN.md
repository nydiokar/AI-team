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

### M2 — On-Demand Worker Pull (DONE)
**Goal:** Gateway can obtain a worker's current state right now, not wait 30s for the
next heartbeat. Used before dispatch and during stuck-task investigation.

**What changes:**
- On nudge receipt, worker immediately sends a heartbeat (in addition to waking the
  poll loop). Implemented by sharing the nudge signal with the heartbeat loop.
- Gateway orchestrator nudges the target worker after enqueueing remote work, so
  workers wake promptly instead of waiting for the next poll interval.
- For unpinned routing, the gateway nudges capable workers before choosing a node,
  waits briefly for a newer `live_state_updated_at`, then prefers fresh live_state
  with available slots.

**What does NOT change:**
- The nudge server remains a minimal raw asyncio socket — no GET /status route.
  Adding HTTP routing to a hand-rolled 512-byte server is fragile. The nudge →
  immediate-heartbeat pattern achieves the same freshness without a new protocol.

**Unblocks:** Gateway can make routing decisions on near-fresh state. Stuck task
investigation no longer requires reading 30s-stale data.

---

### M3 — Session State Reconciliation (CORE DONE)
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
  lost without updating the session. Implemented as `list_stale_busy_sessions()` plus
  the gateway's periodic stale-busy reconciler.
- On divergence: mark session `error` and emit an event.
- TODO for M4: surface stale-busy reconciliation events in /nodes dashboard.
- Optional auto-recovery: re-enqueue or mark the session idle for reattach.

**What does NOT change:**
- Worker heartbeat payload — no session_states field added.
- Worker backends — no get_session_states() method added.

**Unblocks:** Catches stuck sessions without manual investigation. Foundation for
self-healing mesh where divergence resolves automatically.

---

### M4 — Network-Wide Dashboard (DONE FOR THIS BRANCH)
**Goal:** Full network observability from a single view. Operators and the gateway
itself can see every node's live state, session assignments, slot utilization, and
health trends.

**What changes:**
- `/nodes` endpoint includes in-memory `live_state` and `live_state_updated_at`.
- Telegram `/nodes` and `/node <id>` show per-node slot load, active task count,
  active task ids, heartbeat age, and live-state age.
- `/metrics` includes aggregate slots used/total/available, active task count,
  live-state freshness counts, and stale-busy session count.
- `/status` also surfaces aggregate slot load plus stale-busy/stale-live-state
  counts for the quick operator path.

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

---

## Implementation Status / Handoff

Last updated on this branch after commits:
- `b3101f8` — M1 correctness, M2 worker heartbeat wake, M3 DB primitive, test transport cleanup
- `39f8cba` — M2 dispatch-side worker nudge
- `2b4baa9` — test hygiene: fixed mesh dispatch timeout test and marked real full-pipeline test as e2e
- `dec1790` — M3 periodic stale-busy session reconciliation
- `0b2e982` — M4 node live-load visibility in Telegram and NodeInfo responses
- `7cd7471` — M4 aggregate live-load metrics
- current wrap-up — M2 slot-aware pre-route freshness and `/status` mesh anomaly summary

Completed:
- M1 enriched heartbeats:
  - worker sends nested `live_state` with `v`, `active_tasks`, `slots_used`, `slots_total`
  - `slots_used` is semaphore-acquired count, not `len(_active)`
  - DB stores `live_state` and `live_state_updated_at`
- M2 core:
  - raw nudge listener still only accepts `POST /nudge`
  - nudge wakes poll loop and heartbeat loop
  - remote dispatch sends best-effort nudge after enqueue
  - unpinned routing nudges capable workers before picking, waits briefly for a fresh heartbeat, then prefers workers with fresh available slots
- M3 core:
  - DB has `list_stale_busy_sessions()`
  - orchestrator runs periodic stale-busy reconciliation when `MESH_ENABLED=true`
  - stale busy sessions are marked `ERROR`, session event is appended, and `stale_busy_session_reconciled` is emitted
  - interval is `MESH_SESSION_RECONCILE_INTERVAL_SEC`, default 60, 0 disables
- M4 partial:
  - Telegram `/nodes` and `/node <id>` show slot load, active task counts/ids, heartbeat age, and live-state freshness
  - `/metrics` exposes aggregate slot utilization, active tasks, live-state freshness, and stale-busy session count
  - Telegram `/status` shows aggregate mesh load and stale-busy/stale-live-state counts
  - current stale-busy count is visible in the operator views; detailed reconciliation history remains in session/system events

Known remaining work:
- If this grows beyond a two-node personal mesh, add historical trend storage for stale-busy reconciliation and live-state freshness.
- Default tests already force `AI_TEAM_TEST_MODE`, disable mesh routing and file watching, and skip `e2e` unless `--run-e2e` is passed. Add stricter timeouts only if a specific default-suite stall reappears.
