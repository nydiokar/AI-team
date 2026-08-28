# Peer Messaging Transport Investigation

Status: design recommendation for A68, no implementation
Date: 2026-08-27

## Scope

AI-Team has a governed collaboration path today: a Manager opens a Case, dispatches workers, waits or arms a wait-group, reviews results, records a verdict, and closes or continues the Case. That is a policy layer. This investigation is about a lower layer: a durable communication primitive that lets agents send bounded, auditable messages to each other without granting them any new authority to mutate state.

The core distinction is load-bearing:

- A peer message may inform, ask, answer, summarize, attach context, or request attention.
- A peer message must not directly dispatch work, merge code, close Cases, approve spend, change flags, or release workers.
- State-changing actions still go through existing service boundaries: `submit_instruction`, `SessionService`, `WorkflowService`, Case review/close APIs, approvals, and Manager MCP tools.

## Current Substrate

The repo already has most of the lower-level pieces, but they are not assembled as peer messaging.

Existing durable substrate:

- `flow_events` in `src/control/db.py` is an append-only Case audit ledger. `append_flow_event()` writes compact JSON payloads and `list_flow_events()` reads ordered history.
- `FLOW_EVENT_TYPES` already reserves lifecycle, task, wait, review, approval, artifact, spec, and Case status events, but no `message.*` types.
- `record_review()` and `record_worker_wait()` show the established pattern: bounded MCP input -> authenticated Control API route -> orchestrator seam -> DB event write.
- `arm_wait_group()` and `compute_continuation_tick()` derive wake state from `flow_events`, then `_continue_case_once()` delivers a coalesced Manager turn through `submit_instruction`.

Existing live substrate:

- `GET /api/events/stream` in `src/control/control_api.py` is SSE over `events.ndjson`. It is a live push path, not durable state authority.
- `web/src/hooks/useEventStream.ts` deliberately opens one `EventSource` for the whole app and keeps a bounded rolling client log.
- `src/worker/agent.py` forwards remote `task_activity` signals back to the gateway through `/events/activity`, and the gateway re-emits them into the shared SSE feed.

Existing read substrate:

- `GET /api/sessions/{id}/messages` reads whole-turn transcript history from the DB task ledger. The frontend explicitly treats `message.delta` as post-v1 and does not splice SSE into durable chat history.
- `read_session_history` is a Manager MCP tool for bounded transcript reads.

What is missing:

- A durable message record with sender, recipients, scope, intent, body, Case/session/task context, and idempotency key.
- Per-recipient inbox/read state.
- A bounded `send_peer_message` / `read_peer_messages` tool pair.
- Delivery semantics: how an idle recipient sees the message, how a busy recipient avoids unsafe mid-turn interruption, and how duplicates are avoided.
- Governance controls: rate limits, payload caps, recipient ACLs, storm prevention, prompt-injection framing, and audit visibility.

Honest estimate: about 60-70% of the needed plumbing exists for a thin Case-scoped version. The missing 30-40% is the important part: durable recipient state and safe delivery.

## Prior Art

The A68 packet points at hcom. hcom's useful design shape is:

- Agents have identities, statuses, inboxes, transcripts, and event logs.
- Messages use scope and routing concepts: broadcast vs explicit mentions.
- Message envelopes carry intent such as request, inform, and ack, plus thread/reply metadata.
- Delivery is backed by hooks and a local SQLite database.
- Messages can be delivered mid-turn or wake idle agents.
- Subscriptions let agents react to status, file-edit, or other events.
- Collision detection is a first-class notification use case.

Borrow these ideas:

- Bounded message envelope: `intent`, `thread_id`, `reply_to`, `scope`, `mentions`, `sender`, `recipients`.
- Strict recipient resolution: unknown or ambiguous targets fail early.
- Read receipts / per-recipient cursor state.
- Subscriptions later, not in the first cut.
- Collision/status notifications as system-authored peer messages later.

Do not borrow these as-is:

- PTY injection as the canonical transport. AI-Team's durable system of record is the gateway DB and Case ledger.
- Agent-side authority to spawn, kill, or mutate workflow state through free-form messages.
- Local-only hook DB as the primary substrate; AI-Team already has a mesh DB and gateway-owned control plane.

For future broker-backed transport, Redis Streams and NATS JetStream both match the durable-log direction better than plain pub/sub. Redis Streams gives ordered append, consumer groups, acknowledgments, replay, and retention. NATS JetStream gives persistent streams, durable consumers, explicit acknowledgments, redelivery, and server-side filtering. Both are later infrastructure choices; neither is required for a first useful implementation.

## Candidate Designs

### Option A: Thin Case-Scoped Message Layer

Summary: add `message.sent`, `message.read`, and maybe `message.delivered` to `flow_events`; add bounded Manager MCP tools and Control API routes; reuse existing Case timeline and SSE invalidation.

Data model:

- No new table initially.
- Extend `FLOW_EVENT_TYPES` with `message.sent`, `message.read`, `message.delivery_failed`.
- Store compact payloads on `flow_events`:
  - `message_id`
  - `from_session_id`
  - `from_role`
  - `to_session_ids`
  - `scope`: `direct` or `case`
  - `intent`: `inform`, `request`, `ack`
  - `thread_id`
  - `reply_to_message_id`
  - `body`
  - `priority`: `normal` or `attention`
- Entity fields should identify the main target when possible: `entity_type='session'`, `entity_id=<recipient session id>` for direct messages; Case-level messages can leave the entity as `case`.

API/tool surface:

- `POST /api/cases/{id}/messages`
- `GET /api/cases/{id}/messages?recipient_session_id=...&since_id=...`
- `POST /api/cases/{id}/messages/{message_id}/read`
- MCP tools: `send_peer_message`, `read_peer_messages`, `ack_peer_message`.

Delivery:

- Reading is pull-based in v1: recipients call `read_peer_messages`.
- The Web UI sees timeline events through existing Case timeline and live SSE invalidation.
- Optional idle wake can be a second increment: if recipient is a Manager session in `AWAITING_INPUT`, deliver a coalesced proactive turn that says new peer messages are available.

Advantages:

- Minimal disruption.
- Uses existing DB, Case ledger, SSE, idempotency, auth, and review patterns.
- Strong audit story.
- Low infrastructure cost.

Challenges:

- Per-recipient read state is awkward in append-only `flow_events`; many `message.read` rows are acceptable at small scale but not ideal long-term.
- Case-scoped only: not a general cross-Case agent mesh.
- Busy workers do not receive mid-turn messages unless they explicitly poll through tools.

Best fit:

- First implementation. It proves the governance model and real utility without adding a broker or new scheduler.

### Option B: Durable Message Outbox Table + Existing SSE/Wake

Summary: add first-class DB tables for messages and recipients, while still using existing SSE and wake-dispatch mechanisms for notification.

Data model:

- `agent_messages`
  - `message_id`
  - `case_id`
  - `thread_id`
  - `reply_to_message_id`
  - `from_session_id`
  - `from_role`
  - `intent`
  - `scope`
  - `body`
  - `created_at`
  - `idempotency_key`
- `agent_message_recipients`
  - `message_id`
  - `recipient_session_id`
  - `delivery_state`: `pending`, `notified`, `read`, `expired`
  - `notified_at`
  - `read_at`

API/tool surface:

- Same as Option A, but backed by tables rather than ledger-only derivation.
- Append compact `message.sent` / `message.read` references into `flow_events` for Case audit visibility.

Delivery:

- Existing SSE can notify UI clients of new messages.
- Existing Wake-Dispatcher can scan pending recipient rows for idle eligible sessions and deliver coalesced prompts.
- Busy sessions remain non-interruptible by default; they receive messages at their next tool call only if backend-specific hooks are intentionally added later.

Advantages:

- Clean inbox queries and read receipts.
- Easier rate limiting, TTL, unread counts, and recipient-specific delivery.
- Does not overload `flow_events` as both audit trail and inbox state.
- Broker migration later is straightforward: DB remains canonical, broker is notification/transport.

Challenges:

- Requires migration and more tests.
- More design surface: retention, indexes, duplicate suppression, per-recipient state transitions.
- Needs explicit service-boundary hardening because this is a new external-input endpoint.

Best fit:

- Recommended production design once the first build is approved.

### Option C: Broker-Backed Peer Event Bus

Summary: introduce Redis Streams or NATS JetStream as the shared event bus; gateway, workers, and UI subscribe/publish message events; DB remains canonical for state.

Data model:

- Keep `agent_messages` / recipients in DB as canonical truth.
- Publish `message.sent`, `message.delivery_requested`, `message.read` events to a broker stream.
- SSE endpoint reads from broker or a gateway projection instead of tailing `events.ndjson`.

Delivery:

- Per-recipient durable consumers or filtered streams.
- Wake workers through existing `/nudge` or a new message-notification endpoint.
- Future true peer mesh can move coordinator role without losing the event space.

Advantages:

- Best long-term distributed architecture.
- Handles replay, acknowledgments, fanout, and backpressure better than file tailing or DB polling.
- Lines up with the deferred M-Mesh direction.

Challenges:

- High operational cost for current maturity.
- Needs new infrastructure, credentials, deployment/runbooks, failure modes, monitoring, and migration strategy.
- Easy to overbuild before the product semantics are proven.

Best fit:

- Future milestone after Option B semantics are validated.

### Option D: Do Not Build Free-Form Messaging

Summary: keep only governed dispatch/review, plus maybe improve `handoff.created`.

Advantages:

- Lowest cost and lowest attack surface.
- Existing Manager path already provides review, lineage, waits, approvals, and closure.

Challenges:

- Agents cannot cheaply ask each other questions, share partial findings, or coordinate without dispatching formal work.
- Manager becomes a bottleneck for all collaboration.
- File-edit collision and cross-worker context sharing stay clumsy.

Best fit:

- Valid fallback if operator governance risk outweighs collaboration benefit.

## Recommendation

Build Option B, but land it in two increments:

1. Increment 1: Case-scoped messaging API/tools with DB outbox tables, compact `flow_events` audit references, pull-based reads, no autonomous wake. This delivers useful agent-to-agent communication while keeping all state changes behind existing gates.
2. Increment 2: Add coalesced idle wake for eligible Manager/worker sessions using the existing Wake-Dispatcher pattern. A message wake should only notify and invite the recipient to read/respond; it should not execute the sender's requested action.

Do not start with Redis/NATS. The repo already has a working gateway DB, SSE stream, and wake-dispatch loop. A broker is justified only when file-backed SSE and DB polling become the bottleneck or when M-Mesh requires an external event space.

Do not use PTY injection as the backbone. If backend-specific mid-turn hooks are later added, they should be an optimization that reads from the gateway-canonical inbox, never the source of truth.

## Concrete Intended Functionality

Initial user-facing capability:

- A Manager or worker can send a message to another session in the same Case.
- A message can be direct or Case-broadcast.
- Message intent is one of `inform`, `request`, `ack`.
- A message can belong to a thread and can reply to a previous message.
- Recipients can list unread messages and mark messages read/acked.
- The Case timeline shows compact message events for audit.
- The Web UI can show unread counts and recent messages using normal API reads plus existing SSE invalidation.

Explicit non-capabilities in v1:

- No token streaming.
- No unbounded broadcast outside a Case.
- No automatic execution of requests contained in messages.
- No worker-to-worker release/close/merge/approve actions.
- No broker dependency.
- No mid-turn interruption unless a later backend-specific design proves it safe.

## Design Constraints To Enforce Before Build

Service-boundary checklist for `POST /api/cases/{id}/messages`:

- Concurrency: endpoint must be O(1) or bounded by recipient count. Cap recipients per message, e.g. 20.
- Memory at scale: cap body and metadata size; never accept attachments inline.
- Request size: enforce Pydantic field limits and Content-Length maximum.
- Timeout: DB writes are short; no synchronous wake delivery inside the request.
- Malformed input: strict enum validation for scope/intent/priority; reject unknown sessions and ambiguous recipients.
- Backing resource failure: if DB is unavailable, return 503 and do not emit best-effort-only events.

Security/governance:

- Same-Case recipient ACL: sender and recipients must be affiliated to the Case unless operator/system explicitly overrides.
- Message body cap: start at 8-16 KiB; larger context goes through artifacts or transcript references.
- Rate limits: per-sender per-Case budget, e.g. 30 messages / 10 min and 5 attention wakes / 10 min.
- Storm prevention: no automatic reply loops; wake turns must include a small round/backoff cap.
- Prompt-injection framing: delivered messages are untrusted peer content with sender metadata, not system instructions.
- Audit: every sent/read/expired state change has a Case audit reference.
- Idempotency: client-provided or derived idempotency key prevents duplicate sends on retry.
- Retention: keep message records for Case lifetime; expired unread delivery state can be derived or swept later.

## Implementation Shape

Suggested build packet:

1. Add migration for `agent_messages` and `agent_message_recipients` with indexes on `(case_id, created_at)`, `(recipient_session_id, delivery_state, created_at)`, and unique `(case_id, idempotency_key)`.
2. Add Pydantic models in `src/control/control_api.py` or a small adjacent module.
3. Add DB helpers in `src/control/db.py`: `create_agent_message`, `list_agent_messages`, `list_unread_agent_messages`, `mark_agent_message_read`.
4. Add orchestrator seams mirroring `record_review`: validate Case/session affiliation, write DB rows, append `flow_events` references, emit a live operational event for SSE invalidation.
5. Add Control API routes under `/api/cases/{id}/messages`.
6. Add Manager MCP tools in `scripts/mcp_manager.py`: `send_peer_message`, `read_peer_messages`, `ack_peer_message`.
7. Add tests first:
   - DB migration/helper round trip.
   - idempotent duplicate send.
   - rejects unknown Case, sender, recipient, cross-Case recipient.
   - request size and recipient count caps.
   - `flow_events` audit reference appended.
   - MCP tool input bounds and idempotency key.
   - no auto-wake in increment 1.
8. Add optional Web UI read surface after backend is stable.
9. Increment 2 adds wake scanning and delivery tests with `AWAITING_INPUT`, blocked Case, offline node, and round-cap scenarios.

## Open Decisions

- Whether v1 allows Case-broadcast or only direct messages. Direct-only is safer; Case-broadcast is useful if recipient count is capped.
- Whether workers may message peers directly or only Managers initially. Direct worker-to-worker is the feature goal, but Manager-mediated first is safer.
- Whether message bodies may include artifact/session/task references. Recommendation: yes, as structured references, not inline bulk content.
- Whether unread state is a state table or append-only-only. Recommendation: state table plus audit events.
- Whether attention messages can wake workers in v1. Recommendation: no; pull-only first, idle wake second.

## Hard Constraints Check

Option A:

- Canonical system-of-record stays: yes, via `flow_events`.
- Control stays centralized: yes, if messages are communication only.
- Roles/review gates stay intact: yes.
- No PTY-persistence backbone/no SDK decoupling: yes.

Option B:

- Canonical system-of-record stays: yes, via DB message tables plus audit references.
- Control stays centralized: yes.
- Roles/review gates stay intact: yes.
- No PTY-persistence backbone/no SDK decoupling: yes.

Option C:

- Canonical system-of-record stays: yes only if DB remains canonical and broker is transport/projection.
- Control stays centralized: yes with care.
- Roles/review gates stay intact: yes with care.
- No PTY-persistence backbone/no SDK decoupling: yes.

Option D:

- All hard constraints honored by not adding the primitive, but the capability remains missing.

