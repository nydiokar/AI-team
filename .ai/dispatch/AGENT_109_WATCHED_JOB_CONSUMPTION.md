```yaml
job_id: AGENT_109_WATCHED_JOB_CONSUMPTION
created_at: "2026-11-04T02:11:24.580839+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-04T02:11:24.580864+00:00"
```

# DISPATCH — 109 · Consume watched-job results once while preserving durable wake-up

**Level:** 3 (operator-reported autonomy, controller/agent boundary, persisted delivery state) · **Type:** code
**Authored:** 2026-11-04 · **Authorization:** operator explicitly requested implementation and shipping in the originating conversation.
**Depends on:** none; A110 tracks telemetry separately.
**Branch:** `feat/watched-job-consumption`
**Issue:** https://github.com/nydiokar/AI-team/issues/272

> Session `a32f0cf19326` ran one instruction on Horse from 2026-11-03T16:07:12Z to
> 16:57:56Z, ending on quota. Four detached jobs completed meanwhile; each admitted one
> future `watched_job` request. Those requests never started, although the agent continued
> working in its original instruction. Each receipt shows one delivery attempt. No evidence
> establishes duplicate gateway execution; the gap is that handled results cannot be acknowledged.

## Why (intent)
Keep the intended contract: a detached job belongs to its requesting session and wakes that
session when work finishes, including after the agent yields. Registration never forces a
turn to end. Once a result is explicitly consumed, it must not cause another continuation.
Being busy, reading an unrelated file, or starting a different job does not prove consumption.

## TASK
1. Trace registration → terminal process report → receipt → queued wake → claim/start,
   including the universal MCP tool grant and existing agent inbox ownership rules.
2. Add a caller-owned, bounded job-result/acknowledgment surface over the existing receipt.
   Persist consumption and fence unstarted follow-ups against concurrent admission/start.
   Preserve restart safety, idempotency, operator holds and notify-only jobs.
3. Correct tool instructions: continue independent work or yield; use the result surface
   to consume terminal evidence. Preserve default notification of unconsumed completions.
4. Prove idle wake, active consumption before/after admission, concurrent start, restart,
   malformed/foreign input, and resource failure without a paid backend invocation.
5. Ship through PR and deploy controller changes. Surface worker activation requirements;
   worker restarts require the existing operator decision and workload/ancestry checks.

## ACCEPTANCE (proof, not vibes)
- A yielded agent receives its job completion; a busy agent does not lose it.
- Reading/acknowledging a terminal result prevents any subsequent unstarted wake for that job.
- A running job cannot be acknowledged as complete. A different session cannot consume it.
- Poll retries/restarts cannot resurrect consumed delivery. Claim/start races cannot execute
  a stale result after its consumption won the transaction race.
- Status, exit code and bounded output remain available to the operator and owning agent.
- Relevant DB, protocol, MCP, grant and integration regressions pass; §7 boundary review recorded.

## RESERVED DECISIONS
- Operator intent is settled: preserve wake-on-completion, fix repeat delivery; do not
  infer acknowledgment from session activity. No live historical notice is deleted blindly.
- If a consumer has already started, report that fact rather than silently canceling live work.

## SCOPE OUT
Manager subscriptions to other sessions' jobs (related issue #228), new brokers, unrelated
queue redesign, quota policy, paid proof turns, worker restarts without authorization.

## TRAIL / EVIDENCE
- Production diagnosis: four `jturn_*` requests with no activation/start, one failed
  `task_e97d6f99`, four receipts with notification/action attempts = 1.
- Native Horse transcript unavailable at diagnosis; do not assert how the original agent
  inspected results without further evidence.

## Milestone
- [ ] Issue opened and root cause/invariants recorded.
- [ ] Delivery and consumption tests pass across admission/start/restart seams.
- [ ] Tool contract and grants verified.
- [ ] PR merged, controller deployed, worker activation limitation recorded.

## Closure
Pending implementation.
