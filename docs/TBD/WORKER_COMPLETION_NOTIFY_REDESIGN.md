# Worker → Manager Completion Notification — Fundamental Redesign

**Status:** PROPOSAL (for second review)
**Date:** 2026-09-22
**Grounded against:** `d59cca2`
**Supersedes (proposes to retire):** the wait-group half of `docs/AUTONOMOUS_CASE_CONTINUATION_DESIGN.md`
**Owns:** how a Manager learns that a worker it dispatched has finished.

> **For the second reviewer:** an adversarial pass is already embedded (§8, traps T1–T12) with a
> disposition for each. Do **not** re-derive those — they are covered. Spend your effort on the
> two things I could not close from inside the repo: (a) whether any real caller needs true
> *barrier* (ALL/quorum) join semantics that the per-completion model cannot express (T7), and
> (b) the migration cutover for Cases in flight at deploy (T9). Everything else below is
> code-grounded; go **deeper** on those two and **wider** on any dispatch/terminal seam I did not
> name in §3.

---

## 1. The one-sentence problem

A worker finishes its work but the Manager is never told, so the Manager waits forever (or is only
rescued by an out-of-band operator poke). This has now been observed **twice, from opposite
directions**, on the same live Case `150d908488fd431a8258deea81cd3ec0`:

- **Incident A (silent finish).** Workers `a9ceffbcfd27`/`task_c1067539` and `21b787ff50f6`/
  `task_64e3acc6` finished (`task.finished` at events 7690, 7691) into a **void** — no armed
  wait-group was watching them, so no wake ever fired. The Manager only adjudicated them via an
  out-of-band `review.accepted` (7693/7694) during an operator turn, not via the completion path.
- **Incident B (perceived-done, actually running).** Worker `0c04a6df6001`/`task_b845da3f` was
  genuinely still executing on Horse (`live_state.phase=running`, `llm_turn.final_status=running`,
  `timeout_status=none`) with no terminal event yet. The Manager *correctly* waited, but there was
  no liveness backstop that would ever have rescued it had the worker died silently.

Both are the same defect wearing two masks: **completion is not a signal the system delivers; it is
a fact the Manager must arrange to re-derive.**

---

## 2. Root cause — the wrong primitive

The system models "waiting" as a **Manager-authored, mutable, reusable object** (`wait_group`)
that a **separate polling projection** (`compute_continuation_tick`) reconciles against the event
log using a **consumption watermark**. That is a pull-based re-derivation of something that is
intrinsically a **push-based, edge-triggered completion callback**.

Every failure mode is a direct consequence of that choice:

| Symptom seen live | Direct consequence of the wrong primitive |
|---|---|
| Generation collision (`nfc-o0-o1` reused across dispatch rounds → a stale gen-2 ACK retired the freshly re-armed gen-3 group; 7687→7689) | the correlation id is **Manager-authored and reused**, and retire is keyed on bare `wait_group_id` |
| Finishes land in a void (Incident A) | the wait set is a **thing the Manager declares separately from the dispatch**, so the two can drift — and did |
| `reviewed`/`consumed`/`retire_only`/`reviewed_out_of_band` reconciliation | you only need a "have I already seen this?" ledger because notification is **re-derived**, not **delivered once** |
| `boot_reconcile_case`, IDLE-gate, respawn re-arm | crash-safety bolted onto a projection instead of falling out of a durable delivery queue |

### 2.1 The evidence is structural, not anecdotal

The relational structure the *correct* design needs **already exists in the schema and is left
unpopulated**, while the relationship is instead reconstructed in a parallel event log:

- `mesh_tasks.parent_task_id` — **empty on every task.**
- `mesh_tasks.flow_run_id` — **empty on all 6782 rows.** A worker task does not even record which
  Case it belongs to.
- The parent↔child↔case relationship lives *only* in `flow_links` (roles: `manager`/`worker`/`task`)
  and `flow_events`, as denormalized projections that **both** the Manager (`arm_wait_group`) **and**
  the Wake-Dispatcher (`compute_continuation_tick`) must independently re-derive.

A worker literally does not know who its parent is or which Case it is in. That is the disease.

---

## 3. What is actually sound (keep it) vs. what is rotten (replace it)

This is a **targeted replacement, not a rewrite.** The grounded code seams:

**KEEP — the resume transport is good** (`orchestrator._continue_case_once`, ~L1583–1791):
- It **coalesces** N finished workers into **one** wake turn (`_render_wake_turn`, L1793) — critical
  for cost.
- It uses a **deterministic, atomically-claimed** continuation row (`continuation_task_id(case,gen)`
  + `claim_task`) → real single-flight; a racing tick collapses on the UNIQUE id.
- It **late-binds** the awaiter via `case_manager_session_id(case)` (survives Manager respawn, A55).
- It records consumption only when the wake turn **returns** (`_finalize_continuation`), which is the
  crash-safe "delivered" point.
- It preserves round-cap escalation, headless-Case escalation, and dead-Manager respawn.

**REPLACE — the satisfaction derivation is rotten:**
- `db.arm_wait_group` (L3616) + the `arm_wait_group` **Manager MCP tool** (`mcp_manager.py` L798).
- `db.compute_continuation_tick` (L3724) satisfaction logic + the `consumed`/`reviewed`/`retire_only`
  reconciliation in `_continue_case_once` (L1640–1666).
- `db.reconcile_worker_waits` / `boot_reconcile_case` re-arm paths.

**ADD:**
- A **transactional completion outbox** written at the terminal seam
  (`orchestrator._record_terminal_outcome`, L6149, where `task.finished` is emitted).
- Population of the **existing** `mesh_tasks.flow_run_id` (and optionally `parent_task_id`) **at
  dispatch** (`mcp_manager._dispatch_worker`, L401).
- A **liveness backstop** that synthesizes a terminal for a lost/dead worker (closes Incident B).

---

## 4. Reference architectures (this is a solved problem)

The same shape appears in five well-worn places; they all agree on the primitive:

| Pattern | Primitive | Why you cannot "forget to wait" |
|---|---|---|
| Futures / promises | `spawn()` returns a handle; `await(handle)` resolves once | runtime delivers the result to the awaiter, exactly once, edge-triggered |
| Structured concurrency (Trio nursery, Kotlin scope) | children belong to a scope; the scope does not exit while a child lives | join *is* the scope's lifecycle, not a declaration |
| Erlang/OTP `monitor` | `monitor(pid)` at spawn ⇒ guaranteed `{'DOWN', …}` on exit | monitoring is established at spawn; the DOWN message is delivered by the runtime |
| Temporal / durable execution | a workflow starts an activity; the runtime persists the promise, delivers completion, resumes | "did I already see it?" **cannot occur** — completion is a durable exactly-once signal to a specific await point |
| EIP *Correlation Identifier* | a **unique, system-owned** id ties request→reply | never agent-authored, never reused — precisely the rule this code violates |

**The unifying law we adopt:**

> Completion is a signal the system delivers to the specific awaiter, established at spawn time,
> keyed by a **unique, system-owned** id, written in the **same transaction** as the terminal state
> change — **not** a fact re-derived by polling a log and de-duplicated with a watermark.

---

## 5. Target design

### 5.1 Bind the child to its scope at dispatch (use columns that already exist)

In `_dispatch_worker`, when the worker task is created, stamp:
- `mesh_tasks.flow_run_id = case_id` (the scope) — **the** structural fact that is missing today.
- `mesh_tasks.parent_task_id = <manager dispatch turn id>` (optional, finer-grain provenance).

The **scope is the Case**, not the session (a warm worker session serves multiple Cases
sequentially — see T6). The awaiter is resolved *late*, at delivery, via
`case_manager_session_id(case_id)`. **`arm_wait_group` is deleted from the Manager's surface** — the
act of dispatching a worker into a Case *is* the registration of the wait. The Manager can no longer
author, mis-scope, or reuse a wait id, because it never touches one.

### 5.2 Transactional completion outbox

A single append-only table, written in the **same `_write()` transaction** as the `task.finished`
event (§ T5 makes this the load-bearing invariant):

```
completion_outbox(
  child_task_id   TEXT PRIMARY KEY,   -- system-owned, unique per dispatch → collision impossible
  case_id         TEXT NOT NULL,      -- the scope to resume
  outcome         TEXT NOT NULL,      -- success | failed | timeout | lost
  created_at      TEXT NOT NULL,
  delivered_at    TEXT,               -- NULL = pending; set when the awaiter has OBSERVED it
  delivery_reason TEXT                -- wake | reviewed_in_turn | superseded
)
```

- **`PRIMARY KEY(child_task_id)`** is the entire exactly-once guarantee. It replaces the watermark,
  the `consumed` set, the `reviewed` set, and `retire_only`. "Have I seen it?" becomes "does a
  delivered row exist?" — a lookup, not a projection.
- A re-fired terminal (worker restart / at-least-once result reporting) collapses on the PK (T11).

### 5.3 Delivery loop (drives the transport we keep)

Per open Case, one pass (serialized by the existing atomic continuation claim, T8/T12):

1. Read **pending** outbox rows for the Case (`delivered_at IS NULL`).
2. **Suppress** any whose child already carries a Manager `review.*` tagged to that task —
   mark them `delivered(reason='reviewed_in_turn')` with **no wake** (this is the *legitimate*
   core of today's `reviewed_out_of_band`, re-expressed; T3).
3. If any genuinely-undelivered rows remain **and** the Case's Manager session is `AWAITING_INPUT`:
   create/claim the deterministic continuation row carrying **all** pending child ids (coalesced,
   T1), deliver **one** wake, and on return (`_finalize_continuation`) mark those outbox rows
   `delivered(reason='wake')` — the same crash-safe "delivered = turn returned" point that exists
   today (T2).
4. Round-cap / headless / dead-Manager escalation paths are unchanged (T8).

`compute_continuation_tick` collapses from "re-derive satisfaction of Manager-authored wait-groups
against the full event log with a watermark" to "**SELECT pending FROM completion_outbox WHERE
case_id=? AND delivered_at IS NULL**." ALL/quorum joins, if ever needed, become the structural query
"Case has zero non-terminal worker children" (T7) — a derived predicate the Manager cannot get wrong.

### 5.4 Liveness backstop (closes Incident B)

Push only fires if `task.finished` is written. A worker that dies silently (node crash, dropped
result, wedged turn with `timeout_status=none`) writes nothing → the Manager waits forever. So the
design **requires** a reaper that reconciles `claimed`/running children against ground truth and
synthesizes a terminal:

- A `claimed` child whose node no longer lists it in `nodes.live_state.active_tasks`, **or** whose
  turn exceeds a per-turn timeout, with no terminal event → emit a synthesized
  `task.finished(outcome='lost'|'timeout')` **through the same terminal seam** → outbox row → wake.

This unifies both incidents under one mechanism: the Manager always learns the fate of every child,
whether it succeeded, failed, timed out, or vanished. (May land as a follow-up PR, but it is part of
"notify" being *complete*, not optional.)

---

## 6. Why NOT adopt a durable-workflow engine wholesale (determinism trap)

The obvious reviewer reflex is "just use Temporal/Cadence." We adopt its **principle** (§4) but
**not** its engine, for a load-bearing reason: durable-execution engines require workflow code to be
**deterministically replayable**. The Manager is an **LLM** — its turns are non-deterministic and
cannot be replayed to reconstruct state. So the Manager loop itself can never be a replay-based
durable workflow. We take the durable *completion signal* (which needs no determinism) and keep the
Manager as a live, late-bound, respawnable session. Pre-empting this so the reviewer does not send us
down a dead end (T10).

---

## 7. What gets deleted / kept / added (change ledger)

| Component | Fate |
|---|---|
| `arm_wait_group` MCP tool + Manager prompt instructions to call it | **DELETE** |
| `db.arm_wait_group`, wait_group flow markers | **DELETE** |
| `compute_continuation_tick` satisfaction + watermark/reviewed/retire logic | **REPLACE** with outbox SELECT |
| `reconcile_worker_waits`, `boot_reconcile_case` re-arm | **DELETE** (outbox is the durable state) |
| `_continue_case_once` transport (coalesce, atomic claim, late-bind, finalize) | **KEEP**, fed by outbox |
| round-cap / headless / dead-Manager respawn escalation | **KEEP** |
| `mesh_tasks.flow_run_id` / `parent_task_id` | **POPULATE** at dispatch |
| `completion_outbox` table + transactional insert at `task.finished` | **ADD** |
| liveness/timeout reaper → synthesized terminal | **ADD** |

Net: one new table, two populated columns, one deleted tool, and a large deletion of reconciliation
code.

---

## 8. Adversarial review — traps already caught (reviewer: do not re-derive these)

| # | Trap | Disposition in this design |
|---|---|---|
| **T1** | **Cost regression** — a naive per-outbox-row wake = N paid Manager turns. TEST COST GUARD. | Delivery loop drains **all** pending rows for a Case into **one** coalesced continuation generation (§5.3). Coalescing is preserved from `_render_wake_turn`. |
| **T2** | **Exactly-once resume across crash.** Marking a row delivered before the turn lands strands the completion. | `delivered_at` is set only in `_finalize_continuation` (turn **returned**), the existing crash-safe point. A crash before return leaves the row pending → redelivered. |
| **T3** | **Out-of-band review suppression is a real requirement, not cruft.** The Manager sometimes reviews a child during an operator poke before the wake; re-waking burns a paid turn to re-conclude "already done." | Kept and re-expressed: step 2 marks rows whose child has a tagged `review.*` as `delivered(reason='reviewed_in_turn')`, no wake. A completion is delivered when observed by **any** path. |
| **T4** | **Lost terminal signal** — push fires only if `task.finished` is written; a dead worker writes nothing (Incident B). | §5.4 liveness backstop synthesizes `task.finished(lost|timeout)` → outbox. Named as **required** for completeness. |
| **T5** | **Non-atomic terminal.** `_record_terminal_outcome` is best-effort/try-except and cannot raise. An outbox insert that fails silently regresses to today. | **Invariant:** the outbox insert shares the **same `_write()` transaction** as the `task.finished` append (one DB method `record_task_terminal`). "Terminal recorded ⟺ outbox row exists" must hold or the whole design is void. |
| **T6** | **Session ≠ scope.** A warm worker session serves multiple Cases (PR #21). Keying the wait on the session is wrong. | Key on `(case_id, child_task_id)`. Bind `task.flow_run_id = case` (task is single-Case). Awaiter resolved late via `case_manager_session_id`. Never key on session. |
| **T7** | **Barrier (ALL/NAMED) semantics loss.** Pure per-child push cannot express "only when all of {A,B,C}." | The Manager already reviews each child serially; "all done" = structural query "Case has zero non-terminal worker children." **Flagged for the reviewer to confirm no caller needs a true synchronized barrier** (I could not find one; verify wider). |
| **T8** | **Safety escalations dropped.** round-cap, headless, dead-Manager respawn live in `_continue_case_once`. | Explicitly KEEP; the delivery loop wraps the same transport, escalations unchanged. |
| **T9** | **In-flight Cases at cutover** have wait-group markers, no outbox rows, empty `flow_run_id`. A big-bang swap strands them. | Flag-gated on the existing `CASE_CONTINUATION_ENABLED`; **Case-boundary cutover** (new Cases new path; old Cases drain on old path) + a one-time boot reconcile that seeds the outbox from `task.finished` events lacking a matching consumption. **Flagged for the reviewer to go deeper.** |
| **T10** | **"Just use Temporal."** | §6: the Manager is a non-deterministic LLM; durable-execution replay cannot apply to it. Borrow the principle, not the engine. |
| **T11** | **Duplicate terminal** (worker restart / at-least-once result reporting) → double wake. | `PRIMARY KEY(child_task_id)` collapses re-fires to one row. |
| **T12** | **Manager BUSY at completion.** Cannot deliver a turn to a busy/mid-turn session. | Outbox rows persist as a durable pending queue; delivery only when the awaiter is `AWAITING_INPUT` (existing gate). Completions survive arbitrary Manager-busy windows. |

---

## 9. Rejected alternatives (do not relitigate)

- **Make `wait_group_id` unique per arm.** Fixes only the collision (Incident A's proximate cause);
  keeps the pull-derivation, the watermark, and all of `reviewed`/`retire_only`. Leaves Incident B
  untouched. A patch on the wrong primitive.
- **Guard: refuse to retire a group with unfinished current members.** Same objection — hardens the
  broken machinery instead of removing the reason it exists.
- **Full Temporal/Cadence adoption.** T10.

---

## 10. For the second reviewer — go deeper / wider here

1. **T7 (barrier semantics):** sweep every current/intended caller of `arm_wait_group` and the
   Manager role prompt for any genuine *synchronized-barrier* need that "wake-per-completion + Manager
   checks remaining children" cannot serve. I believe none exists; confirm wider.
2. **T9 (migration):** design the exact boot-reconcile that seeds `completion_outbox` from the
   historical ledger for Cases open at deploy, and the precise flag cutover so no in-flight Case is
   stranded on either path. Go deeper.
3. **Seams I may have missed:** verify there is exactly **one** terminal seam
   (`_record_terminal_outcome`) — if any other path can mark a task terminal without routing through
   it, the T5 invariant leaks. Go wider on the result-reporting / cross-node paths.
4. **Reaper sizing (§5.4, §7 service boundary):** per-turn timeout value, and the
   `live_state.active_tasks` reconciliation cadence, need a concrete bound; today `timeout_status`
   is `none` and nothing reaps a wedged claimed child on an *online* node.
