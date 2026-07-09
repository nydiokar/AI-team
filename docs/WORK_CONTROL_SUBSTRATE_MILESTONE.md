# Work Control Substrate Milestone

**Status:** planned milestone, operator-aligned on 2026-07-08.
**Roadmap position:** v0.6 M2 foundation, before Manager-as-invoked-role (M3) and before
any product-grade mobile Work UI.
**Recommendation:** build now as a foundational dependency.

This milestone turns the current `flow_runs` record into an honest operational substrate
for mobile work control. It does **not** build autonomous manager behavior. It does **not**
build a desktop workflow editor. It creates the durable relationships and read model needed
before those things can be safe or useful.

## Decision

Use the hybrid product model:

- **Work / Case** is the operational layer: objective, stage, linked tasks, decisions,
  reviews, blockers, lineage, closure, and audit trail.
- **Session** is the runtime layer: backend connection, model, machine, transcript, and
  execution context.
- **Task** (`mesh_tasks`) is the delegated execution ledger.
- **Timeline** is evidence, not authority.
- **Graph/DAG** is navigation over authoritative lineage, not an editable planning canvas.

User-facing vocabulary should prefer **Work** for the mobile tab and **Case** for a
durable work item. Keep `flow_runs` as the implementation name.

## Why This Is Now

The existing M1 work shipped the `flow_runs` row and read API, but the read model is not
yet sufficient for a truthful Work UI:

- `flow_runs.task_id` links one task, but there is no general authoritative link from a
  flow to all tasks, sessions, approvals, artifacts, and jobs.
- `parent_flow_run_id` and `dispatched_by` exist, but M2 lineage population is not wired.
- `current_stage` is a mutable, best-effort record; it cannot be the only audit trail.
- session timelines are strong evidence, but they are session-scoped.
- approvals and reviews are not reliably scoped to a flow/case.

Building Work UI directly on these partial correlations would force fragile heuristics.
The substrate must come first.

## Product Goal

A phone-sized operator view can answer these questions without reading dispatch prose:

- What active work exists?
- What needs a decision now?
- Which manager/worker/reviewer sessions belong to this case?
- Which tasks, approvals, artifacts, and watched jobs are evidence for this case?
- Which worker was dispatched by which parent case or manager decision?
- Is the case blocked, stale, in review, in rework, closed, or superseded?
- What durable events explain how it reached this state?

## Scope

### In Scope

- Add authoritative relationships between cases and runtime/execution entities.
- Add append-only case events for audit and recovery.
- Populate links/events on existing dispatch, approval, session, and artifact paths where
  the relationship is known.
- Build a read-only Work read model API over those authoritative records.
- Add a minimal mobile Work tab only after the read model is truthful.
- Label Sessions as standalone, manager, worker, reviewer, or evidence for a case.
- Preserve current session-first ad hoc work.

### Out of Scope

- No autonomous Manager role yet.
- No Manager-spawns-worker behavior unless lineage is already observable.
- No editable DAG or workflow canvas.
- No new broad workflow engine.
- No transcript parsing to infer state.
- No prose-based "accepted" or "closed" inference.
- No execution path that reads `current_stage` to decide what runs.
- No public bind or auth redesign.

## Minimal Authoritative Model

Keep the current `flow_runs` table as the mutable summary row. Add a small relationship
and audit layer.

### Flow Links

`flow_links` is the authoritative relationship ledger between a case and entities already
owned by the gateway.

Required fields:

- `id`
- `flow_run_id`
- `entity_type`: `task | session | approval | artifact | job | flow`
- `entity_id`
- `role`: `root_task | manager | worker | reviewer | approval | artifact | job | child_flow | evidence`
- `created_at`
- optional `created_by`
- optional `metadata_json`

Recommended constraints:

- unique `(flow_run_id, entity_type, entity_id, role)`
- indexed by `flow_run_id`
- indexed by `(entity_type, entity_id)`
- no cascade delete of historical relationships

### Flow Events

`flow_events` is append-only evidence for case lifecycle changes and manual overrides.

Required fields:

- `id`
- `flow_run_id`
- `event_type`
- `actor`: `operator | manager | worker | reviewer | system`
- optional `from_state`
- optional `to_state`
- optional `entity_type`
- optional `entity_id`
- `payload_json`
- `created_at`

Important event types:

- `flow.created`
- `flow.stage_changed`
- `flow.status_changed`
- `flow.linked`
- `flow.unlinked`
- `task.dispatched`
- `session.attached`
- `approval.requested`
- `approval.resolved`
- `review.requested`
- `review.accepted`
- `review.rework_requested`
- `review.waived`
- `flow.blocked`
- `flow.unblocked`
- `flow.interrupted`
- `flow.superseded`
- `flow.closed`

### Optional Direct Columns

Direct nullable columns are allowed only when they remove repeated joins on hot paths:

- `mesh_tasks.flow_run_id`
- `approvals.flow_run_id`

They are convenience indexes, not replacements for `flow_links` and `flow_events`.

## Authority Rules

When records disagree:

1. terminal `mesh_tasks.status` wins for task execution outcome.
2. pending/resolved `approvals` rows win for approval gate state.
3. fresh worker `live_state` can refine active execution state, but stale live state must
   render as stale/unknown.
4. `flow_runs.status/current_stage` is the current case summary, not the audit trail.
5. `flow_events` is the audit trail for case transitions and manual overrides.
6. transcripts, summaries, and dispatch docs are evidence only.
7. missing links must render as `unlinked` or `unknown`, never be inferred silently.

## Entrypoint Policy

The product migrates toward Work-first for managed work, while preserving Sessions for
runtime inspection and ad hoc execution.

- **Now:** Sessions remains the primary creation flow. Work substrate is built underneath.
- **Next:** Work tab appears as read-only/attention-first once the read model is honest.
- **Later:** Start Work becomes the primary entrypoint for managed multi-step objectives.
- **Always:** Sessions lists every backend session, including workflow-owned sessions.

Session labels:

- `Standalone`
- `Manager for Case <id/title>`
- `Worker for Case <id/title>`
- `Reviewer for Case <id/title>`
- `Evidence for Case <id/title>`

## Mobile Target

The mobile Work tab must be an operations inbox, not a workflow editor.

Default screen:

- Needs decision
- Active work
- Blocked/rework/review
- Recent closed, collapsed

Case detail:

- case header: title/objective, status, stage, next action, confidence/staleness
- ledger: linked tasks, approvals, sessions, artifacts, jobs
- compact lineage: parent/children as a vertical tree
- evidence: flow timeline + links to session timelines/transcripts

Hidden behind drill-down:

- raw transcript
- full event payloads
- full artifact metadata
- DAG details
- session runtime details

## Dispatch Plan

This milestone is split into five jobs:

1. **A25 — Flow relationship schema.** Add `flow_links`, `flow_events`, optional
   convenience `flow_run_id` columns, and DB helpers/tests.
2. **A26 — Link/event write path.** Populate relationships and events at flow creation,
   task dispatch, session attachment, approval request/resolution, and terminal task state.
3. **A27 — Work read model API.** Add read-only `/api/work`, `/api/work/{id}`,
   `/api/work/{id}/timeline`, and `/api/work/{id}/graph` projections over authoritative
   records.
4. **A28 — Mobile Work surface.** Add the read-only Work tab and session affiliation labels,
   driven only by the Work read model.
5. **A29 — Hardening and closure.** Adversarial review, stale/conflict fixtures, docs,
   and acceptance checks proving the UI does not infer from prose.

## Done Condition

The milestone is achieved when:

- relationships between a case and its tasks/sessions/approvals/artifacts/jobs are
  queryable without heuristic joins.
- every lifecycle/status transition shown in Work UI has a current row and an event trail.
- M3 can safely create manager/worker sessions because child work will be observable.
- Sessions still works for standalone ad hoc work.
- the mobile Work tab can show active/blocked/review/rework cases without reading
  transcript prose.
- missing data is rendered as `unknown`/`unlinked`/`stale`, not guessed.

## Adversarial Review

- **[F1 · P0 · false authority] The UI could treat `current_stage` as an execution driver or
  proof of completion.**
  Resolution: `current_stage` remains summary state only. `flow_events` carries audit, and
  terminal task state comes from `mesh_tasks`.

- **[F2 · P0 · heuristic linkage] Work UI could infer session/task ownership from timestamps,
  last task id, or transcript adjacency.**
  Resolution: Work UI may only use `flow_links`, direct `flow_run_id` columns, and explicit
  lineage fields. Missing links render as unknown/unlinked.

- **[F3 · P0 · autonomous drift] Building substrate could smuggle in Manager automation.**
  Resolution: A25-A29 are read/link/audit/UI jobs. M3 Manager invocation remains out of
  scope until this milestone closes.

- **[F4 · P1 · duplicate ledger] `flow_links` could become a second task ledger.**
  Resolution: links only relate existing entities. Execution truth stays in `mesh_tasks`;
  external process truth stays in `jobs`; gate truth stays in `approvals`.

- **[F5 · P1 · over-modeled drops] Adding a first-class `drops` table now would freeze an
  unproven abstraction.**
  Resolution: drops/decisions are initially derived from explicit case status, approvals,
  reviews, blockers, and next action. Add a table only after real UI use proves the need.

- **[F6 · P1 · mobile scope creep] The Work tab could become a desktop DAG/editor.**
  Resolution: mobile default is attention ledger. DAG is compact navigation only; no editing.

- **[F7 · P1 · event bloat] Flow events could become unbounded payload storage.**
  Resolution: payloads stay compact references plus short reasons. Large evidence remains in
  existing artifacts, timelines, task results, and transcripts.

- **[F8 · P1 · migration risk] New schema touches hot tables and could break legacy sessions.**
  Resolution: all schema changes are additive/nullable; helpers are best-effort until callers
  deliberately require them; tests cover old rows and flag-off behavior.

Verdict: the milestone fits v0.6 M2 and should precede M3/M4 wiring. The dispatch split is
properly ordered: schema first, then write path, then read model, then UI, then hardening.

## A29 Closure — Milestone Achieved (2026-07-09)

A29 ran the integrated adversarial review over A25–A28, folded in the deferred write-path
seams, and removed the one truthfulness defect found in A28. **The milestone is achieved.**

### Final shipped state (what is now true)

- **Session affiliations are authoritative and whole-substrate.** A28 resolved a session's
  case role by fetching each case's detail (O(N) requests) and reading `ledger.sessions`,
  **capped at the first 100 cases** — a session linked to a case beyond that window rendered
  a **false `Standalone`** (a silent violation of authority rule 7). A29 replaces this with a
  single authoritative JOIN, `db.list_session_case_links` → `build_session_affiliations`,
  exposed read-only at **`GET /api/work/affiliations/sessions`**. No per-case fanout, **no
  cap** — every session link in the backlog resolves regardless of case-set size. A
  multi-case session resolves to its **most recent** case deterministically. Absent ⇒
  `Standalone`, never inferred.
- **Deferred write-path seams are wired** (additive, `HARNESS_FLOW_DRIVE`-gated, best-effort
  — OFF stays byte-identical; a write failure can never raise into task/approval execution):
  - **Session attachment:** a flow's worker session is linked (`session`/`worker`) and
    `session.attached` appended at flow creation, sourced from `task.metadata.session_id`
    (the actual execution session — authoritative, not adjacency). This is what lights the
    affiliation labels.
  - **Terminal OUTCOME:** `closure` is only a *stage*; the case now records its real result —
    success closes it (`flow.closed` + `flow_runs.status='closed'`), a failure blocks it
    (`flow.status_changed`→`blocked` + `status='blocked'`). This is what moves a case out of
    the `active` bucket into `closed`/`blocked` so the inbox sections are live.
  - **Approval lifecycle:** an approval on a task that owns a flow links `approval`→flow and
    appends `approval.requested` / `approval.resolved` (actor = system / operator).

### Deliberately still deferred (honesty-first — no fabrication)

- **`review.accepted` / `review.rework_requested` / `review.waived`** are NOT emitted: there
  is no reviewer role or review gate in the harness yet (arrives with **M3**). Emitting them
  now would fabricate an outcome the substrate cannot observe. The read model + UI already
  render them the moment M3 produces them (zero change needed).
- **`flow.interrupted` on cancellation** is not wired (the cancel path exits before the
  completion seam). Low value until managed multi-step work exists; parked for M3.
- **Mobile inbox list cap (100, newest-first).** The Work *inbox* still fetches the newest
  100 cases — an intentional attention-first UX bound, not a correctness limit (the
  *affiliation* index, the operator-flagged concern, is uncapped). `bucket_counts` reflect
  the returned window. Revisit with server-side pagination if the active set routinely
  exceeds 100.

### Adversarial review outcomes (F-tags)

- **F1 (false authority)** — held: `current_stage` never drives execution; terminal outcome
  is written as an explicit `flow_events` record + summary `status`. Test:
  `test_terminal_status_wins_over_stage`, `test_conflict_flow_summary_vs_task_link_is_not_silently_resolved`.
- **F2 (heuristic linkage)** — **defect found & fixed**: the 100-case cap produced false
  `Standalone`. Now a whole-substrate authoritative JOIN. The worker-session link is from
  the real execution `session_id`, not timestamp/adjacency.
- **F3 (autonomous drift)** — held: A29 adds only link/audit records + one read endpoint. No
  Manager automation; all writes flag-gated + shadow.
- **F4 (duplicate ledger)** — held: links only relate existing entities; execution truth
  stays in `mesh_tasks`, gate truth in `approvals`.
- **F7 (event bloat)** — held: new event payloads are compact references (`{outcome,...}`).
- **F8 (migration risk)** — held: **no schema change** in A29 (reuses the existing
  `flow_runs.status` column and A25 tables); flag-OFF byte-identical proven by tests.

### Verification (2026-07-09)

Backend: `test_flow_substrate_hardening.py`, `test_session_affiliations.py`,
`test_work_read_model.py` (+ A29 authority fixtures), plus the A25–A28 suite —
244 passed / 2 skipped in the substrate+control+approval regression. Frontend:
`npm run typecheck` clean, `vitest` 90 passed (+4 `toSessionAffiliationIndex`). Live gateway
untouched (`/health` ok); populating the substrate live still needs `HARNESS_FLOW_DRIVE=on`
+ a gateway restart (operator's call — drops the active session).

## A30 — Post-closure truthfulness polish (2026-07-09)

A fresh adversarial pass over the shipped A28+A29 surface (write-path seams re-reviewed:
session attachment, terminal outcome, approval lifecycle — all confirmed correct,
flag-gated, isolated, and covered by `test_flow_substrate_hardening.py`). The milestone
code is sound; the remaining functional gaps (`review.*`, `flow.interrupted`, inbox
list-cap pagination) are **deliberately parked** for M3/operator and were left parked —
reopening a parked UX decision unprompted would be the wrong move.

Two real defects were found and fixed, both **doc/label truthfulness** (this milestone's
own thesis — labels must not lie):

- **Docs lied about affiliation resolution order.** `build_session_affiliations`'s
  docstring and the `useSessionAffiliations` hook comment claimed a multi-case session
  resolves to the "FIRST (oldest link)". The code is correct — `db.list_session_case_links`
  is `ORDER BY fl.id DESC` (newest-first), so the kept row is the **most recent** case — but
  the comments described the opposite, a trap for the next maintainer. Comments corrected to
  match the code + the DB layer + this doc. No behavior change.
- **Affiliation label hid the case's terminal state.** The read model already returns each
  case's authoritative `status`, but the frontend adapter dropped it, so a session showed
  "Worker · ‹case›" identically whether the case was active or closed weeks ago — implying
  active work on finished cases. `SessionAffiliation` now carries `caseStatus`; the chip/link
  read a **closed** case as muted history with a "closed" marker (closed/active derived only
  from the case's own status via `isClosedCaseStatus`, mirroring the backend
  `_CLOSED_STATUSES` authority set — never inferred). Also refreshed the stale `useWork.ts`
  poll comment that still described the pre-A29 per-case fanout.

Tests: backend substrate suite green (37); frontend `typecheck` clean, `vitest` 93 passed
(+3: `caseStatus` surfaced/nulled in `toSessionAffiliationIndex`, `isClosedCaseStatus`
authority mirror), production build OK. Additive/read-only; live gateway untouched
(`/health` ok). No schema change.
