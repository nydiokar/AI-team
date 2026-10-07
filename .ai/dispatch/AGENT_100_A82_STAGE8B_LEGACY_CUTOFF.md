```yaml
job_id: AGENT_100_A82_STAGE8B_LEGACY_CUTOFF
created_at: "2026-10-07T16:28:38.145464+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-07T17:14:58.132428+00:00"
```

# DISPATCH — AGENT_100_A82_STAGE8B_LEGACY_CUTOFF

**Level:** 3 (deletes control-plane execution code + its tests; crosses the admission seam) — needs
operator go + the gating proofs below before execution. Authored `ready`, NOT started.
**Type:** fix/refactor (legacy-path deletion = the A82 convergence cutoff)
**Authored:** 2026-10-07
**Depends on:** A82 Stage-8a merged + live (done). **GATED** on the two live proofs below.
**Branch:** one `feat/a82-stage8b` branch + PR (src/ change).

## Read this first
- `.ai/dispatch/A82_E2E_CERTIFICATION.md` — §5 is the precise deletion scope + preconditions; §8 is
  the burndown. This packet executes §5.
- `.ai/dispatch/AGENT_82_SESSION_TURN_QUEUE.md` — the backend contract; L2432/L2448 flag the legacy
  branches as "8b deletes".
- `.ai/dispatch/A82_STAGE8A_BACKEND_AUDIT.md` §6 — the dual-front-door fold plan.

## Why
Stage-8a flipped the cutover (born-managed sessions, `WORKER_MANAGED_TURNS` default-ON) and made the
legacy session-execution / single-flight-respawn path **unreachable in production** (guarded by
`_LEGACY_SESSION_EXECUTION_RETIRED = True`). Both paths now coexist in code — the convergence is
behaviorally done but the **cutoff (deleting the dead legacy path) is not**. Leaving it is exactly the
"half-implemented" state the operator flagged: a dormant second execution path + a dual admission
front-door that every future reader/maintainer must reason around.

## TASK (execute cert §5 — do NOT start until GATE is cleared)
Delete the now-dormant legacy path and fold the dual front-door into one, with tests proving the
managed path is the sole survivor. Concrete targets (verified present on `main`):
1. `_LEGACY_SESSION_EXECUTION_RETIRED` flag + the legacy branch it guards — `src/orchestrator.py:12281`
   (branches `:11161`, `:12286`).
2. `_do_respawn_manager_for_case` legacy single-flight branch — `src/orchestrator.py:3900`.
3. Legacy `RESPAWN_ACTION` single-flight respawn mechanics + pure-legacy tests —
   `tests/test_case_respawn.py` (packet L2406 flags 5 tests as retire-only).
4. Legacy `/api/instructions` non-enrolled execution branch + the bypassed in-memory
   `SessionTaskQueue` path for enrolled sessions (`orchestrator.py:6265`).
5. Fold the dual front-door — `routes/turn_requests.py:115` REVISIT note + the `/api/instructions`
   enrolled branch in `routes/sessions.py`.
6. The 14 harness-only converted tests + their `_setup(enroll=False)` fixtures that only run with
   `_LEGACY_SESSION_EXECUTION_RETIRED` flipped False (packet L2401/L2448). KEEP the operational
   switches `_REFUSE_SESSION_TURNS_WITHOUT_MESH` (`:12273`) and `_QUEUE_TURNS_FOR_OFFLINE_CARRIER`
   (`:12313`) — they are not legacy-exec.

## GATE — CLEARED 2026-10-07 (operator authorized execution)
* **Operator go** on the delete-now-vs-later fork — **GIVEN 2026-10-07** ("if the cutoff is to be
  done now just do it, in case you are sure"). Deletion is git/GitHub-reversible; Manager is sure
  (preconditions below met; legacy path unreachable in prod behind `_LEGACY_SESSION_EXECUTION_RETIRED`).
* ~~A Codex AND an opencode-server managed turn proven live~~ — **MET 2026-10-07**
  (`A82_MULTIBACKEND_VALIDATION.md`: Codex `task_a0e13623` + opencode-server `task_b6557da4` both
  completed managed live. The cert §4(ii) "Codex has no managed methods" warning was STALE/WRONG —
  `codex_native.py:373/:385` exist.)
* ~~One agent-source send proven live~~ — **MET 2026-10-07** (same doc: `task_834db4e9` admitted
  `turn_source=agent`, FIFO seq 2, no clobber). *Note: opencode-server cannot source agent sends by
  design — `opencode.py:1297` `provision_sender_capability` returns False (shared per-process MCP);
  documented limitation, not a gap.*
* A84 carry (o) Case-outbox — **DECOUPLED / accepted out-of-scope for the cutoff** (operator: don't
  wait on it). It is orthogonal durability work (completion *delivery*), not the legacy *execution*
  code this packet deletes. Tracked separately under A84; A84 slice-2 builds on this post-cutoff base.

**Gate status: CLEARED — executing. Agent MUST respect in-code `REVISIT`/TODO/docstring notes and
the packet's test-retirement guidance (keep tests that encode real managed behavior; retire only the
pure-legacy ones). Prove no behavior change before merge.**

## ACCEPTANCE — done only when all true
* The §5 targets are deleted; `grep` shows no remaining `_LEGACY_SESSION_EXECUTION_RETIRED` /
  legacy-branch references; the dual front-door is one path.
* Targeted pytest green on the touched modules (NEVER the full/e2e suite); the retired legacy tests
  are removed, not skipped; managed-path tests still green.
* `web` typecheck/test green if any UI touches the folded route.
* Live smoke on the running gateway: a born-managed session still submits + runs a turn FIFO
  (regression proof the cutoff changed nothing observable).
* PR opened + merged to `main`; gateway redeployed; CONTEXT/DISPATCH_LOG A82 rows closed out.

## RESERVED DECISIONS
Operator owns the GATE above (go + sequencing). No deletion before the gate clears.

## SCOPE OUT
No new features; no change to the managed path behavior; no removal of the operational switches
(`_REFUSE_SESSION_TURNS_WITHOUT_MESH`, `_QUEUE_TURNS_FOR_OFFLINE_CARRIER`); no telemetry-DB changes.

## TRAIL
PR + merge + redeploy; append closure here; flip CONTEXT/DISPATCH_LOG A82 to done.

## Milestone checklist
- [ ] GATE cleared (operator go + multi-backend live proof + agent-send live proof + carry (o))
- [ ] §5 deletion targets removed; dual front-door folded
- [ ] Targeted tests green; retired legacy tests removed (not skipped)
- [ ] Live FIFO regression smoke on a born-managed session
- [ ] PR merged + gateway redeployed + A82 docs closed out

## Closure
_(append the closure summary here on completion — do not create a sibling file)_
