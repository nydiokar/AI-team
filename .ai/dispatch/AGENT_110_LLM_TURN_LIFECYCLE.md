```yaml
job_id: AGENT_110_LLM_TURN_LIFECYCLE
created_at: "2026-11-04T02:11:25.550323+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A110             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["tests/test_telemetry_projection.py","tests/test_telemetry_store.py","tests/test_turn_queue_a84_effects.py"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-04T22:48:14.972264+00:00"
```

# DISPATCH — 110 · Report queued requests separately from executing LLM turns

**Level:** 3 (operator-reported lifecycle, telemetry/read-model and persisted projection) · **Type:** code
**Authored:** 2026-11-04 · **Authorization:** operator explicitly requested implementation and shipping.
**Depends on:** none; A109 is the higher-priority completion-delivery repair.
**Branch:** `feat/watched-job-consumption` (shared incident fix, separate acceptance/evidence).
**Issue:** https://github.com/nydiokar/AI-team/issues/273

> Session `a32f0cf19326` displays four running LLM turns even though their canonical
> queue rows are queued and each has only `turn.accepted` and `turn.queued` events.
> Replaying those events through `project_turn` reproduces `running` and an invented
> start timestamp with `turn_start_inferred`; all four have zero invocations.

## Why (intent)
An accepted instruction can be durably queued before any model executes. Its telemetry
must preserve that distinction, and execution duration must not include queue wait.

## TASK
1. Write regressions for accepted/queued-only events, native invocation start without a
   gateway start event, terminal never-ran requests, and historical projections.
2. Correct `src/core/telemetry_projection.py` to derive lifecycle from actual evidence.
3. Trace persistence, reprojection/reconciliation, API and SessionTurns display so existing
   false-running rows become honest without manufacturing execution or usage.
4. Run focused projection/store/API/UI checks and ship with A109.

## ACCEPTANCE (proof, not vibes)
- Accepted/queued requests report queued and have no execution start timestamp.
- Real invocation/start evidence reports running; terminal events report the terminal outcome.
- Terminal never-ran requests remain terminal with no fabricated execution duration.
- Existing persisted queued-only telemetry is corrected through a bounded/recoverable path.
- UI and API agree with canonical queue truth; no paid CLI invocation is used as proof.

## RESERVED DECISIONS
None. Durable queued requests remain valid; telemetry must not imply execution.

## SCOPE OUT
Job consumption semantics (A109), telemetry DB separation, unrelated cost/usage redesign.

## TRAIL / EVIDENCE
- Incident reproduction: `jturn_82a3a43f24457fee35c8a2b6` projected from accepted/queued
  events returns `running`, `started_at=2026-11-03T16:31:18.062983Z`, no execution node.
- Projection version 2 defaults to queued, uses gateway/worker start evidence for execution
  timestamps and leaves never-ran terminal duration unavailable. An indexed five-row
  background repair rebuilds old projections from retained raw events, newest first;
  process restart resumes the remainder. This includes already-withdrawn requests with
  fabricated timestamps. Pruned raw history is preserved, not reconstructed.
- Focused projection/store regressions pass, including persisted false-running repair and
  exclusion of queue wait from execution duration. Existing SessionTurns renders the
  returned status directly and uses a placeholder when start/duration is unavailable.
- CI exposed older acceptance assertions that encoded the false-running behavior and
  legacy-schema fixtures with divergent telemetry ancestry. Those assertions now require
  queued/no-start evidence; index creation follows the existing merged-schema repair.
  All 121 affected migration/reconciliation checks passed; 68 final lifecycle/migration
  checks passed after extending repair to retained terminal history.

## Milestone
- [x] Issue opened (#273).
- [x] Focused lifecycle regressions pass.
- [x] Persisted history repair verified in SQLite; live API validation pending deploy.
- [x] PR merged and controller deployed; live history repair completed.

## Closure
### LLM turn lifecycle — SHIPPED (2026-11-05)
**What changed:** projection version 2 distinguishes queued from execution and infers
start only from actual start evidence. The task-server's existing background flusher
repairs retained history in five-row batches using a partial index created after legacy
schema repair. Accepted-only and withdrawn-only histories no longer invent execution.

**Verification:** PR #274 merged as `29e8225`, CI run `41909784004` green. After the first
CI run exposed old false-running test expectations and divergent-schema fixtures, all
121 affected checks passed; the final 68 lifecycle/migration checks also passed. A copy
of the production DB migrated 48→49 and reproduced correct historical outcomes.

**Live evidence:** both controllers healthy on `29e8225`, schema 49. Authenticated
`GET /api/turns/{id}` for the four original `jturn_*` rows in session `a32f0cf19326`
returns `withdrawn`, `started_at=null`, projection version 2. `task_e97d6f99` remains
failed, with actual worker invocation start `2026-11-03T16:07:12.192491Z` rather than
admission time. Retained projections with version <2 declined from 6140 to 0; the four
original notices have zero phantom starts. Pruned raw history was preserved.

**Deploy evidence:** same controller backup, rollback image and deployment tag as A109.
No synthetic execution, paid proof turn or acknowledgment of historical job results.
