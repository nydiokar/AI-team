# A23 — Flow read API + minimal operator surface (M1)

**Milestone:** v0.6 M1. **Level:** 2. **Branch:** code → `feat/m1-flow-read-api` → PR.
**Depends on:** A21 (schema + get_flow_run). Reads what A22 writes but only needs A21 to build.
**Parallelism:** after A21 lands, runs **in parallel with A22** — disjoint files (A23 =
task_server.py; A22 = orchestrator surface).

```xml
<task_packet>
  <meta><task_name>A23-flow-read-api</task_name><harness_level>2</harness_level></meta>
  <objective_lock>
    <real_objective>The operator can query "what flows exist and at what stage" from the durable
      table — the first payoff of the state machine: state you can chase, not grep.</real_objective>
    <literal_request>"let me see the flows"</literal_request>
    <interpreted_task>Add read-only control API: GET /api/flows (list: flow_run_id, task_id,
      current_stage, status, created_at, updated_at) and GET /api/flows/{id} (full §11 record).
      Read-only; reuse list_flow_runs / a get_flow_run. Loopback/tailnet-bound like the rest of
      the control API.</interpreted_task>
    <constraints>Read-only — no mutation endpoints. No new state. Honest fields: NULL stays NULL,
      never fabricated. Auth/binding consistent with existing control API. No paid CLI; pytest.</constraints>
    <non_goals>No write/transition endpoints. No lineage rendering (M2). No cockpit graph view
      (M2). No auth redesign.</non_goals>
    <assumptions>Control API host is loopback on kanebra (check §9003 health from kanebra, not a
      worker box) — VERIFY the existing API auth/bind pattern before adding routes.</assumptions>
    <drift_risks>Adding a mutation endpoint; exposing on a public bind; fabricating absent fields.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Read the existing control API route + auth pattern (task_server.py). 2. Add
      get_flow_run(id) to db.py if absent. 3. Add GET /api/flows + /api/flows/{id} (read-only).
      4. Tests: list returns rows; detail returns full record; unknown id ⇒ 404; NULL fields
      serialize as null. 5. Verify live with curl http://127.0.0.1:9003/... from kanebra.</steps>
    <validation>pytest green; curl returns expected JSON from kanebra; 404 on unknown id; no
      mutation route present (grep).</validation>
    <definition_of_done>Operator can list flows + read one flow's full state over the control
      API, read-only, honest nulls.</definition_of_done>
    <risks>Bind/auth mismatch — mirror the existing routes exactly.</risks>
  </approved_plan>
  <execution_rules>
    <do>Update milestone Live Log; commit at checkpoints; open PR at close. Do NOT edit
      DISPATCH_LOG (A20 owns the batch rows).</do>
    <do_not>No mutation endpoint; no public bind; no python main.py status; no paid CLI.</do_not>
    <report_format>closure_summary.md + /code-review + /security-review on committed diff; relay.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone: A23 flow read API
**Current Status:** dispatched
**Burndown:** [ ] read API+auth pattern · [ ] get_flow_run · [ ] 2 read routes · [ ] tests (list/detail/404/null) · [ ] live curl from kanebra · [ ] PR
**Live Log:** — dispatched 2026-07-06
**Next Action:** worker studies the existing control-API route pattern before adding routes.
