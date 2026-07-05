# AGENT_19 — FlowRun record (v0.4 §13 item 1) — **operator override of COLD verdict**

> **Level 3.** New gateway state + a DB migration. Operator pre-approved and explicitly
> **overrode the promotion-ladder COLD verdict** for Row 1 (`flow_runs`) on 2026-07-05.
> This packet records the override as a first-class fact — see `<override_record>`. Future
> Managers: **this is NOT a tripped trigger.** The Row 1 evidence gate (≥2 lost-handoff
> resumes) was **never observed**; this was built as an operator-directed experiment.

---

## The XML packet (locked)

```xml
<task_packet>
  <meta>
    <task_name>A19-flow-runs-record</task_name>
    <harness_level>3</harness_level>
    <branch>feat/harness-flow-runs (cut fresh from main, 2026-07-05)</branch>
  </meta>

  <override_record>
    v0.4 §13 item 1 "FlowRun record" is promotion_ladder.md Row 1, standing verdict COLD
    ("Phase 2 = NO", A12). Its trigger — a multi-slice dispatch losing handoff state across
    ≥2 resumes, recorded ≥2× — was NEVER observed (A12/A13/A14/A18 ran with zero lost state).
    The OPERATOR explicitly overrode this on 2026-07-05 to begin building the autonomous-loop
    engine. This packet builds the smallest additive slice under that override. The override,
    not a tripped gate, is the authority. It must be recorded in promotion_ladder.md at close.
  </override_record>

  <objective_lock>
    <real_objective>The gateway can PERSIST and QUERY a minimal flow-state record — one row
      per dispatch flow, carrying flow_run_id, task_id, current_stage, objective_lock, created_at
      — as the first real code brick of the v0.4 autonomous-loop engine. "Smallest shippable
      slice": a durable record + a write path + a read path + tests, and NOTHING that drives or
      gates existing task execution.</real_objective>
    <literal_request>"a new mesh.db table flow_runs (flow_run_id, task_id, current_stage,
      objective_lock TEXT, created_at) written by the orchestrator when a dispatch starts and
      updated at stage transitions, plus one read path (/api/flows or a db.list_flows) and
      pytest coverage."</literal_request>
    <interpreted_task>
      1. Migration 21: CREATE TABLE flow_runs (additive; no ALTER of existing tables).
      2. db.create_flow_run(task_id, current_stage, objective_lock) -> flow_run_id.
      3. db.update_flow_stage(flow_run_id, current_stage) -> updates the row.
      4. db.list_flow_runs(...) -> read path (the "db.list_flows" alternative; NOT the HTTP one).
      5. A THIN, GUARDED orchestrator hook that calls create_flow_run at dispatch-start and
         update_flow_stage at a stage transition — write-only, wrapped so a failure NEVER
         breaks task execution (best-effort telemetry, not a dependency).
      6. pytest: table exists after migration, create/update/list round-trip, orchestrator
         hook writes a row without altering task outcome, failure of the hook is swallowed.
    </interpreted_task>
    <constraints>
      - ADDITIVE ONLY. New table + new methods + one guarded write hook. Do NOT read flow_runs
        from any existing code path. Do NOT modify mesh_tasks, the task lifecycle, _enqueue_task
        admission logic, or any stage/gate behavior.
      - It is a RECORD, not a driver/stage-machine. Nothing reads current_stage to decide what
        runs next. (v0.4 §13 says "FlowRun record"; a driver is out of scope — see non_goals.)
      - Migration framework discipline: append (21, sql) to _get_migrations() AND bump
        _CURRENT_VERSION 20 -> 21. Fresh-DB and existing-DB converge through the same path.
      - No paid CLI. Plain `pytest` only on the new test file(s). Never `python main.py status`.
      - The orchestrator hook must be best-effort: try/except around the DB write, logged at
        debug/warning, so a write failure cannot fail or delay a real task.
    </constraints>
    <non_goals>
      - NO stage machine / driver. No code transitions a flow through stages automatically.
      - NO /api/flows HTTP endpoint (deferred — db.list_flow_runs is the shippable read path;
        the operator offered "/api/flows OR db.list_flows"; take the smaller).
      - NO WebUI, no Telegram surface, no dashboard.
      - NO wiring current_stage into any decision, routing, or gate.
      - NO retrofilling flow_runs for historical tasks.
      - NO change to DISPATCH_LOG-as-state or the file-and-dispatch convention.
      - NO extra columns beyond the 5 named + flow_run_id PK (approved_plan/plan_review/
        burn_down_items etc. from v0.4 §11 are explicitly OUT — smallest slice).
    </non_goals>
    <assumptions>
      - VERIFY in code, do NOT trust this packet: src/control/db.py holds _DDL, _CURRENT_VERSION
        (=20 as of grounding), _get_migrations() (last row 20), and list_*/enrich_task methods
        as the pattern to copy. Migration 17 (mesh_tasks ADD COLUMN block) and 19/20 (CREATE
        TABLE in a migration) are the shape templates.
      - flow_run_id is a uuid4 hex string (uuid module already imported in db.py). created_at
        uses the existing _now() helper (UTC-aware ISO). objective_lock stored as TEXT (caller
        may pass a JSON string; the table does not parse it).
      - task_id may be reused across flows; flow_run_id is the PK, task_id is indexed non-unique.
    </assumptions>
    <drift_risks>
      - Scope-creep into a stage MACHINE (auto-advancing stages) — the #1 forbidden thing.
        STOP if you find yourself making anything READ current_stage to decide behavior.
      - Scope-creep into the full v0.4 §11 column set — only the 5 named columns.
      - Building the HTTP /api/flows endpoint — explicitly a non-goal this slice.
      - Making the orchestrator hook a hard dependency (task fails if the DB write fails) —
        it MUST be swallowed.
      - Touching mesh_tasks or the admission/enqueue path — additive-elsewhere only.
    </drift_risks>
  </objective_lock>

  <approved_plan>
    <steps>
      1. db.py: add flow_runs to _DDL as a CREATE TABLE IF NOT EXISTS (so fresh DBs get it),
         AND add (21, "CREATE TABLE IF NOT EXISTS flow_runs (...)") to _get_migrations() (so
         existing DBs get it), AND bump _CURRENT_VERSION to 21. (Mirror how 19/20 did both.)
      2. db.py: add create_flow_run(), update_flow_stage(), list_flow_runs() near the other
         list_/enrich_ methods, using self._conn() and the existing lock/commit pattern.
      3. orchestrator.py: at the dispatch-start point (where a task is enqueued/dispatched),
         add a best-effort create_flow_run(...) call in try/except; at ONE clear stage
         transition, a best-effort update_flow_stage(...). Locate the real call site by
         grep, do not invent one.
      4. tests/test_flow_runs.py: migration/round-trip/guarded-hook tests (see validation).
    </steps>
    <validation>
      - `pytest tests/test_flow_runs.py -q` green.
      - Migration: open a fresh MeshDB on a temp path → schema_version MAX == 21 and
        flow_runs table exists (sqlite_master query).
      - Round-trip: create_flow_run returns an id; row has the 5 fields; update_flow_stage
        changes current_stage; list_flow_runs returns it; filter by task_id works.
      - Guard: monkeypatch create_flow_run to raise → the orchestrator hook does NOT propagate
        (task path unaffected). (Assert the exception is swallowed / logged.)
      - No existing test regresses: `pytest tests/test_control_api.py tests/test_telemetry_store.py -q`
        (DB-touching suites) still green. Targeted only — NO full e2e, NO paid CLI.
    </validation>
    <definition_of_done>
      Migration 21 applies on fresh AND existing DBs; the 3 db methods round-trip; a guarded
      orchestrator hook writes a flow_runs row at dispatch-start + one transition WITHOUT being
      able to fail a task; tests green; nothing reads current_stage to drive behavior; only the
      5 named columns exist. Committed diff is additive (new table, new methods, one guarded
      hook, one test file).
    </definition_of_done>
    <risks>Migration ordering / version-bump mismatch (fresh vs existing DB divergence) is the
      classic bug — the test that opens a fresh temp DB and asserts version==21 + table-exists
      is the guard. Orchestrator call-site: if none is cleanly "dispatch start", place the hook
      at the narrowest correct point and say so in the milestone rather than forcing it.</risks>
  </approved_plan>

  <execution_rules>
    <do>Update the ## Milestone Live Log after every meaningful step. Commit additively with a
      clear message. Grep for the real orchestrator dispatch-start site before wiring. Keep the
      hook best-effort (try/except).</do>
    <do_not>No stage machine. No reading current_stage to decide anything. No /api/flows. No
      extra columns. No paid CLI. No `python main.py status`. No touching mesh_tasks/enqueue.</do_not>
    <report_format>closure_summary.md shape: SHIPPED/PARTIAL/BLOCKED, per-file changes,
      verification commands + results, F-tag outcomes, what follows.</report_format>
    <when_to_stop>Stop when DoD is met and committed, OR if you hit the stage-machine/HTTP/extra-
      column drift line (stop and report — do not build past the slice), OR if there is no clean
      orchestrator dispatch-start call site (place the hook at the narrowest correct point, note
      it, and continue).</when_to_stop>
  </execution_rules>

  <context_snippets>
    <snippet id="S1" source="src/control/db.py L57, L1929-2018">
      <quote>_CURRENT_VERSION = 20 … _get_migrations() returns [(1,""),…,(20, "CREATE TABLE IF NOT EXISTS push_subscriptions (…)")]. "To add a migration: 1. Append (N, ...) 2. Bump _CURRENT_VERSION to N."</quote>
      <why_relevant>The exact migration pattern to copy; 19 & 20 show CREATE TABLE inside a migration.</why_relevant>
    </snippet>
    <snippet id="S2" source="src/control/db.py L2025 _now()">
      <quote>_now() returns datetime.now(tz=timezone.utc).isoformat() — always UTC-aware.</quote>
      <why_relevant>Use for created_at so the browser/telemetry timestamps stay consistent (no naive-utc skew scar).</why_relevant>
    </snippet>
    <snippet id="S3" source="promotion_ladder.md Row 1 + this packet override_record">
      <quote>flow_runs is COLD; trigger (≥2 lost-handoff resumes) NEVER observed; operator override 2026-07-05 is the authority, not a tripped gate.</quote>
      <why_relevant>Closure must record the override in promotion_ladder.md so it is never mistaken for a satisfied trigger.</why_relevant>
    </snippet>
  </context_snippets>
</task_packet>
```

---

## Manager self-review (adversarial pass over this packet — node 2/3)

Round 1 findings on my own draft:

**F1 (P0 — the forbidden drift) — "written by the orchestrator … updated at stage transitions" could pull the Executor into building a stage MACHINE.**
Failure scenario: Executor wires `update_flow_stage` into the task loop such that *something reads* `current_stage` to decide the next step — that is Phase 2's driver, the single most-forbidden thing, smuggled in under a "record."
Resolution: **fixed inline** — `<constraints>` "It is a RECORD, not a driver"; `<non_goals>` "NO stage machine"; `<drift_risks>` + `<when_to_stop>` both make "anything READS current_stage to decide behavior" an explicit STOP line.

**F2 (P1 — dependency-injection risk) — a naive orchestrator hook makes task execution depend on a DB write succeeding.**
Failure scenario: `create_flow_run` throws (locked DB, migration mid-flight) and a real user task fails or stalls because of a *telemetry* write — regression in the hot path.
Resolution: **fixed inline** — hook is `best-effort` try/except in `<constraints>`, `<risks>`, `<do>`, and there is a dedicated guard test in `<validation>`.

**F3 (P1 — migration fresh-vs-existing divergence) — adding the table to only `_DDL` OR only the migration list splits fresh and existing DBs.**
Failure scenario: table added to `_DDL` but not `_get_migrations()` → existing prod DB never gets it; or version not bumped → migration never runs. This is the classic scar.
Resolution: **fixed inline** — plan step 1 requires BOTH `_DDL` and a `(21, …)` migration AND the version bump, mirroring how 19/20 did it; `<validation>` opens a *fresh temp DB* and asserts `version==21` + table-exists.

**F4 (P1 — scope-creep into v0.4 §11's full column set) — §11 lists 14 columns; the Executor might "helpfully" add them.**
Resolution: **fixed inline** — `<non_goals>` "NO extra columns beyond the 5 named + flow_run_id PK"; `<drift_risks>` repeats it; DoD asserts "only the 5 named columns exist."

**F5 (P2 — HTTP endpoint ambiguity) — operator wrote "/api/flows OR db.list_flows".**
Resolution: **fixed inline** — chose `db.list_flow_runs` (the smaller shippable read path) explicitly in `<interpreted_task>` + `<non_goals>`; HTTP endpoint named as a derivable follow-up, not this slice. *(P2, but resolving it removes an execution fork, so kept.)*

Re-review round 2: no new P0/P1. **Locked** (1 fix round, under the 2-cap). Nothing unresolved → nothing spilled to non-goals beyond what's already there.

---

## Milestone

## Objective
Persist + query a minimal `flow_runs` record (5 fields) as the first additive code brick of the v0.4 loop engine, under an explicit operator override of the COLD verdict — without building any stage machine or touching existing task execution.

## Current Status
dispatched

## Burndown
- [ ] Migration 21: `flow_runs` table in `_DDL` + `_get_migrations()` + `_CURRENT_VERSION`→21
- [ ] `db.create_flow_run()` / `db.update_flow_stage()` / `db.list_flow_runs()`
- [ ] Best-effort (try/except) orchestrator write hook at dispatch-start + one transition
- [ ] `tests/test_flow_runs.py`: migration, round-trip, guarded-hook-swallows-failure
- [ ] Targeted regression suites green; committed additive diff
- [ ] (Manager) closure records the override in `promotion_ladder.md`

## Live Log
- 2026-07-05 — Manager drafted packet (Level 3, operator override grounded), self-reviewed → 5 F-tags fixed inline → locked after 1 round → dispatched to Executor.

## Blockers
none

## Next Action
Executor: implement per the locked packet; update this Live Log after each step.
