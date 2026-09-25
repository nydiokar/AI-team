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
- (11) the lineage hold (30 s) is a bounded crash window without Case lineage.

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
