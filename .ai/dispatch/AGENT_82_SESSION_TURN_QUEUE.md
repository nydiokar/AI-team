```yaml
job_id: AGENT_82_SESSION_TURN_QUEUE
created_at: "2026-09-22T11:39:06.841262+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: worker-a82-stage2
depends_on: []
results_ref: DISPATCH_LOG.md#A82             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-09-25T09:33:04.329154+00:00"
```

# A82 — Build the unified session turn queue

**READY — start the staged implementation on the feature branch.** Claude SDK
is mandatory. The [Claude regression checklist](AGENT_83_CLAUDE_TURN_QUEUE_FEASIBILITY.md)
is part of this build, not a separate prerequisite job. Prove ordinary queued
message/answer delivery first; test native-background interactions during carrier
integration. Failed safety tests block completion and rollout, not unrelated
schema/admission work. No evidence currently establishes that the queue is
infeasible for Claude.

**Implementation branch:** `feat/session-turn-queue` (isolated worktree).
**Design:** [SESSION_TURN_QUEUE_DESIGN.md](../../docs/SESSION_TURN_QUEUE_DESIGN.md).
**Starting code reference:** `5f58d2e`; earlier design review commit `d59cca2`.
Read the current committed design, including the dispatch clarification changes,
not an old copy of that commit.
**Deliverable:** tested, independently reviewed implementation committed on the
feature branch, with a PR if authenticated repository tooling is available.
Production activation is separate. Proceed with implementation and resolve the
backend integration questions using the required tests; do not ask the owner
to commission another investigation before starting.

## 0. Mission, authority, and stopping rules

Build the design end to end: a sender can submit several instructions to a busy
session, each is durably acknowledged and editable until activation, and the
recipient executes them serially without corrupting its native conversation.
Human, authorized agent and system origins use the same turn ledger.
Include durable recovery, truthful UI, agent sender tooling and regression tests.

The owner authorizes autonomous repository inspection, implementation, offline
testing, narrow design corrections supported by code, and independent review.
Do not repeatedly ask the owner how to find files, use APIs, choose helper
names, resolve an ordinary failing test, or interpret a behavior already fixed
here. Investigate the tree and installed dependency source. Record material
discoveries in this packet's Execution record; amend the design in the same
branch when a real contradiction is found. Preserve all safety invariants.

The following are the only escalation boundaries:

- A required guarantee is impossible with the available backend/API after
  inspection and a reproducing test, and alternatives require a product change.
  Report the exact failed gate, evidence and least disruptive alternatives.
- Destructive live changes, deleting user work, production data repair, changing
  paid usage policy, or worker restart/redeploy not already authorized.
- A required external credential/capability remains unavailable after discovery.
  Complete independent local work first; don't fake a review or working feature.

Do not mark done simply because a flag is OFF. A safe unfinished feature is still
unfinished. Mandatory tests cannot be skipped, xfailed or replaced with mocks of
the behavior they are supposed to prove. Record an actual block honestly if a
required gate cannot pass.

The independent code-review worker(s) described in §12 are authorized. This is
not authorization for arbitrary extra agents or a paid full-backend e2e suite.
Use at most one reviewer at a time; fix findings before asking for another pass.
Do not change live flags, run live load tests, restart services, merge or deploy
as part of this packet. The requested handoff is the reviewed feature branch;
this task-specific scope narrows older generic self-merge/deploy instructions.

## 1. Required boot and isolated workspace

1. Run `pwd` as its own terminal call; read `.ai/CONTEXT.md` first. Read the
   applicable repository instructions and `.ai/dispatch/CLAUDE.md`. Summarize
   purpose, relevant files, setup and risks before changing code.
2. Inspect `git status --short --branch`, existing worktrees and this packet's
   YAML state. Never stash, reset, delete, overwrite or commit somebody else's
   changes. The sibling design files `SESSION_WAIT_STATE_GRANULARITY.md` and
   `WORKER_CACHE_HEARTBEAT_EXTENSION.md` were untracked when this packet was
   authored; they are unrelated work, not implementation requirements.
3. Create the feature branch in an isolated worktree from the commit containing
   this packet. Example, only after checking that both branch/path are unused:
   `git worktree add ../AI-team-session-turn-queue -b feat/session-turn-queue HEAD`.
   If the branch already exists, inspect and resume it when it belongs to A82;
   otherwise use a unique `feat/session-turn-queue-<suffix>`. Never force-create.
4. Run all implementation commands in that worktree. Record its absolute path,
   branch and base commit below. Set A82 status active and owner through the
   dispatch script in the worktree, not by editing the YAML block.
5. Use repo-local Python environment. For a new worktree, use the existing
   project install workflow with `pyproject.toml`, dev extras and
   `constraints.txt`; do not install into system Python. Inspect pytest fixtures
   before running them: a copied/shared venv or editable install must not cause
   imports from the original live checkout. Verify imported `src.__file__`.
6. Linux/Bash only. Each new terminal sequence starts with a separate `pwd`.
   Use `pnpm` for web tooling when available. Never run `python main.py status`,
   `--force`, a full pytest/e2e suite, or pytest faulthandler timeout flags.
   Redirect any full diagnostic dump to /tmp. Clean up child processes/listeners
   created by tests and reviewers, not just their shell wrappers.

Dispatch commands (use the real worktree root):

```bash
.venv/bin/python scripts/dispatch/dispatch_state.py --set AGENT_82_SESSION_TURN_QUEUE status active
.venv/bin/python scripts/dispatch/dispatch_state.py --set AGENT_82_SESSION_TURN_QUEUE owner <actual-agent-name>
```

These are separate commands. The generic `pnpm dispatch:set` wording in older
dispatch docs does not apply here. Use the Python script, never hand-edit YAML
or generated `_DISPATCH_STATE.md`. Keep DISPATCH_LOG's A82 entry current.

## 2. Read map and current behavior to verify

Read the full design, then these symbols and their callers before proposing
code. Use `rg`; when a symbol moved, find it rather than create a duplicate.

| Area | Existing source and why it matters |
| --- | --- |
| Admission/execution | `src/orchestrator.py`: `submit_instruction`, `_enqueue_task`, `_task_worker`, `process_task`, context preparation, `_mesh_enqueue_task`, `_dispatch_to_node`, `_mesh_complete_task`, reconcile spool and stale-BUSY repair. |
| Durable state | `src/control/db.py`: migrations, `_write/_begin_immediate`, task insert/claim/release/node-release/complete/fail, `get_session_turns`, session upsert, flow links, close_case and producer token helpers. |
| API | `src/control/control_api.py`: instruction body/route, process-local idempotency cache, operator auth, telemetry /api/turns, stop/close/compact, uploads, events and app factory. |
| Carrier protocol | `src/control/task_server.py`: registration, polling, claim/result/release/reaper, result telemetry reconciliation, staging. `src/control/node_registry.py`: incarnation handling. |
| Worker | `src/worker/agent.py`: `_poll_loop`, `_fetch_pending`, `_handle_task`, `_handle_close_session`, `_execute_task`, `_make_session_from_payload`, result posting and shutdown. |
| Session state | Actual path `src/services/session_service.py`; `src/services/session_store.py`; `src/core/interfaces.py`. The CONTEXT path `src/core/session_service.py` is stale. |
| Backend ownership | `src/backends/claude_driver.py`: `_SDKSession.send/_submit_turn/_reader_loop/_dispatch`, `_get_or_create`, role/tool config. `src/backends/codex_native.py`: `_thread_config`, `_run`, cancellation/compaction. OpenCode adapters and registry. |
| Internal producers | Orchestrator Case continuation/finalizers, quota/transient resume, respawn, cache heartbeat, watched-job completion, .task.md session-scoped ingestion and Telegram session sends. |
| Sender tooling | `scripts/mcp_manager.py`: `_dispatch_worker`, `_api_request`, shared token fallback and tool schemas. `scripts/mcp_jobs.py`; role filters in Claude/Codex paths. |
| UI truth | `web/src/components/timeline/Composer.tsx`, `useSessionActions.ts`, `useLiveData.ts`, `apiClient.ts`, timeline adapters/stores, `liveInvalidation.ts`, `refreshPolicy.ts`. |
| Flags | `config/settings.py`, db runtime-flag registry, orchestrator flag exports, managed env keys, `docs/ENV_FEATURE_FLAGS.md`, `.env.example`; locate every real registration seam. |

Observed root cause: API acceptance enters an ephemeral gateway queue and
prematurely mutates session state; durable execution rows arrive later.
Serialization is backend/path-specific, and local shadow claims are best
effort. The worker can schedule the same pending ID repeatedly and does not
serialize different turns of one session. Result task status becomes terminal
before the session/native ID is necessarily reconciled. These are the causes
to fix; “enable send while busy” alone is not the implementation.

## 3. Fixed decisions and interpretation traps

1. **One execution ledger, not one table for every purpose.** `mesh_tasks` holds
   managed turn intent. Revision audit, hashed credentials and completed-result
   spool are ancillary state; none may become a second prompt/execution queue.
2. **Protocol 0 stays legacy/control.** Protocol 1 is explicit managed execution,
   scoped to enrolled sessions. Active uniqueness must not include stop/close,
   sentinel Case tokens or legacy rows. Reject legacy execution bypasses for
   an enrolled session at DB insert/claim boundaries.
3. **One session owner includes uncertainty.** `recovery_required` retains the
   active slot. Offline, expired heartbeat, a new incarnation, coroutine
   cancellation, a terminal-looking status or API timeout do not prove the
   old process stopped. No exactly-once backend-effects claim.
4. **Canonical commit precedes acknowledgement.** New helpers throw typed
   failures; do not reuse swallowing helpers unchanged. No accepted SSE/event
   or backend call on failed commit. Existing mirror/reconcile helpers are not
   an alternative admission authority.
5. **Queued is not BUSY.** Acceptance must not overwrite the active task ID,
   last executed prompt or native backend ID. Read active ownership from the
   ledger. Stop must not cancel the newest waiting ID by mistake.
6. **Fresh config is not stale whole-session save.** Version configuration;
   update completion-owned fields only. Preserve concurrent close/model/pin
   changes. Do not let legacy completion later overwrite canonical completion.
7. **Routing keeps host affinity.** Gateway-local carrier and local standalone
   daemon may share a hostname: include carrier kind/process identity. Remote
   pinned sessions never relocate just because the node is unavailable.
8. **Acceptance FIFO has no hidden priorities.** Commit order defines sequence;
   timestamps/client clocks do not. No reorder endpoint. Edit preserves sequence.
   Source does not grant priority or permission.
9. **Retry decision is fixed.** Failed A + earlier waiting B + eligible automatic
   pause: supersede A's automatic retry and release only that eligible pause;
   run B. No B: admit head retry R, allow through its own eligible pause only,
   close/replace that pause on R's terminal outcome. Later B stays after R.
   Never deadlock B behind a pause whose retry is behind B. Preserve approval,
   operator stop, Case block and future provider deadlines.
10. **Optional automation can expire; humans cannot silently disappear.**
    Heartbeat is idle-only. Revalidate continuation work at activation; an
    intervening human review can make the wake obsolete. Do not coalesce humans.
11. **Routes/query keys are new names.** Use `turn-requests` API resources,
    `useSessionTurnQueue` and `["session-turn-queue", sessionId]`. Existing
    `/api/turns`, its detail route and `useSessionTurns` are telemetry.
12. **Two different clocks.** Scheduler fallback is 3 seconds. UI uses A81
    post-commit SSE/reconnect invalidation plus existing 60-second
    `SAFETY_NET_MS`. Do not regress all chat reads to 3-second polling.
13. **Two auth scopes.** Existing admin callers retain operator authority.
    The new sender tool uses only a dedicated session send credential; no
    dashboard/worker-token fallback. Tool allowlists are not authentication.
14. **One off is outside this change.** Leave stateless tasks and unenrolled
    legacy sessions working. Do not turn this into a framework rewrite,
    backend replacement, telemetry DB split, broker, or generic peer inbox.
15. **Prepared prompt differs from editable intent.** Context/role/attachments
    are assembled once for the consumed revision. Never prepend them again on
    result retries or reuse the wrong session's carry context.
16. **A feature gate does not abandon accepted work.** Disabling new enrollment
    leaves existing managed consumers/recovery/read APIs available until drained.
17. **Real SDK capability must be proved.** The lock currently interrupts the
    old turn on conflict; post-hoc proactive output is not an idle signal.
    A mock that always reports idle cannot establish correctness.
18. **No invented public helpers.** `enqueue_turn`, the new routes, protocol
    fields, scheduler, sender credentials, result spool and tests below are
    to-be-created contracts. Names in this packet do not mean they exist.
    Reuse current helpers only after checking their semantics.

## 4. Stage 0 — source map and carrier test oracle

Map the current implementation and define the carrier test oracle early. The
Claude checklist is supporting material for this stage and Stage 3, not an
external dependency. Start with ordinary explicit message/answer serialization.
Investigate native-background ordering against actual SDK source and reachable
traces; do not assume a speculative collision has been proved. Schema, admission
and test development can proceed while this integration work is completed.

- Build an execution-path inventory in the Execution record: source → admission
  → DB row → carrier → backend → result → session update. Include compact,
  retry loops, startup recovery, watched jobs, proactive output and remote close.
  Every actual path must either join protocol 1 or be explicitly legacy/control.
- Read the installed Claude SDK source/types and current driver reader. Construct
  controlled stream traces for a native background task completing immediately
  before explicit dispatch, while it is reserved, and immediately after a
  terminal result. Include multiple background tasks and a stopped/killed task.
  Identify exactly which observations prove quiescence and correct attribution.
  A task-finished notification alone does not prove its ensuing model continuation
  ended. An empty `_pending` deque is also not sufficient.
- Establish the driver reservation contract during Stage 3, retaining
  ownership until all native work affecting that session is quiescent, using
  actual supported SDK lifecycle signals. Reserve on the SDK loop before
  submitting a query; don't infer safety from delayed gateway polling.
  Keep background functionality and cache/session continuity. Protocol-1 lock
  conflicts must never call `cancel_inflight`.
- Test same-process once-only execution on a lost start response and restarted
  carrier refusal to reuse old authorization. Model lost claim responses too.
- Verify per-session MCP configuration can carry sender-only credentials on
  both Claude SDK and Codex native, without global env/config mutation.
- Verify migration on a fixture with duplicate legacy active rows, cancellation
  rows and NULL-session scheduling tokens; schema must still install.
- Request the first independent review under §12 of these contracts/red tests
  before enabling the carrier integration. The reviewer can identify a narrow defect;
  fix it autonomously and recheck.

**Stage 0 exit:** execution paths, source facts and required race tests are
mapped. Passing Claude SDK and Codex ownership tests is mandatory for Stage 3
and final completion; it is not a reason to postpone starting this build.
Investigate supported source/API alternatives and amend the design narrowly if
a real failing test requires it. Do not silently disable background work,
switch drivers or exclude Claude. Escalate only a concrete demonstrated
capability limit, not the existence of a test still to implement.

## 5. Stage 1 — acceptance tests before feature code

Create these test files (or extend an exact equivalent already in the tree).
Write executable cases for the entire contract now, before Stages 2–8.
Use existing factories/fixtures, a real temporary file-backed SQLite DB and
fake backend/carrier transports. Test discovery must succeed. Missing features
must fail assertions at runtime, not be hidden with skip/xfail or fail merely
because a test imported a nonexistent module.

| ID / proposed test file | Required assertions |
| --- | --- |
| DB01–08 `tests/test_turn_queue_db.py` | Additive migration, scoped unique slot, required managed fields, monotonic sequence, original-request hash, replay after edit/withdraw/close, revision audit rollback, no legacy execution bypass. |
| API01–08 `tests/test_turn_queue_api.py` | Commit-before-202, unchanged compatibility envelope/status, one-item full read vs summary page, stale revision 409, auth/closure/Case races, 429 limits, 503 DB failure, unchanged telemetry routes. |
| SCH01–06 `tests/test_turn_queue_scheduler.py` | Head-only FIFO, delayed blocked prefix fairness, distinct-session progress, activation/config races, no prompt reinjection, no writes/network/expensive context under transaction. |
| OWN01–10 `tests/test_turn_queue_ownership.py` | Carrier kind/incarnation/token fencing, lost claim/start response replay, once-only invocation, post-start restart hold, old result rejection, same result idempotency, stop/close/compact, native ID atomic commit, stale session-save defense, confirmed-quiescence recovery resolution. |
| WRK01–06 `tests/test_turn_queue_worker.py` | Dedup before task creation, bounded scheduled+executing IDs, claim response payload authoritative, result spool boot replay/receipt cleanup, disk/full/oversize failure, shutdown does not release a running backend. |
| SDK01–04 `tests/test_turn_queue_sdk_ownership.py` | Deterministic native lifecycle race traces from Stage 0, no implicit interrupt on lock conflict, no autonomous result misattribution, held native work retains ownership. |
| SYS01–08 `tests/test_turn_queue_producers.py` | Busy Manager wake, obsolete continuation, durable token→turn crash handoff, restart finalizer, A/B/R retry matrix, heartbeat expiry, respawn linkage, watched-job single notification/Case membership. |
| AUTH01–06 `tests/test_turn_queue_sender.py` | Dedicated capability issuance/validation/revocation, sender binding, cross-Case rejection, shared credential not accepted as scoped sender, concurrent Claude/Codex MCP env separation, same operation retry after tool restart. |
| INT01–06 `tests/test_turn_queue_integration.py` | Real control API → DB → scheduler → real task-server handlers → worker fake backend → result → next activation, with two carriers/sessions and restart/fault barriers; no direct fake completion shortcut. |
| LOAD01–04 `tests/test_turn_queue_pressure.py` | 100 admissions, bounded count/bytes/executor tasks, pre-parse chunked size rejection/body timeout, real SQLite lock contention, lifecycle responsiveness, no phantom acknowledgements. |
| ROLL01–05 `tests/test_turn_queue_rollout.py` | Flag-off, mesh-off, missing capability, race-free enrollment, disable-new-enrollment/drain with existing rows, no downgrade losing accepted work. |
| UI01–08 `web/src/lib/turnQueue.test.ts` and queue component/hook tests | Send while running, reload reconciliation, edit/withdraw conflicts, stop/pause behavior, distinct telemetry, SSE/reconnect/safety poll, pending/terminal dedup, attachments/carry/draft preservation and keyboard/accessibility. |

Minimum integration stories, with observable barriers rather than sleeps:

1. A running on local Claude; B and C acknowledged; revise C; complete A;
   B alone starts; withdraw still-queued C; reload sees no vanished B.
2. Repeat on a remote fake carrier; A's first result supplies native ID X.
   Hold result reconciliation at a barrier: B must not start until commit,
   then B must resume X and must not call create_session.
3. Admit on two sessions; block the oldest head on one, fill its waiting queue;
   the other must progress within one eligible scheduler pass.
4. Crash gateway after admission, after activation, after result commit before
   invalidation, and after producer token claim. Recreate objects against the
   same DB. IDs and accepted intent survive; no duplicate execution.
5. Simulate isolated carrier still executing after heartbeat timeout. Second
   carrier must not run successor or same started prompt. Replay persisted result;
   only then release. A new incarnation alone does not end the hold.
6. Have a credentialed worker send two instructions to a busy same-Case Manager.
   Verify distinct accepted IDs, source binding, FIFO, no interruption, and
   failure for forged/cross-Case/revoked sender. Human sends use the same ledger.

Record the initial red assertions and existing baseline failures below. Don't
“fix” a baseline failure by weakening tests or editing unrelated code. Reproduce
and classify it; run the affected test once after a justified fix.

## 6. Stage 2 — schema, transaction seams, and session ownership

Implement design §§3–4 and scoped indexes exactly in intent. Use the next
available migration number; never renumber historical migrations.
Keep code small: strict DB helpers in the DB layer, Pydantic request/result
types and a narrow service/scheduler module if it avoids further orchestrator
growth. Do not introduce a generic repository/event-bus framework.

Required atomic operations:

- Enqueue: idempotency lookup → current permissions/state/capacity → sequence →
  task + Case membership + required token linkage. One transaction.
- Revise/withdraw: revision + queued predicate + bounded audit. One transaction.
- Activate: head + no owner + config/Case/intent revisions → immutable prepared
  payload + carrier assignment + pending. One transaction.
- Claim/start/release: task + protocol + carrier kind/process + token + state
  predicates. Return structured authoritative ownership/payload.
- Complete: outcome + native ID/driver/result fields + session active identity +
  required retry/pause state + task terminal. One transaction.
- Close/interrupt: canonical state + applicable queued withdrawals + admission
  exclusion. Existing approval/completion criteria remain enforced.
- Enrollment: canonical DB present, no legacy in-memory/durable/native work,
  eligible backend/carrier, persisted marker, no racing legacy admission.
- Recovery resolution: current token plus recorded authenticated quiescence or
  result; conditional terminalization, no implicit rerun.

Use typed errors mapped consistently: 401 invalid credential; 403 disallowed
scope; 404 unknown/inaccessible resource as existing policy requires; 409 state,
revision, idempotency mismatch or missing recovery evidence; 413 byte cap;
422 malformed model data; 429 capacity/rate; 503 DB unavailable/deadline.
Internal producers get the corresponding typed outcome, not HTTP objects.

Body and mutation admission gates apply before parsing/submitting executor jobs.
Use a process-shared, loop-independent finite permit mechanism across the two
gateway loops; do not share an asyncio semaphore across event loops.
Use a monotonic end-to-end deadline and remaining-time lock/SQLite timeouts;
restore thread-local SQLite timeout state afterward. Don't change every legacy
DB operation as an incidental refactor.

All session writers affecting enrolled sessions must preserve field ownership.
Cover model/effort/pin/close and file-shadow saves, not just the new result API.
No JSON fallback for managed admission/activation/recovery.

**Gate:** DB/API red tests now pass; index query plans are attached in Execution
record; legacy migration and legacy nonqueue regression tests still pass.

## 7. Stage 3 — carrier protocol, results, and failure recovery

Add version/capability negotiation before managed pending rows are visible.
Extend existing poll/claim/result infrastructure rather than a parallel worker
protocol. Managed start is a new conditional operation; release before start
is safe only for the current token. Release after start is forbidden without
quiescence. Legacy handlers remain unchanged for protocol 0.

Wire DTOs include protocol version, task ID, carrier kind/process incarnation,
claim token and state. The claim response supplies the frozen payload; execute
that response, not the poll snapshot. Never serialize credentials/tokens into
public transcript/telemetry views. Claim token is an execution credential.

On a lost claim response, retry/lookup using the same task and requesting
carrier process; retrieve its existing ownership. On a repeated start with the
same live attempt, return the same authorization. A single carrier invocation
owner consumes it once; no parallel handler may create another backend call.
After carrier restart old authorization cannot start anything. It may submit
a durably spooled result bearing the old token if that token still owns the
held task; reject only if superseded, not merely because a node restarted.

Worker bookkeeping:

- Do not schedule a fetched ID already in scheduled/executing/result-delivery
  state. Acquire bounded scheduling capacity before create_task.
- Cap scheduled+executing work to at most twice configured execution slots.
  Backend execution retains existing max_concurrent bound.
- Control cancellation has separate small bounded capacity and targets the
  current attempt. Close preserves established safe drain ordering.
- Stop new claims before graceful shutdown; await/stop and verify children
  before any ownership release. Cancelling an awaiter is not backend shutdown.
- Result completion pending delivery keeps session ownership even if the backend
  slot can be returned. It must not create unlimited background retry tasks.

Managed result spool initial limits: 8 MiB serialized envelope per result,
128 MiB retained envelopes per carrier, 2 concurrent result-delivery requests.
Reserve one envelope allowance before start; if reservation is unavailable,
leave pending rather than run and discard. Store outside repo source, under
carrier state with mode 0600, atomic replacement and bounded replay batches.
Use only validated task/token identifiers for spool paths; never client paths.

Preserve full backend artifacts using existing artifact storage. If a result
cannot fit its envelope, preserve the full artifact locally, keep the durable
ownership hold with a bounded diagnostic/result-reference record, and stop new
claims if delivery cannot be reconciled. Do not truncate a reply and pretend
full canonical delivery succeeded. Test this failure path explicitly. Do not
delete a spool because an HTTP timeout elapsed, or because any 2xx arrived:
parse durable accepted/stale receipt and match task/token. Disk-write failure
leaves a visible recovery obligation; it cannot produce a successful ack.

Expose bounded authenticated carrier quiescence observations (task, token,
carrier/process and native execution identity, terminal/stop evidence). The
operator resolve-recovery endpoint consumes this recorded evidence, not an
operator-supplied boolean. Node registration/offline status is not evidence.
Auto-reconcile a matching valid spooled terminal result; retain unresolved holds.

Extract shared completion classification rather than duplicating salvage,
quota, cache health, telemetry invocation and native-ID rules. Heavy telemetry,
notification, file mirrors and projection remain after canonical commit.

**Gate:** OWN/WRK/SDK tests pass, remote native-ID integration passes, and a fresh
independent reviewer clears ownership/recovery (§12) before admission goes live.

## 8. Stage 4 — admission/scheduler and all execution producers

Wire `submit_instruction` and session-scoped entrypoints to the new service only
when enrolled. Keep the harness gate and Case/role lineage semantics. Do not
double-create a flow or drop a join when changing the enqueue path.

Implement fair scheduler from design §5. With default global waiting cap 50,
scan only the bounded nonterminal subset; select eligible heads before LIMIT 25.
Do not hold a local execution worker per remote pending/running row. Use bounded
batched reconciliation and event hints; waiting work stays in SQLite.

Canonical input/storage bounds are in design §8. Enforce fleet queued+pending
count and stored bytes in the transaction, with per-session cap 20. Ensure
legacy and managed admission do not each consume an independent “50” allowance
in one mixed-mode gateway. Do not load full prompts for queue-list summaries.

Convert these paths individually with their tests, in this order:

1. Web/Telegram/runtime session instructions and local/remote execution.
2. Compaction and ordinary active cancellation/close.
3. Case continuation token→turn linkage and durable finalization.
4. Watched-job notification, preserving existing Case attachment/dedup.
5. Quota and transient retry using the exact A/B/R rule.
6. Cache heartbeat with idle-only eligibility and expiry.
7. Respawn with existing Case lease/approval/role boot and durable new-session link.
8. Session-scoped file ingestion/restart recovery; inspect for remaining bypasses.

For each, record producer → durable trigger identity → turn ID → completion
effect in the Execution record. Permanent idempotency collapses replay even after
the coalesce key no longer covers a terminal row. In-memory finalizers may
accelerate work but cannot be the only path. A crash after token claim/admission
must be recoverable without another turn or lost round accounting.

Revalidate source-specific state at activation and closure. Do not add a new
Case poller or scan all Case event logs for each queue tick. Reuse the current
event watermark/batched quota work.

**Gate:** SCH/SYS/INT tests pass and grep/inventory shows no unmanaged execution
path into an enrolled session.

## 9. Stage 5 — scoped agent sender, not a new messaging platform

Implement `send_instruction(target_session_id, body, operation_id)` as one
dedicated stdio MCP sender tool. Reuse JSON-RPC/HTTP helper conventions without
inheriting `mcp_manager._token_candidates` admin fallback.
Add a dedicated script only if existing tool routing cannot keep credential
selection and permissions separate cleanly.

Credential contract:

- Gateway mints an opaque cryptographically random capability at an authenticated,
  currently owned carrier/session boot/provision request. Bind it from canonical
  claimed task/session to sender session, Case, role, credential generation and
  allowed operation. Request cannot choose arbitrary sender/Case bindings.
- Store only a cryptographic hash plus binding/revocation metadata in mesh DB.
  Raw token appears only in the private provisioning response and session-local
  MCP server environment; never in mesh task payload, list APIs, transcript,
  prompts, telemetry, artifacts, source files or logs.
- Validate current session open state, membership/generation, role and same-Case
  recipient on each send. Worker→Manager and worker→worker are allowed within
  the current open Case; no cross-Case, broadcast, self-send or system-source
  impersonation. Manager→worker is allowed by the same rules.
- Revoke on session close, Case-binding change and carrier replacement; reissue
  via current authenticated ownership, not fallback to admin credentials.
- Credential permits only send_instruction, not operator edit/withdraw/close,
  file staging, claims or credential minting. Unknown/revoked token returns 401;
  valid wrong-scope target returns 403.
- Existing shared node/admin trust remains; this is not an OS sandbox or the
  separate A71 per-node credentials project. Do not claim protection against
  host admin-secret theft. New scoped endpoint must not silently accept the
  shared worker/admin bearer as if it identified an agent.

Provision per backend instance: Claude `_get_or_create → _SDKSession._async_run`
can pass server-specific `ClaudeAgentOptions.mcp_servers`; verify installed
source rather than guess constructor fields. Codex native seam is
`_thread_config` (not the nonexistent `_session_config`).
Preserve user/project MCP settings, global environment and unrelated servers.
Do not turn on strict MCP replacement globally. Test two concurrent sessions
receive different tokens and never inherit each other's credentials.

The tool uses the collision-free admission resource with a scoped auth handler,
stable operation ID header and existing transport address resolution.
Do not create a new session to send to an existing one. Tool retry reuses key;
same key/different body conflicts. Return accepted ID/status, not “agent read it.”
Apply design rate bounds and Case/round policy without silently minting a Case.

**Gate:** AUTH and credentialed-agent INT tests pass on both local and remote
provisioning; no operator-only fallback counts as completed agent support.

## 10. Stage 6 — UI/API truth and compatibility

Implement the route table in design §9, including pause/resume and recovery
resolution. Use explicit Pydantic types and bounded cursor pagination.
Preserve `POST /api/instructions`' existing HTTP success status/envelope for
legacy callers; enrolled admission returns its canonical task ID and truthful
session state. Brand-new create route returns 202. Do not reuse telemetry DTOs.

Queue cards are a separate read model from historical exchanges. On activation,
move the same ID to Starting; on start authorization label Working conservatively;
on unresolved execution show Recovery required. Do not display a waiting prompt
as already consumed. Persisted queued count must not mark sessions BUSY.
Do not mark a session idle just because a later admission was rejected.

Mutations include expected revision; on 409 refetch the item without overwriting
the running prompt. Keep draft, attachment and first-turn carry semantics.
Stop active sets persistent operator queue pause and cancels only current work;
withdraw affects only the selected queued item. Resume clears only operator
pause, not provider/approval/Case/recovery holds.

Extend existing SSE invalidation and reconnect resync for queue keys/counts
after commit. Use shared SAFETY_NET_MS, not an extra interval system. Old
telemetry info tab, transcript, costs and task truth must still work.
Update all known status consumers, orphan scans and terminal-state sets together.

**Gate:** UI tests, web typecheck/build, API compatibility and telemetry regression
tests pass. Inspect component interaction with mocked network/backend; no paid
model needed.

## 11. Stage 7 — regression, pressure and rollout rehearsal

Run tests in bounded groups after reading fixtures. Proposed adjacent regression
inventory (all paths existed at dispatch; narrow/extend based on actual changes):

```text
tests/test_mesh_enqueue_affinity.py
tests/test_claim_reaper.py
tests/test_task_state_truth.py
tests/test_mesh_dispatch_timeout.py
tests/test_mesh_reconcile_spool.py
tests/test_session_cancellation.py
tests/test_session_close_propagation.py
tests/test_session_service.py
tests/test_session_service_lifecycle.py
tests/test_session_payload_roundtrip.py
tests/test_session_case_persist.py
tests/test_case_admission.py
tests/test_case_continuation.py
tests/test_case_interrupt.py
tests/test_case_closure.py
tests/test_case_quota_resume.py
tests/test_case_transient_resume.py
tests/test_case_respawn.py
tests/test_session_cache_heartbeat.py
tests/test_watched_jobs.py
tests/test_sdk_driver_proactive.py
tests/test_proactive_turn_delivery.py
tests/test_codex_ownership.py
tests/test_codex_native.py
tests/test_control_api_write.py
tests/test_control_api_fork.py
tests/test_telegram_session_flow.py
tests/test_mcp_manager.py
tests/test_mcp_jobs.py
tests/test_worker_role.py
tests/test_transcript_read_a81.py
tests/test_session_timeline.py
```

Run the new suites first, then relevant groups from above with
`.venv/bin/python -m pytest <explicit paths> --tb=short`.
Do not pass the entire tests directory. Inspect subprocess/backend fixtures:
stub/block real paid CLI/network invocation while retaining real DB/protocol
logic. A test's name alone does not prove it is offline. Use temporary roots
for DB, sessions, artifacts, spool and logs.

Web commands from the isolated worktree, each separately:
`pnpm --dir web test`, `pnpm --dir web typecheck`,
`pnpm --dir web build`. Follow existing lint/config checks when applicable.
Do not build into the live checkout's served dist.

Pressure rehearsal with fake carriers and temporary DB:

- 100 simultaneous maximum-size caller requests; only 4 queue mutations may
  execute concurrently. Excess gets finite 429; accepted work obeys per-session
  20/global queued+pending 50 and 100 MiB stored-intent bounds.
- New request total 256 KiB/body text 16 KiB; compatibility total 2 MiB with
  existing character limits. Include chunked oversize and slow bodies.
  A whole-body read before checking length fails this gate.
- Mutation lock/DB deadline 5 seconds and body-read deadline 5 seconds, measured
  separately. A request may spend both; do not falsely assert 5-second total
  including body upload. Allow bounded test scheduling tolerance, never multiply
  legacy 15-second busy retries after the new deadline.
- Hold the SQLite write lock from a second connection. Requests terminate
  within the deadline with structured failure; no false acceptance. Heartbeat
  reads/event loop remain responsive; writes under the held lock may fail
  promptly rather than magically succeed. Executor permits stay occupied until
  timed-out threads actually exit; no orphaned threads accumulate.
- Compare 1000 vs 100000 completed rows with the same 50 waiting items.
  EXPLAIN must use session/nonterminal indexes; no full-history scan/temp sort
  introduced by activation. Query count for Case eligibility stays batched.
- Measure peak RSS delta, number of executor threads/SQLite connections,
  scheduled worker handlers, result-delivery tasks, event-loop lag and request
  durations. Initial fake-load guard: less than 256 MiB incremental RSS for
  admission-only run; worker spool contents are not all loaded in memory.
  Exceeding it requires profiling/fixing, not raising the limit silently.
- Once the external test lock is released, pending eligible heads progress;
  admission pressure does not starve result/heartbeat capacity. Reject a design
  that is memory-bounded only because lifecycle work cannot run.

Rollout rehearsal on fixtures: enrollment refuses old worker capabilities and
busy/native-background sessions; admission exclusion closes the cutover race;
all enabled producers honor the marker. Turn the enrollment flag OFF with
accepted managed rows and restart: managed consumption/recovery must continue.
Drain/remove enrollment only after no waiting/active/recovery obligation remains.
Show legacy and MESH_ENABLED=false paths still behave as before.

Explicitly walk all six service-boundary items (concurrency, memory, payload,
timeout, malformed input, backing failure) for admission, start/result,
credential provisioning/send and recovery resolution. Put any true deferral
under A82 in CONTEXT with evidence; required safety gates cannot be deferred
while marking done.

## 12. Independent adversarial review and remediation

Three review gates: after Stage 0 contracts, after ownership/recovery Stage 3,
and after the complete implementation/test pass. Use a fresh reviewer context
at each significant gate. The final reviewer must inspect the complete candidate
and report against exact branch/base plus working-tree diff.

Preferred mechanism: built-in subagent with `fork_turns="none"`, explicit repo/
worktree path, packet/design pointers and read-only task. No parent transcript,
reasoning recap or suggested verdict. Do not pass “we fixed everything.”

If built-in delegation is unavailable, use the existing mesh Manager dispatch
tool to create a fresh Claude SDK reviewer with explicit `model="opus"`,
`backend="claude"`, `role="worker"`, and cwd equal to the isolated candidate
worktree. Inspect actual tool schema/API and available nodes first. Reuse
`dispatch_worker`/`wait_for_worker`; do not guess a /spawn endpoint, invoke
a sessionless oneoff, restart a node, or select a remote cwd that does not contain
the candidate. Reviewer on the gateway host can inspect the uncommitted worktree;
a remote worker must receive a verified candidate snapshot and base/diff hashes
through existing staging before reviewing. If it cannot see the exact candidate,
that review does not count. Never expose tokens in shell commands/transcripts.

User authorized these bounded review turns; no separate owner approval is needed
for the review itself. Use one reviewer at a time, no recursive delegation,
bounded output and normal model budget controls. If Opus is unavailable, another
fresh capable reviewer is acceptable; record the actual model, not the preference.
Read-only review can run on the current legacy mesh; it must not depend on
enabling the new feature being reviewed.

Reviewer prompt (fill factual placeholders only):

> Independently review A82 at WORKTREE, base BASE_SHA and candidate DIFF_HASH.
> Read .ai/CONTEXT.md, .ai/dispatch/AGENT_82_SESSION_TURN_QUEUE.md and the linked
> design, then inspect code/tests yourself. Do not modify files, run paid
> backends/live APIs, deploy, commit, or spawn agents. Try to falsify session
> serialization, claim/start/result fencing, SDK autonomous result attribution,
> completion/native-ID atomicity, FIFO/retry liveness, Case/token crash recovery,
> scoped sender authorization, bounded pressure and mixed-version rollout.
> Check integration fit and unnecessary new infrastructure, not just style.
> At each finding cite file/symbol, reproducible triggering sequence, violated
> requirement, severity and minimal fix/test. Distinguish existing baseline
> issues from introduced defects. Audit tests for mocks that bypass the changed
> boundary. Return findings plus explicit gaps in evidence and accept/rework.
> For final review, inspect UI/telemetry compatibility and all Stage 1 test IDs.
> No request to the owner for facts available in the repository.

Keep reviewer result in this packet's Review record with reviewer/session ID,
candidate hash, stage and concrete findings. No separate summary-document spree.
For each valid finding: write a failing regression, implement the smallest fix,
run affected checks, and have the reviewer independently verify the revised
candidate. A rejected finding needs code/test evidence and reviewer agreement;
author disagreement alone is not clearance.

Require no unresolved correctness, security, data-loss, deadlock, pressure or
compatibility finding before final commit. Record cosmetic suggestions with
reason if intentionally omitted. Test edits after review can alter the oracle;
get them checked too. If the candidate changes materially after final approval,
refresh affected tests and reviewer verification before committing.

Do not substitute the implementation agent's self-review for fresh review.
If all review mechanisms are unavailable, finish the candidate/tests and report
the exact tooling blocker; do not mark reviewed/done or fabricate a transcript.
Track every review session you create and close it only after its terminal
result using existing session lifecycle; leave no active orphan reviewer.

## 13. Final commit, PR and closure

The owner requested review before implementation commit. Keep Stage 0–7
changes in the isolated worktree until the final independent pass and remediation
are complete; do not create unreviewed checkpoint commits merely to transport
them to a reviewer. Preserve work on disk; no stash/reset/cleanup of user files.
A local reviewer can inspect the uncommitted candidate directly.

Once final review passes:

1. Run `git diff --check`, inspect scope and secret leakage, confirm required
   tests/build already passed for this candidate. Repeat only checks affected
   by subsequent changes; don't run the paid full suite.
2. Fill Execution/Review/Closure records with exact test commands, results,
   test-ID coverage, candidate/base reference, measured pressure bounds,
   implemented backend capability matrix and any separately pending live
   deployment checks. Do not report fake-carrier integration as live e2e.
3. Set evidence/results_ref through dispatch_state.py, update DISPATCH_LOG A82,
   remove A82 from active CONTEXT if present, and mark done only if all build
   gates passed. Keep flags OFF and production activation pending explicit
   deployment work. Proof paths must exist, not be future filenames.
4. Stage only A82 implementation/tests/docs plus generated dispatch changes
   produced by the repo hook. Commit on the feature branch after review.
5. Open a PR using existing authenticated Git tooling if available, describing
   resulting behavior, ownership/recovery changes, tests and rollout gates.
   Use a body file for multiline CLI text. Do not force-push. If remote tooling
   is unavailable, the reviewed local commit is still a real deliverable;
   report PR publication separately as unavailable.
6. Leave branch unmerged and live services untouched. Final response: branch,
   commit, PR if created, packet path, test/review verdict, remaining production
   activation requirements. Never say “guaranteed no issues.”

## 14. Milestone checklist

- [ ] Isolated branch/worktree; baseline and producer inventory recorded.
- [ ] Stage 0 source/path inventory and Claude SDK/Codex race-test oracle recorded.
- [ ] Stage 1 acceptance/regression/integration tests written and meaningful red recorded.
- [ ] Stage 2 schema and atomic DB/session/Case boundaries pass.
- [ ] Stage 3 carrier ownership/result spool/recovery and independent review pass.
- [ ] Stage 4 fair admission/scheduler and every producer integrated.
- [ ] Stage 5 actual scoped agent sender works locally/remotely.
- [ ] Stage 6 UI/API truth, edits, pause/recovery and telemetry compatibility pass.
- [ ] Stage 7 pressure, regression and rollout rehearsal pass.
- [ ] Final fresh-context adversarial review; every material finding resolved/rechecked.
- [ ] Reviewed implementation committed on feature branch; PR/handoff completed.

## 15. Execution record

### Stage 0 — ground-truth inventory (2026-09-25, read-only; on `main` @ `96cba58`)
Delivered by a Stage-0 investigation worker under A87 coordination; **every load-bearing symbol
resolved by name** (ctags/`symbol_lookup` unavailable in this env — Grep/Read used) and the P0 seams
re-verified from code by the Manager (A87). Key verified facts:

- **Root cause (design §2) CONFIRMED in code:** `control_api.py:1789` `mark_busy` writes BUSY +
  `last_user_message` **before** `submit_instruction` (1793) durably enqueues and **before**
  `session.last_task_id` is saved (1808). `HarnessAdmissionBlocked`→`mark_idle` unwinds, but any
  other failure/crash in that window strands the session BUSY with **no durable row**.
- **Terminal-atomicity gap (feeds A84 too):** `db.complete_task` (db.py:2268) / `fail_task` (:2290)
  do `UPDATE … WHERE id=?` with **no ownership/claim-token/status predicate**, wrapped in
  `try/except: logger.warning` — a **failed write returns silently as success**. Session native-id /
  active identity is reconciled in a **separate later save** (`_dispatch_to_node` / local
  `process_task`), so completion is NOT atomic with native-id commit → a successor turn can re-`create_session`.
- **Claim guard is node-identity, not per-attempt:** `task_server.submit_result` (:869) guards on
  `claimed_by != node_id`; a reaped/re-offered duplicate from the same node passes. Design §6 wants a
  fresh per-attempt `claim_token`.
- **Driver blind to background-task lifecycle:** `_reader_loop` (claude_driver.py:886) branches only on
  `AssistantMessage`/`ResultMessage`; references **no** `Task*Message` in code. The installed SDK
  0.2.110 exposes `TERMINAL_TASK_STATUSES` (types.py:1074), `TaskNotificationMessage` (:1115),
  `TaskUpdatedMessage` (:1140) — terminal via *either* message — so a real quiescence oracle is
  buildable without a driver/backend swap. Quiescence requires ALL of: `_pending` empty **and** no
  non-terminal tracked background task_id **and** the terminal `ResultMessage` of the last `query`
  observed. "task-finished notification" and "empty `_pending`" are each individually INSUFFICIENT.
- **`send()` interrupts on lock conflict (claude_driver.py:1127-1134):** calls `cancel_inflight()` then
  re-acquires (deliberate — the ever-growing-transcript fix). §3.17 tension: the managed path must diverge.
- **Codex seam:** reservation/MCP-injection seam is `CodexBackend._thread_config` (codex_native.py:153);
  `_session_config` does NOT exist. One-active-turn enforced via `_active[key]` + `codex_thread_busy`.
- **Schema baseline:** highest migration = **33**; next available = **34**. `mesh_tasks` has NONE of
  `queue_protocol/queue_sequence/idempotency_*/revision/claim_token/coalesce_key/turn_source/
  sender_session_id/turn_kind/not_before/expires_at/activated_at/started_at` — all to-be-created.
  `flow_run_id` was added by `_ensure_substrate_columns` ALTER (db.py:1475), NOT a numbered migration,
  and `enqueue_task` does not populate it — managed rows must set Case membership explicitly.

### Manager (A87) decisions gating Stage 2 — DECIDED 2026-09-25
1. **Legacy vs managed divergence at `_SDKSession.send`:** build a DISTINCT protocol-1 (managed)
   no-interrupt path returning a typed ownership conflict (409) that NEVER calls `cancel_inflight`;
   legacy protocol-0 interrupt-on-conflict stays **byte-identical** (do not regress the transcript-bug
   fix). One shared path is rejected.
2. **Completion helpers:** ADD new strict helpers (throwing typed failures; ownership/claim-token/status
   predicates + atomic native-id/active-identity commit) used ONLY on the protocol-1 path. Do **NOT**
   modify the legacy swallowing `complete_task`/`fail_task` (least-action; avoids rippling into every
   legacy caller). Legacy callers keep the existing helpers.
3. **Stage 1 "meaningful red" scope APPROVED as:** author now the assertion-capable suites
   (OWN/WRK/SDK/SYS + API compatibility + LOAD) which fail on assertions against current behavior; the
   module-dependent suites (new DB helpers/SCH/AUTH/INT/ROLL/UI) are accepted as ImportError-red until
   Stage 2 skeletons land. Migration-survival DB cases may be authored once migration 34 exists.

Stage 2+ (behavior-changing) remains GATED on the Manager's review of the Stage 1 red tests.

### Stage 2 — schema + transaction seams + session ownership (2026-09-25, on `feat/session-turn-queue`)
Delivered the design §6 Stage-2 subset. **STOP for Manager review + adversarial review before Stages 4-6.**

- **Migration 34** (`src/control/db.py` `_get_migrations`): ADDITIVE, NULLable/DEFAULT-safe on `mesh_tasks`
  (`queue_protocol` DEFAULT 0, `queue_sequence`, `turn_source`, `sender_session_id`, `turn_kind`,
  `idempotency_scope/idempotency_key/admission_hash`, `revision` DEFAULT 1, `not_before/expires_at`,
  `activated_at/started_at`, `claim_token/claim_carrier_kind/claim_incarnation`, `coalesce_key`,
  `blocked_reason`) + `mesh_turn_revisions` audit table + `sessions.turn_queue_enrolled/turn_queue_paused/
  config_revision`. Six partial indexes exactly per design §3 (one-active-session, session-sequence,
  waiting, session-open, idempotency, active-coalesce) — ALL partial on `queue_protocol = 1` so they
  cannot fire on legacy data. Verified installs cleanly over a v33 fixture carrying duplicate legacy
  active rows (l1/l2 pending + l3 claimed same session), a cancellation row, and a NULL-session sentinel
  token; all preserved as protocol 0.
- **Strict managed DB helpers** (new, protocol-1 only, one transaction each, typed errors, NO swallow):
  `enqueue_turn` (idempotency→sequence→insert), `activate_turn` (queued→pending), `revise_turn`/
  `withdraw_turn` (CAS on revision + atomic `mesh_turn_revisions` audit), `claim_turn` (fresh opaque
  token bound to carrier/incarnation), `start_turn` (once-only claimed→running, idempotent same-token,
  incarnation-fenced), `release_turn`, `complete_turn` (atomic terminal + native-id + active identity),
  `_commit_completion_identity`/`update_session_fields` (field-scoped versioned session write),
  `enter_recovery`/`resolve_recovery` (evidence-gated), `get_active_turn`, `enroll_session`,
  `get_turn_revisions`. Legacy `complete_task`/`fail_task`/`claim_task`/`enqueue_task`/`upsert_session`
  are **byte-identical** (zero deletions in `db.py`).
- **Typed outcomes + models**: `src/control/turn_queue.py` (401/403/404/409/413/422/429/503 typed errors
  carrying `.status_code`; `ClaimToken` str-subclass; `StartAuthorization`/`CompletionResult`/
  `RecoveryResolution` Pydantic v2; state-set constants). No HTTP objects, no framework.
- **Managed SDK ownership path** (`src/backends/claude_driver.py`, additive only, zero deletions):
  `_SDKSession.send_managed` (protocol-1 fail-closed typed `OwnershipConflictError` on lock conflict,
  NEVER `cancel_inflight`); `is_quiescent()` oracle (conjunction: empty `_pending` + no non-terminal
  tracked background task + last-query terminal + no in-flight continuation); reader-loop now tracks
  `TaskUpdatedMessage`/`TaskNotificationMessage` + assistant-in-flight. Legacy `send`/`cancel_inflight`
  UNCHANGED.
- **Tests**: authored `tests/test_turn_queue_db.py` (DB01-08 + happy-path lifecycle, 12 cases, real
  file-backed SQLite) — all GREEN. Turned GREEN in `test_turn_queue_ownership.py`: OWN01, OWN02-10 (11/12);
  in `test_turn_queue_sdk_ownership.py`: SDK01, SDK03, SDK04a/b/c (5/6).
  **Still RED (out of Stage-2 scope, flagged for Manager):**
    * `OWN01b` — asserts the LEGACY `claim_task` writes a per-attempt `claim_token`; this contradicts
      binding §15 decision 2 (legacy path stays byte-identical). The test's own name documents legacy
      `claimed_by` is node identity, yet the assertion demands a token — a self-inconsistent contract.
      Left RED, not silently edited (per dispatch instruction).
    * `SDK02` — no-autonomous-misattribution depends on the Stage-3 carrier reservation/result-correlation
      contract (design §6 "reserve on the SDK loop before submitting a query"); a bare background
      `ResultMessage` with a prompt pending carries no lifecycle signal at the DB/ownership layer. Correctly
      stays RED for Stage 3.
  One Stage-1 FIXTURE corrected: `test_turn_queue_ownership.py::_enqueue` now uses `enqueue_turn` +
  `activate_turn` (managed protocol-1) instead of legacy `enqueue_task` — the ownership contract it asserts
  is protocol-1 only; no assertion was weakened.
- **Flag-OFF byte-identity**: `queue_protocol` DEFAULTs 0; legacy `enqueue_task` sets no managed columns
  (verified: protocol-0, NULL sequence, `pending`); zero deletions in `db.py`/`claude_driver.py`; all named
  adjacent regressions pass (session_service, case_admission, sdk_driver_proactive, task_state_truth,
  claim_reaper, mesh_enqueue_affinity, lifecycle/payload/case_persist, proactive_turn_delivery,
  cancellation/close_propagation, case_continuation/interrupt/closure/quota_resume/transient_resume,
  codex_ownership/native, transcript_read_a81, mesh_reconcile_spool/dispatch_timeout, control_api_write,
  cache_heartbeat, watched_jobs — 300+ cases, all green).
- **EXPLAIN plans** (5000-completed-row fixture + ANALYZE): active-slot lookup →
  `idx_mesh_turns_one_active_session`; waiting-head scan → `idx_mesh_turns_waiting` (SCAN, NO temp B-tree);
  idempotency → `idx_mesh_turns_idempotency`; latest-sequence (ordered LIMIT 1) →
  `idx_mesh_turns_session_sequence`; session-open scan → `idx_mesh_turns_session_open`; coalesce →
  `idx_mesh_turns_active_coalesce`. No full-history scan / temp sort on the hot paths.
- **NOT done (out of scope, stay RED)**: Stage 4 admission/scheduler (§8), Stage 5 sender (§9), Stage 6
  UI/API (§10); the api/worker/producers/pressure suites remain red/import-pending by design. NOT merged.

### Stage 3 rework — A87 REWORK findings closed (2026-09-25, on `feat/session-turn-queue`, commits `e4f766d`, `fb45026`)
Each finding → fix → proving test. All tests run offline (fake SDK client / fake backend; no paid CLI).

| Finding | Fix | Test ID(s) |
|---|---|---|
| **M5/M6** bare `ResultMessage` deadlocked legacy `send` + forced `cancel_inflight` | `_dispatch` (claude_driver.py `_dispatch`) restores legacy FIFO byte-identically; correlation gate applies ONLY to a `managed` head. An uncorrelated managed result fails the managed turn closed with typed `RecoveryRequiredError` (turn_queue.py), surfaces output via the proactive sink, keeps the session non-quiescent. Removed the settle-tick hack. Only `SystemMessage(subtype="init")` marks response start (Task*/hook subclasses excluded). | SDK05a, SDK05b (fails on `b309f84`, verified), SDK06a, SDK06b |
| **M4** `is_quiescent` on caller thread (TOCTOU) | `_reserve_and_submit_managed` runs the check on the SDK loop in the same step as the `_pending` registration; `_submit_managed_no_interrupt` replaces `submit()` for managed (deadline ⇒ `RecoveryRequiredError`, never `cancel_inflight`). | SDK07, SDK06c, SDK01 |
| **B1** managed result posted to legacy `/result` | `_post_managed_result_once` → `/result-managed` (atomic `complete_turn` via shared `_commit_managed_result`); prune only on task+token receipt. | INT01, INT02[5xx/timeout/2xx-unmatched] |
| **B2** worker never polled/claimed managed | Flag `WORKER_MANAGED_TURNS` (default OFF). ON: registers `queue_protocols:[0,1]`, `_fetch_pending_managed`, `_claim_and_start_managed` (`/claim-managed` token → envelope reservation → new fenced `/start-managed`), executes the claim response. OFF: legacy `/tasks/pending` + `/claim` + `/result` only (legacy claim no longer swaps in the claim-response row — restored to pre-Stage-3). | INT01, INT05, INT09 |
| **M1** drain guard dead | `run()` drain: managed attempts go through `_managed_shutdown_release_ok` + token-fenced `/release-managed`; running/undelivered retained. | INT04 |
| **M2** reservation dead / budget unenforced | Reservation before start; spool counts outstanding reservations against 128 MiB; no allowance ⇒ release unstarted claim, turn stays pending. | INT03 |
| **M3** boot replay never re-sends | `run()` re-delivers after registration; unacked results retried each poll pass (batch 8, 2 concurrent, no background tasks); old token accepted after restart unless superseded. | INT02 (via real `run()`) |
| **m1** `/quiescence` accepted any non-null result | Evidence must match recorded token + claiming node; a `result` must validate as `ManagedTerminalResult` (boolean `success`) and reconciles via `complete_turn` (native id committed); otherwise quiescent+terminal+native id, resolving only failed/cancelled. | INT07, INT08 |
| **m2** stale receipt echoed unverified token | Stale receipt only if presented token == recorded token (`hmac.compare_digest`); else 409 without echo. | INT06 |

Also fixed while wiring: claim token was stored in `_active_meta` (published in heartbeat `active_task_details`) — moved to `_managed_claims`; `/claim-managed` response now carries `backend`/`action` so the claim response is executable.

**Fixture correction (flagged for review):** SDK02 now submits via the managed send. On the legacy path a bare background `ResultMessage` is indistinguishable from a legitimate result-only reply, and dec.1 + M5/M6 require legacy to keep serving it (SDK05b asserts exactly that). The SDK02 assertion is unchanged and strengthened (no deadlock, no interrupt, output reaches the proactive sink).

**Verification (worktree, targeted only):** turn-queue Stage 1-3 + INT: 55 passed; driver/proactive suites (claude_driver, sdk_driver_proactive, proactive_turn_delivery, sdk_governor, claude_driver_manager_tools): 121 passed; carrier/legacy regressions (claim_reaper, codex_ownership, docker_boundary, heartbeat_live_state, mesh_dispatch_timeout, mesh_enqueue_affinity, mesh_reconcile_spool, mesh_self_awareness, session_close_propagation, task_server_client, task_server_upload_safety, task_state_truth, usage_propagation, worker_pinned_only, worker_startup_registration): 122 passed; session_service/session_cancellation/case_admission/control_api_write/warm_worker_idle_reaper/worker_role/case_observable_worker_session: 96 passed. INT01-09 fail on `b309f84` (verified, non-vacuous).

**Still red, later stages:** `test_turn_queue_api.py` (2: `/turn-requests` routes, commit-before-ack — Stage 6 §10), `test_turn_queue_pressure.py` (3: LOAD01b/02/04 admission byte budgets / pre-parse limits — Stage 4 admission + Stage 7 pressure), `test_turn_queue_producers.py` (7: SYS01/03-08 producers/scheduler — Stage 4 §8).

**Open gap (not in the finding list; not closed):** the backend execution path still calls legacy `_SDKSession.send` (claude_driver.py `_run_turn`) for managed rows — `send_managed` is not yet selected for protocol-1 execution, and the carrier has no route to move a started turn into `recovery_required` on `RecoveryRequiredError`. Wiring it needs a managed marker through `_execute_task`→`call_backend`→`_run_turn` plus an enter-recovery carrier route; proposed as the first Stage 4 item or a follow-up rework, Manager's call. §7 deferrals: `/pending-managed` still trusts the `queue_protocols` query param rather than the registered capability; oversize results block new managed claims but no bounded diagnostic record is posted to the server yet.

### Stage 3 rework 2 — A87 follow-up items 1-5 (2026-09-25, commit `a4b572b`)
| Item | Fix | Test ID(s) |
|---|---|---|
| **1 Managed execution seam** (per A87 correction: interface, not a Claude side door) | `CodingBackend` (src/core/interfaces.py) gains `supports_managed_turns()` / `run_managed_turn(session, message, ownership)` / `is_quiescent(session)`; default = unsupported (`ManagedUnsupportedError`, False). Typed `turn_queue.ManagedTurnOwnership` (claim token `repr=False`). `_execute_task(ownership=...)` dispatches claimed managed rows ONLY through `backend.run_managed_turn` — no backend-name/isinstance branching, no legacy fallback. Claude adapter implements it via `ClaudeSDKClientDriver.run_managed_turn` → private `_SDKSession.send_managed`; legacy `start_session`/`send_turn` signatures are the pre-rework ones. Codex/OpenCode keep the default (verified no override) → never advertised; worker skips claiming a managed row whose backend is not in `_managed_backends()`; server refuses the claim. | INT11 (real Claude SDK driver + fake SDK client: managed row → `run_managed_turn` → `send_managed`, `send`=0), INT13 (legacy row, flag ON → `send`), INT10b, INT14; mutation-checked: bypassing the interface fails INT11/12/14 |
| **2 Enter-recovery route** | `/tasks/{id}/enter-recovery` (token + claiming-node fenced, Stage-2 `enter_recovery`, idempotent, never releases/completes). Worker: `RecoveryRequiredError` (uncorrelated result / managed deadline) → `error_class=recovery_required` → `_enter_managed_recovery`; no result posted; reservation returned; `_managed_claims` status `recovery_required` (drain guard refuses release); DB slot held. | INT12 (bare uncorrelated result on the real driver → `recovery_required`, no `/result-managed`, no interrupt, output in proactive sink, foreign-token route call 409) |
| **3 `/pending-managed` capability** | Gate on the node's REGISTERED `NodeCapabilities.queue_protocols` + `managed_backends` (set from `/nodes/register`); query param ignored. `/claim-managed` refuses a row whose backend is not registered managed. Worker registers `managed_backends` derived from `supports_managed_turns()`. | INT10, INT05, INT10b |
| **4 Integration** | Covered by INT10-INT15 above (18 tests in the file). | — |
| **5 Oversize diagnostic** | IMPLEMENTED (bounded): oversize / spool-write failure → `/enter-recovery` with `blocked_reason` ≤500 chars (`managed_result_oversize: ... output_chars=N`); no truncated result committed; new managed claims stopped. **Still deferred:** the full oversize output is NOT persisted to artifact storage on the carrier (held only in process memory until the handler exits) — §7 "preserve full backend artifacts" remains open. | INT15 |

Also: envelope reservation now uses the spool's configured cap (was the constant).

**Verification:** turn-queue Stage 1-3 + INT: 62 passed; driver suites: 121 passed; carrier/legacy regressions: 122 passed; session/case/control suites: 96 passed; interface-touching suites (affinity_fallback, backend_registry, codex_native, claude_session_backend, control_api, backend_call, claude_telemetry_adapter, node_inspector, claude_job_session_env, output_truncation): 139 passed. Still red (later stages, unchanged): api 2, pressure 3, producers 7.

**Known residuals:** registry capability is in-memory (a gateway restart falls back to `[0]` until the worker re-registers — fail closed); `release_node_claims` on a new incarnation also returns managed `claimed`-not-started rows to pending (safe: not started; stale `claim_token` is re-minted on next claim). A `managed_conflict` (lock busy / not quiescent, raised before submit) is reported as a failed result, not held.

### Stage 3 rework 3 — A87 adversarial review (7 probes) closed (2026-09-25, commit `bcba6e7`)
Governing rule: every managed state has a live, tested exit. Probes P1-P6b adopted as permanent tests in
`tests/test_turn_queue_carrier_recovery.py`, rewritten to assert correct behavior; ran that file against `7863895` in a
temporary worktree: 23/24 fail there (all 7 probes included), 24/24 pass on `bcba6e7`.

**Exit per managed state:**

| State (DB / carrier record) | Exit(s) | Test |
|---|---|---|
| `claimed`, never started | carrier release (token); boot/poll reconciler release (not-invoked); operator `requeue` | B2b, P3, INT03, B2e |
| `running`, start response lost | idempotent start retry; else release w/ not-invoked attestation (running→pending); else durable record → reconciler | P2, P2b, P2c |
| `running`, backend invoked, carrier crashed | boot reconciler: enter-recovery + `carrier_restarted` evidence → failed | B2 |
| `running`, loop-thread conflict (never submitted) | release w/ not-invoked → pending | P3b |
| `recovery_required` (uncorrelated / deadline / oversize / 4xx result) | late reply → `/result-managed` (completed); reconciler `backend_quiescent` → failed once quiescent; operator failed/cancelled | P4b, B2c, B2d, m3, INT15, B2e |
| enter-recovery POST lost | `recovery_acked=False` persisted → reconciler retries until acked | B2c |
| result spooled, unacked | replay each poll pass + boot; definitive 4xx → dead-letter + recovery | INT02, m3 |

| Finding | Fix (file) | Test |
|---|---|---|
| B1 lost start | `_claim_and_start_managed` bounded retries; `release_turn(backend_not_invoked=True)` (db.py) | P2, P2b, P2c |
| B2 no exit | `ManagedClaimStore` (managed_result_spool.py), write-ahead `invoked`, `_reconcile_managed_claims` (agent.py), `/quiescence` evidence kinds `carrier_restarted`/`backend_quiescent` checked against the claim + registered incarnation, operator `POST /api/turn-requests/{id}/resolve-recovery` (control_api.py) | B2-B2f |
| M1 control starvation (legacy) | flag OFF: poll loop exactly as on main; flag ON: capacity gate exempts `close_session`/`cancel_codex` | P1, P1b |
| M2 prompt lost when not quiescent | pre-start `backend.is_quiescent` → release; `managed_conflict` → release not-invoked | P3, P3b |
| M3 late reply dropped | driver `_abandon_managed_pending` + `late_managed` routing + `_late_handoffs` quiescence hold; worker `_capture_late_managed_result` | P4, P4b |
| M4 continuation adopted | `_autonomous_expected` (per terminal TaskNotification); managed head never takes that result | P5 |
| M5 legacy bypass | protocol-0 fence in `claim_task`/`release_task`/`release_node_claims`/`list_stale_claims`/`complete_task`/`fail_task`; legacy `/claim` `/result` `/release` → 409 on protocol-1; `claim_token` stripped | P6, P6b, P6c |
| m1–m6 | token cleared on return to pending; 413 body cap as a dependency before validation + `extra=forbid`; dead-letter (≤256, counts toward the 128 MiB budget); dir fsync + orphan `.spool-*.tmp` cleanup; lazy spool/claim-store construction; reservation released in `finally` | P2b, m2–m6 |

**Test changed to match the M5 ruling:** `test_turn_queue_db.py::test_DB08` last lines asserted legacy `complete_task` completes a
MANAGED row, which is exactly the bypass A87 forbade. The legacy half now uses a protocol-0 row, and the test now also asserts that the managed row is untouched.

**Verification:** turn-queue (incl. carrier integration + recovery): 86 passed; driver suites 121; carrier/legacy regressions 122;
session/case/control 96; interface-touching 139; suites exercising the fenced legacy DB helpers (14 files) 242. Still red,
unchanged later stages: api 2, pressure 3, producers 7.

**Residuals (documented, not closed here):** the `_autonomous_expected` correlation assumes the CLI runs exactly one autonomous
turn per terminal task notification when idle; if it runs none, a managed reply is diverted to the proactive sink and the turn
reaches its deadline, then goes to recovery. Fail-closed, but the late reply is then not recaptured, because it already went to
the proactive transcript. Oversize full output is still not written to carrier artifact storage (A87 to record in CONTEXT.md).
The operator route uses the existing dashboard auth (which also accepts the mesh worker token, as all `/api` routes do).

### Stage 3 rework 4 — A87 round-2 adversarial review closed (2026-09-25, commit `4eaf369`)

**Live spike (authorized by A87):** 3 single-line turns on `haiku`, `max_turns=1`, all tools disallowed, throwaway cwd, SDK
0.2.110, `extra_args={"replay-user-messages": None}`. Script + raw log kept in the worker scratchpad (`spike.py`, `spike_out.log`). Observed stream per turn:
`SystemMessage(init)` → (turn 1 only: `RateLimitEvent`) → `SystemMessage(thinking_tokens)`×N → **`UserMessage` echo** →
`AssistantMessage`(thinking) → `AssistantMessage`(text) → `ResultMessage`.
- Turn 1 (AsyncIterable, caller uuid `d9cc3f10-df82-41a9-8f0d-f54e98b093b6`): echo `UserMessage.uuid == d9cc3f10-…` ✔; result "ONE".
- Turn 2 (caller uuid `4cd1ce9c-f4ea-4b9a-86b6-68d9d660cbba`): echo uuid `4cd1ce9c-…` ✔; result "TWO".
- Turn 3 (string form, no uuid): echo carries a CLI-minted uuid (`b366badb-…`); result "THREE".
**Conclusion: the CLI PRESERVES the caller uuid, and the echo comes from inside the turn that processes the prompt: after that
turn's `init`, before its reply.** NOT verified live (no tools under the authorized budget): where a background
task-notification / autonomous continuation sits relative to our echo. The design does not depend on that. Only the turn begun by
our echo is ours; A87's transcript finding that notifications are folded into the running turn is covered by D1.

| Finding | Fix | Test |
|---|---|---|
| **B1** counter refuted | `_autonomous_expected` REMOVED (and its legacy reset). Echo correlation: sessions of a managed carrier (`WORKER_MANAGED_TURNS`) run `--replay-user-messages`; managed prompt written via AsyncIterable `query` with a caller uuid; the turn begun by that echo is served exactly the `ResultMessage` that closes it; other turns → proactive sink. Legacy FIFO unchanged (echoes ignored; no replay when the flag is OFF). `send_managed` refuses (`ManagedUnsupportedError`, nothing submitted) without replay. Wedge exits: abandoned head popped by its own result, or by the next foreign result if its echo never came (kept as a bounded ghost → a later echo still routes `late_managed`), or close/stream end; `is_quiescent` recovers. | D1, D1b, D2, D3, D4, SDK02 (defer-echo = spike ordering), SDK06b, P5, INT12 |
| **B2** unchecked `carrier_restarted` | backend pid + create_time recorded at invoke via `CodingBackend.run_managed_turn(on_process=…)` → `ManagedClaimStore`; reconciler posts `carrier_restarted` only with `process_proof` (pid absent / create_time mismatch); server requires it. No proof (no psutil / denied / alive / pid unrecorded) → held for the operator route. `psutil>=5.9.0` in pyproject + `psutil==7.2.2` in constraints.txt. | B2 ×5 (no-psutil, denied, alive, pid-reused, absent), B2 server proof, S2 |
| **M1** poll loop death | managed poll-pass work wrapped (logged, legacy polling continues); dead-dir mkdir inside try | S3, M1 poll-survival |
| **M2** dead letters / starvation | dead letter retired once recovery is acked (leaves budget, `.reason` audit bounded); cap/unwritable → parked, never re-POSTed; rotating cursors for replay + reconcile | M2 cap-park, M2 cursor, m3 (updated) |
| **M3** test honesty | mutation-checked in a scratch worktree (removed without `--force`): (a) drop `_late_handoffs` conjunct → M3a fails; (b) drop `observer != claim_inc` → S2 fails; (c) drop reconciler delivery-skip → M3c fails; (d) no legacy reset remains; extra: drop m1 re-check → m1 fails; FIFO-ignore echoes → D3/SDK02/P5 fail | M3a, S2, M3c, m1 |
| m1–m5 + harness | m1 re-check after probe await; m2 streamed ASGI byte cap (`src/control/body_cap.py`) on every managed carrier route + operator route, chunked included; m3 spool sized with the ASCII wire serialization; m4 `_fail_offline_tasks` skips managed rows; m5 reply served between timeout and abandon → `late_managed`; fake SDK client emulates the verified echo; fake session close awaits the reader (P5 hang) | S1, S1b, m3, m4, m5 |

Round-2 file `tests/test_turn_queue_r2.py`: 22 of 24 tests fail on `789449d` (every adopted probe except the mutation guard M3a); 24/24 pass on `4eaf369`.
**Verification:** turn-queue incl. carrier/recovery/r2: 110 passed; driver 121; carrier/legacy regressions 122; session/case/control 96;
interface-touching 139; legacy-DB-helper users 242. Still red (later stages, unchanged): api 2, pressure 3, producers 7.
**Deploy note:** the live venv has no psutil. Until the prod install workflow runs (`pip install -e … -c constraints.txt`),
`carrier_restarted` cannot be proven and every crashed-invoked attempt waits for the operator route (fail-closed).
`reap_stale_worker_children` was also a silent no-op without it.

### Stage 3 rework 5 — A87 round-3 review closed (2026-09-25, commits `31ffb62`, `2bca420`)

| Finding | Fix | Test (mutation killed) |
|---|---|---|
| **MAJOR-1** late reply bound by session | The carrier picks the managed turn uuid and persists it write-ahead in the claim record. `ManagedTurnOwnership.turn_uuid` passes it through `run_managed_turn` → `send_managed`, and the prompt is submitted and echoed under it. A late outcome carries `managed_turn_uuid`. `_capture_late_managed_result` binds ONLY by that exact identity: an empty or mismatched id is never bound, and there is no session fallback. The no-proof reconciler branch re-consults the server with a rate-limited, idempotent `enter-recovery`. A definitive refusal (terminal / operator-resolved) drops the record. Warnings are rate-limited the same way. | R3 stale-claim probe (adopted), R3 no-session-binding, R3 rate-limit; mutations: bind-by-session → fails; drop held server probe → fails |
| **MAJOR-2** echo-uuid guard uncovered | New test: a managed turn is waiting; a foreign turn carries a tool_result `UserMessage` (different uuid) plus its result. That result goes to the proactive sink and the managed future stays unresolved. Then our echo + reply are served. | `test_MAJOR2_…`; mutation `p.managed and p.turn_uuid == uid` → `p.managed` now FAILS |
| **MINOR-1** unechoed prompt | **Chosen variant:** a managed prompt written to the CLI but not yet echoed stays pending. The session stays NOT quiescent even after the caller's deadline, and an unrelated result neither pops it nor frees the session. Its exits are all bounded and live: (1) its own echo + result → `late_managed`, bound by uuid, then quiescent; (2) session close / stream end → `_fail_pending`; (3) the DB row can always be resolved via the operator route. Ghost map and pop-on-foreign removed. Added: a per-call abandon ticket. If the deadline fires before the managed coroutine even registers (starved loop), the prompt is never submitted, so nobody is left awaiting it. | D3 (rewritten: non-quiescent until own echo), D3b (stream-end exit), m5, m5b; mutation "pop unechoed on foreign result" → fails |
| **MINOR-2** retire on later ack | `_enter_managed_recovery` retires the dead letter / parked envelope on ANY acknowledged recovery, including a later reconciler pass. | R3 dead-letter-later-ack; mutation → fails |
| **MINOR-3a** wall-clock immunity | `process_utils.process_identity/process_gone_proof`. On Linux the identity is `{pid, boot_id, starttime_ticks}` (/proc/<pid>/stat field 22, boot-relative; no wall clock, no psutil needed). Proof: `/proc/<pid>` absent ⇒ absent; boot_id differs ⇒ rebooted; ticks differ ⇒ pid_reused. Elsewhere it uses psutil: create_time must differ by more than 2 s AND the cmdline must differ; NoSuchProcess ⇒ absent. Anything ambiguous or denied ⇒ no proof. The server accepts `absent`/`pid_reused`/`rebooted`. | B2 ×6 (absent, pid-reused, rebooted, alive, ambiguous, unrecorded — real /proc), non-Linux tolerance test; mutations: drop ticks compare → fails; exact wall-clock compare → fails |
| **MINOR-3b** reparented descendants | **Residual, not closed (no vendored-SDK patch).** SDK 0.2.110 `SubprocessCLITransport.connect` calls `anyio.open_process(cmd, stdin, stdout, stderr, cwd, env, user)` with no `start_new_session`/`process_group` hook, so the CLI shares the worker's process group and a pgid-based "no live process in the group" proof is impossible. Alternatives, all rejected for this stage: a `setsid` wrapper as `cli_path` (changes the launch path of every session); a custom `Transport` re-implementing `connect` (effectively forking vendored code). Consequence: `process_proof` covers only the CLI process itself. A `run_in_background` descendant reparented to init can outlive it undetected. → A87 to carry to CONTEXT.md. | — |

Adopted round-3 probes: `tests/test_turn_queue_r3.py`. The stale-claim, no-session-binding and MINOR-2 tests fail on `52b34ec`. The body-cap probe and the rate-limit test already pass there: the cap was fixed in round 4, and the probe-rate path is new. Timing hardening: the r2 driver tests wait until the prompt has actually reached the fake CLI. They were order-dependent under load in the full group, and diagnosing that exposed the abandon-before-submit race fixed above.
**Verification:** turn-queue (db/ownership/sdk/worker/integration/recovery/r2/r3): 120 passed ×2 runs; driver 121; carrier/legacy
regressions 122; session/case/control 96; interface-touching 139; legacy-helper/process_utils users 255. Still red (later stages,
unchanged): api 2, pressure 3, producers 7.

### Stage 3 rework 6 — A87 round-4 review closed (2026-09-25, commits `ca50f0a`, see git log for the follow-up)

| Finding | Fix | Test (mutation killed) |
|---|---|---|
| **MAJOR-1** clean CLI exit wedge | The reader's `finally` now always sets `_reader_ended` (normal EOF or error). `_SDKSession.is_quiescent()` returns True for a dead reader, unless a late reply is still being handed off (the driver defers to it). `_get_or_create` evicts a managed-carrier session (replay on) whose reader ended: it marks the session closed, pokes its idle loop, and respawns with resume. Flag-OFF sessions (no replay) never take the new branch, so legacy is byte-identical. | Adopted EOF probe, both variants (abandoned / in-flight), fresh-session replacement, flag-OFF not-evicted; mutations: no flag / no eviction / evict-regardless-of-flag → fail |
| **MINOR-1** operator resolve = real exit | `CodingBackend.forget_managed_turn(session, turn_uuid)` (default no-op; Claude drops the pending entry on the loop, clears the owner, and marks the query terminal if nothing else is pending). The worker's `_drop_attempt` runs on every definitive 404/409 from `enter-recovery`/`quiescence`. The reconciler now also consults the server (rate-limited) while a live backend is not quiescent, not only when there is no proof. | submitted-never-echoed + alive CLI + operator resolve ⇒ session quiescent ⇒ next managed turn completes; mutation "no forget" → fails |
| **MINOR-2** refusal classes | Only 404/409 are definitive for the recovery/quiescence probes; 401/403/other 4xx are transient (record kept, rate-limited retry). | 401/403/400 keep, 404/409 drop; mutation "any 4xx" → fails |
| **MINOR-3** not-submitted attestation | On the deadline, the caller waits (≤5 s) for the loop-side abandon. If the ticket shows the prompt was never registered, it raises a typed `OwnershipConflictError(not_submitted)` → `managed_conflict` → the worker releases it as not-invoked: back to pending, prompt preserved. | starved-loop test; mutation → fails |
| **MINOR-4** tests | `/proc/<pid>/stat` with `)` in the process name (symlinked `sleep` named "evil) name"); late-capture session-mismatch guard. | mutations `rsplit→split` and "no session guard" → fail |

**Corrected exit table (replaces the wording of rework 3/5 where it differs).** A managed prompt written to the CLI stays pending (session not quiescent) until ONE of:
1. its own echo + result arrive — served, or routed `late_managed` and bound by turn uuid;
2. the CLI's stream ends (clean exit or error) — pending failed, session dead ⇒ quiescent, and replaced on the next managed turn;
3. the session is closed;
4. the server says its row is terminal (operator resolve / any 404/409) — the carrier forgets the pending wait, and the session becomes quiescent while the CLI keeps running.

DB-side exits are unchanged (reconciler evidence or operator route). The earlier claim that an operator resolution alone freed the session was not true before this rework; it is true now (test above).

Cost note: one mutation run (evict-regardless-of-flag) briefly spawned the real CLI through an unpatched `_SDKSession.start` in the flag-OFF test. It connected and exited with code 1 (invalid resume id). No query was sent, so no model turn happened. The test now patches `start` to fail loudly, so the mutant fails without spawning.
Round-4 file `tests/test_turn_queue_r4.py`: 8 of 13 tests fail on `dc970f5`. The other 5 pass there: flag-OFF not-evicted, 404/409 drop, the `/proc` paren test and the session guard. They are guards on behavior that was already correct, and their mutations are verified above.
**Verification:** turn-queue (9 files): 133 passed ×2; driver 121; carrier/legacy 122; session/case/control 96; interface 139;
legacy-helper/process users 255. Still red (later stages, unchanged): api 2, pressure 3, producers 7.

### Stage 3 ACCEPTED (round 5) — final minors (2026-09-25, commit `071a78f`)
- **MINOR-1:** in `_run_turn`, a managed prompt whose stdin write was refused by a terminated CLI ("Cannot write to terminated process") is never sent. The dead session is still torn down, but the result is now `error_class=managed_conflict` / `not_submitted` instead of `transient`. The worker therefore releases the turn as not-invoked (prompt preserved, back to pending). Legacy keeps `transient`. Tests: `test_R5_refused_managed_write_is_not_submitted_at_driver`, `…_returns_turn_to_pending`.
- **MINOR-2 (surviving mutants killed):** (M1) a dead session with a pending late handoff is NOT quiescent, at session or driver level, so the reconciler cannot fail the row in that window (`test_R5_M1_…`). (M3) Starved loop: the managed step was dequeued ahead of the abandon step and stalls past the deadline + abandon wait. The caller then raises `RecoveryRequiredError` and must not attest not-submitted, because the prompt IS written once the loop resumes (`test_R5_M3_…`). The abandon wait is now `_SDKSession._abandon_wait_sec` (default 5 s; tests shorten it).
- **Mutation verification** (scratch worktree with an autouse guard making `_SDKSession.start` / `ClaudeSDKClient.connect` raise — no real CLI was started): the baseline passes (105). Mutant M1 `return not self._late_handoffs`→`return True` → M1 test fails. Mutant M3 `abandoned.wait(...) and pending is None`→`pending is None` → M3 test fails. Mutant "refused write → transient" → both MINOR-1 tests fail.
- **MINOR-3** (the `_turn_owner` clear in `forget_managed_turn` is redundant, because the owner is also cleared on the next dispatch) is left untested by design.
- **Verification:** turn-queue (10 files) 137 passed; driver 121; carrier/legacy 122; session/case/control 96; interface 139; legacy-helper/process users 255. Still red (Stages 4/6/7, unchanged): api 2, pressure 3, producers 7.

### Stage 4a — admission service, fair scheduler, producer 1 (2026-09-25, commits `1a58247`..`163cd6b` + this record)

**Built.**
- **Admission (DB, one bounded txn)** — `MeshDB.enqueue_turn` (db.py:2413). It runs under `_managed_write` (db.py:1445): one 5 s monotonic deadline covers the in-process lock and the SQLite lock (busy_timeout = remaining time, a single BEGIN, no legacy 4×15 s retry). Exhaustion ⇒ typed 503, and busy_timeout is restored afterwards. Steps in order:
  1. Idempotency by (scope, key). A missing hash is derived with `_canonical_admission_hash`, so the same key with a different body ⇒ 409.
  2. Active coalesce, for internal producers only. A human turn with a coalesce key ⇒ 422.
  3. With `require_enrolled`: the session must exist (404), carry the durable `turn_queue_enrolled` marker (409), and not be closed (409).
  4. Capacity counted inside the txn: fleet managed queued+pending + `external_waiting` (legacy occupancy) against `max_queue_size`; per-session queued+pending < 20; stored intent ≤ 2 MiB/row (413) and ≤ 100 MiB fleet (429). Bytes are persisted in the new `intent_bytes` column (migration 35, db.py:7639).
  5. Sequence + insert. The `TurnAdmission` acknowledgement (a str subclass equal to the id, turn_queue.py) is constructed only after COMMIT.

  `revise_turn` re-accounts `intent_bytes` and holds both caps. Open-subset aggregates are pinned `INDEXED BY idx_mesh_turns_session_open`.
- **Service** — `turn_admission.admit_turn` (turn_admission.py:148):
  - a process-wide `threading.BoundedSemaphore(4)`, taken and released inside the worker thread; excess ⇒ immediate 429 with `retry_after`.
  - `SharedWaitingAllowance` (:41): one legacy+managed allowance. A managed admission reserves a slot (visible to legacy puts) and passes the legacy depth into the txn. `SessionTaskQueue.full/put_nowait` counts managed rows plus reservations under the same arithmetic-only lock. Unshared ⇒ plain `asyncio.Queue`.
- **Pre-parse gate** — `/api/instructions` gets a streamed 2 MiB cap and a 5 s body-read deadline (`_preparse_byte_guard`, control_api.py:193; `BodyCapMiddleware(read_deadline_sec)`). This turns LOAD02 green.
- **Fair scheduler** — `turn_scheduler.py`.
  - `select_eligible_turn_heads` (db.py:2712) applies every filter BEFORE `LIMIT 25`: head (no earlier open row), no slot holder, `not_before`, enrolled/not paused/not closed. It is ordered by `created_at, id` and returns summaries only.
  - Preparation (`_prepare_managed_turn`, orchestrator.py:9671) runs outside any txn. It rebuilds the task from the current revision's intent, injects restart/compact context once, and builds the carrier payload with the same `_mesh_dispatch_payload` (extracted pure helper, :9745) as the legacy shadow-write.
  - `activate_prepared_turn` (db.py:2756) rechecks revision, `config_revision`, head/slot and session state, then freezes payload + `machine_id` and sets `pending`. A stale revision ⇒ one re-prepare. Oversize or a prepare failure ⇒ row stays queued with a bounded `blocked_reason`.
  - Expired non-human intent is withdrawn; humans never expire.
  - `TurnScheduler` (:163) is one loop. `notify_turn_queue_changed()` is a coalesced, thread-safe hint fired on admission, `/claim-managed`, `/result-managed`, `/quiescence` and operator resolve-recovery. The 3 s fallback runs only while queued rows exist; otherwise it sleeps until a hint. After activation nothing is held per row. Started and stopped by the orchestrator (`_start_turn_scheduler`, :9723) when a mesh DB exists.
  - Carrier assignment: a pinned session keeps its node. An unpinned session → this host (never left claimable by an accept-unpinned remote node).
- **Producer 1** — `_enqueue_task` (orchestrator.py:5035) branches only on the durable marker (`_session_turn_queue_enrolled`, :9532; mesh-off ⇒ legacy; unreadable ⇒ 503 fail-closed). This happens after the local-execution and harness gates and before any legacy side effect.
  - `_admit_managed_session_turn` (:9553):
    - durable replay probe BEFORE lineage (no double flow);
    - `_record_flow_run_start` exactly once (join/attach/birth), with Case → `flow_run_id`;
    - accepted events/telemetry and the scheduler hint only after commit;
    - no BUSY / last_task_id / last_user_message / native-id write.
  - Web (`_submit_managed_instruction`, control_api.py:167): `Idempotency-Key` = operation id; the 200 `{ok, task_id, session}` envelope is preserved; typed errors map via `_turn_queue_http` (429 + Retry-After).
  - Telegram session text: an enrolled reply is "📥 Queued #n"; a refusal says nothing was queued and the BUSY write is reverted.
  - Unconverted producers (`manager_continuation`, `watched_job`, `cache_heartbeat`, `manager_respawn`, `manager_*_resume`, `manager_invoke`, …) and file ingestion (web upload, Telegram document, `staged_file`) FAIL CLOSED (422 `managed_unsupported`) for an enrolled session. They never fall back to legacy.

**Producer 1 trace.** web `POST /api/instructions` / Telegram session text / runtime `submit_instruction(session_id)` → durable trigger identity `(principal:session:instruction, operation id)`. Web: `Idempotency-Key`. Telegram: none (see below). Runtime: the caller's `operation_id`, else `task:<id>`. → turn id = the `task_…` id (`mesh_tasks` protocol 1, `queued`) → scheduler `pending` (frozen payload, `machine_id`) → carrier `/claim-managed` → `/start-managed` → `/result-managed` → `complete_turn` (terminal + native id + active identity, Stage 3) → scheduler hint → next head. Tested end to end with the real `_prepare_managed_turn` (P1-07).

**Tests (all offline; an autouse guard makes `_SDKSession.start` / `ClaudeSDKClient.connect` / `create_subprocess_exec` raise).**
- New: `tests/test_turn_queue_admission.py` (ADM01-10, 17), `tests/test_turn_queue_scheduler.py` (SCH01-08, 13), `tests/test_turn_queue_producer1.py` (P1-01..10, 24).
- Turned green: SYS01, SYS08, LOAD01b, LOAD02, LOAD04. LOAD01 and LOAD03 were vacuously green before via TypeError and now pass on the real contract.
- Still red: SYS03-07 (producers 3/5/6/7 + finalizer reconcile → Stage 4b-4h), `test_turn_queue_api` ×2 (new `/turn-requests` route + compat-route 503 on a generic failure → Stage 6).
- Counts: turn-queue files 201 passed / 7 red (above); named regression group 303 passed; adjacent case/control/flow/task-server/watched-jobs/queue suites 262 passed.

**Mutation check.** Scratch worktree under the scratchpad, removed with plain `git worktree remove`; spawn guard active. Killed:
- per-session cap (ADM02, LOAD01)
- fleet cap (ADM03)
- idempotency conflict (ADM01/01b)
- COMMIT swallowed (ADM06)
- legacy side ignores managed (ADM04b)
- txn ignores legacy (ADM04e, added after it survived)
- head rule (SCH01/SCH03)
- session filters moved after LIMIT (SCH02b, added after it survived)
- enrollment branch ignored (P1 ×12)

Equivalent, survives: dropping the legacy term from `reserve()` alone. The txn check (ADM04e) is authoritative; the reserve term is only early rejection.

**Service boundary (§7).**
| Item | Admission | Scheduler |
|---|---|---|
| Concurrency | 4 process-wide permits (thread-held; cancel-safe, ADM09/09b); DB writes serialized by `_managed_write` | one loop; ≤25 activations/pass, one per session, small txns; no per-row task (SCH07) |
| Memory | ≤2 MiB/row, ≤100 MiB fleet persisted bytes, ≤50 waiting rows shared with legacy; compat body ≤2 MiB pre-parse | head query returns summaries only; one full row at a time; waiting set bounded by the caps |
| Request size | 2 MiB streamed cap on `/api/instructions` (chunked incl.); 16 KiB on resolve-recovery; strict `AdmissionRequest` | n/a (internal) |
| Timeout | 5 s body read (408); 5 s total lock+DB deadline ⇒ 503 (ADM08, LOAD03) | same 5 s activation txn deadline; prepare runs outside txns |
| Malformed input | 422 (no body/session, non-JSON payload, human coalesce); 409 idempotency mismatch | prepare failure ⇒ queued + `blocked_reason` (SCH05d) |
| Backing failure | DB error / failed COMMIT ⇒ 503, no ack, no event (ADM06); unreadable marker ⇒ 503, no legacy fallback (P1-06) | pass failure logged (rate-limited) and retried on the 3 s clock; rows stay authoritative |

**Deferrals / residuals (for CONTEXT.md).**
1. Telegram has no stable inbound id in these paths (no `update_id` use), so Telegram retries are not deduplicated (key = `task:<id>`). Plumb `update.update_id` in a later producer/surface stage.
2. The shared-allowance cache is cold (0) until the scheduler's first pass at boot. During that window a legacy put could exceed the shared cap by the pre-restart managed backlog. The managed side is exact (the txn counts DB rows).
3. The 4-permit admission bound covers managed admission only. Legacy `/api/instructions` concurrency is unchanged. RSS at 100 concurrent callers has not been measured (Stage 7 load gate).
4. Queue-mutation deadlines apply to enqueue/activation only. Stage-2 `revise_turn` / `withdraw_turn` still use the legacy `_write` (Stage 6 surfaces).
5. Enrollment itself (quiescence / no-legacy-work checks, capability refusal) is not implemented. `enroll_session` is only the marker (Stage 7 rollout).
6. `compact_session` and other direct execution paths are not guarded yet (producer 2+). Only the `submit_instruction` lane and uploads fail closed.
7. After a managed turn completes, session BUSY/IDLE display is not driven by the queue (Stage 6 UI truth).
8. *(Rewritten by the Stage 4a rework, findings 3/4.)* There is no carrier inside the gateway process. A managed row executes only on a Stage-3 managed carrier (`WORKER_MANAGED_TURNS`) whose node id is the row's assignment:
   - The assignment is resolved from the node registration persisted in `nodes.managed_backends`: a session pinned to another node keeps its pin; an unpinned or host-pinned session goes to `MESH_LOCAL_CARRIER_NODE_ID`. A hostname is used only if a carrier registered under exactly that id.
   - Admission refuses with 503 `carrier_unavailable` when nobody can claim.
   - `claim_turn` enforces the assignment inside the CAS.
   - Operator action before enrollment: set `MESH_LOCAL_CARRIER_NODE_ID` to the local daemon's `WORKER_NODE_ID`.

### Stage 4a rework — A87 adversarial review closed (2026-09-25, commits `90564be`..`857a03a` + this record)

| Finding | Fix | Test (mutation killed) |
|---|---|---|
| **M1** blocked heads starve LIMIT 25 | Migration 35 (unreleased) adds `blocked_until` and `blocked_attempts`. A prepare failure or oversize refusal sets an exponential capped backoff: 3 s·2^(n-1), at most 300 s (`_apply_turn_block` / `mark_turn_blocked`). The head query skips a head until its backoff expires; an edit re-arms it. The WARNING is logged only when the reason changes. | SCH09, probe `test_blocked_heads_starve_other_sessions`, SCH09b, SCH09c (no filter → 3 fail; no re-arm → SCH09b) |
| **M2** legacy regression on marker read | Process-level presence flag `MeshDB.any_session_enrolled()`. It is loaded at start from `SELECT … LIMIT 1`, raised before an enrollment commits, and refreshed (cleared when no session is enrolled) each scheduler pass. A generation guard means a refresh never lowers it across a concurrent enroll. While no session is enrolled, the legacy path does no marker read and behaves exactly as main. Otherwise there is one read per request, offloaded; the web route passes its decision on (`turn_queue_enrolled`). An unreadable marker fails closed only when enrollment can exist. | P1-11 (probe L1), P1-11b (L2), P1-11c (exactly one read per request), P1-11d (presence ignored → P1-11/11b fail) |
| **M3** claim ignores assignment | `claim_turn` refuses (409) unless `row.machine_id == node_id`: once as a pre-check and again as an `AND machine_id = ?` predicate in the CAS. An unassigned managed row is claimable by nobody. Stage-2 `activate_turn` now assigns the session pin to an unassigned row (activation = carrier assignment), so no Stage 2/3 fixture changed. | probe `test_claim_ignores_machine_assignment` (the pre-check alone is backed up by the SQL predicate; removing both → fails) |
| **M4** hostname assignment | `_managed_carrier_assignment` resolves the pin, else `MESH_LOCAL_CARRIER_NODE_ID` (new config). The target must have REGISTERED the backend as managed-capable; registration is persisted in `nodes.managed_backends` so an out-of-process task server works. Otherwise typed `CarrierUnavailableError` 503 before any side effect. It is re-resolved at activation; a carrier that disappeared makes the head back off. | P1-07b, P1-07c, P1-07d, P1-07e (hostname fallback / no registry check → P1-07c, P1-07e fail) |
| **m5/m6** phantom / double lineage | Admission is committed first, with a 30 s lineage hold (`not_before`). Only the request whose insert won writes lineage (`_record_flow_run_start`). `finalize_turn_lineage` then attaches Case + lineage metadata and releases the hold. A refusal or a replay writes no lineage. A crash between the two only delays activation by ≤30 s, and that turn then lacks its Case link (best-effort, as in legacy). | P1-12 (probe L3), P1-12b (L4), P1-12c, P1-04 (lineage on replay → P1-04/P1-12 fail) |
| **m7** 408 untested | Test through the real app (`build_control_api`) with a stalled chunked body. | P1-13b (deadline disabled → fails) |
| **m8** 2 MiB refuses valid prompts | The compat cap is derived as 12 B/char (escaped surrogate pair) × (262144 + 48000) + 256 KiB envelope ≈ 3.8 MiB. **This deviates from design §8's 2 MiB**, which would refuse valid escaped non-BMP prompts. A 256 KiB rule is pre-registered for `/api/sessions/{id}/turn-requests`. | P1-13 (2 MiB cap → fails) |
| **m9** raw `QueueFull` | `SessionTaskQueue.put` (shared) retries `put_nowait` until the caller's `wait_for` expires, so the orchestrator raises `RuntimeError("Task queue is full")` as on main. Unshared ⇒ `super().put`. | ADM04f (override removed → fails), ADM04g |
| **m10** steady 3 s poll | Next wake comes from `_next_timeout`. It is immediate if LIMIT was hit, and otherwise the earliest of: (a) a head's due `not_before` / `blocked_until`; (b) for heads waiting on their own slot holder, a backoff 3→30 s, reset by any hint or activation; (c) a 60 s lost-hint safety net. With no queued rows it waits for a hint only. | SCH07 (≤6 passes in 1.6 s; steady poll → fails), SCH10 |

**Why (b) exists.** In the default deployment (`MESH_EMBEDDED_SERVER=false`) `/result-managed` commits in the task-server process, so its hint cannot reach the gateway scheduler. The next head therefore activates within ≤30 s of a completion (≈3 s right after activity). A cross-process wake signal is deferred to the A84 completion-delivery work.

**Adopted probes.**
- `tests/test_turn_queue_4a_probes.py` is the reviewer DB file adopted verbatim, except the claim probe now asserts refusal + the correct claim and the plan probe asserts index use.
- The legacy probes are P1-11/11b/12/12b; the 408 probe is P1-13b.

**Mutation run.** Scratch worktree, removed with plain `git worktree remove`; spawn guard on. All 11 new guards were killed, as listed in the table.

**Verification.**
- turn-queue (15 files): 228 passed / 7 red. The reds are unchanged: SYS03-07 are Stage 4b+, and api ×2 are Stage 6.
- Named regression group: 303 passed. Adjacent group: 262 passed. node/registry/settings users: 255 passed.

**Residuals.** Stage 4a residuals 1-7 stand (2 = cold allowance cache). New:
- (9) cross-process completion wake is bounded by the slot backoff, not immediate;
- (10) the node registration column is persisted at register time only, so a carrier that registered before this migration must re-register before its sessions can be admitted (fail closed);
- (11) ~~the lineage hold (30 s) is a bounded crash window without Case lineage.~~ **Wrong; superseded by rework 2 (B1):** it was permanent Case-lineage loss. Fixed.

### Stage 4a rework 2 — A87 round-2 review closed (2026-09-25, commits `5e987be`, `ccf41af` + this record)

| Finding | Fix | Test (mutation killed) |
|---|---|---|
| **B1** crash after commit ⇒ permanent Case-lineage loss; replay acked without lineage | **Durable "lineage pending" state.** Migration 35 (unreleased) adds `lineage_state` / `lineage_token` / `lineage_lease_until`. Producer 1 inserts the row as `pending` under the admitting request's writer token and a 30 s lease. The replaced `not_before` hold is gone. **Never activatable:** head selection excludes the state, and both `activate_prepared_turn` and Stage-2 `activate_turn` refuse it inside the transaction. **Finalize** (`finalize_turn_lineage`) is a CAS on `status='queued' AND lineage_state='pending' AND lineage_token=?`, and its result is handled. A lost lease raises a typed 503; if another writer already finished, the result counts as done. **Recovery:** each scheduler pass takes lineage-pending rows whose lease expired (`list_lineage_recovery`), CAS-claims them (`claim_turn_lineage`), rebuilds the task from the persisted intent (`__join_case_id` / lineage keys), writes lineage and finalizes. That happens before head selection, so a recovered row activates in the same pass. **Crashed-writer reuse:** partial lineage from the crashed writer is reused (`existing_task_lineage`: an own flow_runs row, or a Case membership link), so no second Case is created. **Replay:** a replay that finds a lineage-pending row waits ≤5 s for the live writer, runs the recovery itself once the lease has expired, and otherwise refuses with 503. It never acks without lineage. Finalize-after-activation is impossible by construction: activation requires not-pending, finalize requires pending + queued. | R1 (probe; 30 s not-activatable → replay 503 → lease expiry → recovered + activated with Case link), R1b, R1c, R2 (probe inverted), R2b, B. Mutations: head filter + in-txn guard removed → R1; replay acks without lineage → R1/R1b; recovery off → R1/R1c/R2; no reuse of prior lineage → R1c; finalize without `status='queued'` → B; finalize result ignored → R2b |
| **M1** offline carrier counted as live | `node_managed_backends` requires `status='online'` and a heartbeat within `mesh.node_heartbeat_timeout_sec`. **Admission** ⇒ 503 `carrier_unavailable`. **Activation** ⇒ back-off with a visible reason (`prepare_failed: CarrierUnavailableError: … '<node>' …`). **Pending on a carrier that goes offline:** each pass, `requeue_turns_on_dead_carriers` returns an UNCLAIMED pending row on an offline/stale/unknown carrier to `queued`, with `blocked_reason='carrier_offline: <node>'` and the backoff. The slot is freed. The row re-activates when a live carrier exists again; a pinned session is never relocated. `claim_turn` clears the reason. **Exit:** carrier returns ⇒ re-activation + claim. Otherwise the row stays queued and visible, with backoff ≤300 s; the operator can withdraw it (queued ⇒ `withdraw_turn`; Stage-6 route). Claimed/running rows stay with the Stage-3 recovery machinery. | R5 (probe inverted), R5b, R5c, R5d. Mutations: no freshness filter → R5/R5b/R5c; no requeue → R5d |
| **M2** refresh lowers the flag across an in-flight enroll | `enroll_session` raises the flag and bumps a generation before the write, and counts enrolls in flight. After the commit (in `finally`) it bumps the generation again and sets the flag True. `refresh_enrollment_presence` lowers the flag only if the generation is unchanged since its read began AND no enroll is in flight. | R3 (probe interleaving, deterministic; asserts the flag inside the pre-commit window), R3b (enroll commits after the refresh read). Mutation A (guard → always assign) → R3/R3b |

**Test-fixture note.** `tests/test_turn_queue_scheduler.py` and the adopted `tests/test_turn_queue_4a_probes.py` `_session` helpers now register the assigned carrier as a live node. A pending row on an unknown carrier is now correctly requeued, and no assertion was weakened. The `P1-12c` hold test was replaced by R1/R1c.

**Verification.**
- turn-queue (16 files): 239 passed / 7 red. The reds are unchanged: SYS03-07 are Stage 4b+, and api ×2 are Stage 6.
- Regression group: 303 passed. Adjacent group: 262 passed. heartbeat/mesh-health/node-inspector/self-awareness: 34 passed.
- Mutation run in a scratch worktree, removed; spawn guard on. All 9 guards listed above were killed. `finalize_result_ignored` first survived, and R2b was added to kill it.

**Residuals carried to CONTEXT.md (A87).**
- **M3 cross-process enrollment (Stage-7 precondition).** The presence flag is per `MeshDB` instance. It is refreshed at gateway start, on each scheduler pass, and set by `enroll_session` in THIS process. An idle scheduler (no queued rows) sleeps until a hint and does not refresh. Therefore **enrollment must be performed inside the gateway process** (the future enrollment route/API), never by a script or another process writing `sessions.turn_queue_enrolled` directly. The alternative is that the gateway re-reads the marker before trusting `False`. Until then, an out-of-process enroll is invisible to the gateway and its sessions take the legacy path (R4 pins this).
- **m2.** Carrier assignment (`node_managed_backends`, one indexed PK read) runs synchronously on the event loop during enrolled admission and in `_prepare_managed_turn`. It is bounded, but it is not offloaded.
- **m3.** A shared-allowance blocking `put` polls every 50 ms until the caller's 5 s `wait_for`. That adds up to 50 ms of latency when room frees on another thread.
- **m4.** The compat cap deviation (≈3.8 MiB derived, vs design 2 MiB) applies to ALL `/api/instructions` callers, unenrolled included. Pathological `target_files` / `upload_attachment` / other unbounded fields that push a request past it are now 413 where main would have parsed them.
- Rework residual 9 stands: a cross-process completion wakes the scheduler via the ≤30 s slot backoff. Rework residual 10 stands: carriers must re-register after migration 35.
- **New:** lineage recovery re-runs `_record_flow_run_start` for join/attach when the crashed writer wrote nothing. Links are idempotent (`INSERT OR IGNORE`), but a `task.attached` flow event can be duplicated if the crash fell between the event and the link. That is an audit row only.
- **New:** `_record_flow_run_start` still swallows its own DB errors (legacy best-effort). A lineage DB failure, as opposed to a crash, finalizes with whatever lineage was written.

### Stage 4a rework 3 — A87 round-3 review closed (2026-09-25, commits `ae90528`..`5ebe54a` + this record)

**Root cause addressed.** The managed lineage was a multi-step, non-transactional sequence: its steps could fail silently, recovery treated a partial result as complete, and it was not fenced by the lease. It is now **one convergent, idempotent, raising procedure keyed on the task id**, `orchestrator._managed_lineage_converge`. The live writer and recovery run exactly this procedure; recovery is a re-run, and there is no "treat partial as done" branch.

**Write inventory.** This is the diff against the live legacy `_record_flow_run_start`; the parity test below enforces it.

| Branch | Writes (all get-or-create / at-most-once, all RAISE on DB error) |
|---|---|
| flag OFF | `flow_runs` dispatch-start row for the task (`get_or_create_task_flow_run`) |
| (J) join, open Case | task link `task` (created_by=manager) · `task.attached` {membership: worker} · affiliation `current_case_id/case_role=worker` (`db.set_session_case`) · session link `worker` (created_by=manager) |
| (B) attach to the session's open Case | task link `task` (created_by=system) · `task.attached` · affiliation (role resolved from the session link, no-op if already current) |
| (C) standalone | nothing |
| (A) birth | `flow_runs` keyed on the task (lineage columns parent_flow_run_id / dispatched_by / dispatch_file / completion_criteria / objective_lock) · `flow.created` · `root_task` link · session `worker` link · `session.attached` {role: worker} · affiliation worker · parent `child_flow` link · parent `task.dispatched` |
| Harness gate outcome | Runs before admission; a refusal inserts no row and writes no lineage, so there is nothing to persist. |

**Mechanics.**
- **Flow runs:** `MeshDB.get_or_create_task_flow_run` does SELECT-by-task_id + INSERT in one write transaction, so two writers converge on the same Case.
- **Events:** `MeshDB.append_flow_event_once` does an existence check + insert on (flow, type, entity_type, entity_id) in one transaction.
- **Links:** these were already unique-keyed.
- **Strict mode:** `_record_flow_link` / `_record_flow_event` take kw-only `strict` / `once`. Their defaults are unchanged, so legacy is byte-identical.
- **Fixed decision:** the durable decision an earlier writer took (its own flow_run, or a membership link) is reused, so a Case closing in between cannot re-route a half-written lineage.
- **Fencing:** `fence()` checks lease ownership and withdrawal after the decision read and before each write group. `finalize_turn_lineage` stays the CAS. A stalled writer's late steps are no-ops on already-created objects.
- **Failure handling:** any error becomes a typed 503 and the row stays `pending`; recovery re-runs after the lease expires. The procedure runs in a worker thread, off the event loop.
- **Withdraw (m1):** `withdraw_turn` sets `lineage_state='void'` on a pending lineage. The writer's fence (or finalize) then reports **withdrawn**: the admitting call returns `TurnAdmission(status='withdrawn')`, replays report withdrawn, and the recovery list skips the row. Telegram answers "Withdrawn".
- **Idle dead-carrier wake (M3):** with no queued rows but managed `pending` rows present, the scheduler keeps the bounded safety-net wake, so `requeue_turns_on_dead_carriers` runs even when the fleet is otherwise idle.
- **Read-first requeue (m2):** the requeue does a read-only existence check first and takes BEGIN IMMEDIATE only when a candidate exists.

**Tests.** `tests/test_turn_queue_4a_r3.py` contains the 8 A87 probes, adapted to assert correct behavior:
- 1a join and 1a birth: raise 503 → not activatable → recovered and activated with the link. The birth probe now injects into `get_or_create_task_flow_run`, the managed birth.
- 1b: recovered equals live, for both join and birth.
- 1c: exactly 1 flow_run and 1 child_flow link.
- 1d: withdrawn / withdrawn / void / no link.
- 2: payload metadata survives requeue.
- 2b: real `TurnScheduler.run` with an idle fleet → requeued.

Plus these additions:
- requeue never touches claimed/running rows;
- requeue on a stale heartbeat while status is still `online`;
- requeue reads before taking the write lock;
- **legacy-parity** tests (join / attach / birth), comparing links, events and affiliation;
- a full re-run after a crash before finalize writes each event once;
- a strict link failure raises and the row stays pending;
- recovery reuses the durable decision.

R1/R1b (rework 2) now inject the crash into `_managed_lineage_converge`, since the managed path no longer calls `_record_flow_run_start`.

**Mutation run.** Scratch worktree, removed; spawn guard on. All killed:

| Mutant | Failing test |
|---|---|
| birth not get-or-create | R1c |
| event not once | full re-run |
| strict swallowed | strict-link |
| join open-check swallowed | 1a join |
| fence removed | 1d |
| withdraw doesn't void | 1d, SCH08 |
| no idle pending wake | 2b |
| requeue touches claimed/running | 5 tests |
| heartbeat staleness ignored | stale-heartbeat, probe 2 |
| read-first removed | read-first |
| no decision reuse | decision-reuse |

The first run left 4 survivors (event-once, strict, decision-reuse, requeue-touches-claimed). I added the tests above until all were killed.

**Verification.**
- turn-queue (17 files): 257 passed / 7 red. The reds are unchanged: SYS03-07 are Stage 4b+, and api ×2 are Stage 6.
- Regression group: 303 passed. Adjacent group: 262 passed.

**Carried (for CONTEXT.md).**
- **m3:** `node_heartbeat_timeout_sec` (default 90 s) must be at least 2× the worker heartbeat interval (30 s). A smaller value marks live carriers dead and requeues their unclaimed rows repeatedly. It is fail-safe (queued, visible, not lost) but noisy. It is documented rather than clamped.
- **m4:** after a gateway restart, a requeued row's backoff can grow to about 48 s (3→6→12→24 s accumulations) before it re-activates.
- **Residual:** a withdrawal that lands AFTER the procedure has written a link (between write groups) leaves that link on a withdrawn turn. The fence stops further writes and reports withdrawn, but written links are not deleted.
- **Stands from rework 2:** M3 enrollment must happen inside the gateway process, and m4 compat cap. m2 (the sync carrier read) is now partly addressed because lineage runs in a thread; the carrier-assignment read in admission/prepare is still inline.

### Stage 4a rework 4 — A87 round-4 review closed (2026-09-25, commit `9d45a37` + this record)

- **MAJOR 1: recovery re-affiliated a session to a CLOSED Case.**
  - **Fix.** When `_managed_lineage_converge` reuses a prior decision, it now checks whether that Case has closed since. This covers both the membership case and the own-flow/birth case. If it has closed, recovery finalizes with that case_id (the link already exists, reproducing "legacy attached, then close cleared it") and writes nothing more: no `affiliate()`, no late `task.attached` / `session.attached`, no session link. The fresh-decision branches were checked too. J re-checks the Case is open, and B uses `find_open_case_for_session`, so they cannot pick a closed Case.
  - **Test correction.** My own `test_recovery_reuses_the_durable_decision` closed the Case with raw SQL, skipping the affiliation clear, and so codified the bug. It now closes via the real `o.close_case` and asserts no re-affiliation and `flow.closed` as the last event.
  - **Probe P2** is adopted as `test_P2_recovery_after_real_close_does_not_reaffiliate`.
  - **Mutation:** dropping the closed check → both tests fail.
- **MINOR 2.** Added join and attach variants of the full-re-run event-once test. The mutation `task.attached once=True→False` is now killed (join, attach, P2).
- **MINOR 3 (carried): the fence is advisory between write groups.** A stalled writer holds `_write_lock` inside a single DB write, so a recovery claim, which needs the same lock, times out at its 5 s deadline instead of interleaving. Across write groups the fence is re-checked, and every step is get-or-create, so a stale writer's late steps converge on the same objects. What it can still do after losing the lease is complete writes that recovery would have made anyway. It cannot finalize (CAS).
- **CARRY — HARD Stage-6 PRECONDITION: phantom child Case on withdraw.**
  - **The defect.** A withdraw landing after a partial or full BIRTH leaves a phantom child Case:
    - the open child `flow_run` blocks the parent's `close_case` (`CaseCloseBlocked`);
    - the session stays affiliated to the phantom (`find_open_case_for_session` returns it);
    - the phantom `task.dispatched` inflates the advancement-gate count (probe P3).
  - **Why it is not a 4a defect.** It is unreachable in 4a. The only withdraw caller is scheduler expiry, and heads exclude lineage-pending rows.
  - **The precondition.** Any Stage-6 withdraw of a lineage-pending or partially-lineaged row must void/close the child flow_run and clear the affiliation, with a test. A87 carries this to CONTEXT.md.
- **Verification.**
  - turn-queue files: 260 passed / 7 red. The reds are unchanged: SYS03-07 are Stage 4b+, and api ×2 are Stage 6.
  - 419 regression group (the 303 group + `test_case_closure`, `test_case_interrupt`, `test_mcp_manager`): 419 passed.
  - Mutations were run in a scratch worktree, removed.

### Stage 4b — producer 2: compaction + operator cancel/stop + session close (2026-09-26, commits `fc207b3`..`fdc4365` + this record)

**Scope.** For ENROLLED sessions only, compaction and ordinary active cancellation/close now go through the managed ledger/ownership model. Unenrolled sessions take the unchanged legacy branch, and while nothing is enrolled no new DB read happens (test C07 traces the statements).

**Built.**
- **Fenced operator cancel.** `MeshDB.request_turn_cancel` (db.py:3262) is one `_managed_write` txn:
  - `pending` (unclaimed) or `claimed` never started → terminal `cancelled` right there. The old token's `/start-managed` then gets a 409 and the carrier drops the attempt.
  - `running` / `recovery_required` → `cancel_token := claim_token` plus ONE protocol-0 `cancel_managed` control row, pinned to `claimed_by`, keyed `cancelm-<task>-<sha256(token)[:12]>`. `INSERT OR IGNORE` makes a repeat converge. The payload carries no token.
  - `queued` → `not_active`; terminal → `already_terminal`.
  - The attempt's own exit then commits truthfully (`_cancel_requested_for`, db.py:8546, token-equal):
    - `complete_turn` failed → `cancelled`; a success that beat the interrupt stays `completed`;
    - `resolve_recovery` failed → `cancelled`;
    - `release_turn` of a cancelled attempt → `cancelled`, never re-offered.
- **Entry points.**
  - `cancel_task` hook (orchestrator.py:8481 → `_cancel_managed_turn_if_managed`, :10024). It keeps the bool contract: a ledger failure returns False. `interrupt_case` and the Case sweep go through it too.
  - `stop_managed_session_turn` (:10048) cancels the turn that OWNS the active slot (`get_active_turn`), never `last_task_id`. It does no whole-session CANCELLED save.
  - Callers: web `POST /api/sessions/{id}/stop`, Telegram `/session_cancel`, and session-scoped `/cancel` (`_managed_session_cancel_reply`). An explicit task id goes through `cancel_task`.
- **Carrier delivery.**
  - `WorkerAgent._handle_cancel_managed` (agent.py:2790) runs outside the turn slot (control semaphore) and is exempt from the capacity gate.
  - It finds the attempt's `turn_uuid` in the durable claim record and calls the NEW interface `CodingBackend.cancel_managed_turn(session, turn_uuid)` (interfaces.py:388; default False; no backend-name branching).
  - Claude: `_SDKSession.cancel_managed_turn` (claude_driver.py:1505) decides on the SDK loop. It interrupts only if that uuid's echo began the CLI's current turn. If the prompt is written but not yet echoed, it arms `cancel_requested`, and the interrupt fires on that echo. It never interrupts a foreign or autonomous turn.
  - `task_server` treats `cancel_managed` as a control row (no session turn event).
- **What "stop" means (vs Stage 6 pause).**
  - Stop = cancel ONE active attempt (terminal `cancelled`). Queued turns behind it are untouched, and the scheduler activates the next head normally.
  - This deviates from design §7 ("queued work stays paused until explicit resume") until Stage 6 adds persistent `turn_queue_paused` pause/resume.
  - Cancelling a queued turn by explicit id is refused (`not_active`); withdraw is a Stage-6 surface.
- **Compaction = a managed turn.**
  - `compact_session` (enrolled) → `_admit_managed_compaction` (:9955): `turn_kind='compaction'`, `action='compact_session'`, body `/compact`, `turn_source='operator'`, no Case lineage.
  - Serialized by the active slot: never concurrent with a managed turn, and never interrupting one.
  - Preparation keeps the bare command (no restart/compact context injection) and sets action `compact_session`.
  - The carrier dispatches it only through the NEW `CodingBackend.run_managed_compaction` (interfaces.py:372). The default raises `ManagedUnsupportedError`; Codex/OpenCode keep the default (K06).
  - **Why a special driver path.** The bundled CLI 2.1.191 source (`_bundled/claude`, the query engine's `!shouldQuery` branch) yields `init` / local-command stdout / `compact_boundary` / `result` for a local slash command and NEVER echoes the caller uuid, so echo correlation would wedge `/compact` into recovery.
  - `ClaudeSDKClientDriver.run_managed_compaction` (claude_driver.py:1976) runs it on a process that never received a query (`_ever_submitted`):
    - a used pooled process is retired only if quiescent, decided on its loop in the same step as marking it closed (`retire_if_quiescent`, :1556), with no interrupt;
    - otherwise it returns `managed_conflict`, meaning released not-invoked;
    - the fresh process resumes the session's native id (`resume_if_new`);
    - `send_managed(local_command=True)` owns the first turn by construction and refuses on a used process.
  - Web route: an `Idempotency-Key` becomes the operation id. The enrolled envelope adds `queued/task_id/status`; the unenrolled envelope is byte-identical. Telegram replies "📥 Compaction queued as turn #n".
- **Session close.**
  - `SessionService(managed_close=…)` → `_close_managed_session` (:10069). None ⇒ legacy.
  - One txn (`close_session_turns`, db.py:3368): the session is `closed` and every queued row is `withdrawn`, with a revision audit (`session_close:<actor>`).
  - `lineage_state` `pending|done` → `void`. A pending writer keeps its lease expiry.
  - Then the void procedure runs, then `request_turn_cancel(active)`, then the existing `close_session` teardown row pinned to the carrier that owns the process (active `claimed_by`, else `_managed_carrier_node`). The gateway-local `backend.close` is skipped.
  - The native id is cleared as in legacy. `_commit_completion_identity` no longer writes `backend_session_id` on a closed session, so a late cancelled result cannot resurrect it.
  - A ledger failure returns `turn_queue_unavailable` (web 503, Telegram "retry"). Every step is idempotent, so a retry converges (L01c).
- **Stage-6 precondition (4a carry) implemented for close.** `_void_withdrawn_lineage` (:10114) is ONE convergent, raising procedure keyed on the turn id. The live closer and the scheduler sweep (`run_scheduler_pass(void_lineage=…)`, `list_void_lineage` served by partial index `idx_mesh_turns_lineage_void`, migration 36) run exactly this. It waits while the withdrawn writer's lease is live. For a Case BORN for the turn it:
  1. appends parent `task.dispatch_voided` (once);
  2. closes the child `cancelled` (force) via the real `close_case`;
  3. clears the affiliation strictly (`clear_session_case_if`);
  4. CAS `void` → `voided`.

  The advancement gate ignores voided dispatches. Join/attach memberships are left as written (legacy close parity).

**Producer → durable trigger identity → turn id → completion effect.**
| Producer | Durable trigger identity | Turn id | Completion effect |
|---|---|---|---|
| compaction (web `/compact`, Telegram `/compact`) | idempotency (`operator:<sid>:compaction`, `Idempotency-Key` or `compact:<turn id>`) + active coalesce `compaction:<sid>` | `compact-<sid8>-<hex12>` (protocol 1, `turn_kind=compaction`) | scheduler → carrier `run_managed_compaction` → `/result-managed` → `complete_turn` (terminal + native id) |
| operator cancel/stop (web stop, Telegram `/session_cancel` `/cancel`, `interrupt_case`) | (task id, claim token) → `cancel_token` + control row `cancelm-<task>-<hash(token)>` | the targeted active turn (no new turn) | unclaimed/unstarted: `cancelled` in the cancel txn; running/held: the attempt's result / recovery / release commits `cancelled` |
| session close (web, Telegram, Case worker close) | session id (`close_session_turns`, idempotent) | none new (withdrawn ids + active id) | queued → `withdrawn` + lineage `voided`; active → cancel row above; carrier `close_session` teardown |

**Exit table (new managed states).**
| State | Exits | Tests |
|---|---|---|
| running/recovery_required with `cancel_token` | interrupt → own result → `cancelled` (`completed` if it won the race); released not-invoked → `cancelled`; recovery resolution → `cancelled`; carrier offline → Stage-3 exits unchanged | X01, X02, C03, C03b, C04, C05 |
| `cancel_managed` control row (protocol 0) | carrier claim + handle → completed (no attempt held ⇒ no-op); carrier offline → waits like `close_session` rows; crash mid-handle → legacy reaper re-offers, idempotent | X01, X03, X05 |
| withdrawn + `lineage_state='void'` | inline void at close; sweep once the lease expires (3 s idle wake while outstanding) → `voided` | L02, L02b, L03, L03b, S01 |
| session `closed` with managed rows | queued → `withdrawn` (same txn); active → cancel path; admission/activation refuse inside the window | L01, L01b, L05 |
| compaction row | the Stage-3 exits of any managed turn; not quiescent ⇒ `managed_conflict` → released to pending | K04, K04b, K05 |

**Tests (offline; autouse guard makes `_SDKSession.start` / `ClaudeSDKClient.connect` / `create_subprocess_exec` raise).**
- New files:
  - `tests/test_turn_queue_4b.py` (28): real file-backed MeshDB, real orchestrator methods, real `SessionService.close_session`, real control API app, real scheduler / `close_case`, real Telegram handlers.
  - `tests/test_turn_queue_4b_carrier.py` (10): real task-server app, real `WorkerAgent`, the REAL Claude driver and `_SDKSession`s on a fake SDK client. Fresh processes boot through a subclass `start`; the base `start` stays guarded. Compaction E2E is gateway admission → scheduler → carrier → driver.
- Counts:
  - turn-queue files: 298 passed / 7 red. The reds are unchanged (SYS03-07, api ×2), versus the 260 baseline plus 38 new.
  - Regression group: 419 passed. `test_session_cancellation` + `test_compact_context_injection`: 12 passed.
  - Adjacent (backend_call/registry, claude_session_backend, codex_native/ownership, control_api/flows/work, flow_links/write_path, session_affiliations, task_server_client/upload, worker_pinned_only/role, warm_worker_idle_reaper, case_observable_worker_session, queue_persistence): 188 passed.
- **Mutation run** (scratch worktree `mut4b`, spawn guard on, removed with plain `git worktree remove`): 42 mutants over db/orchestrator/scheduler/service/agent/driver/routes/task_server, all KILLED.
  - The first run left 1 survivor ("cancel_task reads the ledger when nothing is enrolled"). C07 was strengthened.
  - 3 driver mutants were first killed only by the 180 s timeout (hang). The fixture now bounds the managed deadline at 3 s, so they fail fast.
  - Equivalent: dropping the token-equality inside `_cancel_requested_for`. No path re-mints a token on a row that carries `cancel_token`, because release of a cancelled attempt ends it.

**Service boundary (§7).**
| Item | web stop / Telegram cancel | web / Telegram compact | close (web / Telegram / Case) | carrier `cancel_managed` handler |
|---|---|---|---|---|
| Concurrency | sync route; one indexed read + one `_managed_write` txn (serialized, 5 s); repeat converges | admission service (4 permits, caps, coalesce) | `to_thread`; 2 txns + ≤20 voids (per-session cap) | control semaphore (1), outside turn slots; ≤1 row per (task, token) |
| Memory | O(1) | ≤2 MiB/row caps (4a) | O(withdrawn ≤ 20) | O(1) |
| Request size | no body | no body; Idempotency-Key truncated to 256 | no body | target ≤256 chars validated |
| Timeout | 5 s ledger deadline ⇒ 503 | 5 s admission ⇒ 503/429 | 5 s per txn ⇒ `turn_queue_unavailable` 503, retry converges | loop-side cancel waits ≤5 s; result post bounded by the delivery deadline |
| Malformed input | 404 unknown session; not-active ⇒ `cancelled:false` | 409 key/body mismatch; 503 no carrier | 404 unknown session | invalid target ⇒ completed with a reason |
| Backing failure | unreadable marker/ledger ⇒ 503, nothing recorded; `cancel_task` ⇒ False | 503, no row | fail closed (session not closed if the txn failed) | claim error ⇒ row stays pending (re-polled); no attempt ⇒ no-op |

**Residuals / carried (for CONTEXT.md).**
1. Stop is not a pause: queued work behind a stopped turn runs next (Stage 6 pause/resume).
2. The compaction attribution relies on the fresh-process argument. It is verified from the bundled CLI 2.1.191 source plus fake-stream tests, NOT live (no live calls authorized). Retiring the warm process drops its prompt cache (compaction resets the context anyway).
3. The enrolled compaction response means "accepted/queued", not "compacted"; the truth is the turn's terminal status. BUSY/IDLE and queue display are Stage 6.
4. `interrupt_case` / the Case sweep cancel running/pending managed Case turns through the fenced path. QUEUED managed rows of a blocked/closed Case are not withdrawn yet (Stage 4c: Case revalidation at activation).
5. The carrier teardown `close_session` row still uses the legacy swallowing `enqueue_task` (best-effort, as legacy; the ledger is authoritative). The carrier's close interrupts a still-pending held (recovery_required) prompt; that is the operator's stop, and the attempt then resolves `cancelled` via the Stage-3 reconciler.
6. Links written by a withdrawn join/attach writer stay (4a residual stands); only born child Cases are voided.

### Stage 4b rework — A87 review (1 major + 3) closed (2026-09-26, commits `874ce07`, `cadefa0` + this record)

| Finding | Fix | Test (mutation killed) |
|---|---|---|
| **MAJOR 1** cancel lost in the CLI boot window | Two layers, no await gap between them. (a) **Carrier:** `_handle_cancel_managed` records `cancel_requested` durably on an attempt this process holds BEFORE any await. A **pre-invoke check** runs in `_handle_task` (agent.py:2710), with no await between it and recording the turn uuid; it releases the attempt not-invoked, and `release_turn`'s cancelled branch makes it `cancelled`. (b) **Driver:** `ClaudeCodeBackend.cancel_managed_turn` first ARMS the uuid (`arm_managed_cancel`, claude_driver.py:355; bounded 256, uuid-keyed), then delivers to a registered prompt (and disarms). `_SDKSession._submit_turn` (claude_driver.py:1312) consumes an armed uuid in the same loop step that would register the prompt, and never submits it. The typed not-submitted conflict leads to a not-invoked release and then `cancelled`. The control row now completes "interrupt delivered or armed" or "cancel held for the attempt". | carrier R01 (adopted P1, inverted: row `cancelled`, prompt never sent, 0 interrupts), carrier R02 (cancel before the uuid exists ⇒ backend never called); mutants: driver ignores armed / backend does not arm / always disarms / no pre-invoke check / carrier does not record ⇒ all killed |
| **2** C07 traced one thread | C07 wraps `MeshDB._conn` so EVERY thread's connection is traced (adopted P2). It asserts ≥2 threads were traced, and that no enrollment/ledger/lineage/cancel SQL runs for stop, compact and close through the real web routes. | C07 |
| **3** surviving M1/M3 | kill tests | R02 (release of a cancelled attempt by another node ⇒ refused, still running), R01 (void never wipes a NEWER affiliation); both mutants killed |
| **4** stop did not hold automation | Managed stop = `request_turn_cancel(hold_session=True)`. In the SAME txn it sets session `cancelled`, the legacy stop status that the wake dispatcher, transient/quota resume, resume-mode choice and orphan sweep already honour. Activation honours it too: head selection and `count_slot_waiting_sessions` exclude `cancelled`, and `activate_prepared_turn` refuses it in-txn. The hold is released only by an operator action: a new `human`/`operator` admission sets `idle` in the admission txn (legacy parity: the next send clears CANCELLED). Automation admissions never release it. A plain `cancel_task` (Case interrupt / explicit id) does not hold. **This supersedes the 4b "stop is not a pause" residual.** | R03 (held, then released by a new instruction, FIFO resumes), R03b, R03c (each layer independently), R04 (real `_continue_case_once` + `_handle_transient_paused_case`: control wakes/delivers; after stop: 0 wakes, 0 deliveries); mutants: stop without hold, hold not written, activation / head-select ignore hold, operator never releases, automation releases ⇒ all killed |

**Mutation run** (scratch worktree `mut4br`, removed with plain `git worktree remove`; spawn guard on): 13 mutants, all killed.
- The first pass left 2 survivors: head select and activation each ignoring the hold, because each was redundant with the other. R03c now tests them separately.
- One mutant was first "killed" by an IndentationError. It was redone as a valid mutant, and R02 fails on it.

**Verification.**
- turn-queue files: 306 passed / 7 red. The reds are unchanged: SYS03-07 and api ×2.
- Regression group: 450 passed. That is the 420 group (the 419 group plus `test_session_cancellation` + `test_compact_context_injection`) plus `test_case_transient_resume` + `test_wake_dispatcher_eventdriven`.
- The reviewer's probe file now yields `ROW cancelled interrupts 0` for P1, and P2 passes.

**Residuals — carried (A87 → CONTEXT.md).**
1. The interrupt is scheduled via `ensure_future`, so it may land late. It can then only hit a CLI-autonomous turn, never a managed one: the managed owner's result was already dispatched.
2. A slow lineage writer running past its lease can birth an open child Case after the void has run (this inherits the 4a lease model).
3. 4b residuals 2-6 stand as ruled. Residual 1 is superseded: stop now holds.
4. New: the armed-cancel registry is per carrier process. A carrier restart loses armed entries, but the durable claim record's `cancel_requested` plus the server's `cancel_token` still end the attempt `cancelled` via the not-invoked release / reconciler.
5. New: a held (`cancelled`) session with queued turns stays held until the operator sends or compacts. Queue-level resume/send-next routes are Stage 6.

### Stage 4b rework 2 — A87 round-2 review (stop-hold invariant) closed (2026-09-26, commits `fd8db8a`..HEAD + this record)

**Invariant now made true.** An operator stop of an ENROLLED session holds it. Only an operator action releases the hold. Case automation neither runs turns into a held session nor replaces it.

**Durable record.** Migration 37 adds `sessions.turn_queue_hold` (`'operator_stop'` | NULL). It is written in the stop's cancel txn together with status `cancelled`, guarded by `!= 'closed'`.
- It is cleared by an operator admission. The release is `status := CASE cancelled→idle ELSE status`, so a live BUSY / AWAITING_INPUT / ERROR is never clobbered.
- `close_session_turns` also clears it: a closed session is dead and goes back to the crash path.
- `upsert_session` is `ON CONFLICT DO UPDATE` and does not list the column, so a stale whole-row save cannot erase the record.
- Head selection, `activate_prepared_turn` (in the txn) and `count_slot_waiting_sessions` honour the RECORD as well as the status.

| Finding | Fix | Test (mutation killed) |
|---|---|---|
| **1** automation released the hold (dispatch_worker was labelled human) | `POST /api/instructions` accepts `X-AI-Team-Principal: automation`. For an enrolled session that maps to source `automation_session` (principal `automation`, `turn_source='system'`), which never releases. `scripts/mcp_manager.py` `dispatch_worker` sends it. With no header the caller is the operator (web UI). **This is a trust-model LABEL, not authentication**: both callers hold the same bearer token, and a caller that omits the header is treated as the operator. The other in-repo caller, `scripts/verify_a11_affinity.py`, is a manual operator verification script; it stays operator. | R05 (dispatch_worker-shaped ⇒ hold stays, queued not activated, row `system` / `automation:` scope; web-shaped ⇒ releases and the queue resumes); mcp test asserts the header; mutants: header ignored / automation counted human / mcp sends no header ⇒ killed |
| **2** wake dispatcher replaced a stopped Manager | Module helper `_operator_stop_held(db, sid)` reads the durable record. It does no read while nothing is enrolled; an unreadable record counts as held (fail closed). Three consumers use it: (a) `_continue_case_once` returns 0 BEFORE the dead/crash-respawn branch, with no respawn, no approval, no escalation and the wait state intact; (b) `_handle_transient_paused_case` returns True, so the pause keeps owning the Case and is not closed "session_unavailable" and handed to respawn; (c) `_handle_quota_paused_case` returns True, so there is no auto/approved `resume_case`. "Stopped" and "dead" are told apart by the record, not the status: after close the record is cleared and the crash path owns the session again. | R04 (now asserts NO respawn, NO approval, pause kept; the control without stop wakes/delivers), R04b (default approval ON: no crash-respawn call for a held Manager; after close it takes the crash path), R10 (quota auto-resume: control resumes; held ⇒ no `resume_case`), R11 (unenrolled: no hold read); mutants: wake / transient / quota ignore hold, helper reads when nothing enrolled ⇒ killed |
| **3** surviving M2/M3/M4 | kill tests | R06 (stop racing close never turns closed→cancelled), R07 + R07b (operator send never clobbers BUSY/AWAITING_INPUT/ERROR, even with the record set after a stale save), R09 (slot-waiting count excludes held sessions), R08/R08b (heads and activation each hold on the record alone) |

M1 (reviewer) is near-equivalent and noted, not killed.

**Mutation run** (scratch worktree `mut4b2`, removed with plain `git worktree remove`; spawn guard on): 15 mutants, all killed. The first pass left 5 survivors (M3, activation-on-record, transient, quota, helper-read); R07b/R08b/R10/R11 and a strengthened R04 were added until every one was killed.

**Verification.**
- turn-queue files: 316 passed / 7 red. The reds are unchanged: SYS03-07 and api ×2.
- Regression group + `test_case_transient_resume`, `test_wake_dispatcher_eventdriven`, `test_mcp_manager`, `test_case_respawn`, `test_case_quota_resume`: 517 passed.
- Reviewer probes: Q3/Q4 now hold. Q1 as written (no principal header = operator) still releases, by design. Q2 is a carried item.

**Residuals — carried (A87 → CONTEXT.md).**
1. MINOR 4: stale whole-row `upsert_session` saves (e.g. `_record_job_session_turn`, orchestrator.py ~4354) can rewrite the `cancelled` STATUS; legacy has the same race. Activation and Case automation no longer depend on status: they read the durable record. Other status-only readers (UI, resume-mode choice) can still be misled.
2. MINOR 5: a stop with no active turn (between turns, or while a head is blocked) does not hold (legacy parity: mark_cancelled only on an actual cancel).
3. Armed-cancel registry: eviction beyond 256 entries drops the oldest arms, and leaked arms of already-finished turns occupy slots until evicted.
4. The principal header is self-declared. An automation caller that omits it is treated as the operator and releases the hold. Authenticated per-caller principals belong to A71 (per-node credentials).
5. Stands from rework 1: the late `ensure_future` interrupt; a slow lineage writer past its lease; 4b residuals 2-6; the per-process armed registry.

### Stage 4b ACCEPTED — final minors (2026-09-26, commit `7b51e75` + this record)

1. **Test honesty for the fail-closed hold read.**
   - F01 kills N1. A stopped Manager's hold read raises: no respawn, no approval, no delivery. The next tick, with a healthy read, still holds.
   - F01b is the adopted probe P1: an unheld Manager whose read fails once is skipped for that tick and woken on the next.
   - F02 kills N2: a quota pause that names no session falls back to `case_manager_session_id` and honours the hold.
2. **A coalesced operator admission releases the hold.** The release is factored into `_release_stop_hold(conn, sid, now)` (db.py) and now runs both in the coalesce branch (human/operator only) and on a fresh insert. A pure idempotent replay still skips it, which is harmless.
   - F04 is P6 inverted: compaction queued behind a running turn, then Stop, then Compact again (coalesced). The hold is released and the compaction activates next.
   - F04b: a coalesced automation admission keeps the hold.
3. **Lazy quota lookup.** `_operator_stop_held` accepts a zero-arg resolver, called only after the enrollment short-circuit. The quota handler passes `lambda: pause.session_id or db.case_manager_session_id(case_id)`. F03 shows no Manager lookup happens while nothing is enrolled.
4. **Web upload-with-instruction: checked, NOT real.** `_store_session_upload` (control_api.py) already refuses an enrolled session with 422 `managed_unsupported` as its first step, before any file write, `mark_busy`, staging or admission (Stage 4a). The `mark_busy` calls the review cited are therefore unreachable for an enrolled session, and no code was changed. F05 proves it through the real app, with and without an instruction: 422, status unchanged (AWAITING_INPUT), no `uploads/` directory, no managed row. An ACCEPTED upload cannot happen for an enrolled session until producer 8.

**Mutation run** (scratch worktree `mut4b3`, removed with plain `git worktree remove`; spawn guard on): 5 mutants, all killed — N1, N2, eager quota lookup, coalesced operator not releasing, and coalesced automation releasing.

**Verification.**
- turn-queue files: 323 passed / 7 red. The reds are unchanged: SYS03-07 and api ×2.
- Regression group: 517 passed. That is the 420 group plus `test_case_transient_resume`, `test_wake_dispatcher_eventdriven`, `test_case_respawn` and `test_case_quota_resume`; `test_mcp_manager` is already in the 420 group.

**Residuals — carried (A87 → CONTEXT.md).**
1. An operator-invoked `sweep_orphaned_cases` force-closes a held Manager's Case (legacy parity). Since "held" is now resumable, the sweep may close a Case the operator meant only to pause.
2. An automation close (`release_worker` / `close_case` with worker close) of a held session ends the hold as a terminal action: `close_session_turns` clears the record and withdraws queued work.
3. A quota pause bound to an old held session can keep owning the Case after an operator-approved respawn onto a new session. The held-session check returns True, so the pause keeps holding until closed.
4. All earlier 4b / rework / rework-2 residuals stand.

### Stage 4c — producer 3: Case continuation token→turn linkage + durable finalization (2026-09-26, commits `9a8db54`..`8dde7d2` + this record)

**Scope.** For an ENROLLED Manager, the Wake-Dispatcher's continuation is ONE durable managed turn. The durable trigger identity is the existing generation token `cont:{case}:{gen}`. Finalization is driven by the turn's terminal outcome in the DB, not by an in-memory task. Unenrolled Managers stay on the unchanged legacy branch.

**Built.**
- **Migration 38** (db.py `_get_migrations`): `mesh_tasks.producer_turn_id` + partial index `idx_mesh_tasks_producer_link` (`WHERE producer_turn_id IS NOT NULL AND status='claimed'`). NULL on every legacy row.
- **Deterministic id** `producer_turn_id(token, session, attempt)` (db.py:857) → `cturn_<sha256[:24]>`. The attempt is durable in the token payload and is bumped only by a non-consuming finalize.
- **In-txn link.** `enqueue_turn(producer_token=…, producer_meta=…)` calls `_link_producer_token` (db.py:8807) inside the admission transaction, on every branch (fresh / replay / coalesce). It links the token as `claimed` with `claimed_by=__manager_continuation__`, `claimed_at=NULL` (the lease reaper selects `claimed_at IS NOT NULL`, so it never re-offers it) and `producer_turn_id`, and merges case / generation / attempt / presented / retired into the token payload. It converges if already linked to the same turn. It raises, rolling the admission back, when the token is missing, finalized, linked elsewhere, or the turn is terminal (a stale-attempt replay).
- **Admission.** `_continue_case_once` (orchestrator.py:1809) branches on `session_enrollment` after the hold and dead-session checks and before the legacy `AWAITING_INPUT` gate. A BUSY Manager is admitted and queued behind its active turn, with no interrupt.
  - `_continue_case_managed` (:1863): a linked or finalized token ⇒ 0, with no admission txn. Otherwise it writes the token (legacy `enqueue_task`, idempotent) and admits via `_enqueue_task` (same harness/local gates) → `_admit_managed_producer_turn` (:9857).
  - The admission itself: principal `automation` (`turn_source='system'`, scope `automation:<sid>:continuation`, key `<token>#<attempt>`, hash of the trigger identity), `turn_kind='continuation'`, coalesce `case:<cid>:gen:<n>`. Lineage comes from the same convergent procedure as producer 1 (B-attach). A refusal (typed 503/429/422, harness block) leaves the token pending, and the next tick replays to the same id.
- **Durable finalizer.** `MeshDB.reconcile_finalizers` (db.py:5996) runs one bounded query over the link index (≤25). Per token, one convergent procedure (`_finalize_producer_token`):
  - completed / failed / failed_node_offline (legacy consumed failures too): `worker.wait_resolved` for retired groups via `append_flow_event_once`, then a fenced CAS token→`completed` with `{generation, consumed_task_ids, turn_id, turn_status}`. The token row IS the round, so it is counted once.
  - withdrawn / cancelled: a fenced CAS token→`pending`, link cleared, attempt+1. No round is counted and nothing is consumed.
  - It is called at the top of every Wake-Dispatcher tick (`_reconcile_continuation_finalizers`, :1935; gateway process, no read while nothing is enrolled), so it needs no in-memory finalizer and survives a restart. There is no new poller and no event-log scan.
- **Activation-time revalidation** (`_managed_turn_obsolete`, orchestrator.py:10490, called from `_prepare_managed_turn`). It applies only to automation-principal rows (`system` + `automation:` scope). Human, operator and runtime rows are never touched and cost no read. If the Case (the token's Case for a continuation, else `flow_run_id`) is blocked ⇒ `case_blocked`; closed ⇒ `case_closed`. A continuation is also obsolete when unlinked, when the Manager was rebound (`manager_rebound`), or when none of its presented tasks is still unresolved (`reviewed`). `turn_scheduler._activate_head` catches `TurnObsolete` (turn_scheduler.py:45/109) → `withdraw_turn(actor="scheduler:obsolete:<reason>")`, counted in `withdrawn`. **This closes 4b residual 4**: queued Manager-dispatch and continuation turns of a killed or closed Case are withdrawn when they reach the head.

**Producer inventory.**
| Producer | Durable trigger identity | Turn id | Completion effect |
|---|---|---|---|
| Case continuation (Wake-Dispatcher, enrolled Manager) | token `cont:{case}:{gen}` + durable attempt; idempotency (`automation:<sid>:continuation`, `<token>#<attempt>`), coalesce `case:<cid>:gen:<n>`; linked in the admission txn | `cturn_<sha256(sid,token,attempt)[:24]>` (protocol 1, `turn_kind=continuation`, `system`) | scheduler (revalidated) → carrier → `complete_turn` → next tick `reconcile_finalizers`: consumed ⇒ token `completed` (round + watermark + `wait_resolved`); withdrawn/cancelled ⇒ token re-armed (attempt+1) |

**Exit table (new managed states).**
| State | Exits | Tests |
|---|---|---|
| token pending, unlinked (written, admission not committed) | next satisfied tick admits the SAME id; Case closed/blocked ⇒ inert (as legacy tokens) | Q02 |
| token linked (`claimed`, `claimed_at` NULL) | linked turn terminal → finalizer consume or re-arm; turn exits = Stage 3/4b exits (carrier, recovery, cancel, session close ⇒ withdrawn) | Q03, Q04, Q05, Q06, Q14 |
| continuation turn lineage-pending (crash after commit) | 4a lineage recovery after the lease; tick never mints a second turn | Q02b |
| queued automation turn of a blocked/closed Case, rebound or reviewed wake | withdrawn at activation → token re-armed | Q06, Q07, Q07b, Q08 |
| re-armed token (attempt n+1) | next satisfied tick admits the new deterministic id; held ⇒ waits for the operator | Q05 |

**Tests** (`tests/test_turn_queue_4c.py`, 21, all offline, spawn guard autouse; real file-backed MeshDB, real orchestrator continuation / admission / lineage / finalizer, real scheduler pass, real `stop_managed_session_turn` / `interrupt_case` / `close_case` / `record_review`): Q01 busy⇒one queued turn, no interrupt, no admission txn on later ticks; Q01b concurrent ticks; Q02/Q02b crash windows; Q03/Q03b restart finalization + exactly-once round; Q04 failed consumes; Q05 cancelled re-arms under hold; Q06 review ⇒ withdraw; Q07 interrupt (automation withdrawn, human + runtime kept); Q07b close; Q08 rebind; Q09 control; Q10/Q10b in-txn link rollback + stale attempt; Q11 reaper exclusion; Q12 index plan; Q13 unenrolled all-thread trace (≥2 threads, no managed SQL, legacy delivery unchanged); Q14 tick end-to-end incl. round 2; Q15 fenced CAS; Q16 admission racing a stop keeps the hold.
- 4b `R04` control adjusted: each scenario uses its own DB, and the control now asserts ONE managed continuation (no legacy `manager_continuation` delivery) plus the transient delivery. The held scenario assertions are unchanged plus `cont == []`.
- Turned green: SYS03, SYS04. SYS02 was already green.
- turn-queue files: **346 passed / 5 red** (SYS05-07, api ×2; baseline 323 / 7).
- Named regression group: **506 passed**, + `test_control_api_wait_group` / `test_session_cache_heartbeat` 41 passed.
- schema/flow/control/telemetry users: 136 passed.

**Mutation run** (scratch worktree `mut4c`, removed with plain `git worktree remove`; spawn guard on): 24 mutants, 23 killed.
- M9 (automation-scope filter dropped) first survived because Q07's runtime row never reached the head. Fixed by giving it its own session; re-run: killed.
- **Equivalent:** M23 (`token_to_turn` ignores the link), because the link always names the derived id of the current attempt.
- Also equivalent by construction: the trigger-identity admission hash (a same-key replay with a different body cannot occur except for a concurrent tick, which then gets a harmless 409 → 0). *(Corrected by the rework, m5: the `continuation_unlinked` check is NOT equivalent — see below.)*

**Service boundary (§7).**
| Item | managed wake branch | finalizer | activation revalidation |
|---|---|---|---|
| Concurrency | the single Wake-Dispatcher loop; admission via the 4-permit service; the durable link collapses racing ticks | ≤25 tokens per tick, one small `_managed_write` txn each | ≤25 heads/pass; reads only for automation rows |
| Memory | O(presented tasks) per token | O(25) | one continuation tick per continuation head |
| Request size | internal only: source/metadata are server-set, and `__turn_producer` is not reachable from any HTTP body | internal | internal |
| Timeout | 5 s admission / txn deadline ⇒ typed error, token pending, retried next tick | 5 s per txn; failure logged, token stays linked, retried | withdraw race ⇒ `ineligible`, re-read next pass |
| Malformed input | the human+coalesce guard stays; stale attempt ⇒ 409 rollback | garbled payload ⇒ `{}`: consumed without events; *(corrected by the rework, m2)* the next attempt is derived from the terminal turns (`producer_token_attempt`), and the link rewrites the payload | missing Case/token ⇒ withdraw with reason |
| Backing failure | unreadable marker ⇒ raises (Case skipped this tick, no legacy fallback) | read error ⇒ logged, tick continues | prepare error ⇒ existing 4a blocked/backoff |

**Residuals (for CONTEXT.md).**
1. Finalization latency is bounded by the Wake-Dispatcher interval (default 30 s). There is no in-memory accelerator, because the completion commits in the task-server process. With `CASE_CONTINUATION_ENABLED` off, finalization pauses; so do wakes.
2. **Deviation:** an operator-cancelled wake re-arms (the Manager is re-woken after the operator releases the hold). Legacy consumed it after its 30 min finalize deadline. An intervening operator review makes the re-armed wake obsolete.
3. Continuation lineage is B-attach to the session's newest open Case (legacy parity). For a multi-Case Manager that can differ from the woken Case. Revalidation and finalization use the token's Case.
4. Obsolete automation turns are withdrawn when they reach the head, not eagerly at `interrupt_case` / close. A row queued behind a held session keeps its per-session cap slot until then. Written links of withdrawn rows stay (4a residual).
5. A token claimed by the LEGACY path before enrollment blocks the managed wake until the legacy finalizer or lease reaper resolves it. Re-armed tokens of a closed Case stay pending (inert).
6. While a wake is in flight, each tick still recomputes that Case's continuation tick and does one marker read + one token read (no write).
7. Producers 4-8, Stage 5/6 and managed Codex/OpenCode are untouched. `manager_*_resume` / `manager_respawn` / heartbeat still fail closed for enrolled sessions.

### Stage 4c rework — A87 review (M1 + kill tests + residual 3 + m2/m3/m5) closed (2026-09-26, commits `d1fd0b4`..HEAD + this record)

| Finding | Fix | Test (mutation killed) |
|---|---|---|
| **M1** a late Manager binding was lost while the old Manager was held or long-busy | `_withdraw_rebound_continuation` (orchestrator.py:1944) is called from `_continue_case_once` right after the Manager lookup, before the hold / enrollment branches. It runs only when something is enrolled; otherwise no read. If the generation's token is linked to a wake still QUEUED on a session that is no longer `case_manager_session_id`, it calls `withdraw_turn(actor="scheduler:obsolete:manager_rebound")` (an automation row, so the hold is untouched) and re-arms the token at once via the finalizer. The SAME tick then wakes the rebound Manager on either path: managed (attempt+1, new session ⇒ new id) or legacy (claims the re-armed pending token). A claimed or running wake is left to finish and be consumed. | Q17 (P1 adopted: unenrolled S2 woken on the next tick, exactly once; S1 still held), Q17b (enrolled S2 gets the managed wake), Q17c (a running wake is not withdrawn); mutants R1 (not called), R3 (no immediate re-arm), R8 (read without the enrollment gate; Q13 now forbids the token-row read) — killed |
| **MA / MB / MC** surviving mutants | kill tests | Q21 (P6 adopted: after a re-arm the fresh presented/retired lists win: consumed `[w1, w2]`, one `wait_resolved`), Q22 (`failed_node_offline` consumes), Q23 (a partial review does not withdraw) — all killed |
| **Residual 3 (fixed now)** lineage went to the session's newest open Case | New intent-metadata key `__attach_case_id` (`_ATTACH_CASE_META_KEY`, :9765) = the token's Case. It is persisted, so lineage recovery converges on the same Case. `_managed_lineage_converge` (:10091): pinned ⇒ attach to that Case only if it is still open, else standalone. It is never a different Case, and a pinned attach never changes the session's `current_case_id`. | Q18 (P2 adopted: Manager on A and B; wake for A ⇒ task link + `task.attached` on A only, current Case unchanged, round on A only), Q18b (crash before lineage ⇒ recovery attaches to A only); mutants R4 (not pinned), R5 (pinned attach re-affiliates) — killed |
| **m2** garbled token payload retried forever | `MeshDB.producer_token_attempt` (db.py:5982) takes the payload counter but never an attempt whose turn (key `<token>#<n>` in this session's automation scope, one idempotency-index range read) already ended. The link then rewrites the payload with fresh facts. | Q20 (P5 adopted: ticks `[1, 0]`, attempt-2 id linked, payload self-healed); R6 killed |
| **m3** `wait_resolved` appended after `flow.closed` | `_finalize_producer_token` reads the Case. If it is closed or unknown, the token is consumed (round + watermark) but nothing is appended to the Case (4a rule). | Q19 (P3b adopted: no event after the close); R7 killed |
| **m5** (record correction) | The earlier "equivalent by construction" claim for `continuation_unlinked` was WRONG. Legacy `record_continuation_consumed` is an unfenced UPDATE, so at cutover a legacy in-memory finalizer can complete a token that a managed wake is linked to. The activation guard then withdraws the duplicate managed wake. It is a live guard. | M21 (revalidation removed) killed; the path itself is a cutover race and has no dedicated test |

**Mutation run** (scratch worktree `mut4c`, removed with plain `git worktree remove`; spawn guard on): 35 mutants (the 24 from Stage 4c + MA/MB/MC + R1-R8), 33 killed.
- First pass: R8 survived. The traced SQL carries bound values, so Q13's pattern was fixed to match them; re-run: killed. M22's pattern was updated to the new loop and is killed.
- **Equivalent:** R2 (the rebound check also tries running wakes) — `withdraw_turn` itself refuses non-queued rows, so the queued check is defence in depth. M23 as before.

**Verification.**
- turn-queue files: **356 passed / 5 red** (SYS05-07, api ×2; baseline 346 / 5).
- Regression group incl. `wait_group` + `cache_heartbeat`: **547 passed** (baseline 547).

**Carried (A87 → CONTEXT.md).**
- m4: `failed` / `failed_node_offline` consume the wake even if the Manager never ran (legacy parity).
- Residual 2 (re-arm after the operator releases the hold) is ACCEPTED as desired.
- A84 note: when A84 lands, fold `wait_resolved`, outbox consumption and the token CAS into one transaction.
- Stage 4c residual 3 is superseded (pinned). Residuals 1, 4-7 stand.
- New: the rebound check costs one token read per satisfied Case per tick while anything is enrolled.
- New: a pinned wake to a Case that closed before lineage was written runs standalone. Revalidation then withdraws it (`case_closed`) at activation.

### Stage 4c ACCEPTED — round-2 follow-up (2026-09-26, this commit)

- **The Manager's (A87) round-2 verdict: ACCEPT.**
- **Reviewer probe P4 adopted** as `Q18c`. Scenario: a pinned wake whose Case (A) closed before its lineage was written. Result:
  - it runs standalone, then is withdrawn `case_closed` at activation;
  - nothing is written to Case A or Case B (no event, no task link);
  - `flow_run_id` stays empty and the session's `current_case_id` is untouched.
- **Mutation-verified:** in `_managed_lineage_converge`, changing the pinned-branch closed-check to `if True:` makes Q18c fail. The mutant ran in a scratch worktree, removed afterwards.
- **Counts:** `tests/test_turn_queue_4c.py` 32 passed.

**Carried residuals (reviewer → CONTEXT.md):**
1. If the rebound withdrawal loses the race to the old Manager's activation, the wake runs on the old session and is consumed there. The rebound Manager is woken on a later round, which costs one extra tick.
2. Wait groups on closed Cases stay "pending" in projections, because the finalizer appends no `wait_resolved` after `flow.closed`. This is cosmetic: a closed Case is never ticked.

### Stage 4d — producers 4 (watched-job notification) + 6 (cache heartbeat) (2026-09-26, commits `91ad69f`..HEAD + this record)

**Scope.** For ENROLLED sessions only: a watched-job completion and a due cache heartbeat each become ONE durable managed turn under the automation principal. Unenrolled sessions take the unchanged legacy branches, and while nothing is enrolled neither producer adds a single enrollment/ledger read (U01, all-thread SQL trace).

**Built.**
- **Shared admission.** `_admit_managed_producer_turn` (orchestrator.py:10161) is generalized. Continuation keeps its exact 4c request. `watched_job` / `cache_heartbeat` are routed there only when the server-set `__turn_producer` facts carry the matching `turn_kind` (`_MANAGED_AUTOMATION_PRODUCERS`, :10018); no HTTP body can set that key. Without the facts, both sources still FAIL CLOSED (4a). Every automation producer row gets principal `system`, scope `automation:<sid>:<kind>`, idempotency key = the trigger key (permanent: a replay collapses even after the turn is terminal), and a hash of the trigger identity. `producer_turn_id(..., prefix=)` (db.py) derives the deterministic ids (`jturn_` / `hturn_`).
- **Producer 4 — watched job.** `_process_terminal_job` (orchestrator.py:4770) resolves enrollment via `session_enrollment`: no read while nothing is enrolled, and an unreadable marker fails closed (no session turn or record; Telegram notify unchanged).
  - `notify_agent` → `_admit_managed_watched_job` (:4729): trigger `watched:<job_id>` (= coalesce key, SYS08 shape), `dispatched_by=watched_job:<id>`. Case attachment is the existing watched-job rule, through the shared convergent lineage procedure: attach to the session's open Case and never relabel a Manager. The jobs schema has no Case, so there is nothing to pin (no `__attach_case_id`).
  - A refused admission (typed 4xx/5xx, harness) falls back to the audit record (legacy parity), never to legacy execution.
  - notify-only / fallback → `_record_managed_job_audit` (:4679) → `MeshDB.record_audit_turn` (db.py:2157): ONE already-terminal protocol-0 insert (no claimable `pending` window) plus enrich/event, and **NO whole-row session save**. This closes 4b MINOR 4 on the managed path: a stale snapshot cannot rewrite `cancelled`.
- **Producer 6 — cache heartbeat.** In `_process_due_cache_heartbeats` (:1511), an enrolled session goes to `_admit_managed_cache_heartbeat` (:1593).
  - The window lease `cachehb:<sid>:<slot>` (protocol 0, sentinel) is the durable trigger. It is linked to `hturn_…` inside the admission txn: `PRODUCER_TOKEN_SENTINELS` (db.py:830) generalizes `_link_producer_token` beyond continuations. An already linked or finalized lease ⇒ 0, with no admission txn.
  - Eligibility (`managed=True`) takes idleness from the ledger, not from in-memory tasks or BUSY/IDLE; the quota / owner / evidence / pinned-node / driver checks are unchanged.
  - **Idle-only**: `enqueue_turn(idle_only=True)` re-checks `_session_idle_for_optional_turn` (db.py:9062) INSIDE the txn (db.py:2686). The predicate refuses when: the session is closed / cancelled / errored; the durable hold record is set; the session is paused; any other managed open row exists; or any legacy nonterminal row exists. `MeshDB.heartbeat_eligible` (:3785) exposes it (SYS06).
  - **Deadline**: `expires_at = admission + MANAGED_HEARTBEAT_TTL_SEC` (300 s, orchestrator.py:176). Expiry is enforced at two points. (a) The existing scheduler head expiry. (b) NEW claim-time expiry in `claim_turn`: a `pending` non-human row past its deadline is withdrawn in the claim txn (audit `claim:expired`) and refused with 409 `reason=expired`. That covers a heartbeat activated in time but released not-invoked while the CLI was busy. The `/claim-managed` route hints the scheduler.
  - **Activation revalidation**: `_managed_heartbeat_obsolete` (:10878) checks, in order: lease still linked; idle excluding itself; `CACHE_HEARTBEAT_ACTIVE`; controller active (owner live); quota; no Case pause; cache evidence ≥ threshold (`_cache_heartbeat_evidence_sufficient`, :179, shared). Only then does the generic Case check run.
  - **Durable finalizer**: `MeshDB.reconcile_heartbeat_finalizers` (db.py:6244) runs at the top of each heartbeat tick, and only while something is enrolled. For each linked lease whose turn is terminal it runs ONE txn (`_finalize_heartbeat_lease`, :6293): a fenced CAS moves the lease to `completed`. Only the CAS winner, and only when the turn completed or failed, applies the shared controller transition (`_apply_cache_heartbeat_result`, :8320, which legacy `record_cache_heartbeat_result` now wraps with identical SQL). Withdrawn, cancelled and node-offline turns ⇒ no beat. There is no in-memory finalizer on the managed path.

**Producer inventory.**
| Producer | Durable trigger identity | Turn id | Completion effect |
|---|---|---|---|
| watched-job notification (`notify_agent`, enrolled) | job id: idempotency (`automation:<sid>:watched_job`, `watched:<job_id>`) + coalesce `watched:<job_id>` | `jturn_<sha256(sid, watched:<job>, 1)[:24]>` (protocol 1, `turn_kind=watched_job`, `system`) | scheduler (hold honoured; Case revalidated) → carrier → `complete_turn`; nothing to finalize (legacy has no round) |
| watched-job record (notify-only / refused, enrolled) | job id = audit row id (`INSERT OR IGNORE`) | none (terminal audit row, protocol 0) | none |
| cache heartbeat (enrolled) | window lease `cachehb:<sid>:<slot>` linked in the admission txn; idempotency (`automation:<sid>:heartbeat`, lease id) | `hturn_<sha256(sid, lease, 1)[:24]>` (protocol 1, `turn_kind=heartbeat`, `system`, `expires_at`) | scheduler (revalidated, expiry) → carrier → `complete_turn` → next tick `reconcile_heartbeat_finalizers`: beat recorded once (completed/failed) or none (withdrawn/cancelled/offline); lease `completed` |

**Exit table (new managed states).**
| State | Exits | Tests |
|---|---|---|
| heartbeat lease pending, unlinked (admission refused after the lease write) | same-window tick re-admits the same id; window passes ⇒ inert sentinel row (never claimable; legacy parity) | H02b |
| lease linked (`claimed`, `claimed_at` NULL, sentinel) | linked turn terminal → finalizer CAS → `completed` (+beat iff ran) | H07, H07b, H08, H09 |
| queued heartbeat | activated when idle + revalidated; withdrawn `scheduler:obsolete:{session_not_idle, heartbeat_disabled, heartbeat_stopped, quota_exhausted, case_pause_active, cache_below_threshold, case_*}`; `scheduler:expired`; session close ⇒ withdrawn | H04, H05, H06, H06b, H08 |
| pending heartbeat past its deadline | claim ⇒ `withdrawn` (`claim:expired`), slot freed; human rows exempt | H05b |
| queued watched-job turn | head + not held ⇒ activated; held ⇒ waits for the operator's release (FIFO); Case blocked/closed ⇒ withdrawn; close ⇒ withdrawn | W03, W06 |
| watched-job audit row | terminal at insert | W04, W05 |

**Tests** (`tests/test_turn_queue_4d.py`, 22; offline, autouse spawn guard). They use a real file-backed MeshDB and the real `_process_terminal_job` / `_process_due_cache_heartbeats` / admission / lineage / scheduler pass / claim / `complete_turn`. State changes go through the real APIs: `stop_managed_session_turn`, `session_service.close_session`, `interrupt_case`, `stop_cache_heartbeat`, a legacy `upsert_session` for the stale save, and `claim_task` / `complete_task` for the cancel control row. The only exceptions are clock / time-of-due fixtures.
- W01 one turn; double poll, restart, re-reported status and post-terminal replay all collapse. W02 Case attach once, no birth / relabel. W03 hold honoured, not released, FIFO after release. W04 audit-only, no pending window, stale snapshot cannot undo the hold. W05 refusal ⇒ audit fallback. W06 interrupted Case ⇒ withdrawn.
- H01 one heartbeat per window + linked lease + restart. H02 / H02b not idle (incl. the in-txn recheck against a stale pre-check). H03 / H03b hold (incl. after a stale status save). H04 human work behind ⇒ withdrawn. H05 / H05b expiry at head / at claim (human exempt). H06 / H06b quota / owner revalidation. H07 / H07b durable exactly-once beat across restart and racing finalizers. H08 close ⇒ no beat. H09 failure stops the controller as legacy. SYS06 predicate. U01 unenrolled: all-thread trace shows no managed SQL, legacy deliveries unchanged.
- Turned green: **SYS06**. SYS08 stays green.
- turn-queue files: **380 passed / 4 red** (SYS05, SYS07, api ×2; baseline 357 / 5).
- Named regression group: **547 passed** (baseline 547). `test_watched_jobs`, `test_mcp_jobs`, `test_dispatch_lineage`, `test_heartbeat_live_state`, `test_quota_window_coordinator` / `_prewarmer` and `test_task_server_client`: 122 passed.

**Mutation run** (scratch worktree `mut4d`, removed with plain `git worktree remove`; spawn guard on). 26 mutants; 25 killed.
- Killed: M1 idle_only in-txn off; M2 activation idle recheck off; M3 claim expiry off; M4 no deadline; M5 / M22 non-durable key / scope; M6 automation counted as operator (hold); M7 beat on every outcome; M8 finalizer CAS ignored (survived the first pass; H07b added); M9 / M25 managed audit via whole-row save / pending window; M10 / M11 enrollment reads when nothing enrolled; M12 / M13 quota / owner not revalidated; M14 finalizer not called; M15 hold record ignored; M16 legacy rows ignored; M17 refusal drops the record; M18 lease not linked; M20 activation sees itself; M21 revalidation branch removed; M23 producer branch ignored; M24 idle_only not requested; M26 human claim exemption dropped.
- H05b first killed several mutants spuriously through a 1 s wall-clock TTL. It now uses the ledger clock (`db._now`) and is deterministic.
- **Equivalent:** M19, the tick's managed pre-check (`heartbeat_eligible`) removed. It is advisory; the in-txn `idle_only` check is authoritative (H02b). This is the same shape as 4a's reserve term.

**Service boundary (§7).**
| Item | watched-job managed branch | heartbeat managed tick | heartbeat finalizer | claim-time expiry |
|---|---|---|---|---|
| Concurrency | single job poller; admission via the 4-permit service; the permanent key collapses double polls / restarts | single Wake-Dispatcher loop; ≤20 due / tick; idle-only in-txn ⇒ ≤1 open heartbeat / session | ≤25 leases / tick, one `_managed_write` txn each; CAS ⇒ racing finalizers count once | inside the existing claim txn |
| Memory | O(1) / job (tail ≤1500 chars in the prompt) | O(20) | O(25) | O(1) |
| Request size | internal; producer facts server-set, unreachable from HTTP | internal | internal | claim body unchanged (16 KiB route cap) |
| Timeout | 5 s admission deadline ⇒ typed error ⇒ audit fallback | 5 s admission ⇒ skipped this tick | 5 s per txn; failure logged, lease stays linked, retried | same claim txn deadline |
| Malformed input | missing session ⇒ skipped (legacy); garbled job fields ⇒ same payload rendering as legacy | missing / garbled heartbeat row ⇒ legacy eligibility stops it | garbled turn result ⇒ `{}` ⇒ success defaults, evidence fallback | non-human only; missing deadline ⇒ unchanged claim |
| Backing failure | unreadable marker ⇒ fail closed (no session turn / record); DB error in audit ⇒ logged (best-effort record) | unreadable marker ⇒ that heartbeat skipped; no carrier ⇒ skipped | read error ⇒ logged, tick continues | DB error ⇒ typed 503 (unchanged) |

**Residuals (for CONTEXT.md).**
1. Watched-job delivery is at-most-once across restarts (legacy parity): the poll watermark and processed set are in memory, so a job that finished while the gateway was down is never notified. The durable identity guarantees collapse, not delivery. A refused admission records the audit row and is not retried.
2. A watched-job turn of a Case that is blocked or closed before activation is withdrawn (the generic 4c automation rule). The agent then never sees that notification, and no audit row is written for it.
3. Enrolled audit records and managed turns do not write the session summary fields (`last_result_summary` / `task_history` / `last_task_id`). The ledger is the transcript truth, and BUSY/IDLE plus summaries are Stage 6.
4. Backend quiescence (a CLI-autonomous turn) is not observable at admission. Never-interrupt and never-late rest on the carrier's `managed_conflict` not-invoked release plus the 300 s deadline, so a released heartbeat can be re-claimed until then.
5. Heartbeat finalization runs only on heartbeat ticks (the Wake-Dispatcher interval, and only while `CACHE_HEARTBEAT_ACTIVE`). With the flag off, linked leases wait.
6. A heartbeat withdrawn in a window is not retried in that window (legacy lease parity). Heartbeat turns still B-attach to the session's open Case (legacy parity, not improved).
7. The idle predicate's legacy-row probe uses `idx_mesh_tasks_session`, which is O(session rows). It runs only for enrolled due heartbeats (≤20 / tick) and at heartbeat activation.
8. `continuation_token_for_turn` is used action-agnostically for heartbeat leases. The name is kept to avoid churn.
9. Producers 5, 7 and 8, Stage 5/6, and managed Codex/OpenCode are untouched.

### Stage 4d rework — A87 review (M1, kill tests R1-R4, m1, residuals 2/5) closed (2026-09-26, commits `ad712bd`..HEAD + this record)

| Finding | Fix | Test (mutation killed) |
|---|---|---|
| **M1a** an untyped error escaped managed job admission, so the poller dropped the job (no turn, no audit) and the rest of its batch, legacy included | `_admit_managed_watched_job` catches `Exception`. A typed refusal logs WARNING; an untyped error logs ERROR. Both fall back to the audit record for THAT job, and `_process_terminal_job` never raises out of the managed branch. | RW01 (P1 inverted); N1 (typed-only catch) killed |
| **M1b** one failing enrolled heartbeat starved the due list | the `_admit_managed_cache_heartbeat` call is wrapped per heartbeat (log, continue) | RW02 (enrolled sess-1 raises; unenrolled sess-2 still gets its legacy heartbeat); N2 killed |
| **m1** an activated (pending) heartbeat ran ahead of a human admitted behind it | `claim_turn`: a pending non-human row carrying `expires_at` is withdrawn in the claim txn when it is past the deadline (`claim:expired`) OR the session is no longer idle apart from it (`_session_idle_for_optional_turn(conn, sid, task_id)` ⇒ `claim:not_idle`). The claim is refused with 409, and `/claim-managed` hints the scheduler for both reasons. | RW03 (P2 inverted; the human activates next; an idle heartbeat stays claimable); N3 killed |
| **Residual 2** a withdrawn job notification vanished | `_prepare_managed_turn`: an obsolete `watched_job` row first writes its audit record (`_record_withdrawn_job_audit`: the job-id row, terminal, carrying the notification text + `withdrawn_reason`), `record_audit_turn(strict=True)`, THEN raises `TurnObsolete`. If the audit write fails, the head backs off and stays queued: it is never withdrawn without its record. | RW05, RW05b (storage-layer fault trigger); N6 and N7 (audit best-effort) killed. N7 survived the first pass, while RW05b patched the method rather than the storage. |
| **Residual 5** leases were not finalized while heartbeats were off | Module helper `_reconcile_heartbeat_leases` (no read while nothing is enrolled). It runs BEFORE the `CACHE_HEARTBEAT_ACTIVE` gate in `_process_due_cache_heartbeats`, and the Wake-Dispatcher tick runs it when heartbeats are off. | RW04 (P3 inverted), RW04b (real `_wake_dispatcher_tick_once`, continuation on, heartbeats off); N4 and N5 killed |
| **R1-R4** untested guards | kill tests | R1 pause (the column is set directly: there is no pause API before Stage 6, same as `test_turn_queue_scheduler`); R2 `case_pause_active` (real wait-group owner + `flow.quota_paused`); R3 `cache_below_threshold`; R4 unreadable marker on the job path ⇒ no managed row, no audit, no legacy submit, Telegram notify unchanged. N8, N9, N10 and N11 (marker failure ⇒ legacy) killed. |

**Mutation run** (scratch worktree `mut4d`, removed with plain `git worktree remove`; spawn guard on): 11 new mutants (N1-N11), all killed after RW05b was strengthened.

**Verification.**
- turn-queue files: **391 passed / 4 red** (SYS05, SYS07, api ×2; baseline 380 / 4).
- Regression group + `test_watched_jobs` + `test_mcp_jobs`: **576 passed** (baseline 576).
- `tests/test_turn_queue_4d.py`: 33 passed.

**Carried (A87 → CONTEXT.md).**
- **m3.** The claim-time withdrawal does not run the lineage-void procedure. That is harmless today: automation producers (heartbeat, watched job) never BIRTH a Case, and a join/attach membership stays as written (4a residual). A future deadline-carrying producer that can birth a Case must add the void.
- **m4.** A `failed_node_offline` (and `cancelled`) heartbeat turn counts NO beat. Legacy counted a failed beat, which STOPS the controller (`heartbeat_failed`). This deviation is deliberate:
  - `failed_node_offline` means the carrier vanished, not that the heartbeat or the cache failed. Stopping the controller on a carrier outage would permanently lose warmth tracking for a cache that may still be warm, whereas the next window re-evaluates eligibility.
  - `cancelled` means an operator stop. That stop already HOLDS the session, which blocks heartbeats until the operator releases it. Stopping the controller as well would silently disable heartbeats after the release.
  - A genuine `failed` result still stops the controller as in legacy (H09).
- **Residual 2 superseded** (an audit record is written). **Residual 5 narrowed:** finalization is still paused when BOTH `CASE_CONTINUATION_ENABLED` and `CACHE_HEARTBEAT_ACTIVE` are off, because the Wake-Dispatcher loop does not start. Linked leases are then inert: no paid effect, finalized once either flag is on.
- New: an untyped admission error on the job path now records the audit row and does not retry. That is at-most-once, legacy parity (Stage 4d residual 1).
- Stage 4d residuals 1, 3, 4, 6, 7, 8 and 9 stand.

## 16. Review record

### Stage 0 review — Manager/A87 — 2026-09-25 — VERDICT: ACCEPT (authorize Stage 1)
Independently re-verified 5/5 load-bearing pillars from the tree: the control_api root-cause window,
`complete_task` swallow+no-predicate, driver `send()` `cancel_inflight`-on-conflict, SDK Task* messages
present + driver-code blind, and the `flow_run_id`-outside-numbered-migration fact. Inventory is honest
(line numbers marked approximate where not personally opened; ctags-unavailable caveat stated). No
capability limit demonstrated — the SDK exposes the signals the oracle needs. Three escalated decisions
resolved above. Stage 1 authorized (assertion-capable red tests); Stage 2 gated on Stage-1 review.

## 17. Closure

Pending implementation. Acceptance of this packet authorizes the work and tests
above; it does not assert that the design already passed its backend proof gates.
