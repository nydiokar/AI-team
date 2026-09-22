# Session Turn Queue — Unified Durable Delivery Design

**Status:** ready for staged implementation; backend acceptance tests and review required before rollout

**Date:** 2026-09-22

**Code baseline:** `5f58d2e`; original draft preserved in `0f832bf`

**Decision:** extend `mesh_tasks`, with the safety and migration contracts below.

**Build entrypoint:** [A82](../.ai/dispatch/AGENT_82_SESSION_TURN_QUEUE.md).
Proceed on a feature branch. The [Claude regression checklist](../.ai/dispatch/AGENT_83_CLAUDE_TURN_QUEUE_FEASIBILITY.md)
belongs to the build's carrier-integration tests, not a separate feasibility
prerequisite. Claude is mandatory. An unproven background-ordering concern is
not evidence that ordinary queued delivery is impossible; required tests and
review must pass before completion/rollout.

## 1. Owner verdict and scope

Keep one durable ledger for requested agent turns. A human instruction, an
agent instruction, and a system continuation all request the same operation:
deliver a prompt to a recipient session when that session can safely run it.
Their origin affects authorization and eligibility, not which queue stores them.

The original direction is sound; its implementation contract was not ready.
In particular, a unique index is not an execution lease, a terminal task row
does not currently mean its session has been reconciled, and the existing
table contains control commands and scheduling tokens as well as turns.
The revisions below address those gaps without adding another message queue,
broker, or workflow engine.

The product promise is:

- Accept several instructions while a session is busy, acknowledge only after
  persistence, and show their durable waiting state.
- Execute accepted requests in per-session acceptance order, without injecting
  into the current turn. Different sessions share bounded carrier capacity.
- Allow revision/withdrawal until activation; preserve the same task ID.
- Reconcile retries by durable idempotency. Recover safely after process
  restart; visibly hold uncertain execution instead of silently replaying it.
- Support permitted human, Manager, worker, and internal senders through one
  admission service. “Anybody” means an authorized sender, not arbitrary
  network access or permission to impersonate another agent.

Do **not** promise exactly-once backend execution or uninterrupted availability
during partitions. SQLite cannot atomically commit a backend invocation or
undo its external effects. The defensible guarantee is idempotent admission,
one authorized execution owner, and no successor while prior execution is
uncertain. Current WAL `synchronous=NORMAL` protects process-crash recovery,
not loss of the latest commits under every power/storage failure.

Information-only chat that should not run a model is outside this milestone.
Do not create an inbox as a prerequisite for delivering instructions.

## 2. Adversarial findings grounded in the tree

References are paths and symbols at the code baseline, so they survive line
number changes. These are implementation requirements, not optional cleanup.

| Priority | Evidence / challenged assumption | Required correction |
| --- | --- | --- |
| P0 | [db.py](../src/control/db.py), `enqueue_task`, `claim_task`, `complete_task`: writes catch errors; completion updates by ID without an ownership predicate. [orchestrator.py](../src/orchestrator.py), `_mesh_enqueue_task`: local execution is not stopped by a failed self-claim. | New canonical queue helpers must propagate failure and make ownership transitions conditional. No backend call after failed admission/claim/start. |
| P0 | `mesh_tasks` also stores `close_session`, `cancel_codex`, file staging and sentinel-pinned continuation/quota/heartbeat leases. [agent.py](../src/worker/agent.py), `_handle_task`, deliberately runs cancellation outside turn capacity. | Classify rows. Scope session uniqueness to managed execution turns. Never block cancellation behind the turn it must stop. Sentinel leases have NULL session IDs already; retain that separation. |
| P0 | Existing legacy rows can contain multiple pending/claimed turns for a session. The original unconditional partial unique index applies even with the new flag OFF. | Add opt-in row classification and gate per-session enrollment; do not install an index that invalidates legacy data or changes flag-off writers. |
| P0 | [task_server.py](../src/control/task_server.py), `submit_result`, commits terminal status; `_dispatch_to_node` in the orchestrator later saves the returned backend session ID. | Commit terminal outcome and correctness-critical session fields atomically before releasing the slot. Otherwise the next request can dispatch as `create_session` again. |
| P0 | `release_task`, `release_node_claims`, `list_stale_claims`, the stale-claim reaper, and worker shutdown can re-offer a claimed row. Results identify a node, not a unique claim attempt. | Add attempt fencing and a persisted start boundary; do not reuse legacy release/reaper behavior for possibly-started managed turns. Timeouts/offline labels do not prove the old backend stopped. |
| P1 | [session_store.py](../src/services/session_store.py), `get/save`: DB-first reads with file fallback; save writes JSON then whole-session DB upsert. | Flag-on scheduling must use strict canonical DB reads and field-scoped/versioned updates. A stale completion snapshot must not revert model, pin, close state, or active task identity. |
| P1 | `compact_session` directly invokes the local backend or dispatches a remote task. SDK proactive turns are recorded only after they happen (`_deliver_proactive_turn`, [claude_driver.py](../src/backends/claude_driver.py)). | Compaction must use session serialization. Native unsolicited work requires a proven driver idle/ownership gate or exclusion from enrollment; changing `submit_instruction` alone cannot cover it. |
| P1 | `_continue_case_once` waits for AWAITING_INPUT, then separately claims a scheduling token, submits a random-ID turn, and launches an in-memory finalizer. | Durable token-to-turn linkage, admission while busy where valid, activation-time revalidation, and restart reconciliation are required. Coalescing alone does not close the crash gap. |
| P1 | `mesh_tasks.flow_run_id` is an optional convenience column; `enqueue_task` does not populate it. Case identity is also in `flow_links` and task metadata. | Populate the explicit Case association and authoritative membership together for managed rows; do not assume existing rows carry it. |
| P1 | Selecting the oldest 25 due rows before removing blocked sessions can repeatedly select the same blocked rows. A batch size caps writes, not SQL work or fairness. | Filter eligible session heads before LIMIT, index the nonterminal subset, and bound total waiting work. Test a blocked-prefix workload and query plans. |
| P1 | `MeshDB._write` uses an unbounded-wait Python lock and up to four 15-second SQLite busy waits. `asyncio.to_thread` does not bound submitted work. | Separate bounded admission capacity, finite lock/transaction deadlines, and strict request-body limits from per-session serialization. |
| P1 | Worker `_poll_loop` creates handlers for every fetched row, including rows already scheduled and waiting for its semaphore; it overwrites `_active[task_id]`. | Deduplicate scheduled IDs and bound scheduled handlers before creating tasks, not just concurrent backend calls. |
| P2 | [Composer.tsx](../web/src/components/timeline/Composer.tsx), `send`, blocks on `submit.isPending`, not on the session's running state. | The UI already accepts successive sends; the missing pieces are durable queue truth, editing, and safe backend scheduling. Do not sell a button change as the fix. |
| P2 | `task_events` contains outcome fields, not arbitrary revision payloads. Current instruction limit is 262144 characters, plus a separate 48000-character carry-context limit. | Specify revision storage; do not claim it already exists. A new 16 KiB limit is a deliberate new-route policy, not the existing instruction maximum. |
| P1 | `GET /api/turns/{turn_id}` and `useSessionTurns` already expose telemetry; A81 changed event-covered reads to `SAFETY_NET_MS=60000`. | Use separate turn-request routes/query keys and preserve the event-first refresh policy. |
| P1 | `_SDKSession.send` cancels the previous turn when its lock is occupied; worker result posting has an in-memory delivery deadline. | Managed conflict must fail closed; persist bounded result-delivery obligations before posting. |

Also correct the context map: the actual service is
[`src/services/session_service.py`](../src/services/session_service.py), not
`src/core/session_service.py`.

## 3. Durable model: one turn ledger, distinct row purposes

Keep `mesh_tasks`, its task IDs, result/artifact linkage, and existing transport.
Do not rename “task” throughout the repository for this feature.

Add nullable/default-safe fields:

| Field | Contract |
| --- | --- |
| `queue_protocol INTEGER NOT NULL DEFAULT 0` | 0 = legacy/control/scheduling rows; 1 = managed execution turn. Server-owned, never client-selectable. |
| `queue_sequence INTEGER` | Monotonic per-session acceptance order; allocated inside admission transaction. |
| `turn_source`, `sender_session_id` | Server-derived source and attributable agent sender where applicable. |
| `turn_kind` | instruction, continuation, retry, heartbeat, compaction; label does not grant permissions. |
| `idempotency_scope`, `idempotency_key`, `admission_hash` | Durable original-request identity; hash includes target, body, attachments and relevant request options. |
| `revision INTEGER NOT NULL DEFAULT 1` | Compare-and-swap for queued edits/withdrawal. |
| `not_before`, `expires_at` | Optional internal eligibility and expiration; humans have no automatic expiry. |
| `activated_at`, `started_at` | Persisted activation and start authorization timestamps. |
| `claim_token` | Fresh opaque execution-attempt identity returned by claim, required by start/result/release. |
| `coalesce_key` | Namespaced internal producer key; never used to merge human instructions. |
| `blocked_reason` | Bounded reason for an ineligible queue head or uncertain execution. |

Reuse `flow_run_id` for the explicitly validated Case association of managed
turns and retain authoritative `flow_links`. Preserve `parent_task_id` for
retry/lineage linkage. Store bounded producer-specific preconditions in the
existing payload, not another scheduler database.

On the existing session row, add durable enrollment and queue-pause markers
and a configuration revision used by activation. Derive the active turn from
the indexed ledger rather than maintaining a competing active-turn table.
Configuration writers increment that revision; completion updates only the
fields it owns. Pause/resume is an explicit authenticated queue control and
must survive restart. A new admission does not implicitly clear a recovery,
quota, approval, or operator-stop hold.

For revision history, add a small append-only `mesh_turn_revisions` table
keyed by `(task_id, revision)`, containing changed body/reference fields,
actor and timestamp. This is audit history, not a second queue or execution
authority. It is inserted atomically with the queued revision. Do not overload
outcome-only `task_events` or append an ever-growing JSON array to a task row.
Cap edits at 20 per turn initially; terminal retention follows task history.

Managed turn states:

```text
queued --activate--> pending --claim--> claimed --start--> running
   |                      |                |                 |
withdrawn                 +---------- terminal outcome ------+
                                           |
                                     recovery_required
```

Terminal outcomes are `completed`, `failed`, `cancelled`,
`failed_node_offline`, and `withdrawn`. `recovery_required` is **not**
terminal: it retains the session slot until backend quiescence/result is
established. It can be entered from claimed/running when ownership or start is
uncertain. A pending item can be cancelled safely before any claim. A queued
withdrawal never becomes an execution failure.

Use these index shapes (include all required columns in the migration):

```sql
CREATE UNIQUE INDEX idx_mesh_turns_one_active_session
ON mesh_tasks(session_id)
WHERE queue_protocol = 1 AND session_id IS NOT NULL
  AND status IN ('pending', 'claimed', 'running', 'recovery_required');

CREATE UNIQUE INDEX idx_mesh_turns_session_sequence
ON mesh_tasks(session_id, queue_sequence)
WHERE queue_protocol = 1;

CREATE INDEX idx_mesh_turns_waiting
ON mesh_tasks(created_at, id)
WHERE queue_protocol = 1 AND status = 'queued';

CREATE INDEX idx_mesh_turns_session_open
ON mesh_tasks(session_id, queue_sequence)
WHERE queue_protocol = 1
  AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required');

CREATE UNIQUE INDEX idx_mesh_turns_idempotency
ON mesh_tasks(idempotency_scope, idempotency_key)
WHERE queue_protocol = 1;

CREATE UNIQUE INDEX idx_mesh_turns_active_coalesce
ON mesh_tasks(coalesce_key)
WHERE queue_protocol = 1 AND coalesce_key IS NOT NULL
  AND status IN ('queued', 'pending', 'claimed', 'running', 'recovery_required');
```

Require non-NULL session, sequence and idempotency fields for protocol 1 via
DB constraints/triggers as appropriate to an additive SQLite migration.
State transitions must also have conditional predicates; the unique index is
only the final backstop. Index operations are logarithmic, not a literal O(1)
guarantee. Latest-sequence lookup uses the session-sequence index, not an
aggregate scan over completed history.

Control commands and Case scheduling tokens stay protocol 0. Cancellation is
out of band and targets an active task **and attempt**, while close stops new
admission and drains/cancels according to the existing carrier close ordering.
File fetches are prerequisites/control work, not conversational turns; they
must finish before a referencing turn starts. Compaction mutates backend
context and therefore is a protocol-1 serialized operation.

Guard legacy inserts/claims at the DB boundary for enrolled sessions: reject
protocol-0 execution actions while allowing the explicit control-action
catalog. Do not infer action category from `session_id IS NULL` or trust a
caller-supplied `queue_protocol`. This closes a bypass that the scoped unique
index alone cannot prevent.

## 4. Admission, idempotency, and editable intent

One transport-neutral `enqueue_turn` service is shared by web, Telegram,
Manager/worker tools, file ingestion when session-scoped, watched-job
continuations, and internal producers. Reuse the existing harness admission
gate and metadata/Case lineage builders; do not bypass them by writing rows
straight from an HTTP handler.

Within one finite write transaction:

1. Resolve durable idempotency first. Matching scope/key and original hash
   returns the same ID and **current** status/revision, even if the queue is
   now full or the session has since closed. Recheck caller read permission.
   The same key with different original input returns 409.
2. Validate the canonical recipient, enrollment, Case membership/state,
   sender permission, harness policy, and total/per-session capacity.
3. Allocate sequence and insert bounded intent, explicit Case link, and any
   required producer-token linkage. Commit before acknowledgement.

The idempotency scope includes the server-known trust principal/domain,
recipient and operation; aliases of the same admission route share the same
scope. Do not persist bearer secrets as scope values. Browser and MCP retries
reuse an operation ID; Telegram uses its stable inbound update identity;
internal producers derive keys from their durable trigger identity.
Concurrent distinct requests are ordered by transaction acceptance, not by
client clock. Sequential sends that await acknowledgement preserve that order.

Replaying create after edit returns the existing revised turn; it never restores
the original text. Keep the original admission hash separate from revision
hashes. Durable idempotency tombstones must outlive any future history pruning.
Retention bounds are part of the implementation packet, not an excuse to delete
unconsumed requests.

Acceptance does not modify `BUSY`, `last_task_id`, `last_user_message`, or
the native backend session ID. Those describe active/last-executed work.
Expose queued count separately. In particular, stop must resolve the active
ledger row, not the most recently submitted ID.

Revision and withdrawal are conditional updates on
`id + queue_protocol + status='queued' + expected_revision`, with audit in
the same transaction. Revision can change bounded body/attachment references;
it cannot change recipient, source, Case, sequence, or scheduling authority.
Return 409 and a safe current summary on a stale revision/consumption race.
System-generated turns are not human-editable; their owning producer controls
withdrawal, and operator Case controls remain available.

Persist sender intent separately from the prepared execution prompt. At
activation, construct role/context/attachment payload once for the winning
revision, retaining prompt/metadata conventions from `process_task`.
Do not repeatedly prepend context on retries or run staging/backend work under
the DB write lock.

## 5. Activation, fairness, and routing

Use one gateway scheduler with a coalesced in-process event and a bounded
periodic fallback (initially 3 seconds). Events are hints; queued rows are
authority. Completion, admission, withdrawal and relevant session/Case changes
signal it. No process or task is created per waiting message.

For each pass:

1. Read a bounded snapshot of waiting rows and session heads using partial
   indexes. Eligibility/head/no-active filtering precedes the activation LIMIT.
   With the global waiting cap in §8, inspecting the entire waiting subset is
   bounded; do not scan completed history or all Cases.
2. A delayed/blocked head holds its own session, not unrelated sessions.
   Never skip an earlier human request to run a later one. Order eligible heads
   by acceptance timestamp plus stable ID; activate at most 25 per pass, one
   per session, yielding between small transactions.
3. Revalidate session open state, Case policy, authorization, producer
   preconditions, routing and config revision. Withdraw obsolete system intent
   with a reason. Leave temporarily ineligible intent queued with a reason.
4. Prepare expensive context outside the transaction against a versioned
   snapshot, then conditionally commit `queued -> pending`, immutable payload
   and activation timestamp. Retry preparation if relevant revisions changed.
   The transaction rechecks head and slot ownership; no file/network/model
   operation occurs inside it.
5. Fetch only routable pending rows. Claim must independently check carrier
   assignment/capability; filtering the poll response is insufficient.

Fresh session configuration at activation means model changes made while
queued apply to that next turn. It does not authorize moving a machine-local
native session to another node. Preserve existing pin/repin policy. An explicit
carrier assignment is needed because a gateway local worker and a standalone
daemon can share a hostname: a hostname alone is not exclusive ownership.
Preserve the current default local routing for unpinned gateway submissions
unless a separate placement policy is deliberately changed.

Local and remote execution claim the **same** managed row and use the returned
immutable payload. The current worker ignores the claim response and executes
the earlier poll snapshot; migrate that behavior. Local managed execution must
not call the legacy shadow enqueue/self-claim path again.

The in-memory `SessionTaskQueue` remains for legacy work while flag-off support
exists. Managed execution can use a bounded ID-only scheduling hint, but never
a second authoritative prompt copy. Waiting remote results must not occupy all
gateway execution slots: preserve bounded result reconciliation without one
local worker/polling coroutine per queued remote turn.

## 6. Execution ownership, completion, and recovery

Claim returns a fresh token bound to task, carrier process/incarnation and
session. A new worker endpoint authorizes start with a conditional
`claimed -> running` update for that token; only a successful start response
permits a backend call. All result/release/cancel acknowledgements use the token.

An exact repeated start request from the same live carrier process/token returns
the same authorization. The carrier has one invocation owner per attempt and
must not create a new executor on a start-response retry. A restarted carrier
may replay a persisted result but may not invoke using an old authorization.
Lost claim responses are resolved by looking up that process's ownership, not
by inventing a second task or token.

Record `started_at` as **start authorized**, not proof the model has seen the
prompt. A crash between this commit and invocation is inherently ambiguous.
An expired claim before start may be invalidated/reoffered atomically: its old
token can no longer pass start. Once start was authorized, no automatic replay
on lease age, heartbeat loss, shutdown, or registration.

A fencing token prevents stale DB updates; it cannot stop an isolated CLI from
writing files. After start uncertainty, hold `recovery_required` and require
carrier/backend reconciliation or explicit operator resolution with proof of
quiescence before releasing the session slot. A runtime timeout without a
confirmed stop is uncertainty, not a safe terminal boundary. The same applies
to `failed_node_offline`. Independent sessions continue normally.

Result commit must atomically:

- verify current token and allowed state; accept identical repeated results
  idempotently and reject/ignore superseded attempts;
- write canonical outcome and correctness-critical transcript/result fields;
- update native session ID, driver state and active identity with field-scoped
  predicates; preserve concurrent user model/settings changes and close state;
- transition the task terminal and release the session slot.

Then schedule notifications, telemetry, file mirrors, artifact enrichment,
Case evaluation and the next activation. Failure of those projections cannot
reopen execution or make an accepted result disappear. Result DB failure is
503; the carrier retains/retries its bounded result-delivery obligation.
Do not return “accepted” after a helper swallowed a write failure.

Persist each managed result in a bounded carrier-local spool before POST,
keyed by task ID and claim token, with atomic file replacement. Reuse the
gateway reconcile-spool pattern; this is a completed delivery obligation, not
queued execution intent. Replay on startup/after transient failures; remove
only after durable acknowledgement or an explicit stale-attempt receipt.
Reserve spool capacity before start and stop claiming when full. Oversized
results or disk failure must visibly hold reconciliation, not silently discard
results. The implementation packet fixes bounds and failure tests.

A protocol-1 completion must not subsequently be overwritten by legacy
`_dispatch_to_node` or `_mesh_complete_task` full-session saves. Extract the
existing outcome classification/cache/native-session logic into the shared
completion path rather than duplicate divergent implementations. Persist any
required retry/pause eligibility before the successor can start.

JSON session files remain recoverable mirrors and are never deleted.
Scheduling must not fall back to them when the canonical DB is unavailable.
File writes happen after commit; rebuilding a mirror is safe, accepting work
against a stale mirror is not.

Startup reattaches owned executions and reconciles results in bounded batches.
Native autonomous SDK turns need a driver-level idle/ownership contract:
the post-hoc proactive sink is not such a contract. Until proven for a backend
mode, do not enroll sessions capable of unsolicited concurrent execution.
This is a named rollout gate, not an unsupported claim that a DB index controls
the SDK stream.

For managed SDK calls, an occupied `_SDKSession.send` lock must return a typed
ownership conflict without `cancel_inflight`. Prove that native background
results cannot fulfill the wrong explicit request. Default Claude SDK support
is a required build gate; excluding all Claude sessions or silently disabling
background functionality does not satisfy this design. Verify SDK lifecycle
semantics with installed source and deterministic fake-stream tests during
carrier integration. If no safe contract exists, record a concrete blocked
gate instead of claiming an executable guarantee.

Provide an operator recovery-resolution operation requiring the current task
and token plus a recorded authenticated carrier observation of backend
quiescence, or a durable terminal result. Free text, offline status or a new
incarnation alone is insufficient. Resolve conditionally to the observed
terminal outcome (cancelled/failed if quiescent without a result); never
implicitly replay that prompt. An explicit retry creates a linked new request.
If quiescence cannot be established, retain the hold and show missing evidence.

## 7. Producer policies and closure

Reuse existing Case generation, round caps, pause policies, role boot,
wait-group satisfaction and heartbeat ownership. The turn scheduler must not
reimplement those evaluators.

| Producer | Admission identity and activation policy |
| --- | --- |
| Human / authorized agent instruction | Never coalesce; FIFO; recheck recipient and Case permissions. Ordinary instruction failure releases the slot only when the backend is quiescent. Existing recovery/approval policy may hold successors. |
| Case continuation | Case + generation maps durably to a turn ID. May be accepted while Manager is busy, but activation rechecks unresolved presented work. Withdraw if an intervening human turn already reviewed it or Case/Manager binding changed. |
| Watched-job notification | Reuse the existing watched-job completion owner and current Case attachment. One notification identity; do not also invent a second wake for the same obligation. |
| Quota/transient retry | Tie to failed task + pause identity + attempt, preserving exact failed prompt and existing budget guards. Retry only if the pause is still current and no intervening successful/manual recovery superseded it. |
| Cache heartbeat | Opportunistic idle-only work with deadline. Do not queue stale heartbeat turns behind real work; skip/expire when useful work exists. Revalidate owner, cache evidence and quota at activation. |
| Manager respawn | Keep the existing Case-level lease, approval, role and reconstruction path. Persist new-session/turn linkage; do not treat it as sending to the closed old session. |
| Compaction | Serialize as a context-changing operation; preserve native backend behavior and report its own outcome. |
| Native proactive output | Audit an already-produced result; not a new inbound prompt. Requires the driver safety gate in §6. |

Case scheduling tokens can remain in `mesh_tasks` with protocol 0 and NULL
session IDs. In one transaction link token/trigger to its deterministic
protocol-1 turn; retries discover that same ID. A task created after a token
claim must not get a new random ID on each crash retry. Finalizers become
restart-reconcilable from durable links/results; an in-memory
`asyncio.create_task(_finalize_...)` cannot be the sole completion mechanism.
Coalesce keys cover open work; durable idempotency covers completed delivery.
Count rounds/retry attempts once at the existing semantic boundary, not once
per HTTP retry or scheduler pass.

Keep strict acceptance FIFO for real instructions. Do not solve retry priority
by silently inserting an exact old prompt after newer user work. Hold the
session under its existing pause policy; if operator recovery supersedes that
pause, invalidate the automatic retry. Withdraw obsolete optional automation,
not real instructions, to remove head-of-line blockage.

Concrete retry rule: after failed A's automatic pause is eligible to end, if
real instruction B was accepted before any retry, atomically supersede A's
retry obligation, clear only that producer's eligible pause and leave B as
head. Do not append R behind B while the same pause blocks B. Otherwise admit
R as head and allow it through its own eligible automatic pause only. All
other holds still apply. Clear/replace that pause at R's terminal commit, not
at enqueue. A later B stays behind R under FIFO. Approval or a future
quota/backoff deadline is never cleared by this rule.

Close/interrupt must race safely with both admission and activation: persist
the authoritative closed/blocked state and withdrawal of matching queued
turns in one transaction, checked by both writers. Current `close_case`
spans multiple calls; it needs a transaction-aware seam, not a new endpoint
calling several best-effort helpers.

For an already pending/claimed/running turn, closure uses cancellation/close
ordering and retains ownership until quiescent. Never delete rows. Do not
auto-withdraw unrelated operator work because it shares a recipient: Case
association and source are explicit. A queued request scoped to a closed or
blocked Case cannot activate regardless of source. Session closure withdraws
all its waiting work and refuses new work. “Stop active” cancels that turn;
queued work stays visible but paused until explicit resume/send-next, so stop
does not immediately launch the next queued instruction.

## 8. Service boundary and pressure limits

Per-session serialization does not protect the HTTP server from 100 concurrent
requests. Apply these initial limits to the new admission service; calibrate on
the Pi before enabling broadly.

| Boundary | Initial requirement |
| --- | --- |
| Waiting capacity | Reuse `config.system.max_queue_size` (currently 50) as the fleet-wide managed queued + pending cap, plus 20 per session. Enforce atomically; edits cannot increase total byte budget beyond its cap. Legacy enrollment must not double the advertised budget. |
| New-route input | 16 KiB UTF-8 body text; at most 8 bounded attachment references. Cap the entire JSON request at 256 KiB including carry context and metadata. Validate all strings/collections, not just prompt. |
| Compatibility route | Preserve existing 262144-character prompt and 48000-character carry limits; add a 2 MiB serialized request ceiling, covering worst-case UTF-8 plus bounded metadata. Do not silently truncate existing accepted prompts to 16 KiB. |
| Stored waiting intent | At most 2 MiB per row and 100 MiB fleet-wide using persisted byte accounting. This includes prompt/payload copies, not only the visible body. Prepared context has its own finite cap; no streamed output in queue reads. |
| Admission concurrency | At most 4 executing queue mutations across control/agent/system ingress; reject excess promptly with structured 429 + Retry-After. Separate bounded carrier lifecycle capacity so ingress cannot exhaust result/heartbeat handling. |
| Time | Queue mutation has a 5-second total lock/DB deadline with bounded lock acquisition and remaining-time SQLite busy timeout. Do not call the existing 60+ second retry path unchanged. Body-read deadline 5 seconds. |
| Reads / scheduler | Read summaries 50/page, max 100; preview at most 2 KiB per row, full body only for one-item read/edit. Activation max 25/pass; bounded outstanding executor submissions. |
| Agent fanout | Initial 30 admissions/10 minutes/Case/sender, with stricter existing automation budgets. Enforce server-side against validated identity, not a caller-supplied source string. |
| Revision history | At most 20 edits/turn, bounded audit fields; no unlimited write amplification by repeatedly editing one queued item. |

Memory at N=100: reject before parsing large bodies or creating 100 executor
jobs. Four largest compatibility requests are at most 8 MiB raw bytes, plus
bounded JSON/validation copies, four worker-thread DB connection caches
(currently ~8 MiB each), and existing process baseline. This is a bounded input
estimate, not a measured RSS guarantee. Measure peak RSS and control-plane
latency in the load gate; do not claim 100 × 16 KiB is the total process cost.

Use streaming byte counting before JSON parsing, also for chunked bodies;
Content-Length and Pydantic checks alone do not bound an incoming read.
The middleware/service capacity budget must cover the admission lanes even
though control API and task server run on different event loops. A bounded
executor/timed DB-lock seam is acceptable; another persistent queue is not.
Timeout cancellation must not release an executor permit while its thread is
still running. If a response is lost after commit, idempotency resolves the
outcome.

Explicit service-boundary closure checks:

- **Concurrency:** bounded ingress and scheduled worker handlers, separate
  lifecycle capacity, atomic session ownership. Session uniqueness alone fails
  this requirement.
- **Memory:** whole-request, waiting byte/count, revision and read-page bounds;
  measured RSS under 100 concurrent callers before broad activation.
- **Request size:** pre-parse byte limits and strict models; reject malformed,
  truncated, unknown-source and invalid-reference payloads structurally.
- **Timeout:** finite body/lock/DB time; no backend/network operation in a
  transaction. Backend execution and uncertain shutdown use §6.
- **Backing failures:** queue enrollment/consumption fails closed if DB or
  required schema is unavailable. API returns 503, preserves drafts and
  emits no acceptance event. Offline workers leave their head recoverable;
  failed notifications do not change durable truth.

DB reads **and writes** from async loops go through bounded offload.
Do not instantiate unbounded threads/SQLite caches or poll every Case to find
queue work. Reuse existing metrics for admission/activation latency, oldest
head age, rejection count, recovery holds and control-plane health; no noisy
per-poll logging. Telemetry separation remains a separate measured optimization,
not a prerequisite or a change this design silently makes.

## 9. Sender API and frontend

Use existing authenticated Control API infrastructure and React Query/SSE
patterns. New endpoints:

| Route | Result |
| --- | --- |
| POST /api/sessions/{id}/turn-requests | 202 after durable admission; idempotent replay returns the same ID and current summary. |
| GET /api/sessions/{id}/turn-requests | Cursor-bounded queued/active summaries, revision and blocked reason. |
| GET /api/turn-requests/{id} | Authorized bounded full intent for inspection/edit. |
| PATCH /api/turn-requests/{id} | Queued edit with required If-Match revision; 409 on conflict. |
| POST /api/turn-requests/{id}/withdraw | Conditional withdrawal; auditable, never deletion. |
| POST /api/sessions/{id}/turn-requests/pause or /resume | Persist operator queue hold/resume; resume does not override recovery, Case, quota or approval gates. |
| POST /api/turn-requests/{id}/resolve-recovery | Operator-authorized resolution using recorded carrier evidence and current ownership; 409 for insufficient evidence. |
| POST /api/instructions | Compatibility shape/status preserved (`ok`, `task_id`, `session`); enrolled session uses the same admission service. |

Return stable `turn_id`/legacy `task_id`, status, revision, acceptance time and
queue position as a snapshot, not a guaranteed start time. Source is
server-derived. Shared dashboard bearer credentials currently identify a trust
domain, not individual users; v1 operator edits are domain-authorized, not
falsely described as author-only.

Finish the agent sender path in this milestone: expose a bounded
`send_instruction(target_session_id, body, operation_id)` tool through the
existing MCP/tool filtering and gateway transport, reusing
`dispatch_worker(session_id=...)` where its role allows it. Add it explicitly
to authorized worker and Manager tool sets; a web endpoint alone does not
deliver “agents can send to agents.” Check Case membership, role and recipient
at the server, not just in the tool wrapper. This milestone includes a narrow
session-bound send credential provisioned through the trusted carrier/boot
path, with revocation on session close/Case membership change. Derive sender
from the credential, not request fields. Shared admin bearer credentials remain
explicit operator authority, not scoped agent identity. This is a send
capability, not an A71 per-node credential rewrite or a sandbox against agents
that can already steal host admin secrets. The packet fixes issuance,
provisioning, storage and validation details.

Attachment references must survive queue wait, restart and revision.
Current staged files need explicit reference retention/ownership validation;
path syntax alone is not authorization. Fetch remotely before start, with
existing staging limits/timeouts, outside queue transactions.

Keep chat-first UI:

- Existing send-while-running behavior now reconciles optimistic cards by
  durable task ID into “Next up.” Refresh retains accepted requests.
- Queued, starting, working and recovery-required have distinct labels.
  Pending/claimed text is not evidence the model consumed it.
- Show source, queue count and blocked reason. Edit/withdraw only queued human
  items; refresh on 409.
- Keep active-turn stop separate from withdrawal and from pause/resume queue.
- Use `useSessionTurnQueue` and `["session-turn-queue", sessionId]`; preserve
  telemetry `useSessionTurns`/`session-turns` and `/api/turns`. Extend post-commit
  invalidation and reconnect resync with existing `SAFETY_NET_MS` (60 seconds)
  as the UI fallback. Scheduler fallback (3 seconds) is separate. No WebSockets.
- Update `task_state_truth.py`, session timeline/transcript, session list and
  stale-BUSY repair together. Current helpers know pending/claimed only;
  “running” must not be mistaken for an orphan.
- Filter/control queued cards by ID so pending prompts and terminal transcript
  exchanges never duplicate under reload/SSE races. Preserve draft, keyboard,
  accessibility, attachment and carry-context behavior.

## 10. Incremental implementation and rollout gates

This affects ownership across layers; an admission-only live rollout is unsafe.
Split into reviewable packets, keeping enrollment disabled until all safety
dependencies are ready. No backend transport rewrite is required.

1. **Schema and strict DB primitives.** Add protocol fields, scoped indexes,
   revision audit, idempotency and transaction-aware session/Case updates.
   Test migrations against duplicate legacy active rows and control commands.
   No default-on global index or reinterpretation of existing rows.
2. **Carrier ownership and atomic completion.** Implement claim/start/result
   fencing, strict local claim, bounded worker scheduling, session reconciliation,
   control-command exceptions and recovery holds. Capability-advertise protocol
   support. Legacy workers must not receive managed rows.
3. **Admission and fair scheduler.** Route session-scoped entrypoints through
   the common service, preserve policy/context preparation, apply pressure
   bounds and activate only eligible heads. Keep stateless one-offs on the
   existing path initially; converting them is not needed for session FIFO.
4. **Producers and backend coverage.** Convert Case/job/retry/heartbeat/respawn
   final delivery with durable linkage and recovery; serialize compaction.
   Prove or gate native unsolicited SDK work. Exercise local Claude/Codex and
   supported OpenCode modes, plus remote carriers.
5. **Surfaces.** Add queue truth and edit/withdraw UI, MCP sender support and
   server authorization; preserve Telegram and compatibility response semantics.
   Editing can ship after safe FIFO delivery; it must not delay fixing ownership.
6. **Enrollment.** Feature defaults OFF. Start with `MESH_ENABLED=true` and
   canonical DB available; `MESH_ENABLED=false` remains unchanged. Persist a
   per-session protocol enrollment marker. Enroll only quiescent sessions with
   no legacy queued, pending, claimed or native unsolicited work, using the
   same admission exclusion that prevents a new legacy arrival racing cutover.
   All producers for an enrolled session must use protocol 1. Reject
   enrollment if any required capability is missing.
7. **Controlled validation.** Only after local test/load gates, use an
   operator-approved noncritical session on local and remote carriers. Worker
   restart/deployment requires surfacing to the operator; this review does not
   authorize it. Test waiting restart, acknowledged-result restart, uncertain
   start and carrier outage without replaying side effects.
8. **Rollback.** Disabling new enrollment must keep the protocol-1 consumer and
   recovery/read surfaces alive for existing rows. Pause new admission, drain
   or explicitly withdraw waiting work, reconcile all active/recovery holds,
   then remove enrollment. Do not downgrade to a binary that ignores managed
   rows. Keep legacy `SessionTaskQueue` until no required legacy path uses it.

Feature flags are admission policy, not permission to abandon durable work.
Changing all mesh tasks or introducing a broker is unnecessary for this scope.

## 11. Required verification matrix

Tests precede each implementation increment where feasible. Use repo .venv,
targeted tests and temporary databases/fake carriers; no paid backend/full e2e
runs by default.

**Migration and DB invariants**

- Legacy duplicate same-session pending/claimed rows survive schema migration.
  Active turn + close/cancel coexist; sentinel leases stay out of uniqueness.
- 100 concurrent submissions with the production per-session cap yield exactly
  the allowed admissions and structured rejections; no lost acknowledged IDs.
  A separate raised-cap fixture tests 100-item ordering. Concurrent retries of
  one key create one row even at capacity.
- Key/input conflict, replay after edit/withdraw/close/restart, and revision
  races preserve identity. Revision audit commits or rolls back with the edit.
- Activation races allow one session owner; a delayed/blocked oldest prefix
  does not starve unrelated sessions. EXPLAIN QUERY PLAN and a large completed
  history fixture verify the nonterminal/indexed access path.

**Ownership and failure boundaries**

- No execution after failed claim/start; carrier affinity/capability checked at
  claim; worker uses claim response, not stale poll data.
- Old incarnation/token result cannot overwrite a new attempt. Identical result
  retries succeed without duplicate session/Case side effects.
- Crash before start can safely re-offer; crash after start authorization holds
  recovery. Partition + old worker still executing never activates successor.
- First remote result commits native session ID before the second turn snapshot.
  Concurrent model change/close survives completion and JSON mirroring.
- Stop, close and cancellation remain usable at capacity; queued work does not
  auto-launch immediately after stop. Compaction cannot overlap a turn.
- Repeated polling while all slots are occupied creates bounded handlers with
  no duplicate `_active` bookkeeping loss. Remote waiters do not starve local work.

**Producer and integration behavior**

- Busy Manager receives one durable continuation; intervening review makes it
  obsolete. Token claim/admission/finalizer crash boundaries recover once.
- Job notification attaches to the right Case with no duplicate wake.
- Retry is invalidated by superseding recovery; heartbeat expires behind useful
  work; Case/session closure races admission and activation correctly.
- Local/remote Claude, Codex and supported OpenCode execution plus native
  proactive mode satisfy the ownership gate or refuse enrollment explicitly.
- Agent tool can queue multiple authorized same-Case instructions; forged sender,
  cross-Case and closed-recipient requests fail on the server.

**Pressure, UI and rollback**

- 100 callers with maximum bodies, chunked oversized bodies and a held SQLite
  write lock: finite response time, bounded threads/RSS and no accepted loss.
  Verify excess 429/DB 503, heartbeat/result responsiveness and caller timeout
  after a successful commit resolved by retrying the same key.
- Queue/read summaries remain bounded; attachment retention and carry context
  survive restart. UI pending/active/terminal reconciliation has no duplicate
  bubbles; drafts, conflicts and accessibility remain usable.
- Capability mismatch, flag-off, mixed legacy sessions, quiescent enrollment,
  disabling new enrollment and safe drain/rollback all have explicit tests.

## 12. Review verification and remaining evidence

This review inspected the actual admission, shadow-dispatch, claim/result,
reaper, session-save, producer, worker polling and composer code. It ran the
existing targeted suites:

```bash
.venv/bin/python -m pytest tests/test_mesh_enqueue_affinity.py tests/test_claim_reaper.py tests/test_task_state_truth.py --tb=short -q
```

All passed. These establish current behavior only; they do not validate the
proposed implementation. A disposable in-memory SQLite 3.40.1 reproduction
confirmed the original unscoped active-session index fails on a legitimate
claimed turn plus pending cancellation for the same session. Partial indexes
are supported locally; deployment migration/capability tests remain required.

No production DB mutation, backend invocation, service restart, worker
deployment or load test was performed. Capacity numbers above are initial
bounded policy, not measured Pi throughput. Implementation must meet the
explicit test and rollout gates before this design's delivery promises are
advertised.

The architectural choice remains one ledger, one accepted turn ID and one
session execution owner. The principal work is making existing ownership and
completion paths authoritative; another queue would leave those defects in place.
