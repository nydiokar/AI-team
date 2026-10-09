```yaml
job_id: AGENT_105_CROSS_PROCESS_TURN_SIGNAL
created_at: "2026-10-09T21:41:28.251301+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-09T21:41:28.251323+00:00"
```

# DISPATCH — 105 · One turn-state signal across processes (completions reach their consumers when they happen)

**Level:** 3 (crosses the task-server ↔ gateway service boundary; new internal endpoint or channel; touches the
turn scheduler, the managed-effects consumer and the Wake-Dispatcher) · **Type:** code
**Authored:** 2026-10-10 by the A104 owner, at the operator's request · **Status:** ready — **Level 3: operator
approval before execution** (D1 below is the one open choice; a recommendation is given).
**Depends on:** A104 (done, `d0f15c8`). Coordinate with **A102** (in flight in `src/backends/`, `src/worker/`): this job
touches neither.
**Branch:** `feat/turn-signal` · one PR per gate, self-merged when green.

> **Read this first.** A104 made a worker's completion a durable message addressed to whoever asked for the work,
> delivered once. It did NOT make delivery *timely*: the redesign it implemented
> (`docs/TBD/WORKER_COMPLETION_NOTIFY_REDESIGN.md` §3 "KEEP — the resume transport", §5.3 "Delivery loop (drives the
> transport we keep)") kept the polled Wake-Dispatcher, and so did A68's recommendation
> (`docs/PEER_MESSAGING_INVESTIGATION.md`: "use the existing Wake-Dispatcher… do not start with Redis/NATS"). Measured
> live 2026-10-09: worker finished 19:45:42.9Z → its wake admitted 19:45:59.4Z (16.5 s, waiting for the 30 s tick).
> The operator's requirement is that agents receive events **when they happen**. A one-off fix for just the inbox was
> written (PR #218) and **rejected and closed unmerged**: it added a third private wake-up event instead of converging
> on the one that exists. This job converges.

## Why (intent)
There is ONE intended signal for "turn state changed" and it is broken across the process boundary:

- `src/control/task_server.py::_hint_turn_scheduler()` — docstring: "Post-commit hint to the **gateway** turn
  scheduler (a no-op when none runs in this process)". It is called at 4 sites (`task_server.py:1105`, `:1107`
  claim-managed; `:1424` result-managed; `:1672` quiescence) — NOT at release / start / enter-recovery, which only emit
  the UI event (`_emit_managed_change`). Since the Docker split, the task-server and the gateway are **separate
  processes**, so this hint reaches nothing.
- `src/control/turn_scheduler.py::notify_turn_queue_changed()` → `TurnScheduler.hint()` (thread-safe, coalescing,
  `loop.call_soon_threadsafe(event.set)`) is the in-gateway half. Gateway-side producers already call it
  (`turn_admission.py:308`, `node_registry.py:379`).

Result today — every gateway reaction to a finished turn is **poll-driven**:

| Gateway consumer | Woken by today | Poll |
|---|---|---|
| Turn scheduler (`TurnScheduler.run`) | in-process hints only | `FALLBACK_INTERVAL_SEC = 3.0` (`turn_scheduler.py:40`) |
| Managed-effects consumer (`orchestrator._managed_effects_loop`; writes `task.finished`, notifies the operator) | nothing ("discovers it through the DB on the scheduler's fallback cadence — never through an in-process hint", `_start_managed_effects_consumer`) | 3 s |
| Wake-Dispatcher / inbox delivery (`_wake_dispatcher_loop` → `_deliver_inbox`) | nothing | `case_continuation_tick_interval_sec` = 30 s (`orchestrator.py:1460`) |

The outcome wanted: **a worker finishes → its requester's wake is admitted within ~1 s**, through **one** signal,
and the polls survive only as safety nets for a missed signal or a crash. The same path then serves every future
event consumer (Governor included) — no consumer invents its own wake-up.

## CONTEXT (verified 2026-10-10 against `main` @ `279a54a`)
- Existing cross-process push, other direction: gateway → worker `/nudge` (A103): `node_inspector.nudge_node_direct`
  (POST, 2 s timeout, fire-and-forget) + worker `agent.py` `_poll_now` event. Pattern to mirror.
- Existing cross-process channel, hint-grade: task-server `_emit_managed_change()` → `emit_turn_queue_changed()` →
  `observability.emit_event` appends `turn_queue_changed{session_id, turn_id, change, status}` to the shared
  `logs/events.ndjson` (both containers mount `controller/logs`). The gateway already tails that file for the web SSE
  stream (`control_api.py` tailer, 1 s poll).
- Delivery semantics are NOT in scope: A104's inbox (one store, `pending_for`, exactly-once, bounded) stays as is. The
  signal only says "look now"; it never carries authority (the DB rows do).

## Target design (invariants)
- **S1 One signal.** "Turn state changed" has exactly one in-gateway channel: `notify_turn_queue_changed()`. The
  task-server's `_hint_turn_scheduler()` reaches it across the process boundary.
- **S2 Every consumer subscribes to it.** Turn scheduler, managed-effects consumer, Wake-Dispatcher. No consumer
  owns a private wake-up event (PR #218's mistake).
- **S3 Hints carry no authority.** A lost, duplicated or forged hint can only cause an extra or a skipped *pass*,
  never a wrong state: every consumer re-reads the DB. Coalescing: N hints in a burst ⇒ one pass per consumer.
- **S4 Polls are safety nets.** Keep the intervals (3 s / 3 s / 30 s); a test per consumer proves it reacts to the
  signal, not the interval.
- **S5 Bounded and fail-open.** The cross-process send is fire-and-forget with a short timeout (≤ 2 s, like
  `nudge_node_direct`), never on the commit path's critical section, never raising into the carrier request.

## DECISIONS
- **D1 — Transport for S1 (open; operator to confirm).**
  - **(a) HTTP nudge, task-server → gateway** — mirror of A103: an internal, authenticated, body-less
    `POST /internal/turn-signal` on the gateway that calls `notify_turn_queue_changed()`. Latency ≈ ms. Needs an auth
    decision (reuse the controller-local shared token; bind to the internal network) and the §7 service-boundary
    checklist (rate: coalesce on the sender, e.g. ≤ 1 send / 100 ms).
  - **(b) Gateway tails the existing `turn_queue_changed` events** in `events.ndjson` and raises the signal. No new
    endpoint; reuses the A81 stream. Latency ≈ 1 s (tail cadence). Uses an observability file as a control hint
    (acceptable only because S3 holds), and is file-system coupled.
  - **Recommendation: (a).** It is the established cross-process nudge pattern (A103), it is explicit, and it keeps
    the observability stream observational. Do NOT introduce a broker (A68's bar — "DB polling is the bottleneck" —
    is not met).
- **D2 — No router/bus layer.** The A104 inbox + `_deliver_inbox` already route a completion to its requester.
  This job adds a trigger, not a router.
- **D3 — Workers untouched.** No worker redeploy/restart; the task-server and gateway are redeployed together.

## TASK (gates)
**Phase 1 — Inventory (read-only, into TRAIL before code).** Every post-commit hint site in the task-server
(`_hint_turn_scheduler`, `_emit_managed_change` callers) and every gateway poll loop that reacts to turn state
(scheduler, effects consumer, Wake-Dispatcher, cache-heartbeat sync, carrier-coverage monitor) — for each: what
state change wakes it today, what interval, fate (subscribe to S1 / stays a pure timer + why).
- **Gate 1:** table in TRAIL; every loop has a fate.

**Phase 2 — Transport (D1).** Implement the cross-process half of S1; sender-side coalescing; fire-and-forget.
- **Gate 2:** tests (RED first): a terminal commit in the task-server app produces exactly one signal at the gateway
  seam (fake transport), N commits in a burst ⇒ ≤ ceil(N / window) sends; a down gateway never fails or slows the
  carrier request (timeout honoured); a forged/garbage signal changes no state (S3). §7 checklist written into TRAIL.

**Phase 3 — Subscribe the consumers (S2).** The managed-effects consumer and the Wake-Dispatcher wait on the same
channel as the scheduler (a shared subscription in `turn_scheduler`, not new private events); intervals stay as
safety nets.
- **Gate 3:** per-consumer tests: with the interval set to 30 s, a signal makes the consumer pass within ~1 s
  (the shape of the rejected #218's M18, but on the shared channel). End-to-end (H3 harness): worker
  `complete_turn` → wake admitted ≤ 1 s with no tick advance. A104 suites + `tests/test_manager_worker_contract.py` +
  `tests/test_a104_single_pending_source.py` stay green (no second pending pathway, no private wake events — add a
  guard: no `asyncio.Event()` wake-up owned by a consumer loop outside `turn_scheduler`).

**Phase 4 — Deploy + live proof.** Deploy (gateway + task-server). Live: one real dispatch → worker finish → wake;
record `worker completed_at → wake created_at` (target ≤ 2 s; before: 16.5 s). 24 h: no increase in scheduler /
dispatcher passes beyond the coalescing bound.

## ACCEPTANCE
1. `rg -n "asyncio.Event\(\)" src/orchestrator.py` shows no consumer-loop wake-up event outside the shared channel
   (guard test); `_hint_turn_scheduler` has a cross-process effect (test).
2. Live latency worker-finish → wake admitted ≤ 2 s on 3 consecutive real completions (ids + timestamps in TRAIL).
3. Polls still rescue a lost signal: with the transport disabled, delivery still happens within the interval (test).
4. Carrier requests are unaffected when the gateway is down (test + one live check of `result-managed` latency).
5. No regression: A104 suites, contract suite, guard suite green; CI green.

## SCOPE OUT
- Delivery semantics / the inbox (A104 — done). A router or bus layer (D2). A broker (Redis/NATS).
- Worker code (A102). Peer free-form messaging (A68 increment 1).

## Related open items found during A104 (NOT this job — listed so nothing is lost)
- **Stale agent identity in the kanebra worker env** (`SESSION_ID`/`AI_TEAM_SESSION_ID=d9342d3315a3`,
  `AI_TEAM_TURN_ID=task_33d791f6`, `CLAUDECODE=1`, persisted in `~/.pm2/dump.pm2` since 2026-09-09 when an agent
  restarted PM2 from inside its turn). `claude_driver.py:933` (`if k not in os.environ`) lets it win; Codex already
  strips these keys (`codex_native.py:279`). Relayed to **A102** (fix + clean worker restart = operator decision).
  Effects: wrong `dispatch_worker` requester (gateway compensates since PR #216) and **watched jobs from kanebra agents
  attributed to the dead session** (their notifications are lost — pre-existing).
- **Horse Managers send an invalid explicit requester** (Case `ae60fb45`, 2026-10-09 19:34Z) — source unknown; the next
  Horse dispatch records it: `select payload_json from flow_events where event_type='inbox.requester_unresolved'
  order by id desc limit 5`.
- **A104 24-h re-check** due 2026-10-10: `docker logs --since 24h ai-team-gateway-1 2>&1 | grep -c
  turn_withdrawn_obsolete` must be 0.
- **Inert history** `completion_outbox` table + `flow_runs.continuation_mode` column — drop in a later cleanup.

## TRAIL / EVIDENCE (fill as gates close)

---
## Milestone (burndown)
- [ ] D1 confirmed by the operator
- [ ] Gate 1 — inventory of hint sites + poll loops, each with a fate
- [ ] Gate 2 — cross-process transport, RED→GREEN, §7 checklist
- [ ] Gate 3 — all consumers on the shared channel; ≤ 1 s end-to-end in tests; guard green
- [ ] Gate 4 — deployed; live latency ≤ 2 s ×3; 24 h pass counts bounded

## Closure (fill on completion)
