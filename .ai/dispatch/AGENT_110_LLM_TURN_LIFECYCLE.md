```yaml
job_id: AGENT_110_LLM_TURN_LIFECYCLE
created_at: "2026-11-04T02:11:25.550323+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-04T02:11:25.550345+00:00"
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

## Milestone
- [ ] Issue opened.
- [ ] Focused lifecycle regressions pass.
- [ ] Persisted history repair and API/UI behavior verified.
- [ ] PR merged and controller deployed.

## Closure
Pending implementation.
