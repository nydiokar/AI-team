# Session State Timeline Architecture Review

Date: 2026-07-01

Scope: adversarial review and implementation plan for making Web UI session, job,
task, action, artifact, telemetry, and recovery state honest and properly surfaced.

No code changes are proposed as complete here. This document is an implementation
brief and review artifact.

## Adversarial Review

### Findings Against The Previous Investigation

1. The report correctly rejects calling task #39 done, but it still risks
   underspecifying "honest state." A UI can show more states and still be dishonest
   if those states come from the wrong authority. The implementation must define
   authority order per state, not just add timeline rows.

2. The proposed `/api/sessions/{id}/timeline` is necessary but not sufficient. If
   it only joins existing tables, it will reproduce current ambiguity:
   `mesh_tasks.status='claimed'` can mean worker-running, detached-after-gateway
   shutdown, stale claim, or a worker that is alive but no longer reporting the
   task in `live_state`.

3. The report treats durable telemetry as available truth. That is only partly
   true. `llm_events` is append-only and durable, but coverage varies by backend
   and event retention can prune raw events. `llm_turns` projections are summaries,
   not a complete action timeline.

4. `events.ts` being false about tool events is an important finding, but the
   correction is not "add tool events to GatewayEvent." The stronger correction is
   to split live operational events from durable session timeline events. Otherwise
   the frontend will mix transport events, task ledger rows, and telemetry facts in
   one union again.

5. The suggested target UX still needs a priority rule for noisy telemetry. A full
   raw `tool.call.*` stream can overwhelm Session Detail. The UI needs collapsed
   default rows with expandable diagnostics, not every telemetry row rendered flat.

6. Jobs need more careful placement than "move out of System." Jobs without
   `session_id` are infrastructure/operator concerns and can remain in System.
   Jobs with `session_id` should appear in the owning Session and Project surfaces.

7. The previous report did not explicitly require stale/unknown state to survive a
   page refresh. That must be a backend read-model property, not just an SSE banner
   or React state.

8. The previous report did not call out enough test failure modes. Tests must cover
   restart ambiguity, missing telemetry, stale worker heartbeats, watched-job loss,
   task claim release, and contradictory sources.

### Hard Gates Before #39 Can Be Marked Done

- A task accepted by the gateway must show where it is in the chain: accepted,
  queued, claimed, worker-running, backend-running, terminal, stale, detached,
  unknown, or recovered.
- A gateway restart must not fabricate terminal failure when a worker may still be
  running.
- A worker restart must not silently leave a task as confidently running unless
  `nodes.live_state` still proves it.
- A task whose worker is unreachable must show "unknown/stale" until reconciliation
  or timeout policy resolves it.
- A terminal worker result must update the session timeline durably.
- The UI must be able to reconstruct the same state after reload without relying
  on the SSE buffer.

## Context Claims That Are Misleading Or Stale

`CONTEXT.md` rows #36-#39 are all partial, not done.

- `.ai/CONTEXT.md:36` says "Remove Tasks Page / Replace With Jobs." Primary nav
  cleanup is real, but replacement is not. `/tasks` redirects to System
  (`web/src/App.tsx:39`), and jobs still live in System
  (`web/src/screens/SystemScreen.tsx:346`, `web/src/components/system/JobsPanel.tsx:68`).
  Session/project-local job ownership is not implemented.

- `.ai/CONTEXT.md:37` says "Move Job Event Sequences Out of System." Routine
  session/task-correlated activity is filtered by default
  (`web/src/hooks/useActivityLog.ts:36`), but the Session Info state sequence is
  still rolling SSE data (`web/src/screens/SessionDetailScreen.tsx:234`) from a
  bounded client buffer (`web/src/hooks/useEventStream.ts:30`). It is not durable.

- `.ai/CONTEXT.md:38` says "Make the System Tab Earn Its Place." System is less
  noisy, but it still owns Jobs and live Activity, and the remaining items listed
  in that row are the actual work: backend/account usage, runtime/process health,
  stuck/orphaned mismatch checks, credential/version warnings.

- `.ai/CONTEXT.md:39` says worker/session state reporting is improved at the
  UI/read-model boundary. That improvement is real but shallow. There is no
  verified server-worker-backend state truth model, no durable uncertain state,
  and no restart/state-mismatch recovery UI.

- `.ai/CONTEXT.md:108`, `.ai/CONTEXT.md:121`, and `.ai/CONTEXT.md:255` say the Web
  UI ladder is complete and ready to merge. That may be historically true for the
  old UI ladder, but it is stale as guidance for the current session-state problem.

## Current Behavior From Code

The frontend has a narrow canonical event contract in
`web/src/domain/events.ts`. Its header says `task.progress` and `tool.*` are
omitted because backend tool events do not exist (`events.ts:9-14`). That is now
false as a global architecture claim: durable telemetry defines `process.*`,
`model.request.*`, `tool.call.*`, `subagent.*`, and `turn.*`
(`src/core/telemetry.py:140-215`).

`web/src/transport/eventAdapter.ts` translates raw SSE events into coarse UI
events. It collapses task-like events into `task.state_changed`
(`eventAdapter.ts:32-42`), maps `mesh_result` to succeeded/failed
(`eventAdapter.ts:124-127`), and turns unknown backend events into
`system.notice` (`eventAdapter.ts:151-153`). That is acceptable for a live
activity feed, but it is not a durable event/action timeline.

`web/src/hooks/useEventStream.ts` opens `/api/events/stream` and keeps a bounded
rolling client log of 500 adapted events (`useEventStream.ts:30-31`). The backend
event API explicitly says gap recovery is not replay and clients should refresh
read endpoints instead (`src/control/control_api.py:583-586`).

Session chat is conversation-first. `useSessionTimeline` deliberately excludes the
raw SSE stream and renders transcript turns, optimistic user messages, approvals,
and one live running indicator (`web/src/hooks/useSessionTimeline.ts:10-18`,
`web/src/hooks/useSessionTimeline.ts:130-139`). This is the right chat behavior,
but it means action/state history must be a separate durable source.

Session Info already fetches LLM turn summaries through `useSessionTurns`
(`web/src/hooks/useLiveData.ts:128-145`) and renders `SessionTurns`
(`web/src/components/timeline/SessionTurns.tsx:190-225`). That surfaces metrics,
not a complete action timeline.

System filters session/task-correlated live activity by default
(`web/src/hooks/useActivityLog.ts:36-38`), but Jobs remain a System panel
(`web/src/screens/SystemScreen.tsx:346-363`).

## Durable Backend State Sources

### Raw SSE/System Event Log

- Durability: log-backed tail, not reliable replay.
- Correlation: often has `session_id`, `task_id`, `node_id`, timestamps.
- Exposed: `/api/events`, `/api/events/stream`.
- UI issue: frontend collapses many events to coarse states or `system.notice`.
- Owner: System live feed only, plus temporary live hints.

### Mesh Task Lifecycle Rows

- Durability: durable SQLite `mesh_tasks`.
- Correlation: `session_id`, `machine_id`, `backend`, `action`, `claimed_by`,
  `claimed_at`, `completed_at`, result/error timestamps.
- Exposed: `/api/tasks`, `/api/tasks?sectioned=true`.
- UI issue: not joined into a session-owned timeline.
- Owner: Session Detail, Project, Jobs. System only for aggregate health.

### Worker Claim/Result/Heartbeat State

- Durability: claims/results are durable in `mesh_tasks`; heartbeat/live_state is
  latest-state durable in `nodes`.
- Correlation: node and task state are split. Worker live_state can prove active
  tasks, but only when fresh and populated.
- Exposed: `/api/nodes`, `/api/mesh/health`, task server endpoints.
- UI issue: no per-session durable derived state that distinguishes claimed,
  worker-running, stale, detached, unknown, recovered.
- Owner: Session Detail for task-specific state; System for node health.

### LLM Turn Telemetry

- Durability: durable `llm_events`, `llm_turns`, child projection tables.
- Correlation: `session_id`, `turn_id`, `invocation_id`, `node_id`, backend,
  model, timestamps.
- Exposed: `/api/turns`, `/api/turns/{turn_id}`, `/api/turns/{turn_id}/events`,
  `/api/turns/{turn_id}/diagnostics`, `/api/turns/{turn_id}/graph`.
- UI issue: summarized as turn metrics only, not timeline actions.
- Owner: Session Detail and Usage.

### Process/Model/Tool/Subagent Events

- Durability: durable via telemetry when emitted and retained.
- Correlation: turn/invocation/process/model/tool/subagent IDs plus node/backend.
- Exposed: turn events/diagnostics/graph endpoints.
- UI issue: absent from `events.ts` and not adapted into session timeline.
- Owner: Session Detail drilldown. Usage for aggregate metrics.

### Artifact/File-Change Data

- Durability: DB-canonical through `mesh_tasks`, file fallback.
- Correlation: session/task/timestamps.
- Exposed: `/api/artifacts`, `/api/artifacts/{task_id}`.
- UI issue: Files tab is separate from action chronology.
- Owner: Session Detail Files and timeline artifact rows.

### Watched Job State

- Durability: durable SQLite `jobs`.
- Correlation: `job_id`, `session_id`, `node_id`, PID/probe/timestamps.
- Exposed: `/api/jobs` and task-server `/jobs`.
- UI issue: System-only display, no session/project-local ownership.
- Owner: Session/Project when `session_id` exists; System for unowned infra jobs.

### Cancellation/Restart/Recovery State

- Durability: partial and scattered. Cancellation appears in events/telemetry/task
  results; restart recovery uses claim release, live_state, reconcile, and
  telemetry reconciliation.
- Correlation: task/session/node depending on source.
- Exposed: indirectly through tasks, turns, mesh health, events.
- UI issue: not first-class, not consistently durable, and not explicit enough.
- Owner: Session Detail for task/session effects; System for infrastructure alerts.

## Root Cause

The Web UI currently treats a live operational event adapter as if it were the
canonical UI event model. Meanwhile, durable truth lives in several DB-backed
read models: mesh tasks, jobs, artifacts, telemetry events, and telemetry turn
projections.

The result is a split-brain UI:

- Chat is mostly honest and conversation-first.
- Session Info has partial metrics and a rolling live state list.
- System has live activity and jobs.
- No surface owns the durable session event/action/artifact/job/recovery timeline.

## Target UX

Session chat remains conversation-first. It should not become a raw event log.

Session Detail needs a durable Activity/State section showing:

- task accepted/queued/claimed/worker-running/backend-running/terminal states
- cancellation and recovery
- stale/unknown/detached states
- LLM turn summaries with expandable telemetry
- model/tool/subagent/process drilldown
- artifacts and file changes
- watched jobs for that session

Jobs should be session/project-local where possible. Unowned jobs can remain in
System.

System should show infrastructure health: mesh/node/runtime/account/credential
warnings, stuck/orphaned mismatch checks, reconcile backlog, and unowned jobs.

Unknown, stale, restarted, and detached states must be explicit. They must not be
silently rendered as running or failed.

## Event Model Recommendation

Do not expand the current `GatewayEvent` union into a universal truth model.
Split it:

1. `LiveGatewayEvent`
   - Source: SSE/log tail.
   - Purpose: live System activity and transient hints.
   - Durability: rolling/log-backed.

2. `SessionTimelineItem`
   - Source: backend timeline API joining durable state.
   - Purpose: reviewable session activity and state.
   - Durability: DB-backed or explicitly marked as volatile.

Proposed `SessionTimelineItem` fields:

- `id`
- `kind`
- `source`
- `durability`
- `timestamp`
- `sessionId`
- `taskId`
- `turnId`
- `jobId`
- `nodeId`
- `backend`
- `status`
- `confidence`
- `staleness`
- `summary`
- `detail`
- `href`
- `rawRefs`

Proposed kinds:

- `message`
- `task_state`
- `worker_state`
- `turn_event`
- `model_request`
- `tool_call`
- `subagent`
- `process`
- `artifact`
- `file_change`
- `job_state`
- `approval`
- `cancellation`
- `recovery`
- `system_notice`

## Implementation Plan

### Phase 1: Backend Read Model

Add `/api/sessions/{session_id}/timeline`.

Inputs:

- bounded `mesh_tasks` rows for the session
- `llm_turns` rows by session
- selected `llm_events` by turn IDs
- artifacts from DB-first artifact helpers
- approvals by session
- watched jobs by session
- optional recent SSE references only as non-authoritative hints

Output:

- stable ordered `SessionTimelineItem[]`
- explicit source/durability/confidence fields
- no raw command/stdout payloads
- pagination or limit/cursor from the start

### Phase 2: Honest State Derivation

Add a backend state derivation layer that distinguishes:

- `accepted`
- `queued`
- `claimed`
- `worker_running`
- `backend_running`
- `waiting_for_input`
- `waiting_for_approval`
- `cancel_requested`
- `cancelled`
- `completed`
- `failed`
- `detached`
- `stale_claim`
- `worker_unknown`
- `recovered`

Authority order:

1. terminal `mesh_tasks` result/error
2. fresh worker `nodes.live_state` active task proof
3. fresh claim row plus matching node incarnation
4. telemetry turn/process state
5. stale claim/reaper/reconcile data
6. SSE/log hints

Contradictions must produce explicit `unknown` or `stale` states with reasons.

### Phase 3: Frontend Domain

Create timeline domain types separate from `GatewayEvent`.

Keep `eventAdapter.ts` for SSE live activity. Do not use it for durable session
history.

Add adapters:

- task row to timeline items
- telemetry turn/event to timeline items
- artifact to timeline items
- job row to timeline items
- approval to timeline items

### Phase 4: UI Placement

Session Detail:

- Chat tab remains conversation-only.
- Add durable Activity/State view in Info or a separate tab.
- Keep LLM turn metrics, but connect each turn to expandable event/graph detail.
- Add artifact/file rows in chronological context.
- Add session-owned watched jobs.

System:

- Keep Mesh, Nodes, reconcile/stale health, runtime warnings, account/usage,
  unowned jobs, and infrastructure notices.
- Remove session-owned job/task progress as primary content.

Project:

- Add project-local active/recent jobs and sessions when project surface exists.

### Phase 5: Tests

Backend tests:

- timeline joins task, turn, artifact, job, approval rows
- ordering is stable under equal timestamps
- stale claim becomes explicit stale/unknown, not running
- gateway restart with claimed task does not fabricate failed
- worker live_state proves active worker-running only while fresh
- telemetry missing/partial coverage still yields honest timeline rows
- watched job `lost` is session-local when `session_id` exists

Frontend tests:

- `GatewayEvent` adapter remains live-only
- timeline adapters map task/telemetry/job/artifact rows
- Session Detail renders stale/unknown/recovered states distinctly
- System excludes session-owned task/job progress by default
- turn drilldown handles missing telemetry events

Existing relevant tests to extend:

- `tests/test_telemetry_store.py`
- `tests/test_telemetry_projection.py`
- `tests/test_telemetry_mesh_integration.py`
- `tests/test_watched_jobs.py`
- `tests/test_mesh_self_awareness.py`
- `tests/test_mesh_reconcile_spool.py`
- `tests/test_control_api.py`
- `web/src/transport/adapters.test.ts`
- `web/src/transport/eventLog.test.ts`
- `web/src/components/timeline/SessionTurns.test.ts`

## Dependency Graph

1. Define backend authority/state derivation.
2. Add backend timeline endpoint and schema.
3. Add backend tests for state contradictions/restart/stale cases.
4. Add frontend timeline domain types.
5. Add frontend adapters and tests.
6. Replace Session Info rolling SSE state with durable timeline data.
7. Move session-owned jobs into Session/Project surfaces.
8. Narrow System to infrastructure health.
9. Update `CONTEXT.md` rows #36-#39 with real done definitions.

## Quick Wins

- Add direct tests for `eventAdapter.ts` assumptions around unknown/dotted events.
- Mark `events.ts` comments as live-SSE-only, not canonical backend truth.
- Add session-owned filtering to jobs data in the UI once a timeline endpoint exists.
- Show telemetry coverage/data-quality flags in Session Turns.

## Architectural Prerequisites

- A durable timeline API is required before changing the UI to claim honest state.
- A state authority order is required before marking #39 done.
- Unknown/stale/restarted states must be represented in backend data, not inferred
  only in React.
- Session-owned and system-owned jobs must be separable by `session_id`.

## Real Done Definitions

### #36 Remove Tasks Page / Replace With Jobs

Done means Tasks nav and legacy route are gone or redirected, the standalone
Tasks module is deleted or intentionally retained with documented ownership, and
jobs are available in session/project-local surfaces when correlated.

### #37 Move Job Event Sequences Out Of System

Done means session/job/task event sequences are durable from DB/API, expandable,
and owned by Session or Project surfaces. System may show only infra-level job
health or unowned jobs.

### #38 Make The System Tab Earn Its Place

Done means System shows infrastructure health: mesh, nodes, runtime/process
health, reconcile/stale/orphaned mismatches, backend account/usage, credential
and version warnings. It must not be the primary task-progress UI.

### #39 Make Worker/Session State Reporting Honest

Done means request accepted, queued, claimed, worker-running, backend-running,
cancelled, completed, failed, stale, unknown, detached, restarted, and recovered
states are derived from server-worker-backend truth, persisted, exposed by API,
tested, and surfaced in Session Detail without silently rendering uncertain work
as running or failed.

## Service Boundary Checklist For The New Timeline Endpoint

- Concurrency: timeline reads are externally callable and can be repeated. Use
  bounded queries and existing DB connection patterns; no per-row N+1 reads.
- Memory at scale: cap rows per source and output items. Default should be small
  enough for mobile, with cursor/limit.
- Request size: endpoint accepts path `session_id` and bounded query params only.
- Timeout: query work must stay bounded; no filesystem scans on hot path unless
  fallback is explicitly limited.
- Malformed input: invalid session IDs should return structured 404 or empty
  timeline according to existing session API behavior.
- Backing resource failure: DB unavailable should fail explicitly or return a
  degraded response with `durability='unavailable'`, not fabricated state.

## Recent Partial Commits: Solved And Not Solved

`4a9f668 Surface session state and simplify cockpit nav`:

- Solved: Tasks removed from bottom nav, `/tasks` redirects to System, Session
  Info shows a live state sequence, event adapter carries `sessionId` on task
  state when available.
- Not solved: durable session timeline, job/session ownership, backend authority
  model, restart/stale/unknown surfacing.

`a2bddc7 Fix cancelled session lifecycle and system activity noise`:

- Solved: cancelled sessions remain open/resumable, frontend lifecycle maps only
  closed to closed, System activity filters session/task-correlated rows by
  default.
- Not solved: durable state truth, worker/backend running verification, recovery
  UI, telemetry-to-timeline model.

`d1dccb0 Refine CONTEXT.md to enhance task/session state clarity and feature tracking`:

- Solved: context wording became less optimistic than earlier versions.
- Not solved: implementation. It is documentation-only.

## Conclusion

#39 is not done. The immediate next implementation work is not another UI polish
pass. It is a backend durable session timeline read model plus an explicit state
authority/derivation layer. Only after that should the Web UI replace rolling SSE
state with an honest session-owned timeline and move session-owned jobs out of
System as primary content.
