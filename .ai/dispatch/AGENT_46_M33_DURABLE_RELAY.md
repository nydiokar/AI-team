# AGENT_46 — M3.3 durable relay (recoverable worker wait)

**Dispatched:** 2026-07-17
**Level:** 3 (code + schema; flag-gated; build on tests, no paid live run required to build)
**Branch:** `feat/m33-durable-relay` (code ⇒ PR at close, do NOT merge — escalate)
**Flags:** new gate (default OFF ⇒ byte-identical); composes with `HARNESS_FLOW_DRIVE` / `MANAGER_ROLE_ENABLED`.

## Why (the last structural fragility in the M3 survivability arc)
`wait_for_worker` (`scripts/mcp_manager.py::_wait_for_worker`, ~L366) is a **pure in-process
long-poll loop** running inside the `manager` MCP subprocess that the Manager session spawns. Its
entire state — `deadline`, `resolved_id`, `polls`, `consecutive_errors` — lives in that
subprocess's memory. If the Manager session, its MCP subprocess, or the gateway **crashes or
restarts mid-wait, the wait is lost**: nothing re-arms it, and a resumed Manager has no durable
record that it had an outstanding worker to wait on.

Crucially, the *signal* is already durable — post-A37 a worker turn records an authoritative
`task.finished` event on the Case timeline (`_terminal_task_event` polls
`/api/work/{case}/timeline`). So the completion evidence survives a crash; **only the waiter does
not.** M3.3 closes that asymmetry: make the wait **recoverable** from durable state, not
re-dependent on a live process.

This is the only genuinely-new build left before the Manager→worker loop is structurally durable
(carrier-independence #18 and observable sessions #19/#22/#23 are already merged).

## Intent (ground before building)
Read and confirm:
- `scripts/mcp_manager.py` — `_dispatch_worker` (where a worker joins the Case), `_wait_for_worker`
  (the ephemeral loop), `_terminal_task_event` (the durable `task.finished` detector).
- `src/control/db.py` — the append-only `flow_events` + `flow_links` substrate (A25/A26). This is
  the durable spine to reuse; **do NOT invent a new table** unless a real need is evidenced.
- `src/orchestrator.py` — the dispatch seam + Case timeline write path.

## Objective
Make an outstanding worker wait **discoverable and reconcilable after a crash/restart**, reusing the
durable substrate — without a new long-lived process and without re-dispatching the worker.

Minimal shape (builder picks the smallest honest one, grounded in the substrate):
1. **Record the wait intent durably at dispatch** — when a worker is dispatched into a Case, append
   a durable pending marker (e.g. a `worker.dispatched`/`wait.pending` `flow_event` or a
   `flow_link` state) keyed by `(case_id, task_id)`. Append-only; no new schema if the existing
   vocab/columns suffice.
2. **Reconcile on resume** — a read path that, given a Manager's Case, returns outstanding worker
   waits and resolves each against the already-durable `task.finished` event: finished ⇒ resolved
   (clear the pending marker), still open ⇒ re-arm a fresh `wait_for_worker` bounded poll. So a
   resumed Manager reconstitutes "who am I still waiting on" from the ledger, not from lost memory.
3. **Idempotent + bounded** — reconciliation must be safe to run repeatedly (crash during
   reconcile ⇒ re-run is a no-op on already-resolved waits); each re-armed wait keeps the existing
   deadline/consecutive-error bounds.
4. **Flag-gated, byte-identical OFF** — the pending-marker write and the reconcile path are inert
   unless the new gate is ON; absent ⇒ exactly today's behavior.
5. **Tests (plain pytest)** — dispatch writes the durable marker; a simulated crash (drop the
   in-process loop) + reconcile resolves a finished worker and re-arms an open one; reconcile is
   idempotent; flag OFF ⇒ no marker written, no behavior change.

## Completion criteria (ONE reconciled string)
A worker dispatched into a Case records a durable, append-only pending-wait marker keyed to (case_id, task_id) reusing the existing flow_events/flow_links substrate; a reconcile read path resolves each outstanding wait against the durable `task.finished` event (resolved ⇒ marker cleared, still-open ⇒ a fresh bounded `wait_for_worker` re-armed) so a Manager recovers its outstanding waits after a restart without re-dispatching; reconciliation is idempotent; the whole path is behind a new default-OFF flag and byte-identical when OFF; plain-pytest tests cover the durable marker, crash+reconcile (resolve + re-arm), idempotency, and flag-OFF byte-identity, and pass; one `feat/m33-durable-relay` branch + PR opened (NOT merged — escalated).

## Bounds
No new long-lived process; no PTY/daemon. Reuse the A25/A26 substrate; new schema only with an
evidenced need. Plain `pytest` only. Default OFF ⇒ byte-identical.

## Sequencing
Queue AFTER the Worker Role Profile (A45) live proof lands, or run in parallel on its own branch —
they touch different seams (A45: role/adapter/dispatch-tier; A46: wait/reconcile/substrate). This
is the packet that makes the loop *durable ground*.

## Live log
- *(unbuilt — dispatched packet only)*
