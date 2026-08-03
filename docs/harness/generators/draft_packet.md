# DRAFT — intent → free-prose dispatch packet + `## Milestone (burndown)` (one dispatch file)

**Role:** the drafting mode (spec §4 Manager / §14 step 1). A cheaper route is fine for
DRAFT. It is NOT a service to build.

**Input:** an operator intent + a level (from `level_rubric.md`) + optional curated
context.
**Output:** one filled free-prose dispatch packet (the house style —
[`packet_template.md`](../packet_template.md)) and one initialized
`## Milestone (burndown)` section.

---

## Prompt

> You are drafting a locked task packet. Your job is to separate what the operator
> *actually wants* from what they literally said, and to lock scope before any code is
> written. You do not execute anything.
>
> **Given:** the intent below, the chosen harness level, and the curated context.
> **Produce:** a filled free-prose dispatch doc in the current house style
> ([`docs/harness/packet_template.md`](../packet_template.md) — the shape real recent
> packets `AGENT_52`–`AGENT_64` actually use), with:
>
> 1. `## Why (intent)` = the outcome in the world; if the literal request and the real
>    goal differ, state both so drift is visible.
> 2. `## TASK` = concrete, independently checkable steps; each names a **non-paid**
>    verification per step (targeted `pytest`, `--collect-only`, import smoke, `tsc -b`,
>    `curl /health`).
> 3. `## TYPE` = docs/code/audit + the branch rule (docs → `main`; `src/`/config →
>    `feat/<slug>` branch + PR + self-merge).
> 4. `## CONTEXT (reuse verbatim)` = the shared grounding, copied from its sources
>    (prior packets, specs, code seams with `file:line`) — never restate history, never
>    invent doctrine.
> 5. `## ACCEPTANCE (proof, not vibes)` = the proof that closes the job.
> 6. `## RESERVED DECISIONS (surface, do not guess)` — an explicit choice list with who
>    decides; an empty one of these is fine only if there genuinely is no fork.
> 7. `## SCOPE OUT` = what this task does NOT do — an empty one is a drafting failure,
>    not a pass.
> 8. `## TRAIL / EVIDENCE (fill at close)` + a `## Milestone (burndown)` checkbox list +
>    a `## Closure (fill on completion)` stub.
>
> **Curate context, never dump it.** Grounding goes in the `## CONTEXT` section, small
> and source-tagged. If you have no snippet worth quoting, say so rather than padding.

---

## Memory — use the two systems that exist; invent nothing

Resume/handoff context comes from **existing** surfaces only:

- **`orchestrator.load_compact_context(task_id)`** — bounded prior
  prompt/summary/files/usage/errors from the DB-canonical `mesh_tasks` ledger. If this
  packet continues a prior turn, set `continues: <prior_task_id>` in the dispatched
  `.task.md` frontmatter; `process_task` prepends the prior context as a fenced
  reference block (spec §7/§14). Do **not** paste that context into the packet yourself —
  the runtime injects it opt-in.
- **File-memory** (`MEMORY.md` + `memory/*.md`) — durable facts/decisions/failure
  patterns. Read it for relevant scars before drafting.

Do **not** build a memory store or an async-compression job. If a fact belongs in
file-memory, write it in the `<memory_entry>` shape (spec §7) — a *write format for
file-memory*, not a database.

## Guardrails

- **No paid CLI.** Drafting is text; it never calls a backend to "verify".
- **No new gateway state.** If the packet references "state", it means the dispatch doc +
  the `mesh_tasks` ledger that already exist.
- **Deterministic level.** Take the level from `level_rubric.md` triggers, not vibes;
  when in doubt, escalate.
