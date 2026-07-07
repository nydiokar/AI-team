# A21 — Flow schema extension: record → full §11 model + lineage (M1)

**Milestone:** v0.6 M1. **Level:** 3 (migration). **Branch:** code → `feat/m1-flow-schema` → PR.
**Parallelism:** runs **in parallel with A20** (disjoint files). **A22 and A23 depend on this**
(they need the columns + stage vocabulary + `get_flow_run`), so this lands first among the code loops.

```xml
<task_packet>
  <meta><task_name>A21-flow-schema-extension</task_name><harness_level>3</harness_level></meta>
  <objective_lock>
    <real_objective>The flow_runs table can durably hold a full v0.4 §11 flow record plus the
      dispatch-lineage columns, so M2/M3 wire behavior onto columns that already exist — no
      second migration for the planned work.</real_objective>
    <literal_request>"promote the 5-column flow record to the full state model"</literal_request>
    <interpreted_task>One additive migration extending flow_runs with the §11 fields —
      approved_plan, plan_review, burn_down_items, execution_result, implementation_review,
      waived_findings, closure_summary, role_assignments, artifact_links, status, updated_at —
      AND the three lineage columns: parent_flow_run_id, dispatched_by, dispatch_file.
      Extend create/update methods to persist them; add get_flow_run(id) if absent; define the
      stage vocabulary as a code constant. All new columns NULLable so existing rows/writers are
      unaffected.</interpreted_task>
    <constraints>Additive ONLY — no column drop/rename, no backfill that rewrites existing rows.
      Nothing reads current_stage to drive execution (unchanged from A19). No paid CLI; plain
      pytest only. Migration must be idempotent + follow the existing db.py migration pattern
      (see migration 21, the A19 flow_runs migration, as the template).</constraints>
    <non_goals>No stage-transition writing (A22). No lineage POPULATION (M2 wires it; this only
      adds the columns). No read API (A23). No execution reading any of these fields. Do NOT add
      worker_task_ids — child→parent is recovered by reverse-lookup on parent_flow_run_id
      (deliberately dropped to avoid a redundant JSON column).</non_goals>
    <assumptions>Current flow_runs = exactly 5 cols (flow_run_id, task_id, current_stage,
      objective_lock, created_at) at src/control/db.py ~L224; A19 methods create_flow_run/
      update_flow_stage/list_flow_runs exist ~L1259-1293 — VERIFY both before writing the
      migration. A19 was migration 21, so this is the next migration.</assumptions>
    <drift_risks>Non-additive change breaking A19's byte-identical guarantee; a non-idempotent
      migration; execution starting to depend on new fields; re-freezing lineage wrong (mitigated:
      additive ALTERs make a later M2 column cheap — do not gold-plate).</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Read db.py flow_runs DDL + the migration-registration pattern (migration 21).
      2. Add the next migration (idempotent additive ALTER/CREATE) with the §11 fields +
      parent_flow_run_id + dispatched_by + dispatch_file, all TEXT/NULLable (JSON-encoded where
      structured). 3. Extend create_flow_run/update to accept+persist the new fields (all optional;
      set updated_at on update). 4. Add get_flow_run(flow_run_id) if missing. 5. Define the stage
      vocabulary constant (intent→objective_lock→plan→plan_review→execution→impl_review→closure);
      keep A19's existing legacy values valid. 6. Tests: migration applies clean on a fresh AND an
      existing DB; re-run is a no-op (idempotent); new fields round-trip; absent fields stay NULL;
      a pre-existing 5-column row still reads; A19 create/update path unchanged.</steps>
    <validation>pytest new flow-schema tests + existing tests/test_flow_runs.py green;
      `--collect-only` clean; migration idempotent (apply twice, no error).</validation>
    <definition_of_done>flow_runs holds the full §11 field set + parent_flow_run_id/dispatched_by/
      dispatch_file; methods persist them; get_flow_run exists; stage vocab defined; zero behavior
      change when new fields are unused.</definition_of_done>
    <risks>Migration ordering vs the concurrent A19 best-effort hook — keep additive + NULLable so
      an in-flight A19 write is always valid.</risks>
  </approved_plan>
  <execution_rules>
    <do>Update milestone Live Log per step; commit at checkpoints; open a PR at close
      (`gh pr create --fill --base main`). Do NOT edit DISPATCH_LOG (A20 owns the batch rows).</do>
    <do_not>No non-additive schema change; no execution dependency on new fields; no worker_task_ids
      column; no paid CLI; no `python main.py status`.</do_not>
    <report_format>closure_summary.md + `/code-review` on the committed diff; relay to operator with the PR link.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone: A21 flow schema extension
**Current Status:** merged to `main` (Manager-reviewed against git; 13 tests green)
**Burndown:** [x] read DDL + migration-21 pattern · [x] additive migration (§11 + 3 lineage cols) · [x] extend create/update + get_flow_run · [x] stage-vocab constant · [x] tests (fresh + existing + idempotent + A19 intact) · [x] PR
**Live Log:**
- dispatched 2026-07-06
- 2026-07-07: verified live state in src/control/db.py — flow_runs was exactly 5 cols (L224); A19 create_flow_run/update_flow_stage/list_flow_runs at L1253; migration 21 = A19; `_CURRENT_VERSION` was 21.
- 2026-07-07: added migration 22 (14 additive TEXT/NULLable cols: 11 §11 fields + parent_flow_run_id/dispatched_by/dispatch_file). Extended create_flow_run (optional kwargs), update_flow_stage (now stamps updated_at), added update_flow_run + get_flow_run. Added FLOW_STAGES constant. Bumped `_CURRENT_VERSION` to 22.
- 2026-07-07: tests green — 10 new (test_flow_schema_extension.py) + 3 existing (test_flow_runs.py, version/col assertions updated for the additive migration); 63 db/flow/migration tests pass overall. Committed 7a57e2b.
- 2026-07-07: Manager reviewed diff in git (additive-only, idempotent guard, no current_stage reader), re-ran tests (13 passed), merged to `main`.
**Next Action:** closed. A22 + A23 (Wave 2) now branch off this schema.
