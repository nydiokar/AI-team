# DISPATCH — A60 · Warm-worker idle-reaper (§7 resource leak)

**Level:** 3 (worker lifecycle) · **Type:** code
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, not yet executed)
**Depends on:** — (independent; complements A52/A55 autonomy). **Unblocks:** safe *long* autonomous runs.
**Ultimate goal this serves:** `SPEC_COMPLETION_PLAN.md` §0 bounded-resources clause; the §7 deferral from A48.

> **Outcome, not a script.** A48 (PR #26) deliberately made joined worker sessions **warm** on Case-close —
> closed only by the Manager's explicit `release_worker`. That was the right call for re-dialogue, but it left
> a **written §7 deferral**: warm workers hold a backend slot with **no idle-reaper**, so accumulation is
> bounded only on Manager discipline. Once autonomous runs get long (A52+), that is a real resource leak.

## Why (intent)
CONTEXT.md §7 note (2026-07-18 STATUS block) and the A48 closure both record: "warm workers hold a backend
slot with no idle-reaper — unbounded accumulation gated only on Manager discipline. Acceptable now; size a
bound/idle-reaper if live load shows it." Autonomous continuation (M3.4, now merged) is exactly the "live
load gets long" condition that promotes this from acceptable-deferral to must-fix.

## TASK
Add a bounded idle-reaper for **warm worker sessions**: a warm worker idle (no turn, not joined to any OPEN
Case) beyond a configurable TTL is closed and its affiliation cleared — reusing the existing session-close +
affiliation-clear plumbing (the same path `close_case`/`release_worker` already use), not a new lifecycle.
A worker still joined to an open Case, or mid-turn, is never reaped.

## TYPE
code. Branch `feat/warm-worker-idle-reaper`; PR at close, merge yourself.

## CONTEXT (reuse verbatim)
- Warm-keep policy: A48 / PR #26 (`feat/manager-decided-worker-close`) — removed auto-close-on-Case-close.
- `release_worker` guard + session→case index: PR #27 (`/api/work/affiliations/sessions`), `case_id`-scoped.
- Existing reaper template: the mesh **stale-claim reaper** / incarnation sweep (T4, `feedback_worker_restart_drops_claim`)
  and the M3.4 stale-busy reconciler loop — mirror the periodic-sweep shape, do not fork a new scheduler.
- Session close + affiliation clear: the same seam `close_case` uses to close `case_role='worker'` sessions (PR #22).

## ACCEPTANCE (proof, not vibes)
1. A warm worker idle > TTL and NOT joined to an open Case is reaped: session closed, affiliation cleared,
   backend slot freed; an event is recorded (reuse existing vocab).
2. A warm worker joined to an OPEN Case, or with activity inside the TTL, is **never** reaped.
3. TTL is configurable with a default; default chosen so it does not fight normal re-dialogue latency.
4. Idempotent; a second sweep over an already-reaped worker is a no-op. Flag/default leaves behavior safe.
5. Targeted pytest green (worker lifecycle + affiliation + close tests).

## REALITY CONSTRAINTS
- **Do NOT** reap the Manager session or any non-worker session. Scope strictly to `case_role='worker'`, idle,
  Case-unbound (or closed-Case) sessions.
- **Do NOT restart or disrupt a live worker** — reaping is for idle warm sessions only; §0/CLAUDE.md worker-restart
  rule still holds (a live worker restart is an operator decision).
- Reuse the periodic reconciler loop; do not add a second scheduler thread.

## RESERVED DECISIONS
- **R1 — TTL default + whether it is flag-gated.** Recommendation: ship with a generous default TTL and a config
  knob; a flag is optional since the behavior is a bounded cleanup, not a semantics change — engine owner decides.

## SCOPE OUT
Turn/cost governor (A53); wake-dispatch (A52); any change to the warm-keep *policy* itself (A48 stands).

## TRAIL / EVIDENCE (fill at close)
- Branch / PR · pytest output · TTL knob name + default · the reaper seam reused.

---
## Milestone (burndown)
- [ ] idle warm-worker reaper on the existing sweep loop (TTL-bounded)
- [ ] never reaps open-Case-joined / mid-turn / non-worker sessions
- [ ] idempotent + configurable TTL + safe default
- [ ] targeted pytest green
- [ ] PR opened + merged

## Closure (fill on completion)
_(verdict + evidence)_
