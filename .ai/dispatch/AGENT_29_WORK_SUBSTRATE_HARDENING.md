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

**Current Status:** built (pending merge + optional live flag pass)
**Burndown:**
- [x] Read A25-A28 closures and implemented code
- [x] Run adversarial review with F-tags
- [x] Add/fix stale/conflict/no-heuristic tests
- [x] Patch P0/P1 defects only
- [x] Update durable docs and milestone status
- [x] Run targeted verification
- [~] Append Closure (below); DISPATCH_LOG/CONTEXT advanced by manager at merge

**Next Action:** operator merges `feat/work-surface-a28-a29`. To see populated Work/affiliation
data live: `HARNESS_FLOW_DRIVE=on` + gateway restart (drops the active session — operator's call).

## Closure

**Date:** 2026-07-09 · **Branch:** `feat/work-surface-a28-a29`

### Spot-check of A28 (built upon)
Swept A28's frontend + the A25–A27 substrate it consumes. Verdict: clean, honesty-first,
well-tested (typecheck clean, 86 tests green at entry). **One real defect** — the session
affiliation index fetched only the first 100 cases and fanned out one detail request per
case, so a session authoritatively linked to a case in a >100 backlog rendered a **false
`Standalone`** (violates authority rule 7). Fixed as the P0 of this job.

### What shipped
1. **Affiliation index — de-capped + de-fanned (P0 fix).** New authoritative whole-substrate
   JOIN `db.list_session_case_links` (newest-link-first, unbounded) → pure
   `build_session_affiliations` → read-only `GET /api/work/affiliations/sessions`. Frontend
   `useSessionAffiliations` now issues ONE query (was N); `toSessionAffiliationIndex` derives
   the title with the SAME `caseTitle` used elsewhere (one source of truth). No UI/component
   change — labels just stop lying at scale.
2. **Deferred A26 seams (additive, flag-gated, best-effort):** session attachment
   (`session`/`worker` link + `session.attached`), terminal OUTCOME
   (`flow.closed`+status`closed` / `flow.status_changed`+status`blocked`), approval lifecycle
   (`approval` link + `approval.requested`/`approval.resolved`).
3. **Adversarial fixtures/tests:** `tests/test_flow_substrate_hardening.py`,
   `tests/test_session_affiliations.py`, read-model authority fixtures in
   `tests/test_work_read_model.py`, + `toSessionAffiliationIndex` frontend tests.

### F-tag outcomes
F1 held, **F2 defect found & fixed** (false Standalone cap), F3/F4/F7/F8 held. Full write-up
in `docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md` → "A29 Closure — Milestone Achieved".

### Deliberately deferred (honest, not fabricated)
`review.*` events (no reviewer role until M3); `flow.interrupted` on cancel; mobile inbox
100-case list cap (attention-first UX bound — the *affiliation* index is uncapped).

### Validation
Backend: 244 passed / 2 skipped across the substrate+control+approval regression. Frontend:
typecheck clean, vitest 90 passed. **No schema change** (reuses existing `flow_runs.status` +
A25 tables). Flag OFF ⇒ byte-identical (proven by tests). Live gateway untouched (`/health` ok).
