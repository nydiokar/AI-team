# A22 — Flow Trace Surface + instrument the manual Manager loop

> **STATUS: DRAFT — pending v0.6 roadmap approval. NOT yet in DISPATCH_LOG.**
> Milestone: **M1 — Durable Flow State Machine** (slice 3 of 3). Level **2**.
> Depends on: **A20 + A21**. Closes M1: "chase what's happening" becomes real for the
> STILL-MANUAL loop (the not-skipped phase before M3 automation).

## Packet

```xml
<task_packet>
  <objective_lock>
    <real_objective>Make "when a manager dispatches a worker I can chase what's happening"
      true for the current MANUAL loop — a read API over flow state + linking each manual
      dispatch (packet + worker task ids) to its parent flow. Traceability BEFORE autonomy.</real_objective>
    <literal_request>Add read-only `/api/flows` + `/api/flows/{id}` (list + detail from
      get_flow_run/list_flow_runs); update the driver + generators so the manual Manager
      records a flow per loop and links dispatch_file + worker_task_ids.</literal_request>
    <interpreted_task>Expose the M1 state as a read surface (loopback/tailscale-bound,
      read-only), and instrument docs/harness/manager_invocation.md + the generators so the
      human-driven Manager writes a flow_run per loop and populates dispatch_file /
      worker_task_ids / parent_flow_run_id — so the manual loop is traceable through the same
      mechanism M3 will later automate.</interpreted_task>
    <constraints>API is READ-ONLY (no state mutation via HTTP). Bind consistent with the
      existing control API (loopback/tailnet, not public). No new autonomous behavior. Docs
      changes must not contradict DOC_MAP roles. Plain pytest only.</constraints>
    <non_goals>Autonomous Manager invocation / worker auto-spawn (M3). A rendered UI/dashboard
      (optional, later). Write endpoints.</non_goals>
    <assumptions>A20+A21 landed (schema, get_flow_run, list_flow_runs, stage writes). The
      control API is FastAPI in src/control/task_server.py; session-trace precedent is
      /api/sessions/{id}/timeline.</assumptions>
    <drift_risks>Adding write/mutation endpoints (scope creep toward M3). Exposing the API
      publicly. Duplicating the session-timeline instead of reusing its pattern.</drift_risks>
  </objective_lock>

  <approved_plan>
    <steps>
      1. Add read-only `GET /api/flows` (list, newest first, optional ?task_id=) and
         `GET /api/flows/{flow_run_id}` (detail incl. stage + artifact links + worker_task_ids),
         backed by list_flow_runs/get_flow_run. Same auth/bind as existing control API.
      2. Update docs/harness/manager_invocation.md: the Manager records a flow_run at LOOP 0,
         advances/links it through the loop, and stores dispatch_file + worker_task_ids +
         parent_flow_run_id. (Prose convention — the human-driven Manager does this; no code
         forces it yet.)
      3. Update generators (draft_packet / closure_summary) to reference the flow_run id so the
         packet<->flow link is bidirectional.
      4. Tests: API returns correct list/detail; unknown id → 404; read-only (no mutation path).
    </steps>
    <validation>API tests green; a manually-recorded flow is retrievable end-to-end; docs pass
      a DOC_MAP anti-overlap read.</validation>
    <definition_of_done>`/api/flows[/{id}]` read surface live; manual loop records+links flows;
      operator can query "which flow, what stage, which worker tasks" without grepping files.</definition_of_done>
    <risks>Manual convention can be skipped by a lazy Manager (acceptable in M1; M3 makes it
      enforced/automatic).</risks>
  </approved_plan>

  <execution_rules>
    <do>Read-only endpoints. Reuse the session-timeline API pattern + auth. Branch
      `feat/m1-flow-trace`; PR at close. Docs-only edits may go straight to main per branch policy.</do>
    <do_not>Add mutation endpoints. Expose publicly. Build a renderer. Auto-spawn anything.</do_not>
    <report_format>Milestone Live Log + closure; PR link.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone
## Objective
Read API + manual-loop flow instrumentation = traceable manual loop (M1 closed).
## Current Status
drafting
## Burndown
- [ ] GET /api/flows + /api/flows/{id} (read-only)
- [ ] manager_invocation.md records+links a flow per loop
- [ ] generators reference the flow_run id (bidirectional link)
- [ ] tests: list/detail/404/read-only
## Live Log
- (executor appends here)
## Blockers
- Depends on A20 + A21.
## Next Action
Await A20+A21 + roadmap approval.
