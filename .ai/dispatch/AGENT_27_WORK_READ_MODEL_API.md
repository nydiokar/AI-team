# A27 — Work read model API: ledger, timeline, and graph over authoritative links

**Dispatch created:** 2026-07-08
**Milestone:** Work Control Substrate, A27 of A25-A29.
**Level:** 2-3 (read model over new substrate). Code branch required: `feat/work-read-model-api` → PR.
**Depends on:** A25 + A26 merged.
**Spec:** [`docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md`](../../docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md).

```xml
<task_packet>
  <meta><task_name>A27-work-read-model-api</task_name><harness_level>2</harness_level></meta>
  <objective_lock>
    <real_objective>The mobile Work UI can consume a single honest read model for active cases,
      case detail, timeline, and lineage graph without reconstructing workflow state in the
      browser.</real_objective>
    <literal_request>"build the read model API for Work/Case state"</literal_request>
    <interpreted_task>Add read-only control API endpoints that project flow_runs + flow_links +
      flow_events + mesh_tasks + sessions + approvals + artifacts/jobs into explicit Work/Case
      shapes. Unknown or missing authoritative links must be represented as unknown/unlinked, not
      filled in by heuristics.</interpreted_task>
    <constraints>Read-only API only. Auth/bind follows existing control API. No mutation endpoints.
      No public bind. No UI. No prose parsing. No fabricated defaults. Bounded queries and
      pagination/caps like session_timeline.</constraints>
    <non_goals>No Work tab (A28). No Manager automation. No creation/start-work endpoint. No action
      endpoints. No raw transcript streaming.</non_goals>
    <assumptions>A25/A26 provide authoritative links/events. Existing session_timeline patterns can
      be reused for coverage and confidence fields.</assumptions>
    <drift_risks>Putting business logic in the frontend; turning flow events into replay-only state;
      N+1 DB queries; hiding partial coverage.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Define response shapes for GET /api/work, /api/work/{flow_run_id},
      /api/work/{flow_run_id}/timeline, and /api/work/{flow_run_id}/graph. 2. Implement a pure
      read-model builder module that accepts already-fetched DB rows where possible, mirroring
      session_timeline discipline. 3. Add control API routes, read-only and auth-guarded. 4. Tests:
      list active/attention/recent cases; detail includes linked entities and explicit unknowns;
      timeline orders flow_events plus linked task/session evidence; graph uses parent/child links;
      missing links do not get inferred; auth/404/null behavior matches existing API patterns.</steps>
    <validation>Targeted pytest with fixture DB. Grep proves no POST/PUT/PATCH/DELETE Work routes.
      Query caps enforced.</validation>
    <definition_of_done>Frontend can render Work/Case list/detail/timeline/graph from one API
      surface with no client-side workflow inference.</definition_of_done>
    <risks>Overlarge payloads. Keep list summaries small; detail endpoints carry larger linked
      evidence.</risks>
  </approved_plan>
  <execution_rules>
    <do>Update milestone Live Log; commit at checkpoint; open PR at close.</do>
    <do_not>No mutation endpoint, no public bind, no Work UI, no transcript parsing, no paid CLI,
      no python main.py status.</do_not>
    <report_format>Closure must include endpoint list, response authority rules, and tests.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone

**Current Status:** built (`feat/work-control-substrate`) — awaiting review/merge
**Burndown:**
- [x] Define Work list/detail/timeline/graph response shapes
- [x] Implement pure read-model builder (`src/control/work_read_model.py`)
- [x] Add read-only control API routes
- [x] Fixtures for linked, unlinked, blocked, review, closed, unknown cases
- [x] Tests for no heuristic inference (buckets from authoritative fields only)
- [x] Run targeted pytest (green)
- [ ] Manager advances DISPATCH_LOG/CONTEXT at merge

## Closure

**Outcome:** Read-only Work/Case read model over the A25/A26 substrate. A PURE
builder module (`work_read_model.py`, no DB access — takes fetched rows) feeds four
auth-guarded GET routes. Honesty-first: buckets derive ONLY from authoritative
`status`/`current_stage` (never "closed" when unknown → `unknown`); missing links
render as present-and-empty ledger sections, never inferred.

**Endpoints (all GET, `_require_auth`, read-only — no mutation verbs):**
- `GET /api/work?bucket=&limit=` — case summaries + `bucket_counts` (needs_decision |
  blocked | review | active | closed | unknown). Small: no per-case link/event queries.
- `GET /api/work/{flow_run_id}` — summary + full record + grouped ledger
  (tasks/sessions/approvals/artifacts/jobs/flows/other) + parent + children + coverage
  flags. 404 `case_not_found`.
- `GET /api/work/{flow_run_id}/timeline` — append-only flow_events in order + linked
  evidence pointers.
- `GET /api/work/{flow_run_id}/graph` — compact parent/self/children lineage graph with
  parent→child edges from authoritative lineage (parent_flow_run_id / child_flow links).

**Authority rules honored:** current_stage/status = summary; flow_events = trail; links =
the only relationship source; unknown renders as `unknown`/empty. No prose parsing, no
timestamp/adjacency inference, no N+1 on the list.

**Tests:** `tests/test_work_read_model.py` (11, pure) + `tests/test_control_api_work.py`
(9, routes incl. auth/404/405/limit/bucket-filter). Regression: 146 green across
flow+substrate+control+workflow+telegram suites.
