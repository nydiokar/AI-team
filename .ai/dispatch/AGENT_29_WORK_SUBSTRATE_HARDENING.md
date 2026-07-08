# A29 — Work substrate hardening: truth, stale/conflict states, and milestone closure

**Dispatch created:** 2026-07-08
**Milestone:** Work Control Substrate, A29 of A25-A29.
**Level:** 2-3 (cross-cutting review/hardening). Code branch required if fixes are needed:
`feat/work-substrate-hardening` → PR.
**Depends on:** A25-A28 merged.
**Spec:** [`docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md`](../../docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md).

```xml
<task_packet>
  <meta><task_name>A29-work-substrate-hardening</task_name><harness_level>2</harness_level></meta>
  <objective_lock>
    <real_objective>Close the Work Control Substrate milestone only after adversarially proving
      the API/UI does not lie about progress, ownership, stale state, review, or closure.</real_objective>
    <literal_request>"review one more time and make sure it fits and is properly planned"</literal_request>
    <interpreted_task>Run a milestone-level adversarial review over A25-A28 artifacts and fill
      gaps: stale/conflict fixtures, no-heuristic assertions, docs updates, and final closure
      criteria. Fix P0/P1 issues before marking the milestone achieved.</interpreted_task>
    <constraints>P0/P1 only for mandatory fixes. Preserve the read-only Work UI scope unless a P0
      truth issue requires a small patch. No new workflow actions. No Manager automation. No broad
      refactors. No paid CLI.</constraints>
    <non_goals>No new feature layer. No Start Work entrypoint. No editable DAG. No decomposer
      generator. No unrelated UI polish.</non_goals>
    <assumptions>A25-A28 landed and have their own tests. This job can add focused fixtures/tests
      where the integrated behavior is not covered.</assumptions>
    <drift_risks>Using "hardening" as a feature dump; missing a truth bug because each slice passed
      in isolation; failing to update durable docs after code changes.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Read A25-A28 closures, Work milestone doc, and implemented code. 2. Run adversarial
      review focused on false authority, heuristic linkage, stale/conflict states, manual override,
      closure evidence, and mobile scope creep. 3. Add or fix tests/fixtures for linked/unlinked,
      stale worker, missing event, conflicting terminal task vs flow summary, superseded, rework,
      waived review, and manual interruption. 4. Update docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md
      with final shipped state and remaining deferred work. 5. Update CONTEXT/DISPATCH_LOG rows and
      append closures.</steps>
    <validation>Targeted backend/frontend tests relevant to touched code; grep/code review confirms
      UI uses Work API authority fields, not ad hoc joins/prose parsing.</validation>
    <definition_of_done>The milestone can honestly be marked achieved: substrate exists, read model
      is authoritative, UI renders unknown/unlinked/stale when appropriate, and M3 can proceed
      without creating opaque child work.</definition_of_done>
    <risks>Over-fixing. Only P0/P1 truth/safety defects are in scope.</risks>
  </approved_plan>
  <execution_rules>
    <do>Perform adversarial review before fixes; record F-tags and outcomes in Closure.</do>
    <do_not>No new features beyond hardening; no paid CLI; no python main.py status.</do_not>
    <report_format>Closure must include F-tag outcomes, tests run, final milestone status, and
      remaining deferred work.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone

**Current Status:** dispatched
**Burndown:**
- [ ] Read A25-A28 closures and implemented code
- [ ] Run adversarial review with F-tags
- [ ] Add/fix stale/conflict/no-heuristic tests
- [ ] Patch P0/P1 defects only
- [ ] Update durable docs and milestone status
- [ ] Run targeted verification
- [ ] Append Closure and advance DISPATCH_LOG

**Next Action:** wait for A25-A28, then review integrated behavior before making fixes.
