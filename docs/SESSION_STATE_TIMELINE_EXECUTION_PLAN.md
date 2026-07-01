# Session State Timeline Execution Plan

Date: 2026-07-01

Purpose: implementation-ready roadmap for completing the broader Web UI
event/state work across:

- #36 Remove Tasks Page / Replace With Jobs
- #37 Move Job Event Sequences Out Of System
- #38 Make The System Tab Earn Its Place
- #39 Make Worker/Session State Reporting Honest

This plan is ordered by implementation dependency, not ticket number.

## Global Rules

- Do not claim #36, #37, #38, or #39 done from UI-only changes.
- Do not treat SSE as durable truth. SSE remains live operational signal.
- Do not silently convert unknown/stale/restarted/detached state to `running` or
  `failed`.
- Do not move jobs out of System blindly. Jobs with `session_id` belong to the
  owning Session/Project; unowned jobs can remain System/operator content.
- Do not add generic architecture that conflicts with existing `MeshDB`,
  `TelemetryStore`, control API, task server, or Web adapter patterns.
- Do not introduce N+1 query paths. Timeline reads must be bounded and batched.

## Dependency Overview

Blocking chain:

1. Backend state authority/derivation layer.
2. Backend durable session timeline/read model.
3. Frontend separation of live SSE events from durable session timeline.
4. Session Detail durable state/activity UI.
5. Job ownership routing: Session/Project vs System.
6. System cleanup and health expansion.
7. Context cleanup and ticket closure.

Independent or semi-independent work:

- `events.ts` comment/model cleanup can start after package 3 design is accepted,
  but should not drive user-visible claims.
- System backend/account usage (#30/#33) can be designed independently from the
  timeline, but #38 should not be closed until both infra health and task/job
  ownership are addressed.
- Removing dead `TasksScreen` code can happen after route/nav audit, but #36 is
  not done until jobs have session/project ownership.

## Package 0: Baseline Audit And Test Fixtures

### Goal

Freeze the current behavior into targeted tests/fixtures so later packages do not
accidentally reintroduce stale "running" or fabricated "failed" states.

### Inspect First

- `.ai/CONTEXT.md`
- `docs/SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md`
- `src/control/db.py`
- `src/control/task_server.py`
- `src/orchestrator.py`
- `src/worker/agent.py`
- `src/control/telemetry_store.py`
- `src/core/telemetry.py`
- `src/core/telemetry_projection.py`
- `web/src/domain/events.ts`
- `web/src/transport/eventAdapter.ts`
- `web/src/hooks/useEventStream.ts`
- `web/src/hooks/useActivityLog.ts`
- `web/src/hooks/useSessionTimeline.ts`
- `web/src/screens/SessionDetailScreen.tsx`
- `web/src/screens/SystemScreen.tsx`
- `web/src/components/system/JobsPanel.tsx`

### Schema/API Changes

None.

### UI Surfaces Affected

None.

### Tests Required

- Add or extend backend tests that construct:
  - pending task
  - claimed task with fresh node live_state
  - claimed task with stale heartbeat
  - claimed task after node incarnation changed
  - terminal task result
  - watched job running/done/failed/lost with and without `session_id`
  - telemetry turn with missing raw event coverage
- Add frontend tests documenting that existing SSE activity is rolling/live only.

Candidate test files:

- `tests/test_mesh_self_awareness.py`
- `tests/test_mesh_reconcile_spool.py`
- `tests/test_watched_jobs.py`
- `tests/test_telemetry_store.py`
- `tests/test_control_api.py`
- `web/src/transport/adapters.test.ts`
- `web/src/transport/eventLog.test.ts`

### Done Definition

The repo has targeted failing or characterization tests for the state cases that
future packages must satisfy.

### Non-Goals

- No new endpoint.
- No UI changes.
- No CONTEXT.md cleanup.

### Risks / False Assumptions To Verify

- Whether `nodes.live_state` reliably includes active task IDs from all worker
  paths.
- Whether local in-process tasks and remote worker tasks report comparable state.
- Whether existing tests can build node live_state without starting real services.

## Package 1: Explicit State Authority And Derivation Layer

### Goal

Create a backend module that derives honest task/session execution state from
durable sources using a documented authority order.

This is the core blocker for #39 and a prerequisite for #37.

### Inspect First

- `src/control/db.py`
- `src/control/mesh_health.py`
- `src/core/task_lifecycle.py`
- `src/control/task_server.py`
- `src/control/node_registry.py`
- `src/orchestrator.py`
- `src/worker/agent.py`
- `tests/test_mesh_self_awareness.py`
- `tests/test_mesh_health.py`
- `tests/test_mesh_reconcile_spool.py`
- `tests/test_mesh_dispatch_timeout.py`

### Schema/API Changes

Prefer no schema change initially.

Add a pure backend module, likely one of:

- `src/core/session_state.py`
- `src/core/task_state_truth.py`
- `src/control/session_timeline.py`

The module should accept already-fetched rows:

- `mesh_tasks`
- owning session view/status
- node row/live_state
- telemetry turn/process summary
- job rows when relevant

Output a strict derived state model:

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

Each derived state must include:

- `state`
- `confidence`
- `reason`
- `authoritative_source`
- `observed_at`
- `stale_after`
- `raw_refs`

Authority order:

1. terminal `mesh_tasks` result/error
2. fresh worker `nodes.live_state` proving active task
3. fresh claim with matching node incarnation
4. telemetry turn/process state
5. stale claim/reaper/reconcile evidence
6. SSE/log hints only as diagnostic, not authority

### UI Surfaces Affected

None directly. This package creates backend truth for later UI.

### Tests Required

- Pure unit tests for every state.
- Contradiction tests:
  - `mesh_tasks.status='claimed'` plus stale heartbeat -> not `running`
  - `claimed_by` node incarnation mismatch -> stale/unknown, not running
  - terminal result plus stale live_state -> terminal wins
  - telemetry running but mesh terminal -> terminal wins
  - missing live_state -> confidence reduced
- Restart case:
  - gateway restart must not turn claimed task into failed without terminal proof.

### Done Definition

Given bounded DB rows, the new module deterministically returns honest state with
source, confidence, and reason. Restart/stale/unknown cases are covered.

### Non-Goals

- No new UI.
- No endpoint yet.
- No deletion of existing `derive_task_state` until replacement callers exist.

### Risks / False Assumptions To Verify

- `nodes.live_state` shape may not include enough task detail to prove
  `backend_running`.
- Local gateway worker and remote worker may need separate derivation branches.
- Some existing task statuses may not map cleanly without adding a DB marker.

## Package 2: Durable Session Timeline Read Model API

### Goal

Add a bounded, durable `/api/sessions/{session_id}/timeline` endpoint that joins
session-owned task, job, telemetry, approval, artifact, and recovery facts into
one reviewable sequence.

This blocks durable #37 UI work and most #36/#39 closure.

### Inspect First

- `src/control/control_api.py`
- `src/control/db.py`
- `src/control/transcript.py`
- `src/control/artifacts.py`
- `src/control/telemetry_store.py`
- `src/core/telemetry_projection.py`
- `src/core/task_lifecycle.py`
- new state derivation module from Package 1
- `tests/test_control_api.py`
- `tests/test_watched_jobs.py`
- `tests/test_telemetry_store.py`

### Schema/API Changes

Add endpoint:

- `GET /api/sessions/{session_id}/timeline?limit=N&cursor=...`

Response:

```json
{
  "items": [],
  "next_cursor": null,
  "generated_at": "...",
  "coverage": {
    "tasks": "complete",
    "telemetry": "partial",
    "jobs": "complete",
    "artifacts": "complete"
  }
}
```

Timeline item fields:

- `id`
- `kind`
- `source`
- `durability`
- `timestamp`
- `session_id`
- `task_id`
- `turn_id`
- `job_id`
- `node_id`
- `backend`
- `status`
- `confidence`
- `staleness`
- `summary`
- `detail`
- `raw_refs`

Implementation notes:

- Query tasks by `session_id` in one bounded query.
- Query jobs by `session_id` in one bounded query.
- Query turns by `session_id` through `TelemetryStore.list_turns`.
- Query selected raw telemetry only by bounded turn IDs if needed.
- Query artifacts via DB-first helpers, filtered by session/task IDs.
- Approvals by existing approvals read path.
- Do not parse `logs/events.ndjson` for durable timeline truth.

### UI Surfaces Affected

None directly, except the endpoint becomes available.

### Tests Required

- API returns bounded ordered items for mixed task/turn/job/artifact data.
- Same timestamp ordering is stable.
- Missing telemetry does not hide task/artifact rows.
- DB unavailable path is structured and non-fabricating.
- Invalid session ID behavior matches existing session API conventions.
- Endpoint does not perform per-row DB queries.

### Done Definition

Session timeline endpoint returns durable, bounded, reloadable state for a
session without relying on SSE.

### Non-Goals

- No frontend migration yet.
- No Project UI yet.
- No System cleanup yet.

### Risks / False Assumptions To Verify

- Existing artifact helpers may not expose efficient session filtering.
- `TelemetryStore.list_turns` newest-first ordering may need normalization.
- Session transcript rows and task rows may duplicate message-like items; timeline
  should not replace chat.

## Package 3: Source Ownership Policy

### Goal

Define and implement ownership rules that decide whether a state belongs to
Session, Project, System, Usage, or nowhere.

This blocks #36 and #38 cleanup.

### Inspect First

- `web/src/screens/SystemScreen.tsx`
- `web/src/components/system/JobsPanel.tsx`
- `web/src/hooks/useLiveData.ts`
- `web/src/lib/activityFormat.ts`
- `src/control/control_api.py`
- `src/control/db.py`
- `src/control/task_server.py`
- `src/control/task_server_client.py`
- `tests/test_watched_jobs.py`

### Schema/API Changes

Backend options:

- Extend `/api/jobs` with optional `session_id`, `project`, and `owned` filters.
- Or add session timeline job items only and leave `/api/jobs` for System.

Recommended minimal API changes:

- `GET /api/jobs?session_id=<id>&limit=N`
- Keep existing unfiltered `/api/jobs` behavior for compatibility.

Ownership policy:

- `session_id` present -> Session-owned.
- `session_id` absent but repo/project path known -> Project-owned if a Project
  surface exists.
- no session/project -> System-owned.
- mesh/node health -> System.
- LLM usage/account limits -> Usage/System depending final nav.
- raw SSE with no owner -> System live activity.

### UI Surfaces Affected

None directly, unless Package 6 starts UI changes.

### Tests Required

- `/api/jobs?session_id=` filters correctly.
- Unfiltered `/api/jobs` remains backward-compatible.
- Session-owned jobs are not counted as System primary progress in later UI tests.

### Done Definition

There is a documented and tested ownership rule, and APIs can fetch session-owned
jobs without client-side global filtering.

### Non-Goals

- No visual job cards yet.
- No Project surface if it does not already exist.

### Risks / False Assumptions To Verify

- Jobs may not carry project path; Project ownership may need to wait.
- Existing remote-controller job merge path may not support `session_id` filter.
- Local/remote split described in CONTEXT may affect job ownership consistency.

## Package 4: Frontend Event Model Split

### Goal

Separate live SSE event types from durable session timeline types so the Web UI
does not treat `eventAdapter.ts` as canonical backend truth.

This blocks Session Detail durable timeline UI.

### Inspect First

- `web/src/domain/events.ts`
- `web/src/domain/models.ts`
- `web/src/domain/status.ts`
- `web/src/transport/eventAdapter.ts`
- `web/src/transport/eventLog.ts`
- `web/src/transport/rawApi.ts`
- `web/src/transport/apiClient.ts`
- `web/src/hooks/useEventStream.ts`
- `web/src/hooks/useActivityLog.ts`
- `web/src/hooks/useLiveData.ts`
- `web/src/hooks/useSessionTimeline.ts`
- `web/src/transport/adapters.test.ts`
- `web/src/transport/eventLog.test.ts`

### Schema/API Changes

Frontend raw types only:

- Add `RawSessionTimelineItem`
- Add `RawSessionTimelineResponse`
- Add `api.sessionTimeline(token, sessionId, limit, cursor)`

Domain types:

- Rename or document current `GatewayEvent` as `LiveGatewayEvent` if feasible.
- Add `SessionTimelineItem` or `SessionActivityItem` for durable timeline.

Adapters:

- `sessionTimelineAdapter.ts`
- Keep `eventAdapter.ts` live-SSE-only.

### UI Surfaces Affected

No visual changes required in this package, but hooks become available:

- `useSessionActivity(sessionId)`
- or `useSessionTimelineItems(sessionId)` if naming does not conflict with chat.

### Tests Required

- `eventAdapter` still handles live SSE activity.
- timeline adapter preserves `durability`, `confidence`, `staleness`, and raw refs.
- unknown item kinds render or degrade safely in later components.
- no `tool.*` telemetry assumptions remain in `events.ts` comments.

### Done Definition

Frontend has separate live-event and durable-timeline contracts, with tested
adapters and no claim that SSE `GatewayEvent` is complete backend truth.

### Non-Goals

- No Session UI replacement yet.
- No System cleanup yet.

### Risks / False Assumptions To Verify

- Existing components may import `GatewayEvent` broadly.
- Renaming `GatewayEvent` may cause unnecessary churn; documentation-only split
  plus new durable types may be lower risk.

## Package 5: Session Detail Durable Activity/State UI

### Goal

Replace the rolling SSE State sequence in Session Detail with durable timeline
items while keeping chat conversation-first.

This advances #37 and #39, but does not complete #39 until restart/stale/unknown
cases are tested end-to-end.

### Inspect First

- `web/src/screens/SessionDetailScreen.tsx`
- `web/src/hooks/useSessionTimeline.ts`
- `web/src/components/timeline/SessionTimeline.tsx`
- `web/src/components/timeline/SessionTurns.tsx`
- `web/src/hooks/useLiveData.ts`
- new timeline hook/adapter from Package 4
- `web/src/components/ui/StatusChip.tsx`
- `web/src/lib/time.ts`
- `web/src/lib/cn.ts`

### Schema/API Changes

None beyond Package 2/4.

### UI Surfaces Affected

Session Detail:

- Chat tab stays conversation-only.
- Info or new Activity tab shows durable state/action timeline.
- LLM Turns remain metrics summary but link/expand to timeline/diagnostics.
- Artifact/file rows appear chronologically and link to Files content.
- Session-owned watched jobs appear in timeline.

### Tests Required

- Durable timeline renders after reload without SSE events.
- Stale/unknown/detached/recovered states have distinct labels.
- Chat does not include raw operational event spam.
- Missing timeline data shows degraded state, not false running.
- Artifact rows route to Files.
- Turn rows route or expand to diagnostics where available.

### Done Definition

Session Detail no longer relies on rolling SSE for its state sequence. A reload
shows the same durable state/action history for the session.

### Non-Goals

- Do not redesign chat.
- Do not build full Project surface.
- Do not remove System Jobs yet.

### Risks / False Assumptions To Verify

- Mobile layout may need a fourth tab; adding tabs can reduce ergonomics.
- Timeline item count may be large; UI must cap/collapse noisy telemetry.
- Existing `useSessionTimeline` name is chat-specific and may need renaming to
  avoid confusion.

## Package 6: Session/Project-Local Jobs And Task Replacement

### Goal

Make jobs and task progress live where the user expects: in the owning Session
and Project where possible. This is the real replacement work for #36 and part
of #37.

### Inspect First

- `web/src/components/system/JobsPanel.tsx`
- `web/src/screens/SystemScreen.tsx`
- `web/src/screens/SessionDetailScreen.tsx`
- `web/src/hooks/useLiveData.ts`
- `web/src/transport/rawApi.ts`
- `web/src/transport/apiClient.ts`
- `src/control/control_api.py`
- `src/control/task_server.py`
- `src/control/db.py`
- any remaining `TasksScreen` files/routes

### Schema/API Changes

Use Package 3 APIs:

- `GET /api/jobs?session_id=<id>`
- Optional later: project-scoped jobs if project identity is available.

May also expose task/job cards through `/api/sessions/{id}/timeline`, avoiding a
separate session jobs endpoint in the first pass.

### UI Surfaces Affected

Session Detail:

- Show active/recent watched jobs for the session.
- Show job result/history with status and terminal tail summary if safe.

Project:

- If a Project surface already exists, add project-local active/recent jobs.
- If not, document Project work as dependent and do not claim it done.

System:

- Keep unowned jobs and infrastructure job health.
- Stop presenting session-owned jobs as the primary Jobs workspace.

Routing:

- Verify `/tasks` legacy redirect.
- Audit and delete or document `TasksScreen` module after no route imports remain.

### Tests Required

- Session-owned jobs render in Session Detail.
- Unowned jobs still render in System.
- System does not duplicate session-owned job cards as primary content.
- `/tasks` redirect remains.
- If deleting `TasksScreen`, TypeScript build catches no references.

### Done Definition

#36 is done only when task replacement is real: jobs with session/project
ownership are surfaced outside System, legacy Tasks route is handled, and any
remaining Tasks module has a documented reason or is removed.

### Non-Goals

- Do not build a large standalone Jobs primary nav.
- Do not expose raw terminal streaming.
- Do not invent Project ownership if the backend lacks project identity.

### Risks / False Assumptions To Verify

- `session_id` may be missing for some jobs that are logically session-owned.
- Remote-controller merged jobs may not carry all local session metadata.
- Job tail may contain sensitive command output; keep summaries bounded.

## Package 7: System Tab Refocus

### Goal

Make System an infrastructure health surface, not a session/task progress dump.
This completes the System part of #37 and #38 after ownership routing exists.

### Inspect First

- `web/src/screens/SystemScreen.tsx`
- `web/src/components/system/JobsPanel.tsx`
- `web/src/components/system/NodeDetailSheet.tsx`
- `web/src/hooks/useActivityLog.ts`
- `web/src/hooks/useLiveData.ts`
- `web/src/lib/activityFormat.ts`
- `src/control/control_api.py`
- `src/control/mesh_health.py`
- `src/control/db.py`
- `tests/test_mesh_health.py`
- `tests/test_mesh_health_samples.py`
- `tests/test_control_api.py`

### Schema/API Changes

Possible additions:

- extend `/api/mesh/health` with explicit stale/orphaned/reconcile details if
  current payload is insufficient
- add backend/account usage endpoint separately for #30/#33 if not already present
- add runtime/process health endpoint only if there is an existing reliable source

Do not invent usage quota/account data.

### UI Surfaces Affected

System:

- Mesh health
- Nodes
- stale/orphaned mismatch checks
- reconcile backlog
- unowned jobs
- backend/account usage if available
- credential/version/runtime warnings if available
- live infra activity

Remove or down-rank:

- session-owned task progress
- session-owned jobs
- rolling task lifecycle noise

### Tests Required

- System filters owned session activity by default.
- System still surfaces infra warnings and unowned jobs.
- Mesh health stale/orphaned states render distinctly.
- Unknown usage/account limits render as unknown, not zero.

### Done Definition

#38 is done only when System is a credible infrastructure health view and not the
primary task/job progress UI.

### Non-Goals

- Do not add a marketing/dashboard redesign.
- Do not fabricate account quota or model limit data.
- Do not remove live activity entirely; keep infra value.

### Risks / False Assumptions To Verify

- Existing mesh health payload may not expose enough actionable mismatch detail.
- Backend usage/account state may require separate research and should not block
  state timeline work unless #38 closure is being claimed.

## Package 8: Restart/Stale/Unknown End-To-End Validation

### Goal

Prove that durable state remains honest across restart, stale worker, unknown
worker, detached task, recovered task, cancellation, and watched-job loss cases.

This is mandatory before #39 can be claimed done.

### Inspect First

- `tests/test_mesh_self_awareness.py`
- `tests/test_mesh_reconcile_spool.py`
- `tests/test_mesh_dispatch_timeout.py`
- `tests/test_watched_jobs.py`
- `tests/test_telemetry_mesh_integration.py`
- `src/control/task_server.py`
- `src/control/db.py`
- `src/orchestrator.py`
- `src/worker/agent.py`
- new timeline/state modules
- Web timeline tests from Packages 4-7

### Schema/API Changes

None expected. If tests reveal missing persisted facts, add the smallest schema
or state marker required and backfill/default safely.

### UI Surfaces Affected

Session Detail and System validation only.

### Tests Required

Backend:

- gateway restart while task claimed does not mark failed without terminal proof
- worker heartbeat stale turns claimed task into `worker_unknown` or `stale_claim`
- worker incarnation mismatch turns old claim into stale/unknown/released path
- terminal worker result recovers timeline state
- cancellation remains open/resumable session lifecycle where appropriate
- watched job lost is shown as job lost, not task failed unless agent continuation
  explicitly fails

Frontend:

- reload shows stale/unknown/recovered state without SSE
- System does not show session-owned task progress as primary content
- Session Detail labels all uncertain states explicitly

### Done Definition

Restart/stale/unknown/detached/recovered cases are tested through backend read
model and frontend rendering. #39 may be considered for closure only after this.

### Non-Goals

- No paid/live backend e2e by default.
- No full e2e suite.

### Risks / False Assumptions To Verify

- Some cases may require integration tests with fake DB rows rather than live
  worker processes.
- Exact worker restart behavior may differ between Windows worker and Pi gateway.

## Package 9: Context And Ticket Closure

### Goal

Update `.ai/CONTEXT.md` only after implementation and tests justify each claim.

### Inspect First

- `.ai/CONTEXT.md`
- `docs/SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md`
- this execution plan
- test output from completed packages
- relevant git commits

### Schema/API Changes

None.

### UI Surfaces Affected

None.

### Tests Required

No new tests; verify package tests are already passing.

### Done Definition

Rows #36-#39 are updated with factual status:

- #36 done only after jobs replace Tasks in session/project-local surfaces.
- #37 done only after durable event/job/task sequences leave System as primary
  progress UI.
- #38 done only after System has infrastructure health value and no primary
  session progress noise.
- #39 done only after state truth is verified across restart/stale/unknown cases.

### Non-Goals

- Do not rewrite broad project history.
- Do not mark uncertain work done.

### Risks / False Assumptions To Verify

- Old "ladder complete" language may still be historically true; adjust wording
  without erasing history.

## Cross-Ticket Completion Matrix

| Package | #36 | #37 | #38 | #39 |
|---|---|---|---|---|
| 0 Baseline fixtures | supports | supports | supports | blocks |
| 1 State authority | supports | blocks | supports | blocks |
| 2 Timeline API | supports | blocks | supports | blocks |
| 3 Ownership policy | blocks | blocks | blocks | supports |
| 4 Frontend split | supports | blocks | supports | blocks |
| 5 Session activity UI | supports | blocks | supports | blocks |
| 6 Session/project jobs | blocks | blocks | supports | supports |
| 7 System refocus | supports | blocks | blocks | supports |
| 8 Restart/stale validation | supports | supports | supports | blocks closure |
| 9 Context cleanup | closes | closes | closes | closes |

## What Can Be Claimed Independently

- Package 0 can be claimed as test baseline only.
- Package 1 can be claimed as backend state derivation only.
- Package 2 can be claimed as timeline API only.
- Package 4 can be claimed as frontend model separation only.
- Package 7 can be partially claimed as System cleanup only if it does not say
  #38 is done before usage/runtime/infra gaps are covered.

## What Must Not Be Claimed Done Early

- #36 until jobs are session/project-local where possible and Tasks is truly
  replaced or intentionally retained.
- #37 until durable sequences, not rolling SSE, own session/job/task history.
- #38 until System is infrastructure-focused and no longer primary progress UI.
- #39 until restart/stale/unknown/detached/recovered cases are tested in backend
  read model and frontend rendering.
