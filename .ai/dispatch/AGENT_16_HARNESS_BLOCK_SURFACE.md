# A16 — WebUI-first surfacing of the Level-3 admission block

> ⚠️ **TEST COST GUARD.** No paid Claude/Codex CLI in tests. Do **not** run the
> full e2e suite. Do **not** run `python main.py status` (it kills the live PM2
> gateway). Check the running gateway with `curl http://127.0.0.1:9003/health`.

**Theme:** Close the surface half that T5 (A9H, commit 5) deliberately deferred.
The backend already raises the typed `HarnessAdmissionBlocked` signal at the
`_enqueue_task` choke point when `HARNESS_LEVEL3_GUARD` is armed and an
un-approved Level-3 task is submitted. **No operator-facing surface renders it
yet.** On the Web lane the exception currently escapes `/api/instructions`
unhandled → an opaque **500**, and (for a session submit) the session is left
stranded **BUSY** because `mark_busy` ran before the blocked `submit_instruction`.

**Level:** 2 (standard). Localized, additive, fully reversible. The gate logic
(`_harness_level3_allows_autopickup`) is **not touched** — this is error
translation + presentation only. The guard is OFF by default, so the changed code
path is unreachable in production until an operator arms `HARNESS_LEVEL3_GUARD`.

```xml
<task_packet>
  <objective_lock>
    <real_objective>When the Level-3 admission guard blocks a Web-submitted task,
      the operator must see a clear "needs approval" result — not a 500, and not a
      session stuck BUSY forever.</real_objective>
    <literal_request>Advance the harness pipe: surface the blocked signal in the WebUI
      (A9H "Next").</literal_request>
    <interpreted_task>Translate HarnessAdmissionBlocked into a clean 409 with a
      stable machine reason + human copy at the control-API write surface, revert the
      stranded session to IDLE, and replace the misleading generic "tap send to retry"
      composer message with an approval-needed message for that 409.</interpreted_task>
    <constraints>
      - ZERO new gateway state (spec §0/§11). No DB, no flow table.
      - Do NOT modify the gate decision function or the orchestrator raise site.
      - Byte-identical when guard is OFF (default) — the new path is unreachable.
      - Client owns wording; backend emits a stable `reason` + a human `detail`
        (mirror the existing invalid_repo_path envelope pattern).
      - No paid CLI in tests; no `python main.py status`.
    </constraints>
    <non_goals>
      - Telegram surfacing (out of scope; WebUI-first per T5). The typed signal
        already reaches Telegram; its reply wiring is a later task.
      - An approval WORKFLOW (approve-from-UI). This only *reports* the block.
        Auto-approval / durable approval gate is deferred (CONTEXT Deferred #25).
      - Changing when/why a task is Level 3.
    </non_goals>
    <assumptions>
      - `apiClient.post` already unwraps `{detail:{reason,detail}}` → ApiError.message.
        (Verified: apiClient.ts lines 72-90.)
      - Composer renders `submit.isError`. (Verified: Composer.tsx line 97/114.)
      - 4xx is already non-retryable in useSubmitInstruction. (Verified: line 42.)
    </assumptions>
    <drift_risks>
      - Over-building a UI approval flow (scope creep) → explicit non-goal.
      - Touching the gate predicate → forbidden; only the surface changes.
    </drift_risks>
  </objective_lock>

  <approved_plan>
    <steps>
      1. session_service.mark_idle(session_id) — revert BUSY→IDLE (mirror mark_busy/
         mark_cancelled). Returns CommandResult.
      2. control_api: add `harness_level3_needs_approval: 409` to _REASON_STATUS;
         wrap BOTH submit_instruction calls in /api/instructions; on
         HarnessAdmissionBlocked → revert session (if any) to IDLE, raise
         HTTPException(409, detail={ok:False, reason, detail:<human>, task_id}).
      3. Composer: when submit error is a 409 admission block, show the human
         approval message instead of "tap send to retry".
      4. Tests: blocked one-off → 409 + reason; blocked session → 409 + session IDLE.
    </steps>
    <validation>
      - `pytest tests/test_control_api_write.py -q` green (targeted, no paid CLI).
      - `pytest tests/test_harness_level3_guard.py -q` still green (unchanged gate).
      - Frontend: `npm run build` / typecheck for the Composer change if toolchain present.
    </validation>
    <definition_of_done>
      - Blocked Web submit returns 409 (not 500) with reason=harness_level3_needs_approval.
      - Blocked session is IDLE, not BUSY.
      - Composer message distinguishes "needs approval" from "retry".
      - Guard OFF ⇒ byte-identical (a normal submit still 200s; existing tests pass).
      - Docs reconciled: harness merged; A16 logged; "Next" ticked.
    </definition_of_done>
    <risks>
      - Low. Reversible (git revert). Unreachable path under default flag.
    </risks>
  </approved_plan>

  <execution_rules>
    <do>Mirror existing envelope + status-map conventions. Keep the diff small.</do>
    <do_not>Touch the gate predicate, add gateway state, or build an approval UI.</do_not>
    <report_format>Milestone file + F-tag self-review + closure summary.</report_format>
  </execution_rules>
</task_packet>
```

---

## Milestone

**Status:** closed

**Burndown:**
- [x] 1. `session_service.mark_idle()` — BUSY→IDLE revert helper
- [x] 2. `control_api` — 409 translation + session revert + reason in `_REASON_STATUS`
- [x] 3. `Composer.tsx` — distinct approval-needed message for 409
- [x] 4. Tests — blocked one-off (409+reason) & blocked session (409+IDLE)
- [x] 5. Adversarial F-tag self-review + fixes (5 tags; no P0/P1; see below)
- [x] 6. Run targeted pytest (no paid CLI) — 64 control-API + 25 session + 689 collect clean
- [x] 7. Docs reconciled (harness merged, A16 row, "Next" ticked)

**Live log:**
- 2026-07-04: branch `feat/harness-block-surface` cut from main; packet + milestone written.
- 2026-07-04: backend done — `mark_idle` added; `/api/instructions` wraps both submit lanes,
  reverts BUSY→IDLE on block, raises 409 via `_harness_blocked_http`. Gate predicate untouched.
- 2026-07-04: merged `origin/main` (A13-A15 doc restructuring landed under this branch's dispatch
  numbers meanwhile) — renumbered this dispatch A13 → A16 to resolve the collision, folded this
  `.milestone.md` sibling into the one-file convention (A14 contract).

---

## Adversarial self-review (F-tags)

Reviewed the shipped diff (P0/P1 focus, house style §14). Outcome per tag:

- **[F1] Idempotency lock/cache poisoning on the raise path.** The 409 is raised
  *inside* `async with _idem_guard_async(...)`. **Verified safe:** the guard uses
  `async with asyncio.Lock`, released by `__aexit__` on exception; `_idem` is only
  written by `_idem_put`, which the block path never reaches. So the lock frees and
  no failure is cached — a retry with the same key correctly re-evaluates (and can
  succeed once the task is approved). *No change needed.*
- **[F2] Composer surfaces `error.message` for ANY 409, not just harness blocks.**
  For `/api/instructions` the only 409 today **is** the admission block (404 =
  session-not-found; 400 = bad backend/model). Even for a future 409, rendering the
  backend's curated human `detail` is correct, not misleading. *Accepted.*
- **[F3] `mark_idle` could clobber a genuinely running task.** The orchestrator
  raises `HarnessAdmissionBlocked` at admission **before** any queue side-effect, so
  no task is running when the except branch fires. Revert-to-IDLE is correct.
  *No defect.*
- **[F4] Concurrent submit onto an already-BUSY session → IDLE while a prior task
  runs.** Unreachable in normal use: the Composer hides the send control (shows Stop)
  while `running`, and the whole path needs `HARNESS_LEVEL3_GUARD` armed. Narrow
  edge, pre-existing shape, out of scope. *Accepted risk, documented.*
- **[F5] Per-request local import of `HarnessAdmissionBlocked`.** `sys.modules` dict
  hit; matches the file's existing local-import idiom (`SessionOrigin`) that avoids
  orchestrator↔control import cycles. *Accepted.*

No P0/P1 defects. 689 tests collect clean; 64 control-API + 25 session-service
tests green; web typecheck clean.

## Closure

**Shipped (branch `feat/harness-block-surface`):** the Web lane now translates the
Level-3 admission block into a clean **409** (`reason=harness_level3_needs_approval`,
human `detail`, echoed `task_id`) instead of an opaque 500; the optimistically-BUSY
session is reverted to **IDLE** via the new `session_service.mark_idle`; and the
Composer shows the approval-needed copy instead of the misleading "tap send to
retry". Gate predicate untouched; guard OFF ⇒ byte-identical (existing suites green).

**Follow-ups (not in scope):** Telegram reply wiring for the same signal; an actual
approve-from-UI workflow (deferred — CONTEXT Deferred #25). **Phase 2 still NOT
justified** — this rode entirely on existing state + envelope conventions.
