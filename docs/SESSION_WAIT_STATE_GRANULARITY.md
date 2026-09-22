# Session State Legibility — Primary State + Secondary Reason

Status: proposed, approved to build (additive, UI-only for now). Not built.
Date: 2026-09-19 (proposed), 2026-09-22 (generalized + approved).
Scope: make a session's state legible by attaching a **secondary reason label** to the existing
**primary** session status — computed on read, owned by the session, without changing the
authoritative session state machine. UI-only for now; the same field is designed so it can later
be *triangulated* (system-truth vs agent-belief) to catch stuck/hallucinated sessions.

Companion (rejected): `docs/WORKER_CACHE_HEARTBEAT_EXTENSION.md`.

---

## 1. Problem

The status tag ("pillow") the operator sees for a session is a single value. For a waiting
session it is always `AWAITING_INPUT` (surfaced as `needs_input=true`), which conflates every
kind of wait: a Manager blocked on workers, a Manager genuinely idle waiting on the operator, a
session parked on a quota pause, a session blocked on a detached script, and a session that just
finished a turn. The operator re-derives, by hand, the same question the code already has the
evidence to answer: *is it working, waiting on a worker, waiting on a script, paused, or idle?*

Two live incidents sharpen the need:
- A worker ran a script **in the foreground** (never detached via `watch_job`); it showed as
  "running" but was effectively just waiting — indistinguishable from real work.
- A **Manager appeared to be "waiting" (or hallucinated a wait)** while, in the ledger, nothing
  was actually pending — no armed wait-group, no running job, no queued continuation. Today both
  look identical to a healthy wait.

"Running" is acceptable as-is. **"Waiting" is too blunt, and a Manager that *believes* it is
waiting while nothing is pending is currently invisible.** Both are legibility problems, not
state-machine problems.

## 2. Current model (grounded in code)

- **Authoritative enum:** `SessionStatus` (`src/core/interfaces.py:149`): `IDLE`, `BUSY`,
  `AWAITING_INPUT`, `ERROR`, `CANCELLED`, `CLOSED`, `PAUSED_PINNED_NODE_OFFLINE`,
  `PINNED_NODE_OFFLINE`. **No `WAITING`, no `RUNNING`.** Persisted in `sessions.status`
  (`state/mesh.db`); source of truth.
- **Role lives on the session row, not the case:** `sessions.case_role`
  (`manager|worker|reviewer|NULL`) + `current_case_id`, exposed directly on the `Session`
  dataclass (`interfaces.py:215-216`). **No case read is needed to know a session's role.**
- **"Waiting" is not a status — the *evidence* of a wait is durable elsewhere:**
  - Manager wait on workers → `flow_events` markers `worker.wait_pending` /
    `worker.wait_resolved`, per-task (A46, `db.py:3502`) or per-wait-group
    (`db.py:3616` `arm_wait_group`). The session stays `AWAITING_INPUT`.
  - Waiting on a script → a `jobs` row with this `session_id` in status `running`
    (batched read: `db.list_jobs_for_sessions(session_ids)`, `db.py:4710`, already N+1-safe).
  - Quota pause → `db.case_quota_pause(case_id)` returns non-`None` (`db.py:3905`).
  - Transient-retry pause → `db.transient_pause(case_id)` returns non-`None` (`db.py:3945`).
  - Pending autonomous continuation → `db.compute_continuation_tick(case_id)` /
    `list_continuation_rows` (`db.py:3724` / `:3682`), gated by `case_continuation_enabled()`.
- **Bounded-read primitive to respect:** `db.max_flow_event_ids(case_ids)` (`db.py:3166`, the
  #147 watermark) — an unchanged Case is a cheap index-served read; use it instead of rescanning
  a Case's whole event log.
- **Consumers of `status` (the blast radius we must NOT disturb):** the cache-heartbeat gate
  (`orchestrator.py:1381`, keys on `== AWAITING_INPUT`); `SessionView.needs_input`/`is_active`
  (`src/core/view_models.py`); the task overlay `awaiting_input → waiting_for_input`
  (`src/core/task_lifecycle.py`); the timeline `TaskTruthState` with `confidence`/`staleness`
  (`src/core/session_timeline.py`); recovery/continuation setters (`orchestrator.py`).

## 3. Design decision — derive a secondary reason; never mutate the enum

**Do NOT add `SessionStatus` values and do NOT split `AWAITING_INPUT`.** The heartbeat gate,
`needs_input`, `is_active`, the task overlay, and recovery all key off the enum by equality;
splitting it would silently break the heartbeat match and every enum comparison — a wide,
dangerous blast radius for a presentational need. Every input the operator wants **already
exists** as a durable side-effect of what the session *did*; it needs to be *read and projected*,
not stored as a new authority.

**Therefore:** add **one general, derived, non-authoritative field — a "secondary reason" — that
any primary state can carry**, attached to the session read-model (`SessionView` / timeline),
computed **on read**, keyed off already-durable evidence. It is session-scoped and dies with the
session. A Case is read only as **evidence of what the session itself did** — the Case is never an
authority and never owns this label. The enum stays the single source of truth.

Shape (illustrative, not final API):

```
SessionReason:
  kind:       str            # vocabulary below, per primary state
  confidence: "high" | "medium"   # honesty about absence-based inferences
  detail:     Optional[str]  # e.g. node id, job label — bounded, presentational
```

This mirrors the codebase's own honesty pattern: the timeline already ships a *derived*
`TaskTruthState` with `confidence`/`staleness` rather than trusting one field.

## 4. The general vocabulary (foreseen now, extensible)

A **primary state + reason** map. Compute a reason only where it adds signal; leave it empty
otherwise. Evaluate the `AWAITING_INPUT`/`IDLE` reasons **in priority order** and take the first
that matches.

### BUSY  → shows "running"
- reason: **empty** (`running`). This is the honest limit: the system cannot tell "running real
  work" from "running a foreground script it should have detached" from *inside* a busy turn.
  That distinction is unrecoverable at the state layer — the cure is the worker detaching via
  `watch_job` (after which it shows as `waiting_job`, below). Do not fake a BUSY sub-reason.
- *(Foreseen future value, NOT v1: `running` + elapsed hint — "running · 47m" — as a soft nudge
  that a long turn may be a stuck foreground script. Add the value later if wanted; the mechanism
  already allows it.)*

### AWAITING_INPUT / IDLE  → shows "waiting"/"idle", refined by reason (priority order)
1. `paused_quota` — `case_quota_pause(case_id)` is non-`None`. **confidence high.** (Today this
   is invisible — looks like plain idle. High-value.)
2. `paused_retry` — `transient_pause(case_id)` is non-`None`. **confidence high.**
3. `waiting_workers` — `case_role=='manager'` AND its Case has an **unresolved** wait-group
   (last `worker.wait_pending` for a group not followed by `worker.wait_resolved`). **high.**
   Reuse the exact bounded scan the heartbeat already uses (`_cache_heartbeat_owner_live`
   `case_wait_group` branch) or the continuation-tick read — do **not** add a new full scan.
4. `waiting_job` — a `jobs` row for this `session_id` is `running`
   (`list_jobs_for_sessions`). **high.**
5. `open_case_idle` — session is joined to an **open** Case but **none** of 1–4 hold.
   **confidence medium** (absence-based; can race a just-written ledger event). This is the
   **stuck / hallucinated-wait signal**:
   - For a **Manager**: it owns an open Case but is waiting on nothing and has no queued
     continuation — it *should* be dispatching, waiting, or closing. This is exactly the "Manager
     looked like it was waiting but nothing was pending" incident, now made visible instead of
     hidden inside a generic "waiting".
   - For a **worker**: parked idle on an open Case; the Manager *may or may not* return. We label
     it honestly as parked-idle — **not** "waiting on the Manager", because that asserts a return
     we cannot promise.
6. `idle` — no Case affiliation and nothing pending. **confidence high.** Plain idle: genuinely
   waiting on the operator (or nobody). We do not dress this up.

### PINNED_NODE_OFFLINE / PAUSED_PINNED_NODE_OFFLINE  → shows the hold reason
- reason: `node_offline`, `detail = <node_id>` (+ grace remaining if cheap). **high.** Turns a
  cryptic status into "held for node `Horse`". Evidence: `session.machine_id` + node row.

### ERROR / CANCELLED / CLOSED  → empty
- No reason in v1. *(Foreseen: `error_class` on ERROR if it's a free read — low priority.)*

## 5. Where and how to compute — bounded, read-path, no new scans

**Hard constraint (CONTEXT.md 2026-09-18, PRs #145/#147):** the Wake-Dispatcher stalled the event
loop by scanning all open Cases' full event logs on a timer. **Do not recreate that.** The reason
field must be:

- **Computed on read, per listed session, on request** — in the `/api/sessions` and
  `/api/sessions/{id}/timeline` projection paths (`control_api.py:~1246`, `view_models.py`), where
  a DB handle already exists. **Never in a background loop; never on a timer.**
- **Lazy & short-circuiting:** `BUSY`/terminal states return empty with zero DB reads. Only
  `AWAITING_INPUT`/`IDLE` (and the two node-offline holds) do any lookup.
- **Batched across the listed sessions (no N+1):** one `list_jobs_for_sessions(ids)` for the whole
  page; role/case straight off the already-loaded session rows; pause checks and the wait-group
  read done per-Case only for the managers on the page, watermark-gated via
  `max_flow_event_ids` so unchanged Cases are a cheap read. No cross-session materialization
  (A80 §15 memory rule).
- If per-request cost is ever measurable, cache on a short TTL keyed by the Case event watermark.
  Start **without** a cache — the watermark check is already O(1).

## 6. Triangulation later (design for it, don't build it yet)

For now the reason is **UI-only** — a richer pillow. But it is deliberately shaped as
*system-derived truth*, which lets a later phase **triangulate it against agent belief**:
- `open_case_idle` on a Manager = system says "nothing pending" while the agent may believe it is
  waiting. A future phase could compare this to the agent's last self-reported intent and, on a
  sustained mismatch, raise a nudge/alert (or feed the Wake-Dispatcher) — catching stuck or
  hallucinated Managers automatically.
- `waiting_job` vs a BUSY turn that never detached = the seam that would flag "foreground script
  that should have been a `watch_job`".
These are **explicitly out of scope for v1** and must not be wired to any control action now.
v1 only *surfaces* the truth; keeping it read-only first is what makes later triangulation safe.

## 7. Blast-radius review — "what changes?" (nothing that reads `status` changes behaviour)

| Consumer | Change? | Note |
|---|---|---|
| Cache-heartbeat gate (`orchestrator.py:1381`) | **None** | Still keys on `AWAITING_INPUT`; reason is presentational. |
| `SessionView.needs_input` / `is_active` | **None** | Unchanged formulas; reason is a sibling field. |
| Task overlay `derive_task_state` | **None** | Base preserved; may pass reason through later. |
| Timeline `TaskTruthState` | **Additive** | Already carries `confidence`/`staleness`; reason sits alongside. |
| Recovery / continuation setters | **None** | They write the enum; derivation reads durable side-effects they already emit. |
| Web UI | **New tag** | "waiting" becomes "waiting · on workers / on a script / paused: quota / idle" and "open case · nothing pending". Frontend-only; needs `web` rebuild (gated, operator's call). |
| Telegram | **None (v1)** | Optional later. |

No enum change, no migration, no scheduler, no new authority, **no gateway restart** for the
backend (read-path). Only the web bundle rebuild renders the new tag.

## 8. Final adversarial review — hallucination & wrong-assumption sweep (pre-lock)

Every asserted method/field below was verified against the tree on 2026-09-22.

- **Verified real:** `SessionStatus` values (`interfaces.py:149`); `case_role`/`current_case_id`
  on `Session` (`interfaces.py:215-216`); `case_quota_pause` (`db.py:3905`), `transient_pause`
  (`db.py:3945`) both return `Optional[dict]` (non-`None` ⇒ paused); `list_jobs_for_sessions`
  (`db.py:4710`, N+1-safe, carries `orphaned`); `max_flow_event_ids` (`db.py:3166`);
  `compute_continuation_tick` (`db.py:3724`); `case_continuation_enabled` (`db.py:522`); the
  heartbeat gate's `== AWAITING_INPUT` (`orchestrator.py:1381`). **No invented names.**
- **Assumption checked — "an idle Manager on an open Case is stuck/hallucinating."** *Not always.*
  It can be a true, momentary between-actions gap, or a ledger race between `task.finished` and
  `worker.wait_resolved`. → We label it `open_case_idle` at **confidence medium**, never assert
  "stuck", and wire it to **no** action in v1. Honest signal, not a verdict.
- **Assumption checked — "reason tells you what a BUSY session is really doing."** *False and
  unrecoverable.* A foreground script is a genuine BUSY turn. We keep BUSY's reason **empty** and
  say so; the fix is `watch_job`, not a label. Rejecting the temptation to fabricate a BUSY
  sub-reason is itself part of the design.
- **Assumption checked — "waiting on the Manager is a safe label for a parked worker."** *No* —
  it implies a return we cannot promise. → labelled `open_case_idle` (parked), not
  `waiting_manager`.
- **Assumption checked — "we can read this cheaply."** Only if bounded: batched job read,
  role off the row, pause/wait reads for managers-on-page only, watermark-gated. The one real
  hazard is someone later moving it into a loop → the #145/#147 stall. → Called out in §5 and to
  be repeated as a code comment.
- **Assumption checked — "the Case is the state owner here."** *No, and must stay no* — the label
  lives on the session, is computed per session, and the Case is read only as evidence of the
  session's own recorded actions. This is session-state management, not case-state management.
- **Residual unknowns (stated, not hidden):** (a) exact node grace-remaining read for the offline
  detail is left to the implementer (surface node id even if grace is omitted); (b) whether the
  operator also wants the reason in Telegram/cost views — deferred to a follow-up.

## 9. Minimal-change summary (least action)

| Change | File | Size |
|---|---|---|
| `SessionReason` derivation helper (bounded, batched, watermark-gated, `confidence`) + priority table | new helper near `session_timeline` / `view_models` | 1 function |
| Attach reason to `SessionView` (+ timeline item) | `src/core/view_models.py`, `src/core/session_timeline.py` | additive field |
| Populate on `/api/sessions` + `/api/sessions/{id}/timeline` (reuse existing DB handle, batch the page) | `src/control/control_api.py` | reuse handle |
| Web: render the richer pillow | `web/` | frontend only (rebuild gated) |
| Tests: full primary×reason truth table incl. quota/retry pause, `open_case_idle` (manager & worker), racy/stale cases, BUSY-empty, no-N+1 assertion | `tests/` | new |

Additive, read-path, session-scoped, default-safe. No enum change, no migration, no scheduler, no
gateway restart for the backend.
