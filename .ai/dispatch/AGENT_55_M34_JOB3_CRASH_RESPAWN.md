```yaml
job_id: AGENT_55_M34_JOB3_CRASH_RESPAWN
created_at: "2026-07-30T02:34:12+03:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: ""
depends_on: AGENT_54_M34_JOB2_RECONSTRUCTION
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T13:34:21.157521+00:00"
```

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
- [x] respawn path on the dead-session branch (reconstruct → respawn → re-arm → resume)
- [x] single-flight guard (atomic claim), no second lock model
- [x] anti-goal test (same flow_run_id, no new Case)
- [x] e2e green, byte-identical when flag OFF
- [x] PR opened (merge left to Manager review)

## Closure (2026-08-03)

**Verdict: BUILT — all four acceptance criteria proven with a fake backend, no paid CLI.**

**What landed**
- `db.py`: `RESPAWN_ACTION = "manager_respawn"` + `respawn_task_id(case, gen)` →
  `"respawn:{case}:{gen}"` (a DISTINCT id namespace from the `cont:` wake row, riding
  the SAME `CONTINUATION_MACHINE_SENTINEL` + the SAME `claim_task`/reaper — no second
  lock model, no new table/column).
- `orchestrator.py`: dead-session branch of `_continue_case_once` now attempts
  `_respawn_manager_for_case` before falling back to the strand escalation. The new
  method reconstructs via `get_case_brief` (A54), atomically claims the single-flight
  respawn row, spawns ONE role-full Manager session via `session_service.create_session`,
  binds it to the SAME Case (`create_flow_link` manager link + affiliation — NEVER
  `open_case`), runs `boot_reconcile_case` to re-arm waits/groups, and delivers a
  role-full resume turn (`_render_respawn_turn`). `case.manager_respawned` marker recorded.

**Single-flight (atomic, not TOCTOU):** the winner is decided by
`claim_task = UPDATE mesh_tasks SET status='claimed' WHERE id=? AND status='pending'`
+ `changes()>0`, inside a `_write()` txn (SQLite serializes writers). Two racing ticks
enqueue the SAME deterministic id (UNIQUE collapses to one row); exactly one UPDATE flips
`pending→claimed`. The loser gets False and returns `True` ("a respawn is owned — don't
escalate/double-spawn"), spawning nothing.

**Crash recovery:** the respawn row is `claimer_incarnation`-stamped; a crash between
claim and spawn leaves it `claimed` by a dead incarnation → `list_stale_claims` reaps it
→ `release_task` → `pending` → a later tick re-claims and retries. No permanent stall. A
spawn failure AFTER the claim also releases the lease and escalates the strand.

**Anti-goal:** no `open_case`, no new `flow_run_id` — proven by
`test_respawn_preserves_flow_run_id_and_creates_no_new_case` (open-case set unchanged,
objective_lock preserved, new session `current_case_id == case_id`).

**Refusal:** `blocked`/`interrupted` (operator-halted) Cases are excluded by the existing
`blocked` guard in `_continue_case_once` (short-circuits BEFORE the dead branch) — proven
by `test_blocked_case_with_dead_manager_is_not_respawned`.

**Flag OFF byte-identical:** with `CASE_CONTINUATION_ENABLED` OFF the tick returns 0 at the
top gate and the loop never starts — the dead-session branch is unreachable (pre-A55
behaviour). Respawn additionally re-gates on the flag defensively.

**Node placement:** reuse the dead Manager's recorded node (`session.machine_id`) if the
row is readable, else gateway host (`__local__`). Remote-node MCP reachability is the known
deferred item — a remote pin is honoured (the mesh path owns reachability); if the spawn/
first-turn then fails, the lease is released and the strand escalated (surfaced, not hacked).

**pytest evidence (from inside the worktree; `src.orchestrator.__file__` =
`/home/cifran/dev/AI-team-wt/a55-respawn/src/orchestrator.py`):**
`tests/test_case_respawn.py` 9 passed · `tests/test_case_continuation.py` +
`tests/test_case_brief.py` + `tests/test_control_api_wait_group.py` → 50 passed together ·
adjacent `tests/test_manager_role.py` + `tests/test_manager_carrier_role.py` +
`tests/test_control_api.py` → 73 passed. No paid CLI, no `--run-e2e`.
