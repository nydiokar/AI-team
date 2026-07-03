# Doc Map — the role contract for the doc tree

**Purpose:** one glance tells a person or an autonomous agent **which doc owns which
kind of information**. Each surface has a single role and an explicit "does NOT hold"
cell — that anti-overlap rule is what keeps one surface from silently absorbing
another's job (the failure mode this contract exists to stop: CONTEXT.md losing its
current-focus role to DISPATCH_LOG).

**The rule for adding docs:** prefer a section in an existing surface over a new file.
FEWER files, clearer roles. A new file is justified only when no surface below owns
the information and a section can't carry it.

**mkdocs note:** this tree is designed mkdocs-*friendly* (flat Markdown, relative
links, no build-time magic) but mkdocs is **not installed** and out of scope — no
`mkdocs.yml`, no nav, no rendering tooling.

---

## The six surfaces

| Surface | Role (one line) | Holds | Does NOT hold |
|---|---|---|---|
| `.ai/CONTEXT.md` | Fast orientation + the single home of forward priorities. | **Current Focus** (what's active now), architecture-as-it-runs, the Shipped Ledger, the **Current Priorities** table + the **Deferred** tables (the roadmap — see below). | Per-dispatch state (→ DISPATCH_LOG). A dispatch's blow-by-blow work log (→ its dispatch doc). Durable reference doctrine (→ `docs/`). |
| `.ai/dispatch/DISPATCH_LOG.md` | Lean **index** of every dispatch — status at a glance, one line each. | One row per dispatch: `# · Dispatch · Date · Level · Status · One-line`. The status vocabulary. | Any paragraph of "what was done / what's left" (→ the dispatch doc). "What to work on next" (→ CONTEXT Current Focus + Priorities). Open operator-TODOs (→ the dispatch doc). |
| The **roadmap** = `.ai/CONTEXT.md` Priorities + Deferred tables | Forward priorities and deliberately-parked work — the one home for "what's next". | Ranked next-work (Current Priorities) and deferred-but-valid items (the two Deferred tables). | Anything a dispatch has already shipped (→ Shipped Ledger). Job status (→ DISPATCH_LOG). **There is no separate `ROADMAP.md`** — this decision is fixed here so it stays unambiguous. |
| The **dispatch doc** `.ai/dispatch/AGENT_N_*.md` | ONE job's whole life in ONE file. | The packet (objective-lock, plan, scope) **plus** a `## Milestone` burndown section **plus** a `## Closure` section, grown in place through the job's lifecycle. Open operator-TODOs relocated from the log row. | Reference doctrine meant to outlive the job (maps, specs → `docs/`). A sibling `.milestone.md` / `.closure.md` file — those are folded in here, never spawned. |
| `docs/` | Durable **reference doctrine** — specs, maps, runbooks that outlive any one job. | Specs, architecture/data-flow maps, runbooks, contracts. Materially-important artifacts a dispatch produces for reuse. | A dispatch byproduct or job-scoped scratch (that stays in the dispatch doc). Live project state / current focus (→ CONTEXT). |
| `docs/harness/` | The task-quality loop's own docs — templates, generators, the pipeline runbook, the config map. | `packet_template.xml`, `milestone_template.md`, generators, `dispatch_pipeline.md`, `loop_config_map.md`, `operating_model.md`, `level_rubric.md`. | Actual dispatch instances (→ `.ai/dispatch/`). Project state / priorities (→ CONTEXT). |

**[F3] This file (`DOC_MAP.md`) holds ONLY role definitions.** It does NOT carry
project state, current focus, architecture, priorities, or job status — those live in
the surfaces above. DOC_MAP is the contract, not a seventh truth surface; if you find
yourself recording *what is happening* here, it belongs in CONTEXT or a dispatch doc.

---

## Consequences (the rules this contract makes concrete)

- **One dispatch = one living file.** A dispatch grows `packet → ## Milestone burndown
  → ## Closure` inside its single `AGENT_N_*.md`. No `.milestone.md` / `.closure.md`
  siblings. (Wired into `docs/harness/milestone_template.md`,
  `docs/harness/generators/closure_summary.md`, `docs/harness/dispatch_pipeline.md`.)
- **Materially-important reference artifacts go in `docs/`, never `.ai/dispatch/`.** The
  dispatch folder is for job packets and their inline lifecycle only. A map/spec/runbook
  that others will reuse is doctrine → `docs/`.
- **DISPATCH_LOG stays an index.** If a row needs a paragraph, that paragraph is in the
  wrong file — it belongs in the dispatch doc.
- **Forward priorities have exactly one home:** CONTEXT.md's Priorities + Deferred
  tables. No surface duplicates them.
