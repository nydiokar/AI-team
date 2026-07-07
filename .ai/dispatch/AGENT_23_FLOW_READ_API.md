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
**Current Status:** code complete — branch pushed, awaiting Manager merge decision
**Burndown:** [x] read API+auth pattern · [x] get_flow_run (from A21) · [x] 2 read routes · [x] tests (list/detail/404/null) · [~] live curl (deferred — code not deployed to live gateway; verified via TestClient instead) · [x] branch pushed (gh not installed → no PR opened)
**Live Log:**
- dispatched 2026-07-06
- 2026-07-07 — Studied precedent. The `/api/*` control surface is `src/control/control_api.py` (NOT
  `task_server.py`, which is the mesh/worker Bearer API). Precedent routes: `api_turns` (list →
  `JSONResponse({"turns": ...})`) + `api_turn_detail` (detail → 404 `turn_not_found`). Auth =
  `Depends(_require_auth)` (Bearer DASHBOARD_TOKEN/WORKER_TOKEN). Bind = the same in-process app,
  served by `EmbeddedControlServer` on `config.mesh.control_api_host or tailscale_ip or 127.0.0.1`
  (orchestrator.py:1381) — loopback/tailnet, never 0.0.0.0.
- 2026-07-07 — Dependency reality check: this worktree branched off `main` BEFORE A21 merged;
  local `main` has only the 5-col A19 flow_runs record and NO `get_flow_run`. The §11 migration 22 +
  `get_flow_run` + `list_flow_runs` live on branch `feat/m1-flow-schema` (A21). Per standing-rule-1
  (verify in-file, don't trust the prompt), based `feat/m1-flow-read-api` on `feat/m1-flow-schema`
  so A23 builds on the real A21 foundation.
- 2026-07-07 — Added two READ-ONLY routes to control_api.py: `GET /api/flows` (list, summary
  projection, optional task_id filter, limit 1..500) + `GET /api/flows/{id}` (full §11 record, 404
  `flow_not_found`). Both delegate verbatim to `db.list_flow_runs` / `db.get_flow_run`; no mutation,
  no new state, honest nulls. Grep-proof: diff adds only `@app.get`, no post/put/patch/delete, no
  `0.0.0.0`/host change.
- 2026-07-07 — Tests (tests/test_control_api_flows.py, plain pytest + FastAPI TestClient + injected
  MeshDB): 8 passed. Full control-API suite (test_control_api.py + new): 36 passed, no regressions.
  Live gateway `127.0.0.1:9003/health` = ok (untouched — not redeployed).
- 2026-07-07 — /code-review + /security-review on the committed diff: no P0/P1 (no findings). Branch
  `feat/m1-flow-read-api` pushed to origin. gh not installed → PR link:
  https://github.com/nydiokar/AI-team/pull/new/feat/m1-flow-read-api
**Next Action:** Manager reviews + merges (worker does NOT merge). Note the base is A21's branch, so
merge A21 first (or ensure A21 is in main) before/with A23.
