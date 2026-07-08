# A25 — Flow relationship schema: case links + append-only events

**Dispatch created:** 2026-07-08
**Milestone:** Work Control Substrate, A25 of A25-A29.
**Level:** 3 (migration). Code branch required: `feat/work-control-schema` → PR.
**Depends on:** A21/A22/A23 merged on `main`.
**Spec:** [`docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md`](../../docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md).

```xml
<task_packet>
  <meta><task_name>A25-flow-relationship-schema</task_name><harness_level>3</harness_level></meta>
  <objective_lock>
    <real_objective>The gateway can persist authoritative case/entity relationships and an
      append-only case event trail, so future Work UI and Manager automation do not infer workflow
      state from timestamps, transcripts, or last-task adjacency.</real_objective>
    <literal_request>"build the substrate needed before Workflow/Work UI"</literal_request>
    <interpreted_task>Add additive schema for flow_links and flow_events, plus optional nullable
      flow_run_id convenience columns on mesh_tasks and approvals if they can be added safely.
      Add MeshDB helpers to create/list links and append/list events. No runtime behavior changes
      beyond schema/helper availability.</interpreted_task>
    <constraints>Additive and NULLable only. Idempotent migration. Existing DBs and old rows must
      keep working. flow_links are relationships only, not a second task ledger. flow_events are
      append-only. No UI, no orchestration, no Manager automation. No execution path may read
      current_stage to decide what runs. No paid CLI; targeted pytest only.</constraints>
    <non_goals>No write-path population beyond helper tests (A26). No read model API (A27). No Work
      UI (A28). No first-class drops table. No public API mutation endpoints. No rework of sessions
      or mesh task lifecycle.</non_goals>
    <assumptions>flow_runs migration 22 exists. mesh_tasks and approvals can accept NULLable
      flow_run_id columns without changing legacy writers. If direct columns prove risky, ship
      flow_links/flow_events first and document direct columns as deferred.</assumptions>
    <drift_risks>Over-modeling a workflow engine; adding cascading deletes that destroy history;
      allowing large payload blobs in events; failing old migration paths.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Read src/control/db.py migrations, flow_runs helpers, mesh_tasks DDL, approvals DDL.
      2. Add the next migration: CREATE flow_links, CREATE flow_events, indexes, and optional
      ALTER mesh_tasks/approvals ADD COLUMN flow_run_id. 3. Add DB helpers:
      create_flow_link, list_flow_links, append_flow_event, list_flow_events; optionally link lookup
      by entity. 4. Tests: fresh DB has schema; upgraded DB has schema; migration re-run is no-op;
      helpers round-trip; duplicate flow_links are idempotent or rejected predictably; flow_events
      preserve insertion order; old mesh task and approval code paths still work with NULL
      flow_run_id.</steps>
    <validation>pytest targeted DB/schema tests plus existing flow/db tests. Grep/diff confirms no
      UI/API/orchestrator write behavior changed.</validation>
    <definition_of_done>Authoritative relationship and event tables exist with helpers and tests,
      old rows remain valid, and no Work UI or automation has been introduced.</definition_of_done>
    <risks>Migration idempotence and duplicate link semantics. Prefer simple unique constraint +
      helper-level idempotence.</risks>
  </approved_plan>
  <execution_rules>
    <do>Update this milestone section as work proceeds; commit at checkpoint; open PR at close.</do>
    <do_not>No broad workflow engine; no Manager automation; no Work UI; no destructive migration;
      no paid CLI; no python main.py status.</do_not>
    <report_format>Append Closure with schema summary, tests run, and explicit confirmation that
      runtime behavior is unchanged.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone

**Current Status:** dispatched
**Burndown:**
- [ ] Verify current flow_runs/mesh_tasks/approvals schema in code
- [ ] Add additive migration for flow_links + flow_events
- [ ] Decide and implement safe nullable flow_run_id convenience columns
- [ ] Add DB helpers for links/events
- [ ] Add migration/helper tests
- [ ] Run targeted pytest
- [ ] Append Closure and advance DISPATCH_LOG

**Next Action:** start with schema verification in `src/control/db.py`, then implement the migration.
