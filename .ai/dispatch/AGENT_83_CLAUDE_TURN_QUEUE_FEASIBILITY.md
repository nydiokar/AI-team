```yaml
job_id: AGENT_83_CLAUDE_TURN_QUEUE_FEASIBILITY
created_at: "2026-09-22T17:11:35.678902+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A83             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-09-22T17:11:36.872576+00:00"
```

# A83 — Resolve Claude SDK turn-queue feasibility before implementation

**Status:** investigation ready; A82 implementation is blocked on this result.
**Priority:** required. Claude SDK is a primary backend, not an optional rollout exclusion.
**Branch:** `investigate/claude-turn-queue`, in an isolated worktree.
**Design:** [SESSION_TURN_QUEUE_DESIGN.md](../../docs/SESSION_TURN_QUEUE_DESIGN.md).
**Dependent build:** [AGENT_82_SESSION_TURN_QUEUE.md](AGENT_82_SESSION_TURN_QUEUE.md).

## 1. Assignment

Finish the investigation needed to make the session turn-queue design executable
for the actual Claude SDK backend. Do not build the queue first and leave Claude
as a later integration problem. Deliver a concrete protocol/driver contract,
reproducing tests, an independently reviewed feasibility verdict, and corrected
design/build instructions.

The owner wants multiple human/agent/system instructions accepted durably while
a recipient is busy, then delivered in order as ordinary turns, without
interrupting existing work, confusing replies, or overwhelming the gateway.
Reuse existing task/mesh infrastructure. Claude is one of the most-used backends.
Neither excluding Claude nor silently disabling its background functionality
counts as satisfying this task.

Work autonomously. Find facts in source, installed libraries, tests and actual
protocol traces before asking the owner. A question about where a helper lives,
how the existing mesh dispatch works, or which SDK is installed is not an owner
decision. If a required behavior is impossible, prove the specific missing
capability and present the smallest concrete alternatives. Do not replace
investigation with “needs a gate” or “verify during implementation.”

## 2. Boot and safe workspace

- Start terminal work with a separate `pwd`; read `.ai/CONTEXT.md` first.
  Summarize project purpose, files and setup before investigation.
- Read repository instructions, dispatch protocol, design and A82 packet.
  Inspect git status/worktrees; preserve unrelated/untracked files.
- Create an isolated investigation branch/worktree from the commit containing
  this packet. Use repo .venv/project install workflow with constraints; verify
  imports point to the investigation checkout. Linux/Bash; no force/destructive
  commands. Do not invoke `python main.py status`.
- Use temporary DB/session/spool/log roots, fake transports and explicit targeted
  pytest paths with `--tb=short`. No full pytest/e2e suite, production DB changes,
  service/worker restart, live flag changes or surprise paid backend runs.
- Source-reading and an isolated minimal executable prototype/test adapter are
  authorized. A full queue implementation, generic refactor, SDK upgrade or
  backend switch is outside this investigation.
- Record state via `scripts/dispatch/dispatch_state.py --set`; do not hand-edit
  YAML/generated boards. Set A83 active while investigating; A82 stays blocked.

## 3. Established observations — verify, don't blindly trust

At code baseline `5f58d2e`:

1. `src/backends/claude_driver.py::_SDKSession.send` protects explicit sends
   with a threading lock; lock contention calls `cancel_inflight` and then waits.
   That is not the desired non-interrupting managed-queue behavior.
2. `_submit_turn` appends a pending future before `await client.query(message)`.
   `_reader_loop/_dispatch` drains a persistent SDK stream; when a result arrives,
   a pending future receives it, otherwise the proactive sink receives it.
   Inspect the exact matching/accumulator logic before claiming misattribution
   is or is not reachable.
3. Native background work can continue a model conversation without a new
   gateway instruction. A post-hoc `_deliver_proactive_turn` callback cannot by
   itself reserve ownership before that execution.
4. The installed SDK includes task lifecycle types. Do not assume a
   task-finished notification means the model's ensuing continuation finished.
   A tool/process finishing and a model turn finishing are different events.
5. `src/worker/agent.py` handles claims/results over HTTP and drops in-memory
   result delivery after a deadline. The revised design requires a completed
   result spool, fencing and reconciliation. Those do not automatically solve
   stream ownership/result attribution inside the SDK.
6. `mesh_tasks` also carries control actions and NULL-session scheduling tokens.
   The proposed protocol-1 unique index controls admitted DB turns only.
   It cannot stop an autonomous native process from acting.
7. The correct Codex configuration seam is `CodexNativeBackend._thread_config`;
   the actual session service is `src/services/session_service.py`.
   Existing telemetry `/api/turns` and `useSessionTurns` must not be repurposed.
8. The fresh design review found unresolved Claude proof requirements; it did
   **not** establish that Claude cannot support the feature.

## 4. Required source investigation

Read these sources and relevant callers:

- `claude_driver.py`: SDK session construction, client setup, reader,
  accumulator/reset, pending deque, dispatch, lock, cancel, timeout, close,
  background/proactive delivery and pooled session reuse.
- `claude_code.py`: backend start/resume/compact and process cleanup.
- `orchestrator.py`: normal/local/remote turn dispatch, cancellation/retries,
  result classification, session-ID propagation and proactive sink.
- `worker/agent.py`: execution, result post, shutdown/children and native
  session reconstruction.
- Existing `test_sdk_driver_proactive.py`, `test_proactive_turn_delivery.py`,
  `test_claude_session_backend.py`, cancellation and mesh result tests.
- Installed `claude_agent_sdk` client/transport/parser/message types, query/
  interrupt/control APIs, hooks and documented lifecycle signals. Record
  actual installed package version and relevant CLI version without invoking
  a paid model. Compare supported dependencies/constraints; do not upgrade.
- Codex native ownership tests as a comparison, not a claim Claude has the
  same protocol.

Prefer installed source. If essential semantics live outside it, use official
SDK/CLI documentation or upstream source and record version/commit. Distinguish
“documented guarantee,” “observed trace,” “local adapter assumption,” and
“unknown.” A plausible fake-stream sequence is not proof that the real SDK emits
that sequence; equally, one happy-path trace is not a concurrency guarantee.

Answer in the Findings section:

1. What identifies an explicit request, response, native background task and
   autonomous model continuation? Which IDs are actually on the wire?
2. Can an autonomous continuation start after the gateway's apparent terminal
   boundary? Can its ResultMessage arrive after a new pending future is added?
3. Is there a supported native idle/reservation barrier, input serialization,
   lifecycle hook or correlation ID that closes that race atomically?
4. Can all outstanding native activity be tracked before it produces effects?
   What about nested tasks, task kill/stop, failed notifications and stream loss?
5. What survives a gateway restart, a worker restart and SDK subprocess loss?
   Which component can attest quiescence, and what evidence does it have?
6. Which semantics must change only for managed sessions, and how does the
   flag-off path remain unchanged?

## 5. Reproducers before proposed fixes

Create `tests/test_claude_turn_queue_feasibility.py` or a tightly scoped equivalent.
Use the real adapter/message dispatch logic with a controllable fake SDK client/
transport and explicit barriers. Do not replace the reader/dispatcher under
test with an always-idle mock.

Required traces:

| Trace | Assertion to establish |
| --- | --- |
| Two ordinary explicit turns | No implicit interrupt; each reply matches its input; exactly one backend invocation owner. |
| Background task finishes before next query | Its continuation/result cannot satisfy the next explicit request's future. |
| Background completion races reservation/query write | Both event orders are tested; serialization/attribution remains correct. |
| Original result followed by native continuation | Releasing DB ownership does not permit overlapping or misattributed next work. |
| Multiple/nested native tasks | Finishing one does not falsely establish global quiescence. |
| Killed/stopped background task | Terminal lifecycle types and any subsequent continuation are handled accurately. |
| Timeout/cancel while background execution survives | Caller timeout is not proof of idle; successor remains held until evidence. |
| Reader disconnect/process death/restart | Old future/result cannot be replayed into a new execution; recovery holds are truthful. |
| Compaction with pending/native work | Context mutation respects the same reservation boundary. |
| Lost start acknowledgement | Retry grants no second invocation; restart cannot reuse an old grant. |

For each failure, state the source/protocol evidence that makes its ordering
legal. Record baseline red and candidate green evidence. Keep a minimal prototype
only where needed to exercise the proposed fix. No queue schema/API/frontend
bulk changes in this job.

If offline source/transport tests cannot establish an essential third-party
behavior, specify the smallest real validation needed: exact setup, scenario,
bounded cost/time, expected discriminating observations and cleanup.
Do not run it silently. Do all other work first. Do not treat optional live
testing as an excuse to stop source investigation early.

## 6. Select one concrete integration contract

Evaluate the existing mechanisms first. Select the least disruptive contract
that proves safety and preserves useful background behavior. Define:

- Driver state machine and transition owner/thread/loop.
- How native and explicit work acquire/retain/release the same session reservation.
- How every result is attributed; no “assume first result belongs to latest query.”
- When the gateway task becomes terminal vs when the session becomes eligible
  for another turn. If those boundaries differ, define persisted ownership and
  read-model semantics without creating another execution queue.
- Claim/start/result and native driver evidence needed after failure/restart.
- SDK lock-conflict behavior for managed sessions: fail closed, never implicit
  interruption of previous work.
- Fairness/liveness: how unrelated sessions proceed, what happens with a long
  background job, and what the waiting user sees. Do not silently hold all
  instructions forever merely because any historical background task exists.
- Effects on warm session/cache continuity, compaction, watched-job wakeups,
  cancellation and role/tool provisioning.
- Exact code seams, data fields, acceptance tests and rollout requirements.

Also resolve any contradiction the selected Claude contract introduces into
A82's completion/native-ID atomicity, result spool, retries or sender capability.
Do not leave two incompatible options in the implementation guide.

If a safe contract requires a product tradeoff—such as holding followups through
all native background work—spell out actual behavior and supported bounds.
Do not label a material behavior change “just an implementation detail.”
If no contract satisfies the requested behavior with the installed backend,
return a proved NO-GO with concrete alternatives; no “Claude optional” escape.

## 7. Independent review

Before finalizing, have a fresh-context reviewer inspect the actual source,
reproducer/prototype and chosen contract. Built-in subagent with
`fork_turns="none"` is preferred. Alternatively use a fresh Claude Opus reviewer
through the existing mesh dispatch API/tool, with explicit model and a verified
copy of this worktree; inspect the real API, never guess a spawn endpoint.
The owner authorizes bounded independent review, not unrelated paid experiments.

Provide only repo/worktree, base/candidate references, this packet, design and
a neutral request to falsify the contract. No author conclusion or conversation
history. Reviewer must check reachable ordering, tests that mock away the race,
unsupported SDK assumptions, liveness and fit with existing infrastructure.
Read-only, no deployment, no recursive agent spawning.

Fix substantiated findings and re-run affected tests. Have the reviewer verify
fixes. Record reviewer identity, exact candidate and findings/disposition here.
If independent tooling is unavailable, report that explicitly; do not claim
review completed. Close only reviewer sessions you created, after terminal
result, without restarting workers.

## 8. Deliverables and release of A82

Deliver all of the following in the investigation branch:

1. Findings/protocol evidence and one selected contract in this packet.
2. Executable race reproducers and minimal proof prototype if necessary.
3. Updated design removing the unresolved Claude placeholder and describing
   the proven mechanism, or a clearly stated NO-GO with evidence.
4. Updated A82 steps/test oracle to consume those results without rediscovering
   the issue. Do not tell the builder to repeat an open-ended feasibility study.
5. Independent review and precise test commands/results.
6. A committed reviewed investigation result; no live deployment.

GO requires default Claude SDK support with its intended native background
functionality, correct attribution, non-interruption, bounded/livable waiting,
recovery semantics and independent review evidence. An optional-backend
exclusion, suppressed test or broad claim of correctness is not GO.

On GO, set A83 done with real evidence paths, update DISPATCH_LOG, and deliberately
set A82 ready only once its current design/packet have no remaining unresolved
build prerequisite. Ensure A82's starting branch includes this investigation
commit/prototype evidence before execution; “done elsewhere” is not enough.

On NO-GO, A83 may close as a completed investigation with a clearly named NO-GO
verdict and evidence, but A82 stays blocked. Never auto-unblock A82 merely because
its dependency job has status done. A82 has no auto-unblock permission.

Owner handoff: chosen mechanism or proved blocker, tests/reviewer verdict,
branch/commit, updated design/build packet paths and the exact next action.
No production-safe or zero-issues guarantee based only on mocked tests.

## Findings

Not investigated yet. Record versioned source facts and trace evidence here.

## Review

Not performed on implementation/prototype yet. Earlier design-review findings
motivate this investigation; they do not prove the selected solution.

## Closure

Pending. Explicit verdict required: GO or NO-GO, with evidence and A82 readiness.
