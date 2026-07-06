# A20 — Flow-State Schema Promotion (RECORD → v0.4 §11 model)

> **STATUS: DRAFT — pending v0.6 roadmap approval. NOT yet in DISPATCH_LOG.**
> Milestone: **M1 — Durable Flow State Machine** (slice 1 of 3). Level **3** (DB migration).
> Prerequisite for: A21, A22, and all of M2/M3.

## Packet

```xml
<task_packet>
  <objective_lock>
    <real_objective>Promote the A19 `flow_runs` RECORD into the durable, queryable
      state-machine spine v0.4 §11 specifies — by EXTENDING the existing seed, not
      replacing it — so later slices can write/read real stage + artifact state.</real_objective>
    <literal_request>Extend flow_runs to the full v0.4 §11 column set with an additive,
      backward-compatible migration; add get/update-fields DB methods; define the stage
      vocabulary in code.</literal_request>
    <interpreted_task>Migration 22 adds the §11 columns to flow_runs (keeping A19's five);
      new db methods `get_flow_run(id)` and `update_flow_fields(id, **fields)`; a Python-side
      `FlowStage` vocabulary (NOT a DB CHECK constraint). No reader that drives behavior yet
      (A21); no API yet (A22); no autonomy.</interpreted_task>
    <constraints>Additive only — never drop/rename A19 columns. Existing flow_runs rows must
      survive. A19 best-effort writes (`create_flow_run`, `update_flow_stage`) must keep
      passing unchanged. Migration idempotent (re-runnable). No behavior change to task
      execution. Plain pytest only — no paid CLI, no `python main.py status`.</constraints>
    <non_goals>Reading current_stage to drive/gate behavior (A21). Read API (A22). Any
      autonomous spawn (M3). UI. Enforcing the stage enum in SQL.</non_goals>
    <assumptions>flow_runs (migration 21) is on the branch base; A19 methods live in
      src/control/db.py ~L1253-1296; migration list ~L2088.</assumptions>
    <drift_risks>Turning this into the whole state machine in one packet (scope creep);
      adding a reader (belongs in A21); a non-additive migration that breaks A19 rows.</drift_risks>
  </objective_lock>

  <approved_plan>
    <steps>
      1. Add migration 22: ALTER flow_runs ADD COLUMN for each of — approved_plan,
         plan_review, burn_down_items, execution_result, implementation_review,
         waived_findings, closure_summary, role_assignments, artifact_links, status,
         updated_at, parent_flow_run_id, dispatch_file, worker_task_ids. (TEXT/nullable;
         JSON-encoded where structured. parent_flow_run_id enables M2 feature→child links;
         dispatch_file/worker_task_ids enable A22 trace linking.)
      2. Add `get_flow_run(flow_run_id)` and `update_flow_fields(flow_run_id, **fields)`
         (whitelist columns; set updated_at) to db.py.
      3. Add a `FlowStage` string vocabulary in code: draft, plan_review, dispatched,
         executing, checkpoint_review, iterating, blocked, closed. (A19's "dispatch_start"/
         "queued" remain valid legacy values — do not delete them.)
      4. Tests: migration idempotency, additive-ness (old row survives + reads back),
         A19 methods still green, get/update round-trip.
    </steps>
    <validation>`pytest tests/test_flow_runs.py` + new test file green; migration re-run is
      a no-op; a pre-existing 5-column row still reads.</validation>
    <definition_of_done>flow_runs carries the §11 columns; get_flow_run/update_flow_fields
      work; FlowStage defined; all tests green; zero behavior change to task execution.</definition_of_done>
    <risks>Non-additive migration (mitigated: ALTER ADD only). JSON columns unparsed by
      readers yet (fine — A21/A22 consume them).</risks>
  </approved_plan>

  <execution_rules>
    <do>Extend the A19 seed. Keep writes best-effort/non-blocking. Whitelist update columns.
      Commit on a branch `feat/m1-flow-schema` and open a PR at close (code loop).</do>
    <do_not>Read current_stage to drive behavior. Add an API. Break A19. Run the e2e suite.</do_not>
    <report_format>Milestone Live Log below + closure; PR link in report.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone
## Objective
Promote flow_runs to the §11 model (additive).
## Current Status
drafting
## Burndown
- [ ] migration 22 (additive §11 columns)
- [ ] get_flow_run / update_flow_fields
- [ ] FlowStage vocabulary
- [ ] tests green (idempotent + additive + A19 intact)
## Live Log
- (executor appends here)
## Blockers
## Next Action
Await roadmap approval, then execute.
