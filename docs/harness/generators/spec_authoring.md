# SPEC — feature intent → authored spec + scored review gate (M4)

**Role:** the spec-authoring front-half of a **feature-sized** Case (spec §M4 / A56).
For a bounded fix a Manager may dispatch straight away; for a *feature-sized* intent
it must first **author a spec**, have a **separate seat score it**, and only then
decompose. This is the same "lock scope before any code is written" job as
`draft_packet.md` — reuse that contract, do not reinvent a weaker version. It is a
*drafting mode any capable model can play*; the independence that matters lives at the
REVIEW seat (below), not in a separate authoring role.

**Input:** a feature-sized operator intent + optional curated context, on an already
`open_case`'d Case (the Manager's own `case_id`).
**Output:** one authored spec, published onto the Case as durable evidence
(`publish_spec` → an `artifact` link + a `spec.authored` event), then a numeric
score from a **separate** reviewer (`record_spec_review`). An accepted score UNLOCKS
`decompose_case`; a below-threshold or critical-zero score BLOCKS it.

---

## Prompt (authoring)

> You are authoring a locked feature spec. Separate what the operator *actually
> wants* from what they literally said, and lock scope before any decomposition.
> You do not execute anything and you do not grade your own spec.
>
> Produce a spec with these sections (reuse `draft_packet.md`'s contract verbatim):
>
> - `<real_objective>` = the outcome in the world; `<literal_request>` = their exact
>   words; `<interpreted_task>` = your reading (flag any divergence between the three).
> - `<scope_in>` / `<scope_out>` = the in/out boundaries — an empty `<scope_out>` is a
>   drafting failure, not a pass.
> - `<assumptions>` and `<drift_risks>` — mandatory; an empty one of these is a
>   drafting failure (the R1 rubric scores you 0 on `risks_and_assumptions`).
> - `<tasks>` = the smallest set of independently-checkable work items this feature
>   decomposes into, each with a single `definition_of_done` and its prerequisite
>   task(s). **Under-decompose over over-decompose.** These become the task-DAG.
> - `<acceptance>` = a non-paid, checkable acceptance test per task (targeted
>   `pytest`, `--collect-only`, import smoke, `curl /health`) — never a paid-CLI
>   "verify".
>
> Publish it with `publish_spec(case_id, spec_id, body, title)`. Then STOP and hand
> to the reviewer — do not decompose until the spec has a passing score.

## The scored review gate (R1) — a SEPARATE seat

The spec is scored by a **separate plan-reviewer seat** — a cheap / cross-model pass,
NOT the authoring Manager grading its own plan (the self-grading risk the M3.2 review
vocab exists to avoid). Call `record_spec_review(case_id, spec_id, scores, reviewer)`
with `reviewer` distinct from the author. The six dimensions, each **0–2**:

| Dimension | Scores what |
|---|---|
| `objective_clarity` ⚠️ | is `<real_objective>` unambiguous and outcome-shaped? |
| `scope_boundaries` | are in/out boundaries explicit and non-empty? |
| `decomposability` ⚠️ | do the tasks split into genuinely independent, checkable units? |
| `acceptance_testability` | does each task name a concrete, non-paid check? |
| `dependency_correctness` | are the `depends_on` edges correct and acyclic? |
| `risks_and_assumptions` | are assumptions + drift-risks surfaced (not empty)? |

**Pass = total ≥ 8/12 AND no zero on `objective_clarity` or `decomposability`** (the
two ⚠️ CRITICAL dimensions — a hard zero on either BLOCKS decomposition even above
threshold). The verdict is **computed by the gateway**, not taken on the reviewer's
word (a malformed or self-serving card cannot report a pass). The threshold and the
critical set are tunable config constants (`SPEC_REVIEW_PASS_THRESHOLD`,
`SPEC_REVIEW_CRITICAL_DIMENSIONS` in `src/control/db.py`) — not magic numbers.

A passing score records `review.accepted`; a failing one records
`review.rework_requested` and `decompose_case` refuses with `spec_not_approved` until
a later passing re-score supersedes it. Revise the spec inline and re-score — cap the
spiral at 2 rounds (spec §3), same as `adversarial_review.md`.

## Guardrails

- **No paid CLI.** Authoring and scoring are text; they never call a backend to
  "verify".
- **No new Case per task.** Decomposition stays inside ONE Case (see
  `decomposer.md`) — orphan `flow_runs` are the anti-goal (§0.2 / M2.5).
- **Separate seat, enforced.** The reviewer actor must differ from the author; the
  gateway records whoever the seam names, so pass a genuinely separate `reviewer`.
- **Flag-gated.** The whole path is gated by `SPEC_AUTHORING_ENABLED`; with it OFF the
  tools return a disabled marker and the routes 404 (byte-identical to pre-M4).
