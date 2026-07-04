# Milestone: A13 — WebUI-first surfacing of the Level-3 admission block

## Objective
Translate the backend `HarnessAdmissionBlocked` signal into an operator-facing
result on the Web lane: a clean 409 (not a 500), a session that reverts to IDLE
(not stranded BUSY), and a Composer message that says "needs approval" instead of
the misleading "tap send to retry". Backend gate logic untouched; guard OFF by
default keeps the path unreachable in production.

## Current Status
closed

## Burndown
- [x] 1. `session_service.mark_idle()` — BUSY→IDLE revert helper
- [x] 2. `control_api` — 409 translation + session revert + reason in `_REASON_STATUS`
- [x] 3. `Composer.tsx` — distinct approval-needed message for 409
- [x] 4. Tests — blocked one-off (409+reason) & blocked session (409+IDLE)
- [x] 5. Adversarial F-tag self-review + fixes (5 tags; no P0/P1; see packet)
- [x] 6. Run targeted pytest (no paid CLI) — 64 control-API + 25 session + 689 collect clean
- [ ] 7. Docs reconciled (harness merged, A13 row, "Next" ticked)

## Live Log
- 2026-07-04: branch `feat/harness-block-surface` cut from main; packet + milestone written.
- 2026-07-04: backend done — `mark_idle` added; `/api/instructions` wraps both submit lanes,
  reverts BUSY→IDLE on block, raises 409 via `_harness_blocked_http`. Gate predicate untouched.

## Blockers
(none)

## Next Action
Reconcile `.ai` docs, commit on branch. (Merge = operator decision.)
