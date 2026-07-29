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
- [ ] spec_authoring stage + generator
- [ ] scored review gate (blocks decomposition on low score)
- [ ] publish_artifact → artifact links/events
- [ ] decomposer → one Case + N task_attached DAG edges (no orphan flow_runs)
- [ ] e2e green, byte-identical when OFF
- [ ] PR(s) opened + merged

## Closure (fill on completion)
_(verdict + evidence)_
