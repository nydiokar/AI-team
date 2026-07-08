# Work Control Substrate Milestone

**Status:** planned milestone, operator-aligned on 2026-07-08.
**Roadmap position:** v0.6 M2 foundation, before Manager-as-invoked-role (M3) and before
any product-grade mobile Work UI.
**Recommendation:** build now as a foundational dependency.

This milestone turns the current `flow_runs` record into an honest operational substrate
for mobile work control. It does **not** build autonomous manager behavior. It does **not**
build a desktop workflow editor. It creates the durable relationships and read model needed
before those things can be safe or useful.

## Decision

Use the hybrid product model:

- **Work / Case** is the operational layer: objective, stage, linked tasks, decisions,
  reviews, blockers, lineage, closure, and audit trail.
- **Session** is the runtime layer: backend connection, model, machine, transcript, and
  execution context.
- **Task** (`mesh_tasks`) is the delegated execution ledger.
- **Timeline** is evidence, not authority.
- **Graph/DAG** is navigation over authoritative lineage, not an editable planning canvas.

User-facing vocabulary should prefer **Work** for the mobile tab and **Case** for a
durable work item. Keep `flow_runs` as the implementation name.

## Why This Is Now

The existing M1 work shipped the `flow_runs` row and read API, but the read model is not
yet sufficient for a truthful Work UI:

- `flow_runs.task_id` links one task, but there is no general authoritative link from a
  flow to all tasks, sessions, approvals, artifacts, and jobs.
- `parent_flow_run_id` and `dispatched_by` exist, but M2 lineage population is not wired.
- `current_stage` is a mutable, best-effort record; it cannot be the only audit trail.
- session timelines are strong evidence, but they are session-scoped.
- approvals and reviews are not reliably scoped to a flow/case.

Building Work UI directly on these partial correlations would force fragile heuristics.
The substrate must come first.

## Product Goal

A phone-sized operator view can answer these questions without reading dispatch prose:

- What active work exists?
- What needs a decision now?
- Which manager/worker/reviewer sessions belong to this case?
- Which tasks, approvals, artifacts, and watched jobs are evidence for this case?
- Which worker was dispatched by which parent case or manager decision?
- Is the case blocked, stale, in review, in rework, closed, or superseded?
- What durable events explain how it reached this state?

## Scope

### In Scope

- Add authoritative relationships between cases and runtime/execution entities.
- Add append-only case events for audit and recovery.
- Populate links/events on existing dispatch, approval, session, and artifact paths where
  the relationship is known.
- Build a read-only Work read model API over those authoritative records.
- Add a minimal mobile Work tab only after the read model is truthful.
- Label Sessions as standalone, manager, worker, reviewer, or evidence for a case.
- Preserve current session-first ad hoc work.

### Out of Scope

- No autonomous Manager role yet.
- No Manager-spawns-worker behavior unless lineage is already observable.
- No editable DAG or workflow canvas.
- No new broad workflow engine.
- No transcript parsing to infer state.
- No prose-based "accepted" or "closed" inference.
- No execution path that reads `current_stage` to decide what runs.
- No public bind or auth redesign.

## Minimal Authoritative Model

Keep the current `flow_runs` table as the mutable summary row. Add a small relationship
and audit layer.

### Flow Links

`flow_links` is the authoritative relationship ledger between a case and entities already
owned by the gateway.

Required fields:

- `id`
- `flow_run_id`
- `entity_type`: `task | session | approval | artifact | job | flow`
- `entity_id`
- `role`: `root_task | manager | worker | reviewer | approval | artifact | job | child_flow | evidence`
- `created_at`
- optional `created_by`
- optional `metadata_json`

Recommended constraints:

- unique `(flow_run_id, entity_type, entity_id, role)`
- indexed by `flow_run_id`
- indexed by `(entity_type, entity_id)`
- no cascade delete of historical relationships

### Flow Events

`flow_events` is append-only evidence for case lifecycle changes and manual overrides.

Required fields:

- `id`
- `flow_run_id`
- `event_type`
- `actor`: `operator | manager | worker | reviewer | system`
- optional `from_state`
- optional `to_state`
- optional `entity_type`
- optional `entity_id`
- `payload_json`
- `created_at`

Important event types:

- `flow.created`
- `flow.stage_changed`
- `flow.status_changed`
- `flow.linked`
- `flow.unlinked`
- `task.dispatched`
- `session.attached`
- `approval.requested`
- `approval.resolved`
- `review.requested`
- `review.accepted`
- `review.rework_requested`
- `review.waived`
- `flow.blocked`
- `flow.unblocked`
- `flow.interrupted`
- `flow.superseded`
- `flow.closed`

### Optional Direct Columns

Direct nullable columns are allowed only when they remove repeated joins on hot paths:

- `mesh_tasks.flow_run_id`
- `approvals.flow_run_id`

They are convenience indexes, not replacements for `flow_links` and `flow_events`.

## Authority Rules

When records disagree:

1. terminal `mesh_tasks.status` wins for task execution outcome.
2. pending/resolved `approvals` rows win for approval gate state.
3. fresh worker `live_state` can refine active execution state, but stale live state must
   render as stale/unknown.
4. `flow_runs.status/current_stage` is the current case summary, not the audit trail.
5. `flow_events` is the audit trail for case transitions and manual overrides.
6. transcripts, summaries, and dispatch docs are evidence only.
7. missing links must render as `unlinked` or `unknown`, never be inferred silently.

## Entrypoint Policy

The product migrates toward Work-first for managed work, while preserving Sessions for
runtime inspection and ad hoc execution.

- **Now:** Sessions remains the primary creation flow. Work substrate is built underneath.
- **Next:** Work tab appears as read-only/attention-first once the read model is honest.
- **Later:** Start Work becomes the primary entrypoint for managed multi-step objectives.
- **Always:** Sessions lists every backend session, including workflow-owned sessions.

Session labels:

- `Standalone`
- `Manager for Case <id/title>`
- `Worker for Case <id/title>`
- `Reviewer for Case <id/title>`
- `Evidence for Case <id/title>`

## Mobile Target

The mobile Work tab must be an operations inbox, not a workflow editor.

Default screen:

- Needs decision
- Active work
- Blocked/rework/review
- Recent closed, collapsed

Case detail:

- case header: title/objective, status, stage, next action, confidence/staleness
- ledger: linked tasks, approvals, sessions, artifacts, jobs
- compact lineage: parent/children as a vertical tree
- evidence: flow timeline + links to session timelines/transcripts

Hidden behind drill-down:

- raw transcript
- full event payloads
- full artifact metadata
- DAG details
- session runtime details

## Dispatch Plan

This milestone is split into five jobs:

1. **A25 — Flow relationship schema.** Add `flow_links`, `flow_events`, optional
   convenience `flow_run_id` columns, and DB helpers/tests.
2. **A26 — Link/event write path.** Populate relationships and events at flow creation,
   task dispatch, session attachment, approval request/resolution, and terminal task state.
3. **A27 — Work read model API.** Add read-only `/api/work`, `/api/work/{id}`,
   `/api/work/{id}/timeline`, and `/api/work/{id}/graph` projections over authoritative
   records.
4. **A28 — Mobile Work surface.** Add the read-only Work tab and session affiliation labels,
   driven only by the Work read model.
5. **A29 — Hardening and closure.** Adversarial review, stale/conflict fixtures, docs,
   and acceptance checks proving the UI does not infer from prose.

## Done Condition

The milestone is achieved when:

- relationships between a case and its tasks/sessions/approvals/artifacts/jobs are
  queryable without heuristic joins.
- every lifecycle/status transition shown in Work UI has a current row and an event trail.
- M3 can safely create manager/worker sessions because child work will be observable.
- Sessions still works for standalone ad hoc work.
- the mobile Work tab can show active/blocked/review/rework cases without reading
  transcript prose.
- missing data is rendered as `unknown`/`unlinked`/`stale`, not guessed.

## Adversarial Review

- **[F1 · P0 · false authority] The UI could treat `current_stage` as an execution driver or
  proof of completion.**
  Resolution: `current_stage` remains summary state only. `flow_events` carries audit, and
  terminal task state comes from `mesh_tasks`.

- **[F2 · P0 · heuristic linkage] Work UI could infer session/task ownership from timestamps,
  last task id, or transcript adjacency.**
  Resolution: Work UI may only use `flow_links`, direct `flow_run_id` columns, and explicit
  lineage fields. Missing links render as unknown/unlinked.

- **[F3 · P0 · autonomous drift] Building substrate could smuggle in Manager automation.**
  Resolution: A25-A29 are read/link/audit/UI jobs. M3 Manager invocation remains out of
  scope until this milestone closes.

- **[F4 · P1 · duplicate ledger] `flow_links` could become a second task ledger.**
  Resolution: links only relate existing entities. Execution truth stays in `mesh_tasks`;
  external process truth stays in `jobs`; gate truth stays in `approvals`.

- **[F5 · P1 · over-modeled drops] Adding a first-class `drops` table now would freeze an
  unproven abstraction.**
  Resolution: drops/decisions are initially derived from explicit case status, approvals,
  reviews, blockers, and next action. Add a table only after real UI use proves the need.

- **[F6 · P1 · mobile scope creep] The Work tab could become a desktop DAG/editor.**
  Resolution: mobile default is attention ledger. DAG is compact navigation only; no editing.

- **[F7 · P1 · event bloat] Flow events could become unbounded payload storage.**
  Resolution: payloads stay compact references plus short reasons. Large evidence remains in
  existing artifacts, timelines, task results, and transcripts.

- **[F8 · P1 · migration risk] New schema touches hot tables and could break legacy sessions.**
  Resolution: all schema changes are additive/nullable; helpers are best-effort until callers
  deliberately require them; tests cover old rows and flag-off behavior.

Verdict: the milestone fits v0.6 M2 and should precede M3/M4 wiring. The dispatch split is
properly ordered: schema first, then write path, then read model, then UI, then hardening.
