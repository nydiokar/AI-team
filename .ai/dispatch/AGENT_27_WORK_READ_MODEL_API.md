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

**Current Status:** dispatched
**Burndown:**
- [ ] Define Work list/detail/timeline/graph response shapes
- [ ] Implement pure read-model builder
- [ ] Add read-only control API routes
- [ ] Add fixtures for linked, unlinked, stale, blocked, review, and closed cases
- [ ] Add tests for no heuristic inference
- [ ] Run targeted pytest
- [ ] Append Closure and advance DISPATCH_LOG

**Next Action:** wait for A25/A26, then design response fixtures before adding routes.
