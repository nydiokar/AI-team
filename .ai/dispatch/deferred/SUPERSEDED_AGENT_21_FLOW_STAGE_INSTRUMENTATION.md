# A21 — Stage-Transition Instrumentation (RECORD → live state machine)

> **STATUS: DRAFT — pending v0.6 roadmap approval. NOT yet in DISPATCH_LOG.**
> Milestone: **M1 — Durable Flow State Machine** (slice 2 of 3). Level **2**.
> Depends on: **A20** (schema). Precedes: A22 (trace surface).

## Packet

```xml
<task_packet>
  <objective_lock>
    <real_objective>Make `current_stage` reflect reality by writing flow state from every
      loop stage, and add the FIRST benign reader — so the flow row becomes a live state
      machine, not a dispatch-start-only record.</real_objective>
    <literal_request>Wire update_flow_fields/update_flow_stage at each loop stage; persist the
      objective_lock/approved_plan/etc as the loop produces them; add one read-only consumer of
      current_stage that changes NO task-execution behavior.</literal_request>
    <interpreted_task>Extend the A19 orchestrator hook so stage advances beyond
      dispatch_start→queued to executing→(checkpoint)→closed, and populate the §11 artifact
      columns from what's already in-hand. Add a benign reader: a `flow_status_summary()` used
      only for reporting/DISPATCH_LOG-style output — never to gate or drive execution.</interpreted_task>
    <constraints>All writes stay best-effort/try-except (a telemetry write must NEVER fail or
      delay a real task — A19 invariant). The reader must be read-only and side-effect-free.
      MESH/gateway behavior byte-identical. Plain pytest only.</constraints>
    <non_goals>Auto-spawn (M3). Gating execution on stage. A trace HTTP API (A22). UI.</non_goals>
    <assumptions>A20 landed the §11 columns + get_flow_run/update_flow_fields; the A19 hooks
      `_record_flow_run_start` / `_record_flow_stage` exist in orchestrator.py ~L1669-1699.</assumptions>
    <drift_risks>Letting a reader influence what runs (turns a RECORD into a controller
      prematurely — that's M3, gated). Making a flow write blocking.</drift_risks>
  </objective_lock>

  <approved_plan>
    <steps>
      1. Define stage-write points across the loop and call update_flow_fields there:
         at execution start (executing), at checkpoint (checkpoint_review), at completion
         (closed) / failure (blocked). Persist execution_result/implementation_review when
         known. Keep dispatch_start/queued from A19.
      2. Populate artifact columns opportunistically from data already in-hand
         (objective_lock/approved_plan from the packet metadata if present; artifact_links).
      3. Add `flow_status_summary(flow_run_id|task_id)` — read-only, returns stage + key
         artifact links for reporting. Wire it into a report/log path ONLY (no gate).
      4. Tests: stage advances end-to-end on a simulated task; a forced write failure does
         NOT fail the task (invariant); reader returns correct stage; no execution path changed.
    </steps>
    <validation>New tests green; existing test_flow_runs.py green; a fault-injected flow write
      leaves task execution unaffected.</validation>
    <definition_of_done>current_stage tracks the real loop; §11 artifact columns populated where
      data exists; one benign reader in use; zero execution-behavior change; tests green.</definition_of_done>
    <risks>Best-effort writes silently no-op if DB unavailable (acceptable — matches A19).</risks>
  </approved_plan>

  <execution_rules>
    <do>Keep every flow write in try/except. Branch `feat/m1-flow-instrumentation`; PR at close.</do>
    <do_not>Gate or drive execution off current_stage. Add an HTTP API. Block on a flow write.</do_not>
    <report_format>Milestone Live Log + closure; PR link.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone
## Objective
current_stage reflects the real loop; one benign reader.
## Current Status
drafting
## Burndown
- [ ] stage-write points wired (executing/checkpoint/closed/blocked)
- [ ] §11 artifact columns populated where data exists
- [ ] flow_status_summary reader (report-only)
- [ ] tests: end-to-end stage advance + fault-injection invariant
## Live Log
- (executor appends here)
## Blockers
- Depends on A20.
## Next Action
Await A20 merge + roadmap approval.
