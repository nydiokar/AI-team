# DISPATCH — A55 · M3.4 Job 3: crash-respawn dispatcher path

**Level:** 3 (orchestrator Wake-Dispatcher + session respawn) · **Type:** code
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, not yet executed)
**Depends on:** A54 (Job 2 reconstruction) — hard. Benefits from A53 (governor). **Ultimate goal this
serves:** `SPEC_COMPLETION_PLAN.md` §0 goal step 6 ("survive process restarts").

> **Outcome, not a script.** When the Manager session driving a Case is dead, the harness itself brings a
> role-full Manager back and resumes the SAME bounded Case — exactly once, never inventing new work. The
> engine owner decides the respawn mechanics; acceptance is the contract.

## Why (intent)
Design §7 job 3. Job 1's Wake-Dispatcher deliberately does NOTHING when it finds the bound Manager session
dead (`_continue_case_once` returns 0 on `session is None`). That is the intended Job-1 behavior and the
last gap in "survive a restart." Job 3 fills it — but ONLY on top of Job 2, because a respawn is unsafe
unless the Case can be fully reconstructed from the DB first.

## TASK
When a Wake-Dispatcher tick finds a satisfied Case whose Manager session is dead, reconstruct via
`get_case_brief` (A54), respawn one role-full Manager bound to that Case, re-arm its waits/groups, and
resume — with strict single-flight so a racing tick never double-respawns.

## TYPE
code. Branch `feat/m34-job3-crash-respawn`; PR at close, merge yourself.

## CONTEXT (reuse verbatim)
- The dead-session branch to replace: `orchestrator._continue_case_once` (`session is None → return 0`).
- Reconstruction: `db.get_case_brief` + the boot reconcile hook (A54). Respawn: the existing Manager
  role-boot path (`_invoke_manager`/`_role_boot`) — reuse it, do not build a second boot.
- Single-flight: the `mesh_tasks` incarnation/reaper already elects one owner (the same mechanism the
  continuation lease uses). Reuse it for "one active invocation per Case"; do NOT add a second lock model.

## CHANGES
- **(a) Respawn path.** In the dead-session branch, if continuation is enabled and the Case is satisfied,
  reconstruct via `get_case_brief`, respawn a role-full Manager session bound to the Case (correct
  `case_role`, tools, prior-context seed), re-arm waits/groups, then let the normal continuation wake drive
  it. Until this lands the branch stays a no-op (unchanged).
- **(b) Single-flight guard.** Ensure at most one respawn in flight per Case (atomic claim on a
  deterministic respawn token, mirroring the continuation lease). A racing tick gets False and stops.
- **(c) Anti-goal guard.** The respawn continues ONLY the same bounded Case (objective-lock preserved); it
  never opens or infers new work. Assert this in a test.

## ACCEPTANCE (proof, not vibes)
1. e2e (fake backend): open a Case, dispatch + finish a worker so it is satisfied, then mark/kill the
   Manager session dead; run a tick → assert exactly one role-full Manager is respawned bound to the Case,
   waits/groups re-armed, and it resumes toward close.
2. e2e: two concurrent ticks against the dead-session Case respawn exactly ONE Manager (single-flight).
3. e2e: the respawned Manager's Case is the SAME `flow_run_id` with the SAME objective — no new Case is
   created (anti-goal preserved).
4. Flag OFF ⇒ byte-identical (dead-session branch stays a no-op).

## REALITY CONSTRAINTS
- Respawn requires Job 2 (A54) merged — do not attempt reconstruction ad-hoc here.
- Do not respawn a Case that is `blocked`/`interrupted` (operator-halted) — only genuinely open+satisfied
  Cases with a dead live session.

## RESERVED DECISIONS
- Node placement of the respawned Manager (gateway host vs the Case's original node). Recommendation: reuse
  the Case's recorded node if reachable, else gateway host; escalate if remote-node MCP reachability blocks
  it (a known deferred item).

## SCOPE OUT
M4; the intra-task executor spike (A57).

## TRAIL / EVIDENCE (fill at close)
- Branch / PR · pytest output · the single-flight + same-Case assertions.

---
## Milestone (burndown)
- [ ] respawn path on the dead-session branch (reconstruct → respawn → re-arm → resume)
- [ ] single-flight guard (atomic claim), no second lock model
- [ ] anti-goal test (same flow_run_id, no new Case)
- [ ] e2e green, byte-identical when flag OFF
- [ ] PR opened + merged

## Closure (fill on completion)
_(verdict + evidence)_
