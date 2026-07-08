# A28 — Mobile Work surface: read-only operations inbox + session affiliation

**Dispatch created:** 2026-07-08
**Milestone:** Work Control Substrate, A28 of A25-A29.
**Level:** 2 (frontend + read-only API consumption). Code branch required: `feat/mobile-work-surface` → PR.
**Depends on:** A27 merged.
**Spec:** [`docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md`](../../docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md).

```xml
<task_packet>
  <meta><task_name>A28-mobile-work-surface</task_name><harness_level>2</harness_level></meta>
  <objective_lock>
    <real_objective>The phone UI gets an honest Work operations inbox that surfaces cases needing
      attention, while Sessions remains the universal runtime/session inspector.</real_objective>
    <literal_request>"add the mobile Work tab without making a workflow theater UI"</literal_request>
    <interpreted_task>Add a bottom-nav Work tab backed only by the A27 read model. Render active
      cases, needs-decision cases, blocked/rework/review cases, and case detail with ledger,
      compact lineage, and evidence/timeline links. Add session affiliation labels in Sessions
      using authoritative Work read data.</interpreted_task>
    <constraints>Mobile-first. Read-only. No editable graph/canvas. No workflow creation. No action
      buttons except navigation/drill-down. No client-side inference from sessions/tasks if the Work
      API does not provide the relationship. Text must fit small screens. Preserve standalone New
      Session flow.</constraints>
    <non_goals>No Start Work entrypoint yet. No Manager automation. No approve/rework/close actions.
      No desktop workflow editor. No raw transcript as primary view.</non_goals>
    <assumptions>A27 exposes enough explicit authority/confidence/staleness fields for the UI.
      Existing frontend data hooks/adapters pattern should be followed.</assumptions>
    <drift_risks>Letting the tab become a graph editor; duplicating SessionDetail as WorkDetail;
      hiding runtime sessions; showing inferred labels for unlinked sessions.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Add domain/transport types and hooks for Work list/detail/timeline/graph. 2. Add
      Work tab to bottom navigation: Work | Sessions | System. 3. Build WorkScreen:
      Needs decision, Active, Blocked/rework/review, Recent closed collapsed. 4. Build WorkDetail:
      header, ledger, compact lineage, evidence/timeline, links to sessions/artifacts. 5. Add
      session affiliation labels in SessionRow/SessionDetail using authoritative links from Work
      data. 6. Tests and typecheck; visual/manual sanity on mobile viewport.</steps>
    <validation>npm test/tsc/build as appropriate; Playwright or screenshot sanity if local app
      already supports it; no text overflow on mobile; no client-side heuristic ownership joins in
      code review.</validation>
    <definition_of_done>Operator can open Work, see cases needing attention, drill into a case,
      and jump to linked sessions/evidence; Sessions still lists all sessions including standalone
      and workflow-owned.</definition_of_done>
    <risks>Frontend scope. Keep actions out; this is read-only navigation and operational truth.</risks>
  </approved_plan>
  <execution_rules>
    <do>Update milestone Live Log; commit at checkpoint; start dev server if needed and report URL at close.</do>
    <do_not>No creation/actions, no editable DAG, no inferred ownership, no marketing/landing UI, no paid CLI.</do_not>
    <report_format>Closure with screenshots/checks, files changed, and residual UI gaps.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone

**Current Status:** dispatched
**Burndown:**
- [ ] Add Work domain/transport/hooks
- [ ] Add Work bottom-nav tab
- [ ] Build Work list screen
- [ ] Build Work detail screen
- [ ] Add session affiliation labels
- [ ] Run frontend tests/typecheck/build and mobile sanity
- [ ] Append Closure and advance DISPATCH_LOG

**Next Action:** wait for A27, then build frontend adapters before UI components.
