```yaml
job_id: AGENT_83_CLAUDE_TURN_QUEUE_FEASIBILITY
created_at: "2026-09-22T17:11:35.678902+00:00"        # CANONICAL — set once at dispatch, never derive again
status: dead              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A82-claude#A83             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-09-22T19:20:38.551305+00:00"
```

# A82 supporting checklist — Claude queued-turn integration

**Standalone investigation superseded:** execute
[AGENT_82_SESSION_TURN_QUEUE.md](AGENT_82_SESSION_TURN_QUEUE.md).
This historical filename is retained for links; it is unrelated to the separate
AGENT_83_SESSION_STATE_LEGIBILITY job. The YAML status is dead because this is
no longer an independently dispatched task, not because Claude support failed.

Start building the unified queue on A82's feature branch. Run the checks below
as part of its source investigation and carrier integration. They do not block
schema, admission or test development. Default Claude SDK support is mandatory
for final completion and rollout.

The core behavior is ordinary sequential dispatch:
persist B/C while A runs; after A's terminal outcome and session update are
committed, dispatch B, then C. The potential native-background result-ordering
collision remains unproven. Test its reachability against actual source and
protocol behavior; do not turn its possibility into a claim the queue cannot
be built.

Use A82's isolated workspace, test-first workflow, safety constraints and
independent review process. Do not create a separate prerequisite branch or ask
the owner to commission another investigation. Keep flags OFF while building.
The next sections retain the detailed code facts and regression traces.

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

## Integration acceptance

First prove normal explicit message/answer serialization through the existing
Claude adapter. Then use the tests above to establish native-background ordering.
A failing reachable trace must get a minimal driver/ownership fix and regression
test within A82. Do not redesign unrelated backend behavior.

Record the actual SDK version, documented versus inferred ordering guarantees,
chosen reservation/result-attribution mechanism, restart behavior and test
evidence in A82's Execution record. Amend the design only where the source or
test demonstrates a real gap. Passing fake-stream tests proves the adapter's
behavior for those traces, not unsupported upstream guarantees.

The final independent reviewer checks these results along with the rest of A82.
Do not exclude Claude or silently disable background functionality to make a
test pass. Do not mark the feature done or enable it until the required ownership,
result attribution, recovery and regression tests pass.

If a concrete reproducer proves a required guarantee cannot be achieved using
the supported backend interfaces, document the specific failed contract and
smallest alternatives. That demonstrated limit may block completion/rollout;
an unimplemented test or speculative race alone does not justify blocking the
whole build.

No feasibility conclusion or implementation result is claimed by this checklist.
