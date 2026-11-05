```yaml
job_id: AGENT_109_WATCHED_JOB_CONSUMPTION
created_at: "2026-11-04T02:11:24.580839+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A109             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["tests/test_watched_job_consumption.py","tests/test_job_completion_protocol.py","tests/test_mcp_jobs.py","tests/test_job_review_regressions.py"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-05T00:08:25.242933+00:00"
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
- Implementation: `job_result` resolves the caller from process identity; the controller
  returns bounded status/tail/log path, consumes terminal results in the existing receipt,
  and atomically withdraws queued/pending/claimed follow-ups. Admission checks the marker
  in its own transaction. A started wake refuses competing consumption (409); successful
  wake completion records consumption. Operator notices remain independent.
- Verification: 51 focused tests passed (consumption, MCP, HTTP, lifecycle projection/store);
  153 queue/recovery/grant/architecture/Codex-telemetry regressions passed. Additional final
  consumption/tool checks and frontend placeholder validation are recorded at closure.
- §7 boundary: four admitted result requests; at N=100 only four parse/execute. Bodies
  cap at 2 KiB with a five-second read deadline; output uses 1500-char tail, 512-char label
  and 1000-char log path. MCP reads cap at 64 KiB with 15-second socket timeout. Each DB
  mutation uses the existing five-second writer/lock budget, off the event loop. Invalid
  input → 413/422, foreign owner → 403, missing job → 404, saturated → 429, unavailable
  store → 503. The shared worker token still is the trust boundary (A71).
- DB/native-IO hard-deadline deferral: the existing managed writer bounds lock waits,
  not filesystem stalls during COMMIT; no new hard native-IO guarantee is claimed.

## Milestone
- [x] Issue opened and root cause/invariants recorded (#272).
- [x] Delivery and consumption tests pass across admission/start/restart seams.
- [x] Tool contract and grants verified.
- [x] PR merged, controller deployed, worker activation verified by fresh build/incarnation reports.

## Closure

### Final adversarial review — SHIPPED (2026-11-05)
The operator requested an adversarial review and correction of any demonstrated fault.
F3: cancelling the result HTTP waiter released the four-slot gate while its SQLite thread
continued. A deterministic blocking-thread regression reproduces this. The executor future
now owns the permit through DB work and response serialization; cancellation is shielded,
and detached exceptions are observed. Body limits/deadlines and start/consumption semantics
remain unchanged. A110 carries F1/F2 history-repair corrections. Issue #272 reopened.

F4: consumption cleared a still-live lineage writer lease. That allowed the existing
Case cleanup sweep to mark the lineage voided before the writer finished. A deterministic
admission-boundary test reproduced the lost lease. Consumption now inherits its expiry,
matching `close_session_turns`; the withdrawn wake is fenced immediately while Case cleanup
waits for the live writer's lease to expire.

Verification: original three failure tests and the lineage-lease test failed before their
corrections; six review regressions pass afterward. Final command:
`.venv/bin/pytest tests/test_job_review_regressions.py tests/test_watched_job_consumption.py tests/test_turn_queue_4d.py --tb=short`
→ 57 passed. Another 129 wider completion/telemetry/migration/retention checks and 50 focused
protocol/projection checks passed (overlapping sets). New-code undefined-name checks pass;
two pre-existing unused imports in task_server.py were verified on origin/main and left alone.

PR #276 merged as `fd8b501`; CI run `41936330373` green. Gateway and task-server healthy
on that build, schema remains 49. No worker code/tool change or worker restart. Backup
`/home/cifran/ai-team-data/backups/mesh-pre-fd8b501-20261104T2350Z.db` passed quick_check;
rollback image `ai-team:pre-fd8b501`, deployment tag `deploy/20261104T2350Z-fd8b501` pushed.
First Docker frontend build failed without switching containers; the normal retry succeeded.
Body/output/lock bounds and independent operator notices remain as before.

### Watched-job consumption — SHIPPED (2026-11-05)
**What changed:** `scripts/mcp_jobs.py` exposes `job_result` and corrects the registration
contract; the base tool profile grants it. `src/control/task_server.py` exposes a bounded,
owner-checked result endpoint. `src/control/db.py` migration 49 adds consumption to the
existing receipt, retires unstarted follow-ups atomically, fences admission and records
successful wake consumption. The orchestrator recognizes consumed admission races.

**Verification:** PR https://github.com/nydiokar/AI-team/pull/274 merged as `29e8225`;
CI run `41909784004` passed. Focused commands and results are recorded above and in the
PR: 51 consumption/projection/store/protocol checks, 153 queue/grant/architecture checks,
45 final transaction checks, 121 migration/reconciliation checks, 68 final historical
repair checks, and 9 timeline tests. These runs overlap; they are not a unique-test total.
Direct MCP `tools/list` advertises `watch_job`, `job_result`, `whoami`.

**Deployment:** gateway + task-server healthy on `29e8225`, schema 49; backup
`/home/cifran/ai-team-data/backups/mesh-pre-29e8225-20261104T2139Z.db` passed `quick_check`
at schema 48. Rollback image `ai-team:pre-29e8225`; production tag
`deploy/20261104T2139Z-29e8225`. Build context contained tracked files only, preserving
the root Compose environment and excluding unrelated untracked scripts.

**Worker activation:** observed after deployment, without this task restarting workers:
Horse build `29e8225`, incarnation `f5fb2eae4f0d4b6eb1797368eaad5759`; kanebra build
`29e8225`, incarnation `6ee1b015088d4573b886d4944f0e6f8f`. Both were online, idle, with
zero pooled sessions. The retired `kanebra-worker` record is offline and is not the local
active carrier. No redundant restart was requested.

**Limits / follow-through:** live missing-job POST returned structured 404 without changing
any real job. Natural new agent consumption/wake behavior was proved in the offline
real-DB/queue harness, not through a paid production agent turn. Direct private-file reads
cannot establish consumption; the tool explicitly instructs the agent to acknowledge via
`job_result`. A started continuation is not cancelled; competing consumption returns 409.
The original four notices had already been withdrawn separately before deployment.
