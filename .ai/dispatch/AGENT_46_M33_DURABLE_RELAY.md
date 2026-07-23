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
- **2026-07-23 — BUILT** on `feat/m33-durable-relay` (self-driven by the fired Manager,
  Case `1b59822e…`, since the gateway restart to re-connect `dispatch_worker` is
  operator-deferred — so the Manager built the work directly instead of dispatching it).

## Closure

**Shape shipped (minimal, byte-identical OFF).** The completion *signal* was already
durable (a worker turn records an authoritative `task.finished` event); only the *waiter*
was not. A46 makes the wait recoverable from that same durable ledger — no new schema, no
new long-lived process. New flag `DURABLE_RELAY_ENABLED` (default OFF).

- **`src/control/db.py`** — `durable_relay_enabled()` (mirrors `review_emitter_enabled()`);
  two new `FLOW_EVENT_TYPES` (`worker.wait_pending` / `worker.wait_resolved`; `event_type`
  has no CHECK constraint, so no migration); `record_worker_wait()` (flag-gated, idempotent
  per unresolved wait) + `reconcile_worker_waits()` (resolve finished ⇒ append
  `worker.wait_resolved`; still-open ⇒ report PENDING; idempotent across re-runs); pure
  `_event_payload`/`_event_outcome` helpers.
- **`src/orchestrator.py`** — thin `record_worker_wait` / `reconcile_worker_waits` seams
  mirroring `record_review` (get_db → db, `{ok,…}`).
- **`src/control/control_api.py`** — `CaseWaitBody` + `POST /api/cases/{id}/waits` and
  `POST /api/cases/{id}/waits/reconcile`, both 404 when the flag is OFF (mirrors the
  `review` route's disabled-gate).
- **`scripts/mcp_manager.py`** — `dispatch_worker` records the durable wait best-effort
  after a Case-joined dispatch (a relay failure/404 never breaks dispatch); new
  `reconcile_waits(case_id)` recovery tool that lists resolved vs. still-open waits and
  tells the Manager to re-arm `wait_for_worker` for each open one. Registered in the catalogue.
- **Tests (plain pytest, 213 green on touched modules):** `tests/test_durable_relay.py`
  (flag-OFF byte-identity, pending-marker write, idempotent record, reconcile resolve+keep-open,
  idempotent re-run, failed-outcome carry, fresh-wait-after-resolve) + 6 client tests in
  `tests/test_mcp_manager.py` (records wait on Case dispatch, non-fatal relay failure, no
  `/waits` without a Case, reconcile formatting, disabled surfaced, tool registered).
  Regression: `test_case_admission/closure`, `test_flow_links_events`, `test_flow_runs`,
  all `test_control_api*` — green.

**Completion criteria:** MET. Durable append-only marker keyed to (case,task) via the
existing substrate; reconcile resolves against `task.finished` (resolved ⇒ cleared,
open ⇒ re-arm); idempotent; behind a default-OFF flag, byte-identical OFF; plain-pytest
tests cover all four required cases and pass; one `feat/m33-durable-relay` branch + PR.

**Honest seam note (cross-layer).** The mcp→control-API HTTP seam is proven at the unit
level (monkeypatched `_api_request`); the db/orchestrator/control-API seams are proven
against a real temp `MeshDB`. A **live** end-to-end proof (a real Manager dispatch that
writes the marker through the running gateway, a simulated crash, then `reconcile_waits`)
needs `DURABLE_RELAY_ENABLED=1` + a gateway restart — **operator-gated**, same as every
prior M3 PR. Not merged; escalated per branch policy.
