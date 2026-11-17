# Cancel / Close / "managed" — teardown of the current command surface

**Date:** 2026-10-10T11:05Z · **Grounded on:** live gateway `git_sha=5c41b93`, live DB
`/home/cifran/ai-team-data/controller/state/mesh.db`, evidence case: Manager session
`8065dea528e1` (case `6a4c523570f3…`) stop at 10:25:08.

**Purpose:** surface — not yet fix — the duplication between the *legacy* command methods and
their *`managed`* twins, and map which operator commands travel **through the task queue**
(poll-claimed by a carrier) versus which are **direct** gateway writes. This is a checklist
surface: a later cleanup can tick each row. No code is changed by this document.

---

## 0) The target mental model (yours, and it is the correct one)

There are exactly **two units** and **two operator intents**:

| Unit | Operator intent | What it should do | Latency |
|------|-----------------|-------------------|---------|
| **turn** | **Interrupt** ("Stop" / "Cancel turn") | Abort the live LLM turn *only*. Session stays open, process stays pooled, queue stays live. | **Immediate / push** |
| **session** | **Close** ("Close session") | Tear down the backend process, withdraw queued work, end the session. | May defer (queue ok) |

Everything below is measured against this model. The mess is that the current code has **more
than two** command paths, and the "Stop" button does **three** things at once instead of one.

---

## 1) What "Stop" actually does today (the conflation)

`POST /api/sessions/{id}/stop` → `orchestrator.stop_managed_session_turn(session, pause_queue=True)`
(`src/orchestrator.py:11589`). For an **enrolled** session that single call does **three** separate
things in one shot:

```
STOP button ─┬─(1) set_turn_queue_paused(True)      persistent pause  [DIRECT DB write]
             ├─(2) sessions.status = 'cancelled'                       [DIRECT DB write,
             │      + turn_queue_hold = 'operator_stop'                 inside the cancel txn]
             └─(3) enqueue cancel_managed control row  → carrier        [QUEUED, poll-claimed]
```

**[CORRECTED 2026-10-10 — operator intent]** Steps (1) pause and (2) `cancelled`+hold are
**desired** behaviour: after a cancel you *want* the queue paused so the next messages don't pour
in. They are good UX, keep them. The actual defects are narrower:

- **(2)** flips the session to `cancelled` **in the same transaction** that enqueues the
  interrupt (3). The no-grace `session_closed` reaper then eats the interrupt before the carrier
  claims it (§4) → **Stop silently never lands.** ✅ **FIXED** in `fix/cancel-interrupt-reaper-race`
  (control actions exempt from `session_closed`); keep the hold, stop reaping the interrupt.
- The interrupt primitive is **duplicated** (`cancel_turn` vs `cancel_managed`, §3) — cleanup,
  not a functional break.
- While paused, your later messages queue **strictly FIFO with no way to jump or overwrite**
  (§7). That is the "stuck behind the queue" feeling; it is a missing capability, not a bug.

For a **legacy** (non-enrolled) session the same button instead runs `cancel_task` +
`mark_cancelled` — a *different* code path reaching the same interrupt.

---

## 2) ASCII — what is behind the queue vs what is direct

"Behind the queue" = a row in `mesh_tasks` that a **carrier/worker picks up on its poll loop**
(~2 s). Control rows bypass the *work-slot* semaphore (so they don't wait behind long turns —
`src/worker/agent.py:2920`) but they **still wait for the poll** and can be reaped.

```
                         GATEWAY (control plane)
   ┌───────────────────────────────────────────────────────────────┐
   │  DIRECT (synchronous DB write / in-process — no carrier)        │
   │   • set_turn_queue_paused / set_queue_paused_sync   (pause flag)│
   │   • request_turn_cancel hold_session → status='cancelled'       │
   │   • mark_cancelled  (legacy session status)                     │
   │   • close_case / interrupt_case  (case rows + events)           │
   │   • _task_cancel_events (asyncio.Event)  [gateway-local exec only]│
   └───────────────────────────────────────────────────────────────┘
                 │ enqueue_task(...)  (mesh_tasks, pinned to carrier)
                 ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  BEHIND THE QUEUE  (mesh_tasks → carrier poll loop, ~2s)        │
   │                                                                 │
   │   WORK turns (correctly queued):                                │
   │     • create_session    • resume_session                        │
   │     • run_oneoff        • compact_session                       │
   │                                                                 │
   │   CONTROL rows (out-of-slot, but STILL poll-claimed):           │
   │     • cancel_turn      → backend.cancel(session)          ← interrupt
   │     • cancel_managed   → backend.cancel_managed_turn(...)  ← interrupt
   │     • cancel_codex     → CodexOwnership.request_cancel                │
   │     • close_session    → backend.close(session)           ← teardown  │
   └───────────────────────────────────────────────────────────────┘
                 │  claimed by  ▼
             CARRIER (Horse / kanebra …)  — runs the SDK/backend
```

**The problem in one line:** the interrupt (`cancel_turn` / `cancel_managed`) is **behind the
poll-claim queue**, so it is *not* immediate and *can be reaped* — while the pause + session-hold
that you did **not** ask for are **direct and instant**. The wiring is backwards relative to §0.

---

## 3) The command inventory — `managed` twin vs legacy counterpart

Columns: **Behind Q?** = is it a `mesh_tasks` row claimed by a carrier. **Should be immediate?**
= must happen the moment you click (push/event-driven) per §0. **Verdict** = cleanup call.

| # | `managed` / current method | Legacy / sibling doing ~the same | Behind Q? | Should be immediate? | Duplicate? | Verdict |
|---|----------------------------|----------------------------------|-----------|----------------------|-----------|---------|
| 1 | `cancel_managed` action → `_handle_cancel_managed` → `backend.cancel_managed_turn(session, turn_uuid)` (`worker/agent.py:3203`) | `cancel_turn` action → `_execute_task` → `backend.cancel(session)` (`worker/agent.py:755`) | **Yes** (both) | **Yes** (both) | **YES — same op** | **MERGE.** Both interrupt the live SDK turn. `cancel_managed` is the turn-uuid-fenced, ownership-checked *upgrade*; `cancel_turn` is coarse (whatever the session is doing). Converge to ONE interrupt action; keep the fenced semantics, drop the coarse twin (or make `cancel_turn` an alias). |
| 2 | `stop_managed_session_turn(pause_queue=True)` (`orchestrator.py:11589`) | legacy stop: `cancel_task` + `mark_cancelled` (`orchestrator.py:9530`, `routes/sessions.py:377`) | row (3) yes | interrupt=yes; pause/hold=should NOT exist on interrupt | twin paths | **SPLIT + DEMOTE.** This is the 3-in-1 conflation (§1). Interrupt must stop doing (1) pause and (2) session-cancelled hold. Keep ONE stop method; move pause/hold to the Close path only. |
| 3 | `_cancel_managed_turn_if_managed(hold_session=…)` (`orchestrator.py:11556`) | `_enqueue_remote_cancel_turn` (`orchestrator.py:10204`) | yes | yes | twin | **MERGE into #1.** Two gateway entrypoints that both end in an SDK interrupt control row. |
| 4 | `request_turn_cancel(hold_session)` (`db.py:4079`) | `cancel_task` (`db.py:5901`) | enqueues the control row | — | twin (ledger vs legacy) | **KEEP managed, retire legacy** once enrollment is universal. Remove the `hold_session` side-effect from the *interrupt* use (it belongs to Close). |
| 5 | `_close_managed_session` (`orchestrator.py:11625`) | `session_service.close_session` + `_dispatch_remote_close` (`orchestrator.py:10256`) | `close_session` row yes | teardown may defer | twin | **MERGE.** One Close path. `close_session` (teardown) correctly waits for the in-flight turn (`_wait_for_inflight_turn`) and is correctly allowed to be queued. |
| 6 | `close_session_turns` (`db.py:4204`) | `cancel_task` legacy close | direct txn + withdraws rows | — | complement | **KEEP.** This is the real Close-unit DB op (withdraw queued + hold). Not a duplicate; it is where session-close *should* live (and where pause/hold from #2 should move). |
| 7 | `set_turn_queue_paused` / `set_queue_paused_sync` (`db.py:5141`, `turn_admission.py:314`) | — | **No** (direct) | n/a | no | **KEEP but UNBIND from interrupt.** Pause is a legitimate standalone operator control; it must not be a hidden side-effect of Stop. |
| 8 | `cancel_codex` action (`worker/agent.py:782`) | n/a (codex-specific) | yes | yes | no (backend-specific) | **KEEP**, fold under the one interrupt action dispatched by backend type. |
| 9 | `interrupt_case` (`orchestrator.py:4027`) / `POST /api/cases/{id}/interrupt` | — | iterates → `cancel_task` per task | yes | no | **KEEP** (case-level safety valve); it inherits whatever #1–#4 converge to. |

**Not duplicates (leave alone):** `synthesize_managed_terminal`, `release_superseded_managed_grants`,
`list_stale_managed_children`, `get_pending_managed_turns`, `managed_waiting_totals`, etc. These are
A82 managed-queue *internals*, not legacy twins — new machinery with no clean-named predecessor.
Flagging them as "managed duplication" would be wrong.

---

## 4) The reaper, in plain words (why your interrupt was eaten)

The **pending-reaper** (`_reap_stale_pending_once`, `src/control/task_server.py:1889`; classifier
`list_stale_pending_tasks`, `src/control/db.py:5946`) is just a **janitor**: every ~30 s it deletes
`mesh_tasks` rows that *can never be claimed* — pinned to a dead/unknown node, or **bound to a
session whose status is `closed`/`cancelled`** (`db.py:6014`, fires **immediately, no grace**).

It is not the villain. The villain is the **ordering in Stop**: step (2) marks the session
`cancelled` in the *same breath* as step (3) enqueues the `cancel_managed` interrupt. The janitor
then correctly sees "a pending task on a cancelled session" and sweeps the interrupt — **1 second
later, before the carrier's ~2 s poll could claim it.** Live proof:

```
cancelm-task_edf1ccb2-…  status=cancelled  claimed_by=None
error = "pending reaped: session_closed (machine_id='Horse', age=1s)"
```

Same minute, session `f316e5b33df9` *won* the race (Horse claimed in 2 s before the sweep) →
`"interrupt delivered or armed"`. Identical code, coin-flip outcome. So Stop could reap its own
interrupt. ✅ **FIXED:** the operator *wants* the session held `cancelled` (good UX), so the fix
keeps the hold and instead **exempts control/teardown actions from the `session_closed` rule**
(`_CONTROL_TEARDOWN_ACTIONS` in `list_stale_pending_tasks`). `node_offline`/`age_exceeded` still
apply, so a genuinely orphaned control row (dead carrier) is still retired — no leak.

---

## 5) Behind-the-queue audit (your columns 3 & 4)

| Action/row | Behind Q (today)? | Should be behind Q? | Gap |
|------------|-------------------|---------------------|-----|
| `create_session` / `resume_session` / `run_oneoff` / `compact_session` | Yes | Yes | ok — real work turns |
| `close_session` (teardown) | Yes | Yes (may defer) | ok |
| `cancel_turn` (interrupt) | Yes (poll-claimed) | **No — immediate/push** | **GAP** — latency + reap-race |
| `cancel_managed` (interrupt) | Yes (poll-claimed) | **No — immediate/push** | **GAP** — latency + reap-race |
| `cancel_codex` (interrupt) | Yes (poll-claimed) | **No — immediate/push** | **GAP** |
| queue **pause** flag | No (direct) | n/a (standalone control) | but must stop being a Stop side-effect |
| session **cancelled hold** | No (direct) | belongs to Close only | **GAP** — fires on interrupt today |
| human message after Stop | **Yes, stuck `queued`** | No (should flow once turn stops) | **GAP** — stranded by the persistent pause |

**Reading:** the three interrupt rows are "behind Q" (poll-claimed) when they should be pushed; the
pause + session-hold are "direct" when they should not fire on an interrupt at all; and your
follow-up messages get parked behind the persistent pause. The wiring is inverted vs §0.

---

## 6) Cleanup checklist

- [x] **Stop actually lands (the real bug).** Exempt control actions (`cancel_turn`,
      `cancel_managed`, `cancel_codex`, `close_session`) from the no-grace `session_closed` rule in
      `list_stale_pending_tasks` (`src/control/db.py`). Shipped in `fix/cancel-interrupt-reaper-race`
      with `tests/test_pending_reaper.py::test_control_action_on_cancelled_session_is_exempt` and
      `…_still_reaped_when_carrier_offline`. **Pause + hold are kept — they are desired UX.**
- [ ] **One interrupt action (dedup).** Converge `cancel_turn` + `cancel_managed` (+ `cancel_codex`
      by backend) into a single turn-uuid-fenced interrupt; keep the managed fencing, retire/alias
      the coarse `cancel_turn` twin. *(Deferred: not required for Stop to work; touches the legacy
      fallback used by non-enrolled sessions + `interrupt_case` — do it when all sessions are
      enrolled, with its own tests.)*
- [ ] **One Close path (dedup).** Merge `_close_managed_session` ↔ `session_service.close_session` /
      `_dispatch_remote_close`. Session unit only; no functional break today.
- [ ] *(optional, later)* **Make interrupt push, not poll.** Signal the carrier the instant the
      interrupt row lands instead of waiting ~2s for its poll. The reaper fix already makes the
      poll path correct; this is a latency nicety.
- [ ] *(goodie, §8)* **Operator queue control** — delete / reorder / jump-to-front for queued
      messages while paused.

---

## 7) Runtime behaviour, confirmed (what actually happens)

Grounded in `src/control/db.py` (`enqueue_turn`, `select_eligible_turn_heads` ~3628,
`set_turn_queue_paused` ~5141) and `turn_scheduler.py`; tests noted.

| You do… | What happens | Evidence / test |
|---------|--------------|-----------------|
| **Send while a turn is RUNNING** | **Queued, FIFO. Does NOT interrupt.** (Changed from the old pre-queue "message interrupts" behaviour — send ≠ interrupt now.) | `enqueue_turn` has no preempt path · `test_TG_R1_…` |
| **Send while queue is PAUSED** | **Accepted and queued** (admission is *not* gated by pause; only *activation* is). Not rejected, not immediate — it sits. | `select_eligible_turn_heads` filters `turn_queue_paused=0` · `test_TG_R1_…` (43–48) |
| **Send A, then B** | **Both rows kept, A then B, FIFO.** B does NOT supersede/overwrite A. Humans are forbidden from coalescing (`db.py:3135` raises on a human `coalesce_key`). | `test_ADM07b_human_turn_cannot_coalesce` |
| **Resume** | Clears pause + operator hold, hints scheduler; it then activates the **oldest** queued turn first (strict FIFO by `queue_sequence`). Does not drain in a loop; does not let the newest run first. | `set_turn_queue_paused(False)` + `_release_stop_hold` · `test_TG_R1_…` (49–56) |
| **Jump the queue / overwrite?** | **Not possible today.** No priority lane, no human supersede, no "latest wins". The whole backlog drains in order. | — (no such path exists) |

**Test gaps:** no test asserts the interrupt actually lands on the carrier under a cancelled
session (the reap race — now covered by the two new reaper tests at the DB layer); and there is no
"newest-wins / jump / overwrite" test because that capability does not exist yet.

---

## 8) Desired behaviour (spec for the cleanup) + the "goodie"

**Target, per operator:**
1. **Cancel = immediate interrupt that actually lands**, aborting only the live turn, leaving the
   session open and the process pooled. ✅ now lands (§6 item 1).
2. **Cancel pauses the queue + holds the session** so follow-ups don't pour in. ✅ keep as-is.
3. **One** `cancel_turn` command; the Stop orchestration calls it (pause → interrupt → hold is fine
   orchestration). Collapse the duplicate stack (§6 dedup items).

**Goodie (nice-to-have, not urgent):** operator **queue control** while paused —
- **delete** one/all queued messages,
- **reorder** them, and/or
- **jump to front** so the newest message runs next instead of after the whole backlog.

This is only needed because a mis-timed cancel can leave stale messages queued; with cancel now
landing reliably it should rarely matter. Would be a new capability on the admission/queue layer
(a priority or an explicit "move to head" that rewrites `queue_sequence`), plus tests — none of
which exists today.

---

**Bottom line:** `cancel_managed` was never a rogue teardown — on the worker it is a *surgical*
turn interrupt. Stop *felt* broken because its own `session_closed` hold let the reaper eat the
interrupt (now fixed), and because queued messages can only drain FIFO (a missing capability, §8).
Pause + hold on cancel are good and stay. The remaining work is pure de-duplication (§6) and the
optional queue-control goodie.
