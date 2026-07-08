# A28 — Mobile Work surface: read-only operations inbox + session affiliation

**Dispatch created:** 2026-07-08
**Milestone:** Work Control Substrate, A28 of A25-A29.
**Level:** 2 (frontend + read-only API consumption). Code branch required: `feat/mobile-work-surface` → PR.
**Depends on:** A27 merged.
**Spec:** [`docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md`](../../docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md).

```xml
<task_packet>
  <meta><task_name>A28-mobile-work-surface</task_name><harness_level>2</harness_level></meta>
  <objective_lock>
    <real_objective>The phone UI gets an honest Work operations inbox that surfaces cases needing
      attention, while Sessions remains the universal runtime/session inspector.</real_objective>
    <literal_request>"add the mobile Work tab without making a workflow theater UI"</literal_request>
    <interpreted_task>Add a bottom-nav Work tab backed only by the A27 read model. Render active
      cases, needs-decision cases, blocked/rework/review cases, and case detail with ledger,
      compact lineage, and evidence/timeline links. Add session affiliation labels in Sessions
      using authoritative Work read data.</interpreted_task>
    <constraints>Mobile-first. Read-only. No editable graph/canvas. No workflow creation. No action
      buttons except navigation/drill-down. No client-side inference from sessions/tasks if the Work
      API does not provide the relationship. Text must fit small screens. Preserve standalone New
      Session flow.</constraints>
    <non_goals>No Start Work entrypoint yet. No Manager automation. No approve/rework/close actions.
      No desktop workflow editor. No raw transcript as primary view.</non_goals>
    <assumptions>A27 exposes enough explicit authority/confidence/staleness fields for the UI.
      Existing frontend data hooks/adapters pattern should be followed.</assumptions>
    <drift_risks>Letting the tab become a graph editor; duplicating SessionDetail as WorkDetail;
      hiding runtime sessions; showing inferred labels for unlinked sessions.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Add domain/transport types and hooks for Work list/detail/timeline/graph. 2. Add
      Work tab to bottom navigation: Work | Sessions | System. 3. Build WorkScreen:
      Needs decision, Active, Blocked/rework/review, Recent closed collapsed. 4. Build WorkDetail:
      header, ledger, compact lineage, evidence/timeline, links to sessions/artifacts. 5. Add
      session affiliation labels in SessionRow/SessionDetail using authoritative links from Work
      data. 6. Tests and typecheck; visual/manual sanity on mobile viewport.</steps>
    <validation>npm test/tsc/build as appropriate; Playwright or screenshot sanity if local app
      already supports it; no text overflow on mobile; no client-side heuristic ownership joins in
      code review.</validation>
    <definition_of_done>Operator can open Work, see cases needing attention, drill into a case,
      and jump to linked sessions/evidence; Sessions still lists all sessions including standalone
      and workflow-owned.</definition_of_done>
    <risks>Frontend scope. Keep actions out; this is read-only navigation and operational truth.</risks>
  </approved_plan>
  <execution_rules>
    <do>Update milestone Live Log; commit at checkpoint; start dev server if needed and report URL at close.</do>
    <do_not>No creation/actions, no editable DAG, no inferred ownership, no marketing/landing UI, no paid CLI.</do_not>
    <report_format>Closure with screenshots/checks, files changed, and residual UI gaps.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone

**Current Status:** built (pending live device screenshots — needs flag ON, see Closure)
**Burndown:**
- [x] Add Work domain/transport/hooks
- [x] Add Work bottom-nav tab
- [x] Build Work list screen
- [x] Build Work detail screen
- [x] Add session affiliation labels
- [x] Run frontend tests/typecheck/build and mobile sanity
- [~] Append Closure (below); DISPATCH_LOG/CONTEXT advanced by manager at merge (handoff §4)

**Next Action:** operator merges `feat/work-surface-a28-a29`; to see populated Work
data live, set `HARNESS_FLOW_DRIVE=on` + restart gateway (handoff §3), then capture
device screenshots. A29 wires the session-role links that fill affiliations.

## Closure

**Date:** 2026-07-08 · **Branch:** `feat/work-surface-a28-a29`

### What shipped (read-only, honesty-first)
A mobile **Work** tab (bottom nav is now Work | Sessions | System), backed ONLY by
the A27 read model — no mutations, no editable DAG, no client-side ownership
inference.

- **Transport/domain/hooks:** `transport/rawApi.ts` (RawCase*/RawFlow* shapes),
  `transport/workAdapter.ts` (+ pure `caseTitle` derivation), `domain/work.ts`,
  `transport/apiClient.ts` (`work`/`workDetail`/`workTimeline`/`workGraph`),
  `hooks/useWork.ts` (`useWorkList`/`useWorkDetail`/`useWorkTimeline`/`useWorkGraph`
  + `useSessionAffiliations`).
- **WorkScreen** (`screens/WorkScreen.tsx`): operations inbox grouped by the
  AUTHORITATIVE bucket order — Needs decision, Blocked/rework, In review, Active,
  Recently closed (collapsed). Empty substrate renders an honest "No cases yet"
  that points runtime users at the Sessions tab (does not hide sessions).
- **WorkDetailScreen** (`screens/WorkDetailScreen.tsx`): header (bucket + status +
  stage + flow/task/dispatch facts), compact vertical **lineage** (parent/self/
  children from `/graph`), grouped **ledger** (sessions deep-link to Sessions;
  other entities shown as honest id+role refs; empty sections explicit), and the
  append-only **timeline**. 404 → "Case not found".
- **Session affiliation labels:** `SessionRow` + `SessionDetail` show a session's
  authoritative case role (Manager/Worker/Reviewer/Evidence) sourced ONLY from
  each case's `ledger.sessions`. Absent ⇒ standalone, never inferred.

### Validation
- `npm run typecheck` clean · `npm test` 86 passed (12 files; +8 new: 20-case
  `workAdapter.test.ts` already present in tree, matched exactly + verified; new
  `workPresentation.test.ts`) · `npm run build` OK.
- Live gateway healthy (`/health` ok, untouched). `/api/work*` routes confirmed
  live on the running gateway (403 without token, identical to `/api/sessions`).

### Residual gaps (honest)
1. **Affiliations render "Standalone" until A29.** Confirmed the A26 write path
   only stamps `task/root_task` + `child_flow` links today; **session-role links
   are the A29 deferred seam** (handoff §5). The UI is built and correct — it will
   light up the moment A29 lands those links, with ZERO UI change. Until then,
   honest standalone is the correct render.
2. **No populated live screenshots.** The substrate only fills with
   `HARNESS_FLOW_DRIVE=on` (default OFF; a restart drops the operator session —
   handoff §3, operator's call). Empty-state and route wiring are verified; a
   populated device pass is the one remaining manual check, to run post-flag.
3. `useSessionAffiliations` resolves one cached case-detail fetch per case (no
   bulk reverse endpoint in A27); shares the WorkDetail query key so it is warm
   and makes ZERO fetches while the substrate is empty. If a bulk session→case
   index is wanted later, that is an A29/backend follow-up, not a UI change.
