# AGENT_24 — Decomposer generator: salvage MAX's one real pattern as a by-hand DRAFT front-half

**Dispatch created:** 2026-07-07
**Level:** 2 (Standard) — one prompt/artifact doc under `docs/harness/generators/`; no code,
no mesh, no migration, no Level-3 trigger, no runtime wiring.
**Branch:** docs-only → **work directly on `main`** (branch policy: the diff touches only
`docs/` and `.ai/`). No branch, no PR, no merge step.
**Spec:** the **MAX salvage map** in `docs/` (the retired-MAX audit — its *decomposition* tier
and its *plug-in points*; find the live path via v0.4 §12 or `.ai/CONTEXT.md`, as the filename
is being finalized by a concurrent reorg) · `docs/Task_Harness_v0.6_AUTOMATION.md` (**M4** —
"generators can ship early, by hand, decoupled from automation") · `docs/Task_Harness_v0.4.md`
(§2.1 packet, §5 review, §12 optional-adapter slot) · sibling exemplar
`docs/harness/generators/draft_packet.md`.

> ⚠️ **TEST COST GUARD.** Docs-only task. No `pytest` beyond a link/cross-ref sanity check.
> NEVER invoke the paid Claude/Codex CLI. Never run the full e2e suite. Never
> `python main.py status`. Live-gateway check only via `curl http://127.0.0.1:9003/health`.

---

## Why this — the operator's grounded intent

The MAX salvage audit (the retired-orchestrator analysis in `docs/`) found that it
contributes exactly **one** reusable pattern: **decomposition** — `TaskExpertAgent`'s
`intent → LLM → dependency-aware structured task list`. That is the missing *front-half* of
the harness's DRAFT step: today `draft_packet.md` turns **one** intent into **one** packet;
it has no mode for "this feature-sized intent is really N dependency-linked packets."

v0.6 M4 explicitly **splits** its generators from its automation: the spec-authoring /
decomposition **prompts are docs and deliver value by hand today**, decoupled from any
wiring (v0.6 §M4 "Split for cheap early value"). The salvage map §5 step 2 says the same:
*"write the TaskExpert-style decomposer prompt as a docs generator (like `draft_packet.md`)
— usable by hand immediately."* This dispatch lands that one artifact and nothing else.

**This is NOT automation.** No code, no `_enqueue_task` wiring, no decomposer service, no
new state, no flag. It is a **prompt a Manager can run by hand** to turn a big intent into
several well-scoped packets. Wiring it into the flow machine is later, M3-class work and is
**out of scope here** (salvage map §5 step 3; ordering rule: never build the producer before
M2 makes dispatches observable).

## Objective-lock

- **Real objective:** the harness gains a *by-hand* decomposition generator so a Manager
  facing a feature-sized intent can produce N dependency-aware packets instead of one
  overstuffed one — mirroring `draft_packet.md`'s role, one rung earlier.
- **Literal request:** "salvage MAX's decomposition pattern as a dispatch quick win."
- **Interpreted task:** author `docs/harness/generators/decomposer.md` — a prompt-only
  generator, in the exact voice/shape of the three existing generators, whose output is
  **normal harness artifacts** (`packet_template.xml` copies + `.task.md` frontmatter), with
  the salvaged fields as planning hints and the Level-3 gate held inviolate.
- **Non-goals (empty = drafting failure):**
  - NO Python, NO change to `orchestrator.py` / `_enqueue_task` / `db.py` / any `src/`.
  - NO new milestone, NO new `flow_runs`/task columns, NO new memory store.
  - NO auto-approval: the generator MUST instruct that a decomposed Level-3 fragment is
    emitted with `approved: false` — a human sets `approved: true` (v0.6 §0.2 bound; salvage
    map §4). State this as a hard rule inside the prompt.
  - NO wiring into DRAFT/dispatch flow (that's M3; name it as deferred, don't do it).
- **Drift risks:** (a) sliding into "build the decomposer service" — refuse, docs only;
  (b) inventing new schema for the DAG — reuse `continues:` + `parent_flow_run_id`/
  `dispatched_by` (salvage map §4, Tier B); (c) letting it emit auto-approved work.

## Plan (each step independently checkable)

1. **Read** the three existing generators (`draft_packet.md`, `adversarial_review.md`,
   `closure_summary.md`) to lock voice/format, and the MAX salvage map's decomposition
   tier + `docs/harness/packet_template.xml` for the target artifacts.
2. **Author `docs/harness/generators/decomposer.md`** with these sections (mirror
   `draft_packet.md`): **Role** (front-half of DRAFT for feature-sized intent; a by-hand
   drafting mode, not a service) · **Input** (a feature-sized intent + level + curated
   context) · **Output** (an ordered set of packets, each a `packet_template.xml` copy with
   a `## Milestone`, plus its `.task.md` frontmatter). The **Prompt** block must require:
   - split the intent into the **smallest set** of packets that each has a single, checkable
     `definition_of_done` — under-decompose over over-decompose;
   - for each packet, a **dependency edge** expressed with the **existing** mechanism
     (`continues: <prior_task_id>` for resume-context lineage; note `parent_flow_run_id`/
     `dispatched_by` as the durable lineage columns M1 adds) — **no new schema**;
   - planning hints as **optional** fields on the packet, borrowed from MAX's `Task`
     (`estimated_hours`, `priority_score`) — clearly marked non-authoritative;
   - a per-packet `human_task` vs `agent_task` tag (MAX's split) so the Manager sees which
     rungs need a person;
   - the **hard rules**: no packet emits `approved: true` for a Level-3 fragment; every
     packet still routes through the normal DRAFT→review→`_enqueue_task`+Level-3 path; the
     generator produces *only* the artifacts the loop already consumes (adds no lane).
3. **Guardrails section** (mirror `draft_packet.md`): No paid CLI · No new gateway state ·
   Deterministic level from `level_rubric.md` · a one-line note that **wiring is M3, not here**.
4. **Provenance:** one line crediting the MAX salvage map (decomposition tier) as the source
   and pointing to v0.6 M4 as the owning milestone.
5. **Sanity check:** all cross-refs resolve (`grep`/file-read the paths cited); the doc reads
   in one voice with its siblings. No `pytest` needed.

## Done-condition

- `docs/harness/generators/decomposer.md` exists, is self-consistent with the other three
  generators, and its every cross-referenced path resolves.
- The prompt provably (by reading it) cannot emit auto-approved Level-3 work and introduces
  no new schema, lane, service, or state.
- DISPATCH_LOG row for A24 advanced to `built`/closed; `## Milestone` + `## Closure`
  appended to THIS file (one-file rule). No branch, no PR (docs-only on `main`).

## When to stop

When the one generator doc is written and cross-checked. Do **not** touch `src/`, do not add
the decomposer to any flow, do not open the M3 wiring question. If the intent seems to demand
code, **stop and surface it** — that's a different (M3) dispatch and an operator fork.

---

## Milestone

**Status:** drafting (packet locked; awaiting the session the operator opens)
**Burndown:**
- [ ] Read 3 sibling generators + salvage map §3–5 + packet_template.xml
- [ ] Author `docs/harness/generators/decomposer.md` (Role/Input/Output/Prompt/Guardrails/Provenance)
- [ ] Hard-rule check: no auto-approve, no new schema/lane/state
- [ ] Cross-ref sanity pass
- [ ] Advance DISPATCH_LOG row; append `## Closure`

**Next action:** read the three sibling generators, then draft `decomposer.md`.
