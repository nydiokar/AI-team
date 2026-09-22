# Session Turn Queue — Unified Durable Delivery Design

**Status:** proposed design; no implementation in this document  
**Date:** 2026-09-22  
**Decision requested:** approve the architecture and split it into implementation packets

## 1. Decision in one sentence

Make `mesh_tasks` the **one durable queue and execution ledger for every agent
turn**, adding a durable pre-dispatch `queued` state and per-session FIFO
activation. Do not create a second task/message queue and do not use a broker
yet.

An instruction that is meant to make an agent act is a **turn request**. It is
not distinguished by whether its author is a human, another agent, or an
internal continuation mechanism. Every turn request follows the same lifecycle:

```text
accepted / editable queue item
       -> activated for its target session
       -> claimed by the target carrier
       -> running in the backend
       -> terminal result
```

The design deliberately keeps a separate future concept for an informational
peer message that should *not* spend a model turn. That is an inbox/audit
object, not a competing execution queue. A peer message that should cause a
next turn creates a turn request and therefore uses this queue.

## 2. Product intent and non-negotiable principles

### The user-visible promise

When a session is working, a sender can still submit another instruction. The
gateway accepts it durably, shows that it is waiting, and starts it only after
the current turn has reached a safe terminal boundary. Until the queue item is
activated, its author can revise or withdraw it. A restart, a node outage, or a
duplicate HTTP retry must not lose or duplicate the instruction.

The same promise applies to:

- a person sending a follow-up to a busy worker or Manager;
- a Manager or worker targeting another existing session in a permitted Case;
- the Wake Dispatcher returning completed-worker information to a Manager;
- quota/transient retry and crash-respawn continuation turns;
- a future peer-message delivery that explicitly asks for a model turn.

### Governing principles

1. **One intent, one ledger, one active turn per session.** `mesh_tasks` is the
   canonical record from acceptance through result. There must never be a
   parallel “message queue” that needs to be reconciled with a “task queue.”
2. **A turn is non-interrupting.** The queue never writes into an active SDK,
   CLI, app-server, or PTY turn. A queued prompt becomes the next ordinary
   backend turn only after the prior turn is terminal.
3. **Durability before acknowledgement.** The API replies “queued” only after
   one short SQLite transaction commits. Live SSE, polling wake-ups, browser
   notification, and scheduler signals are acceleration only, never authority.
4. **Exact-once admission; at-least-once activation; never concurrent session
   execution.** Retried requests return the original turn. Crashes may retry an
   unstarted dispatch, but a database-enforced session lease prevents competing
   turns on the same session.
5. **Mutability stops at consumption.** A `queued` item can be edited or
   withdrawn. The transition to `pending` is the consumption boundary: after
   that it may already be on a remote worker, so it is immutable. A sender can
   still request ordinary task cancellation, but cannot silently rewrite it.
6. **Messages do not grant authority.** A queued peer-originated instruction is
   still untrusted content and goes through the recipient's normal role/tool,
   Case, approval, cost, and admission controls. It does not merge, approve,
   release, close, or otherwise mutate state merely by arriving.
7. **Keep the control plane cheap.** Queue admission, revision, withdrawal,
   activation, claim, and terminal state writes are indexed O(1)/bounded SQLite
   transactions. They never synchronously run a backend, wake a worker over the
   network, project telemetry, rebuild a transcript, or scan all Cases.

## 3. Current state and root cause

The repository already has valuable primitives, but they do not form the
promise above.

| Existing component | What it does now | Missing for a safe next-turn queue |
| --- | --- | --- |
| `mesh_tasks` in `src/control/db.py` | Durable worker dispatch/result ledger: `pending -> claimed -> completed/failed`; task ID, prompt, session ID, node routing, claim/recovery, results | No pre-dispatch editable state, queue position, durable idempotency key, or rule that only one same-session row can be active |
| `SessionTaskQueue` | In-memory gateway queue; skips an already-owned **Codex** session so other sessions can continue | Not durable; its per-session rule is Codex-only; it cannot coordinate remote workers or provide API-visible edit/withdraw state |
| remote worker poller | Fetches a batch of pending `mesh_tasks`, starts concurrent handlers, then claims each row | Tracks in-flight sessions only for close-session deferral; does not defer a second same-session turn |
| `POST /api/instructions` | Marks a target session busy and creates a task immediately | “busy” is not an admission lease; no durable waiting state exists and the status is written before execution begins |
| M3.4 Wake Dispatcher | Coalesces satisfied Case wait-groups and, when a Manager is `AWAITING_INPUT`, calls `submit_instruction` | Correctly avoids mid-turn interruption, but only for Case waits and it bypasses a universal turn queue |
| web composer | Has idempotency keys, optimistic delivery state, a transcript, polling, and a running indicator | Cannot display server-authoritative queued items, revise them, withdraw them, or send freely while a turn is running |

The underlying defect is therefore structural: the system has a durable
**execution** ledger and an ephemeral **pre-execution** queue, but no durable
session-serialized admission-to-execution lifecycle shared by all backends.

## 4. Alternatives considered

### A. New `session_turn_queue` table plus existing `mesh_tasks`

This is superficially small: put drafts in a new table, then create a
`mesh_tasks` row at delivery. It is rejected.

It produces two authoritative records for one instruction, requiring an
outbox-like handoff, reconciliation after crashes, duplicate suppression across
both tables, two status models, and a permanent answer to “which row is the
real task?” It would recreate the synchronization risk this design is intended
to remove.

### B. Keep the in-memory queue and add UI editing around it

Rejected. It loses accepted work on a gateway restart, cannot arbitrate between
the gateway and remote node workers, and exposes no durable truthful state.

### C. Add Redis Streams, NATS JetStream, or another broker first

Rejected for this milestone. A broker carries events; it does not define
editable state, per-session ordering, authorization, transcript linkage, or
the single active-turn invariant. It would add operational state, credentials,
deployment, monitoring, redelivery rules, and another source of failure before
the product semantics have been proven. SQLite is already the canonical
control-plane database and is sufficient for the bounded, single-gateway fleet
today.

### D. Evolve `mesh_tasks` into the unified turn ledger

**Chosen.** The table already identifies the same real-world object: one
instruction/turn with a task ID, target session, routing, result, artifact, and
transcript fields. Add the missing pre-dispatch and scheduling semantics to
that object. The current in-memory queue becomes an implementation detail to
remove after migration, not a second system to preserve.

## 5. Canonical model

### Terminology

- **Turn request:** a durable request for one target session to receive one
  ordinary next prompt. This is the queue object and the existing task ID.
- **Turn:** a turn request after it starts backend execution, plus its result.
- **Peer message:** future durable information intended for reading/audit. It
  only becomes a turn request when policy explicitly requests delivery as a
  next turn.
- **Activation:** the atomic `queued -> pending` transition that consumes a
  mutable request and makes it eligible for a carrier. It is not a model call.
- **Carrier:** the gateway local worker or a mesh worker node that runs the
  backend for the session.

### `mesh_tasks` lifecycle

Existing terminal values remain meaningful. Add one pre-dispatch state and make
the transition contract explicit:

```text
                         PATCH / withdraw allowed
                                      |
                                      v
  API/MCP/system ---> [queued] ---> [pending] ---> [claimed] ---> [running]
                         |              |              |              |
                         +--> withdrawn +--------------+--------------+
                                                        |
                                  completed | failed | cancelled | failed_node_offline
```

- `queued`: committed intent. It has a target session and queue sequence but
  is not visible to a worker claim scan. Editable/withdrawable.
- `pending`: activated, immutable, and routable to the selected carrier.
  Existing worker polling continues to use this state.
- `claimed`: atomically leased to one node. A failed/dead incarnation releases
  it according to the existing claim reaper.
- `running`: the carrier has crossed the backend-call boundary. This transition
  is recorded before invoking the backend, not inferred from a live event.
- `withdrawn`: an unconsumed request was intentionally removed. Retain it for
  audit and idempotency; never delete the row.
- terminal execution states retain the current semantics. A cancelled queued
  request is `withdrawn`; a cancellation after activation is `cancelled` when
  the existing backend cancellation path reaches a terminal result.

`pending`, `claimed`, and `running` together hold the per-session active slot.
They are deliberately separate so the UI and recovery code do not pretend that
a worker has started merely because a sender can no longer edit the prompt.

### Additive columns and indexes

Do not rename `mesh_tasks` or create a parallel queue table. Add a migration
with these nullable/default-safe columns:

| Column | Purpose |
| --- | --- |
| `queue_sequence INTEGER` | Strict FIFO order within `session_id`; `NULL` on legacy rows |
| `turn_source TEXT NOT NULL DEFAULT 'legacy'` | `web`, `telegram`, `manager_tool`, `case_continuation`, `quota_resume`, `transient_retry`, `peer_delivery`, `system`, `legacy` |
| `turn_kind TEXT NOT NULL DEFAULT 'instruction'` | Product/audit label: `instruction`, `continuation`, `retry`, later `peer_delivery`; never used to grant authority |
| `idempotency_key TEXT` | Durable replay protection for admission requests |
| `revision INTEGER NOT NULL DEFAULT 1` | Optimistic concurrency for edit/withdraw |
| `not_before TEXT` | Optional delayed eligibility; supports bounded retry/backoff without a second scheduler |
| `activated_at TEXT`, `started_at TEXT` | Truthful user-visible lifecycle timestamps |
| `coalesce_key TEXT` | Bounded internal deduplication for automation only |

Keep existing `flow_run_id`, `prompt`, `payload`, routing, claim, result, and
artifact fields. `flow_run_id` associates a Case-scoped turn without requiring
a second Case queue.

Required indexes:

```sql
CREATE INDEX idx_mesh_turns_queued_due
  ON mesh_tasks(status, not_before, created_at)
  WHERE status = 'queued';

CREATE INDEX idx_mesh_turns_session_order
  ON mesh_tasks(session_id, queue_sequence, status);

CREATE UNIQUE INDEX idx_mesh_turns_one_active_session
  ON mesh_tasks(session_id)
  WHERE session_id IS NOT NULL
    AND status IN ('pending', 'claimed', 'running');

CREATE UNIQUE INDEX idx_mesh_turns_idempotency
  ON mesh_tasks(idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX idx_mesh_turns_active_coalesce
  ON mesh_tasks(coalesce_key)
  WHERE coalesce_key IS NOT NULL
    AND status IN ('queued', 'pending', 'claimed', 'running');
```

The active-session unique index is a backstop, not the scheduler algorithm. It
makes a missed application-level check fail closed rather than allow two turns
to steer the same SDK/CLI/app-server session.

For a session, `queue_sequence` is allocated inside the same short
`BEGIN IMMEDIATE` write transaction as insertion using the indexed latest
sequence. The current `MeshDB._write()` mechanism already serializes local
writes and SQLite provides the cross-process write lock. This is bounded by the
per-session queue cap, never a scan of task history.

## 6. End-to-end flow

### 6.1 Human/API submission

```text
Composer / Telegram / future peer tool
  -> authenticated Control API
  -> validate input, session, Case/role policy, idempotency key, queue limits
  -> one DB transaction: insert mesh_tasks(status='queued', sequence=N)
  -> commit
  -> HTTP 202 {turn_id, status, revision, queue_position}
  -> non-authoritative scheduler signal + UI invalidation event
```

The Control API must not mark the session `BUSY` at acceptance. A queued item
is not executing. The session becomes `BUSY` only in the same path that makes
the item active/starts it. This removes the current dishonest `BUSY` state when
work is merely waiting.

The old `POST /api/instructions` remains a compatibility facade during the
rollout. For a session-scoped request with the new flag on, it calls the same
enqueue service and returns the same `task_id` field, now meaning the durable
turn ID. New clients use the explicit session-turn routes below. Stateless
one-off work can use the same table with `session_id=NULL`, becomes immediately
`pending`, and is deliberately not editable because it has no persistent
recipient conversation.

### 6.2 Revision and withdrawal

```text
author reads queued turn {turn_id, revision=3}
  -> PATCH with expected revision 3
  -> UPDATE ... WHERE id=? AND status='queued' AND revision=3
  -> revision=4, prompt/payload updated

author withdraws with expected revision 4
  -> UPDATE ... WHERE id=? AND status='queued' AND revision=4
  -> status='withdrawn', revision=5
```

Both responses use `409` for a stale revision or a no-longer-queued item and
return the current safe summary. They never overwrite a prompt that the
scheduler may have activated. An edit is a revision of the same task ID so the
UI, audit trail, and idempotency identity remain stable; the prior body is kept
in an append-only lightweight `task_events` audit event or revision payload,
not silently discarded.

### 6.3 Activation and routing

The gateway owns a small `TurnScheduler` loop, started only when
`SESSION_TURN_QUEUE_ENABLED=1`. It has an in-process `asyncio.Event` for fast
wake-up and a short periodic fallback for recovery. The event is not durable;
the `queued` rows are.

On each bounded pass, it asks `MeshDB.activate_due_turns(limit=25)` to do the
following in a short transaction for each eligible head item:

1. Select only `queued` rows with `not_before IS NULL OR <= now`, using the
   queued-due index. Never scan all Cases, all sessions, or completed history.
2. Verify the row is the lowest non-terminal `queue_sequence` for its session.
3. Verify no row for that session is `pending`, `claimed`, or `running`.
4. Read the fresh session routing/configuration source required for dispatch
   (current backend, node affinity, model, role/CWD). This preserves the
   existing promise that a model change applies on the next turn; do not freeze
   mutable session configuration at queue acceptance.
5. Build the normal execution payload, set routing and `activated_at`, then
   update that row from `queued` to `pending`.

The `UPDATE` predicate repeats the status/head/active-slot conditions. If the
unique active-slot index or predicate rejects a race, that scheduler attempt is
a no-op and the next bounded pass retries. No network call occurs inside this
transaction.

For a local target, the existing gateway execution worker claims the same
`pending` row before it builds/runs the `Task`. For a remote target, the
existing worker poller sees the same `pending` row and claims it through the
existing task server. There is one consumer protocol and one row, not a gateway
queue handing off to a node queue.

The current in-memory `SessionTaskQueue` is removed only after local execution
also claims from this ledger. Until then, the feature flag keeps the existing
path intact; it must not be used as a second authority for flag-on requests.

### 6.4 Carrier execution and completion

```text
pending row
  -> carrier atomically claims row (same session active index still holds)
  -> carrier records running before backend call
  -> SDK / Codex app-server / OpenCode receives normal next turn
  -> task result endpoint commits terminal row + session state
  -> after commit: scheduler signal, UI invalidation, existing notifications
```

The worker daemon must enforce this through the database claim contract, not
only its in-memory `_inflight_sessions` set. A node may fetch several rows, but
only an eligible head row for a session can claim. Different sessions remain
fully concurrent up to the existing node/gateway semaphore limits.

On terminal completion, the result write and session transition complete first.
Only then does a background/signal path wake the scheduler. The following
queued item starts as a new normal turn; it does not get appended into the
current backend stream.

### 6.5 Case continuation and system-generated turns

M3.4's Wake Dispatcher retains its useful semantics: condition-gated,
coalesced, leased, bounded by round cap, and Case-aware. Its final delivery
changes from direct `submit_instruction()` to `enqueue_turn()` with:

```text
turn_source = 'case_continuation'
turn_kind   = 'continuation'
flow_run_id = case_id
coalesce_key = deterministic case/generation key
```

The pre-existing continuation row/lease remains the Case-level deduplication
mechanism. The unified queue is the session-level delivery mechanism. Those are
different scopes and both remain necessary: Case logic decides *whether* a
Manager should be woken; the turn queue decides *when it is safe* to deliver.

Quota resume, transient retry, cache heartbeats, and respawn use the same
enqueue service, each with a deterministic idempotency/coalesce key and their
existing budget/round guards. They must never bypass it with a direct backend
call.

## 7. Public API contract

All routes require the existing authenticated Control API boundary. Payloads
use strict Pydantic models; field names below are conceptual, not a commitment
to exact JSON spelling.

| Route | Purpose | Result |
| --- | --- | --- |
| `POST /api/sessions/{id}/turns` | Enqueue a human turn | `202`, durable turn summary |
| `GET /api/sessions/{id}/turns?state=queued,pending&limit=50` | Bounded queue read model | queued/active turn cards with position and revision |
| `PATCH /api/turns/{turn_id}` | Edit queued body/attachments using `If-Match` revision | updated queued summary or `409` |
| `POST /api/turns/{turn_id}/withdraw` | Logical withdrawal using `If-Match` revision | withdrawn summary or `409` |
| `POST /api/instructions` | Compatibility facade | same task ID and response shape during migration |

The create response includes `turn_id`/legacy `task_id`, `status`,
`queue_position`, `revision`, `created_at`, and an acknowledgement that the
body is persisted. It must not promise an execution time.

Attachment references are immutable staged-file IDs/path references, never
inline arbitrary files in the queue row. A revision replaces the reference only
while queued; cleanup must retain files referenced by any non-terminal turn.

Authorization in the current dashboard is bearer-token scoped, not per-user.
For v1 that is consistent with the existing control-plane trust domain. A
future agent-origin API must additionally validate sender/recipient Case
affiliation and role before it may create a `peer_delivery` turn. Unknown,
closed, cross-Case, or unauthorized target sessions fail before any write.

## 8. Frontend design

The session detail page remains a chat-first view. Sending while a turn is
running is allowed. The composer’s button always means “queue this next turn,”
not “interrupt the current turn.” The stop control remains distinct and only
cancels active work.

```text
Completed conversation
  current assistant turn: Working…

Next up (2)
  [You] “After that, inspect the test failure.”  Queue #1  Edit  Withdraw
  [System] “Workers A and B finished; review results.”   Queue #2

[composer: Send next instruction…]
```

Required UI behavior:

- Add a durable `useSessionTurns` queue query and mutations for enqueue, edit,
  withdraw. Use the current React Query invalidation/polling pattern; SSE only
  causes an earlier refetch.
- Replace client-only optimistic sent bubbles as the authority. An optimistic
  card may appear immediately, but it reconciles by server `turn_id` and is
  replaced by the durable queue item on the `202` response.
- Show queued items as distinct “Next up” cards, not as completed transcript
  user bubbles. This prevents the UI implying the model has already seen text.
- Render the active `pending`/`claimed`/`running` request as “Starting” or
  “Working” with its stable task ID. The durable transcript continues to render
  completed exchanges as it does now.
- Show source honestly: `You`, `Manager`, `Worker`, or `System`; show a compact
  reason for system continuations. Do not expose a peer message body to a
  recipient before policy has delivered it as a turn.
- Permit edit/withdraw only on `queued` cards. On `409`, refresh the card and
  explain that it has already started delivery; offer the existing stop action
  only where appropriate.
- Preserve drafts and accessibility. The composer should say “Queue next
  instruction…” while a session is busy, retain keyboard send, and expose queue
  count/status to screen readers.
- A session list row gains `queuedCount` and a concise state such as
  `Working · 2 next`. It does not use a queued item to mark a session as busy.

No WebSocket is required. The existing 3-second polling model is sufficient for
truth and works after a missed SSE event. Add a small SSE event such as
`turn_queue_changed` only after the DB commit to reduce perceived latency.

## 9. SQLite, transport, and contention plan

### Why SQLite is sufficient now

There is one gateway-owned canonical DB, WAL mode, a bounded process write
lock, an existing retrying `BEGIN IMMEDIATE` write path, and a fleet already
using HTTP against the gateway rather than direct worker DB mutation. Queue
operations are short indexed writes; they are far cheaper than the telemetry
projection work that caused the documented past contention incident.

The queue must not repeat that incident:

- no polling/recomputing all Case event logs to find queue work;
- no synchronous transcript rebuild, notification fanout, wake call, or
  telemetry projection in queue write transactions;
- all async orchestration reads use `asyncio.to_thread` from the gateway event
  loop, as the Wake Dispatcher now does;
- activation has a fixed batch maximum (initially 25) and yields between
  batches; it never drains unbounded backlog while holding the write lock;
- telemetry remains lower priority and bounded as already designed;
- rows contain bounded text/references only, not artifacts or streamed output.

### Capacity and abuse bounds

Initial hard limits, made runtime-configurable only after the behavior is
measured:

| Limit | Initial bound | Reason |
| --- | ---: | --- |
| turn body | existing instruction maximum, capped at 16 KiB for new queue API | bounded DB/request/prompt cost |
| attachments | references only; 8 per queued item | no inline bulk data |
| pending queued turns per session | 20 | prevents a stale or malicious sender building unbounded future work |
| queue read page | 50, max 100 | bounded UI/API memory |
| activation batch | 25 | write-lock fairness |
| system coalesced wake requests | one active deterministic key | prevents worker-completion storms |
| future agent-origin rate | 30 requests/10 min/Case/sender, lower attention-wake cap | prevents ping-pong/cost storms |

At 100 concurrent admission calls, each performs one capped validation and one
indexed short transaction. SQLite serializes the writes rather than allowing
inconsistent session activation. With a 16 KiB body cap, 20 waiting turns per
session uses at most about 320 KiB of queued body text per session on disk; API
responses load at most 50 rows. Queue limits produce `429`/structured
`queue_full`, not silent dropping.

### Transport boundaries

```text
Browser / Telegram / MCP
      HTTPS + Idempotency-Key
             |
             v
Gateway Control API -- one DB transaction --> mesh_tasks
             |                                  (canonical)
             +-- after commit --> scheduler event / SSE invalidation
             |
             v
Gateway local carrier OR existing worker HTTP poll/claim/result protocol
             |
             v
Backend native session (one ordinary turn)
```

Workers continue to communicate only with the task server over authenticated
HTTP. They do not open SQLite directly. The transport protocol changes only to
make claim eligibility session-aware and to record `running`; it does not gain
a broker or a peer-to-peer data path.

### Broker promotion criteria

Do not introduce Redis/NATS pre-emptively. Reconsider only when measured
evidence shows one of these: multiple independent gateway writers need an
external coordination plane; SQLite queue write latency/backlog breaches a
documented SLO after telemetry is separated/fixed; durable fanout to many
independent consumers is required; or the mesh is no longer gateway-owned.
Even then, SQLite remains the canonical turn state and the broker is an
outbox/notification acceleration layer, never the editable source of truth.

## 10. Failure, recovery, and security semantics

| Event | Required behavior |
| --- | --- |
| HTTP retry/time-out after acceptance | durable idempotency returns the original turn, never creates another |
| API DB failure | `503`; do not report accepted or emit a live event |
| gateway restart while `queued` | row remains queued; scheduler rescans bounded due rows |
| gateway restart while `pending`/`claimed` | existing claim/incarnation recovery determines whether the same row is re-offered; no later same-session row activates first |
| node offline before claim | leave/recover the head request according to existing affinity/offline policy; do not skip it and run a later instruction |
| backend start result uncertain | retain the active lease and fail closed under existing backend recovery rules; never replay a possibly-started prompt automatically |
| concurrent edit vs activation | conditional revision/status update permits exactly one; loser receives `409` and fresh state |
| queue full/rate limit | structured rejection before write; caller retains its local draft |
| Case closed/blocked | withdraw queued Case-generated and peer-delivery turns for that Case in the authoritative close/interrupt transaction; ordinary operator turns without that Case link follow existing session policy |
| session closed | refuse new queue entries; close policy withdraws queued session entries and uses existing active-task cancellation/close ordering |
| peer prompt injection | delivered text is marked untrusted, bounded, attributable, Case-authorized, and cannot become an action without normal recipient tools/gates |

The queue service boundary checklist is mandatory before closure:

- **Concurrency:** atomic queue head activation plus partial unique active-session
  index; bounded scheduler batch.
- **Memory:** body/attachment/page/count caps as above; no inline files.
- **Request size:** Pydantic limits and HTTP content-length enforcement.
- **Timeout:** no backend/network work inside writes; worker delivery has its
  existing task timeout; scheduler fallback recovers missed signals.
- **Malformed input:** strict states/source enums, UUID/task ID limits,
  revision validation, session/Case authorization before write.
- **Backing failures:** DB unavailable is 503; worker unavailable leaves the
  durable head pending/recoverable; notification failure does not change truth.

## 11. Migration and rollout

This is a behavior change that must be feature-gated, not a big-bang schema
rewrite.

1. **Schema and pure DB helpers.** Add columns/indexes and helpers for enqueue,
   read, revise, withdraw, activate, claim, and terminal transitions. Existing
   legacy rows remain valid: their `queue_sequence` is `NULL` and current
   `pending` semantics are unchanged.
2. **Flag-off compatibility.** Add `SESSION_TURN_QUEUE_ENABLED`, default OFF.
   With it off, `POST /api/instructions`, local in-memory queuing, and worker
   polling remain byte-compatible.
3. **Flag-on admission only in tests.** Session-scoped instructions create
   `queued` rows. A deterministic test scheduler activates them; prove edit,
   withdraw, idempotency, and FIFO before touching live worker routing.
4. **Unify local and remote claim.** Move local execution to claim from
   `mesh_tasks`; change remote claim eligibility to respect the same session
   active rule. Remove `SessionTaskQueue` as authority for flag-on turns.
5. **Route internal producers.** Convert Case continuation first, then quota,
   transient retry, cache heartbeat, and respawn. Each conversion gets focused
   regression tests that prove no direct bypass remains.
6. **Frontend.** Ship the Next up read model and edit/withdraw actions while the
   flag is off against fixtures/tests, then enable it with backend activation.
7. **Controlled live validation.** Use one non-critical warmed session: send a
   long first turn, queue/revise/withdraw additional turns, restart gateway at a
   safe boundary, prove exact ordering on a local and a remote carrier. Do not
   restart a worker/node carrier without operator approval.
8. **Default-on decision.** Only after load/correctness evidence. Retire the
   legacy in-memory queue after no caller uses it for flag-on execution.

Each code packet belongs on `feat/session-turn-queue-*` with a PR under the
repository branch policy. The design itself is docs-only and belongs on `main`.

## 12. Verification matrix

Tests must precede each implementation increment where feasible.

### Database/concurrency tests

- 100 concurrent same-session enqueues produce one monotonically ordered queue
  with no lost IDs and no duplicate idempotency item.
- same-session activation races produce exactly one active row; different
  sessions activate concurrently.
- second same-session remote claim is rejected even when a node fetched both
  rows in the same poll batch.
- edit/withdraw wins only before activation; activation/edit races produce one
  winner and an honest `409` for the other.
- crash/release/reclaim preserves the head item and does not activate its
  successor early.
- deterministic system coalesce keys collapse only the intended active wake;
  human instructions never coalesce.

### API tests

- create/list/edit/withdraw authorization, validation, payload limits,
  idempotency after process restart, queue cap, stale revision, and `503` DB
  failure are all explicit.
- compatibility `/api/instructions` returns the same task ID as the queue item
  and never marks a merely queued session busy.
- Case/session closure handles queued Case-scoped items safely.

### Orchestrator/worker tests

- local Codex, local Claude, and remote Claude/Codex paths all obey the same
  single active session rule.
- a worker completion while Manager is busy queues exactly one coalesced next
  Manager turn; it does not interrupt the current turn.
- model/pin changes made while an item is queued are read at activation and
  affect that next turn according to existing session semantics.
- no internal producer calls the backend or raw `submit_instruction` directly
  once migrated.

### Frontend tests

- send while running yields a durable queued card; refresh/reload retains it.
- revision/withdraw states and `409` conflict handling are truthful.
- a queued card does not render as though the agent already received it.
- queue count/status remains correct under poll and SSE invalidation races.
- keyboard/accessibility behavior remains usable while a session is running.

### Live acceptance

Run a bounded, operator-approved end-to-end case with a Manager and at least
one remote worker. Queue two human instructions while the Manager is running;
edit the second; let a worker completion produce a continuation at the same
time; verify deterministic order, no active-turn interruption, no duplicate
delivery, truthful UI, and recovery through a gateway restart.

## 13. Relationship to future peer messaging

The prior peer-messaging investigation remains useful, but its inbox is not a
substitute for this queue.

```text
future peer transport
  informational message -> agent_messages / recipient state -> optional notice
  action-worthy delivery -> enqueue_turn(target_session_id, source=peer_delivery)

human / Wake Dispatcher / retry
  action-worthy delivery -> enqueue_turn(...) directly
```

This keeps the shared foundation exactly where it belongs: all **next-turn
delivery** is one unified `mesh_tasks` lifecycle. It avoids forcing every
informational message to burn tokens, and it avoids rebuilding delivery when
peer messaging is later approved.

## 14. Review questions for the next agent

An independent reviewer should challenge these points against the latest tree
before implementation:

1. Does every local and remote execution path actually claim a queue row before
   reaching its backend? Identify any bypass.
2. Can SQLite's supported partial-index/version behavior enforce the proposed
   active-session invariant on the deployment version? If not, replace it with
   an equally atomic guard, not an in-memory lock.
3. Does the actual session shadow-write model permit reading fresh routing/model
   data at activation without a stale overwrite race? If not, define one
   authoritative session snapshot seam before coding.
4. Are `flow_run_id` and Case close/interrupt paths sufficient to withdraw only
   the intended Case-scoped queued turns?
5. Does `pending` have any hidden external consumer that assumes it means
   “already running”? Preserve compatibility or explicitly migrate that
   consumer.
6. Are the row/text/attachment and scheduler batch limits appropriate for the
   measured Pi/SQLite environment? Validate with a focused contention test, not
   intuition.
7. Does the UI preserve the distinction between queued text, a started prompt,
   and a completed transcript exchange under a reload/SSE race?

## 15. Explicit non-goals for the first build

- No broker, Redis, NATS, MQTT, PTY injection, or backend transport rewrite.
- No free-form cross-Case peer chat or unbounded broadcast.
- No automatic execution of a sender's requested state-changing action.
- No streamed partial model replies; the existing whole-turn transcript model
  remains intact.
- No deletion of task/turn history to implement withdrawal.
- No always-on self-initiating agent loop. Case continuation remains bounded by
  its existing operator-started Case and round/cost gates.

## 16. Final verdict

Refactor the meaning of `mesh_tasks` from “a worker task after the gateway has
already decided to run it” to “the durable lifecycle of an instruction/turn.”
That is the least-disruptive design that still gives the desired full behavior:
queued delivery, mutation before consumption, durable recovery, safe
non-interruption, unified human/agent/system sources, and a clean future path
for peer messaging.

The implementation must be incremental and flag-gated, but the destination has
one queue, one task ID, one lifecycle, and one session-serialization invariant.
