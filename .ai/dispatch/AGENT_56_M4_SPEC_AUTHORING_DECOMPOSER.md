```yaml
job_id: AGENT_56_M4_SPEC_AUTHORING_DECOMPOSER
created_at: "2026-07-30T02:34:12+03:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: AGENT_52_M34_JOB1_ADOPTION
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: tests/test_spec_authoring_decompose.py                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-03T14:01:16.305253+00:00"
```

# DISPATCH — A56 · M4: feature-spec authoring + scored review + decomposer-as-task-DAG-in-one-Case

**Level:** 3 (new stage + artifact links + decomposer) · **Type:** code + docs
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, not yet executed)
**Depends on:** — for the generators/docs (can precede); **wiring depends on A52** (adoption). Benefits from
A54. **Ultimate goal this serves:** `SPEC_COMPLETION_PLAN.md` §0 goal steps 2–3 ("author a spec + scored
review before decomposing"). This is the LAST milestone (v0.7 §M4).

> **Outcome, not a script.** For a feature-sized intent, a Manager should not dive straight into dispatch —
> it should author a spec, have it adversarially scored, and only then decompose into workers, all inside
> ONE Case. You choose the generator/prompt shape; acceptance is the contract. This is the largest remaining
> build — split into sub-dispatches if a single loop can't hold it (generators first, then wiring).

## Why (intent)
v0.7 §M4. Verified absent: no `spec_authoring` stage, no `publish_artifact`, no decomposer in `src/`
(grep clean). Without M4 the harness handles bounded fixes well but has no disciplined front-end for a
feature-sized intent, and any decomposition today would scatter N orphan flow_runs instead of a DAG in one
Case. The substrate (M2.5 Case container + M3.1 Manager + M3.2 review vocab) is all present to host it.

> **⚠️ Addendum (2026-08-01) — this is not new territory, it's an abandoned capability. Reuse its
> design, don't reinvent it.** The v0.5 harness already had a proven, evidenced intent-expansion
> contract for exactly this problem — `docs/harness/generators/draft_packet.md`'s job (verbatim):
> *"separate what the operator actually wants from what they literally said, and lock scope before
> any code is written"* — forcing `<real_objective>` (outcome in the world) vs `<literal_request>`
> (their exact words) vs `<interpreted_task>` (your reading, flagging divergence), plus mandatory
> `<assumptions>`/`<drift_risks>` ("an empty one of these is a drafting failure, not a pass"). It ran
> across ~20 real packets (`AGENT_9`–`AGENT_29`). Someone already tried to extend it to feature-sized,
> multi-packet intents — `AGENT_24_DECOMPOSER_GENERATOR.md` (2026-07-07), salvaging a
> `TaskExpertAgent`-style "intent → dependency-aware task list" pattern from a retired orchestrator —
> but it stalled at zero burndown, deferred 2026-07-08 pending the Case substrate. **That blocking
> condition (M2.5) has been live since mid-July; A24 was never resumed.** Separately, dispatch
> *authoring itself* silently drifted off `draft_packet.md`'s structured contract entirely starting
> `AGENT_31` (the M2→M3 cutover) — no decision was ever recorded, see `AGENT_64_HARNESS_DOC_DRIFT_RECONCILIATION.md`.
> The net effect: the **live Manager today receives a flat `objective: str`** (`ManagerInvocation`,
> `src/core/roles.py`) with no literal-vs-interpreted separation and no forced assumptions/drift-risks
> — a real, live gap, not a hypothetical one. **Before designing (a) `spec_authoring` from scratch,
> read `draft_packet.md` and `AGENT_24` and decide explicitly whether M4 reuses/extends that contract
> shape or deliberately supersedes it — do not silently reinvent a weaker version.**
>
> **Open fork, not resolved here — surface it, don't guess:** `draft_packet.md` made a deliberate call
> that this should be *"a drafting mode any capable model can play... NOT a service to build"* — i.e.
> the entity about to execute (the Manager) does its own intent-expansion inline, no separate role.
> That argues for folding this into the Manager's own spec-authoring stage, as this packet currently
> assumes. But the system has since grown real distinct Manager/Worker roles that didn't exist when
> that call was made — it is legitimate to reconsider whether intent-expansion should instead be a
> distinct step/role that hands a locked, expanded spec *to* the Manager, rather than the Manager
> doing double duty as both interpreter and executor of its own interpretation (a self-grading risk
> the M3.2 review vocab exists to avoid elsewhere). Decide this explicitly when scoping (a); don't
> default to "Manager does it" just because that's what's drafted below.

## TASK
Add a spec-authoring stage with a rubric-scored adversarial review gate before decomposition, an artifact
publish path, and a decomposer that expands an objective into a task-DAG **inside one Case**.

## TYPE
code + docs. Branch `feat/m4-spec-authoring` (generators may land as a docs-only precursor PR). PR(s) at
close, merge yourself.

## CONTEXT (reuse verbatim)
- Case container: `db.open_case` + `flow_links(task_attached)` (M2.5). A DAG = N `task_attached` links with
  dependency edges on `flow_links.metadata_json` — NOT N orphan `flow_runs`. (Design re-anchored in
  `PRIOR_ART_MAX_REUSE.md` §8.)
- Review vocab: `record_review` + `review.*` events (M3.2). The scored spec review reuses this; the
  plan-reviewer seat (a cheap/cross-model pass) is the M3.2 "one genuinely separate reviewer."
- Stage machine: `flow_runs.current_stage` (M1) — add `spec_authoring` before LOOP 0.
- Generators live under `docs/harness/` (see the deferred A24 decomposer-generator packet — resume its
  prompt work here, now that durable linkage exists).

## CHANGES
- **(a) `spec_authoring` stage + generator.** A Manager authors a spec (template under `docs/harness/`)
  before decomposing; stage written on the Case.
- **(b) Scored review gate.** A rubric-scored adversarial review of the spec (reuse `record_review`; a
  low score BLOCKS decomposition, mirroring the rework close-gate). Prefer the cheap/cross-model
  plan-reviewer seat so the Manager isn't grading its own plan.
- **(c) `publish_artifact`** → `artifact` `flow_links`+events (the spec is durable evidence on the Case).
- **(d) Decomposer.** Expand an approved objective → `open_case()` (one Case) + N `task_attached` links with
  dependency edges on `metadata_json` → a task-DAG the Manager dispatches from in order. Design the DAG as
  DATA; do not build a bespoke parallel executor (that is the A57 spike).

## ACCEPTANCE (proof, not vibes)
1. e2e (fake backend): a feature intent produces a spec artifact linked to the Case + a `review.*` score;
   a below-threshold score blocks decomposition (asserted), an accepted score allows it.
2. e2e: decomposition yields ONE Case with N `task_attached` links carrying dependency edges — assert NO
   orphan `flow_runs` are created and the DAG edges are queryable.
3. Generators are docs and render/parse; targeted pytest green. Flag/stage OFF ⇒ byte-identical.

## REALITY CONSTRAINTS
- Decomposition MUST stay inside one Case (§0.2 anti-goal + M2.5) — orphan flow_runs are the failure mode
  the whole M2.5 correction exists to prevent.
- The executor is OUT — A56 delivers the DAG as data + ordered dispatch; how tasks run in parallel is the
  A57 spike's decision.

## RESERVED DECISIONS
- **R1** — rubric + pass threshold for the scored spec review (numeric like the 6-dim ×0–2 ≥10/12 Manager
  rubric, or a lighter gate). Escalate the rubric before wiring the block.
- **R2** — whether M4 lands as one PR or a generators-first docs PR then a wiring PR. Recommendation: split.

## SCOPE OUT
The intra-task parallel executor (A57 spike). Provider-native subagents as first-class workers (off-path).

## TRAIL / EVIDENCE (fill at close)
- Branch / PR(s) · pytest output · a sample authored spec + its score + the resulting one-Case DAG dump.

---
## Milestone (burndown)
- [x] spec_authoring stage + generator (`publish_spec` → `spec.authored`; stage set to `spec_authoring`; `docs/harness/generators/spec_authoring.md`)
- [x] scored review gate (R1 6-dim ×0–2, ≥8/12 AND no critical-zero; `record_spec_review` — a low OR critical-zero score BLOCKS `decompose_case` with `spec_not_approved`, a real refusal not a warning)
- [x] publish_artifact → `artifact` links/events (`artifact.published`)
- [x] decomposer → one Case + N `task_attached` DAG edges, ZERO orphan flow_runs (`decompose_case`; `docs/harness/generators/decomposer.md`; cycle/unknown-dep/dup-key refused)
- [x] e2e green (18 new tests), byte-identical when OFF (flag `SPEC_AUTHORING_ENABLED`, default OFF ⇒ methods no-op + routes 404)
- [ ] PR opened (single PR, R2 rationale below); merge left to Manager per role

## Closure (2026-08-03 — A56 built, PR open, NOT merged)

**Verdict: BUILT & PROVEN e2e (fake backend, no paid CLI).** All acceptance items met.

**R2 decision — ONE PR.** The change is ~430 code lines (db + orchestrator seams +
API routes + MCP tools) + 2 generator docs, well under the ≈600 split threshold, and
the generators are meaningless without the wiring they describe (they cite the exact
flag/reason strings). A generators-first docs PR would have shipped un-runnable prose.
So: single well-structured PR `feat/m4-spec-authoring`.

**R1 rubric (as-built, tunable constants in `src/control/db.py`):** 6 dims ×0–2 —
`objective_clarity`⚠️, `scope_boundaries`, `decomposability`⚠️, `acceptance_testability`,
`dependency_correctness`, `risks_and_assumptions`. Pass = `total ≥ SPEC_REVIEW_PASS_THRESHOLD`
(8/12) AND no hard zero on either ⚠️ CRITICAL dim (`SPEC_REVIEW_CRITICAL_DIMENSIONS`).
Verdict is COMPUTED gateway-side (`_score_spec_review`), never taken on the reviewer's word.

**pytest evidence** (from inside the worktree; `src.control.db.__file__` =
`/home/cifran/dev/AI-team-wt/a56-m4/src/control/db.py`):
- `tests/test_spec_authoring_decompose.py` — 18 passed (the M4 e2e/db/API/MCP contract).
- Regression: `test_review_emitter test_case_closure test_mcp_manager test_flow_links_events
  test_control_api_flows` — 134 passed together (incl. the 18); plus
  `test_case_admission test_case_brief test_case_continuation test_case_respawn
  test_flow_runs test_durable_relay` — 57 passed. TEST COST GUARD honoured: no full/e2e suite.

**Sample authored spec + score + one-Case DAG dump** (live run, temp DB):
- Case `open_case('Add a context-fill gauge to the Web UI')` → 1 flow_run.
- `publish_spec('spec-ctxgauge', ...)` (real_objective/literal/interpreted + scope_out +
  assumptions + drift_risks) → `artifact` link + `spec.authored`.
- `record_spec_review(reviewer='cheap-reviewer', scores={oc:2,sb:2,dec:2,at:1,dc:2,ra:1})`
  → **total 10/12, passed=True, verdict=accepted**.
- `decompose_case` with 4 tasks (`backend-projection → api-field → ui-gauge → ui-tests`)
  → order `['backend-projection','api-field','ui-gauge','ui-tests']`.
- **NO-ORPHAN PROOF:** flow_runs before=1, after=1 (delta 0). The whole Case tree is
  exactly ONE flow_run; the 4 tasks are `task_attached` flow_links on it with
  dependency edges on `metadata_json` (queryable via `list_dag_tasks`).

**Adversarial self-review (findings + resolution):**
1. *Orphan flow_run?* Static-traced all four M4 write methods — none call
   `create_flow_run`/`open_case`/`update_flow_run`; only `update_flow_stage` (mutates
   the EXISTING Case's `current_stage`). e2e asserts `COUNT(flow_runs)` unchanged. CLEAN.
2. *Gate a real block?* `decompose_case` returns `spec_not_approved` and writes nothing
   before the gate (checked: no review, low score, AND critical-zero score all blocked;
   asserted no `task_attached` links appear). API returns 422. REAL block.
3. *Reviewer separate from author?* The scored-review actor is the `reviewer` seat
   (default `reviewer`), distinct from the author's `manager` actor; asserted the two
   event actors differ. HONEST BOUNDARY: separation is enforced as a distinct *seat*
   (a cheap/cross-model pass, per Manager decision #1) recorded durably in the actor
   field — the seam does not hard-reject `reviewer=="manager"`; that independence is
   operational (which model is invoked), matching `draft_packet.md`'s "any capable model
   can play" call. Documented in `spec_authoring.md`.
4. *DAG acyclic + queryable? cycle handling?* Kahn topo-sort; self-cycle, multi-node
   cycle → `None` → refused `cyclic_dependencies` (writes nothing); diamond DAG passes.
   Edges queryable via `list_dag_tasks`.
5. *Byte-identical OFF?* `SPEC_AUTHORING_ENABLED` default OFF: methods return
   `spec_authoring_disabled` (no write), routes 404. Asserted no events/links written.
   The only unconditional additions are declarative (event-type tuple + flag def) —
   no code path changes when unused.

**Seams verified / not verified (cross-layer honesty):** verified db → orchestrator
seam → control API route → MCP tool registration, all four, over a real temp MeshDB +
FastAPI TestClient. NOT verified: a live gateway restart / a real Manager session
driving these tools (no paid CLI run — out of test-cost scope); the Manager role prompt
does not yet *instruct* spec-authoring for feature intents (the tools are granted by the
existing manager MCP surface, but wiring the role prompt to prefer this path for
feature-sized intents is a follow-on, not in A56's acceptance).
