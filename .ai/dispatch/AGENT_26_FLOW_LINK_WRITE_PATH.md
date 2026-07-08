# A26 — Flow link/event write path: populate authoritative relationships

**Dispatch created:** 2026-07-08
**Milestone:** Work Control Substrate, A26 of A25-A29.
**Level:** 3 (hot-path lineage writes). Code branch required: `feat/work-control-write-path` → PR.
**Depends on:** A25 merged **and A26a merged** (`feat/m2-dispatch-lineage-wiring` — the flow_runs
lineage stamping/supplier half of M2).
**Spec:** [`docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md`](../../docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md).

> **⚠️ De-confliction (2026-07-08) — read before touching the child-dispatch seam.** The
> `flow_runs.parent_flow_run_id`/`dispatched_by`/`dispatch_file` columns and the
> `_stamp_child_dispatch_lineage` **supplier** are already built by **A26a** at that exact seam
> (`orchestrator._record_flow_run_start` / `submit_instruction`). For the child-flow relationship,
> **CONSUME A26a — do NOT add a second stamping hook.** Read the parent linkage A26a stamped
> (`task.metadata[_PARENT_FLOW_RUN_META_KEY]` / `_dispatch_lineage_fields`) and record it as a
> `flow_links(role=child_flow)` row + a `task.dispatched`/`flow.linked` event. `flow_links` is the
> **authoritative** child→parent ledger; the `flow_runs` column stays a convenience index. Two
> writers at one seam for one edge = the milestone's own F4 "duplicate ledger" — avoid it.

```xml
<task_packet>
  <meta><task_name>A26-flow-link-write-path</task_name><harness_level>3</harness_level></meta>
  <objective_lock>
    <real_objective>Case relationships are populated at the moments the gateway actually knows
      them, so Work state is reconstructed from authoritative links/events instead of UI
      heuristics.</real_objective>
    <literal_request>"wire flow linkage and events into the existing dispatch/session/approval paths"</literal_request>
    <interpreted_task>Use A25 helpers to populate root task links, session role links, child-flow
      lineage, approval links, terminal task events, and manual/system lifecycle events where the
      relationship is known. Keep writes best-effort unless the existing operation already depends
      on the entity being written.</interpreted_task>
    <constraints>No new autonomous Manager behavior. No UI. No new ingestion lane. No state
      inference from transcripts. Existing task/session execution must remain byte-identical when
      no flow_run_id is present. Event/link write failures must not fail user tasks unless the
      caller is explicitly creating a flow-control record.</constraints>
    <non_goals>No read model API (A27). No Work tab (A28). No action endpoints. No first-class
      drops. No execution dependency on current_stage.</non_goals>
    <assumptions>A25 provides helpers and optional direct flow_run_id columns. A22 stashes
      __flow_run_id in task metadata when HARNESS_FLOW_DRIVE is on; A19/A22 paths can be extended
      without changing task execution semantics.</assumptions>
    <drift_risks>Creating duplicate or contradictory links; silently dropping lineage when helper
      writes fail; attaching unrelated standalone sessions to flows; making flow writes required
      on the hot path.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Read orchestrator flow-run hooks, submit_instruction/session creation paths,
      approval service/API paths, and session timeline builder. 2. Populate links/events at:
      flow creation, root task enqueue, task dispatch/claim/terminal result, approval request and
      resolution, session attach when role is explicit, and child-flow creation when
      parent_flow_run_id/dispatched_by is supplied. 3. Add helper-level safeguards for duplicate
      link writes and missing flow ids. 4. Tests: no-flow legacy path unchanged; flow root task
      creates root_task link + flow.created event; approval with flow id creates approval link +
      events; terminal task appends terminal event; failures in link/event writes are isolated;
      child flow reverse lookup works.</steps>
    <validation>Targeted pytest for orchestrator/db/control-api approval paths. Fault-injection
      test proves link/event failure does not fail normal task dispatch.</validation>
    <definition_of_done>Known relationships are populated where created; missing relationships are
      explicitly absent, not inferred; legacy standalone sessions/tasks are unaffected.</definition_of_done>
    <risks>Hook placement. Prefer the narrowest existing choke points and keep writes
      best-effort/logged.</risks>
  </approved_plan>
  <execution_rules>
    <do>Update milestone Live Log; commit at checkpoint; open PR at close.</do>
    <do_not>No Manager automation, no Work UI, no broad stage driver, no paid CLI, no python main.py
      status.</do_not>
    <report_format>Closure must list every populated relationship and every intentionally missing
      relationship left for future work.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone

**Current Status:** dispatched
**Burndown:**
- [ ] Read current flow-run, dispatch, session, and approval write paths
- [ ] Wire root task/session/approval/entity links
- [ ] Wire append-only flow events at known lifecycle points
- [ ] Add duplicate/missing-flow safeguards
- [ ] Add legacy parity and fault-isolation tests
- [ ] Run targeted pytest
- [ ] Append Closure and advance DISPATCH_LOG

**Next Action:** wait for A25, then identify the exact write hooks before editing.
