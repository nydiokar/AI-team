# AGENT_14 — Document Structure Contract: stop the doc landfill before autonomous agents multiply it

**Dispatch created:** 2026-07-03
**Level:** 2 (Standard) — docs/prompt-artifact restructure; no `src/` code, no mesh, no migration.
**Branch:** cut `feat/doc-structure` from `main`.
**Spec:** `docs/Task_harness_workflow.md` v0.5 · `docs/harness/operating_model.md` · this dispatch.

> ⚠️ **TEST COST GUARD.** Docs-only. No `pytest` needed beyond a link/cross-ref check.
> NEVER the paid Claude/Codex CLI. Never the full e2e suite. Never `python main.py status`.
> Live gateway = `curl http://127.0.0.1:9003/health` (you won't need it).

---

## Why this — the operator's actual, grounded complaint

The doc system has **no separation of concerns and is rotting like CONTEXT.md did.** Evidence (git-verified 2026-07-03):
- `.ai/dispatch/` holds **18 files for ~10 dispatches** — each job spawns packet + `_REVIEW` + `_BUILD_REVIEW` + `.milestone` + `.closure` siblings (up to 5 files/job). Autonomous throughput turns this into a landfill.
- `DISPATCH_LOG.md` is **11 KB** — an "index" the size of an essay. Each row carries paragraph-long "what was done / what's left" prose. This is the exact CONTEXT.md failure mode repeating.
- `CONTEXT.md` has **zero trace of current focus** — so DISPATCH_LOG has silently absorbed that role. Three surfaces (CONTEXT / DISPATCH_LOG / roadmap) have **blurred, overlapping roles**; state lives in all three, authoritative in none.
- Stray `job_*.log` files dumped in `.ai/` root with no home.
- The litter is **mandated by the templates themselves**: `milestone_template.md` L10 says "WHERE IT LIVES: `.ai/dispatch/<task-id>.milestone.md`"; `closure_summary.md` produces a standalone artifact. So agents *correctly follow the docs* and produce the mess.

**Operator intent (verbatim):** *"we can't produce materially important artifacts … in the dispatch folder … this is becoming something else but not an index … context have NO trace whatsoever of the current focus so dispatch log seems to be replacing it … we need to have the 'structure' set in, so if autonomous agents start producing it won't be a mess."*

**The fix is to set the structure/contract FIRST** — define what each surface is *for*, what shape it holds, and where artifact *types* live — so autonomous agents produce into a clean skeleton, not a landfill. mkdocs is **explicitly out of scope** (design mkdocs-*friendly*, install nothing).

---

## Objective (locked)

- **Real objective:** a person or an autonomous agent, opening this repo, can tell in one
  glance **which doc owns which kind of information**, and every dispatch produces **one
  living file** instead of a scatter of siblings — so the doc tree stays legible as agent
  throughput scales, and no surface silently absorbs another's role.
- **Literal request:** "set the structure in — role of dispatch/roadmap/context; keep
  DISPATCH_LOG a concise index (`| dispatch | date | status |`), not a dump; stop 3 files
  per task; don't put materially-important artifacts in the dispatch folder; mkdocs-ready
  but not now."
- **Interpreted task:** author a **doc-role contract**, **slim DISPATCH_LOG to a real
  index**, **restore Current Focus to CONTEXT.md**, and **change the harness templates +
  pipeline so one dispatch = one living file** (fold milestone + closure into the dispatch
  doc's lifecycle; reference artifacts live in `docs/`, never `.ai/dispatch/`). No `src/`
  code. No new gateway state.

---

## Approved plan (concrete, each step independently checkable)

0. **Branch off `main`.** `git checkout main && git pull --ff-only && git checkout -b feat/doc-structure`. Confirm `git log --oneline -1` is the current `main` tip.

1. **Author the doc-role contract** → `.ai/DOC_MAP.md` (short, ≤1 screen). A table with one
   row per surface stating **its single role, what belongs in it, and what does NOT**:
   | Surface | Role (one line) | Holds | Does NOT hold |
   Cover exactly these: `.ai/CONTEXT.md` (orientation: **current focus** + arch-as-it-runs +
   shipped ledger), `.ai/dispatch/DISPATCH_LOG.md` (lean **index** of dispatches — status
   only), the **roadmap** (forward priorities / deferred — see step 4), the **dispatch doc**
   (`AGENT_N_*.md` = ONE job's whole life: packet + burndown + closure folded in),
   `docs/` (durable **reference doctrine** — specs, the harness, maps; NEVER a dispatch
   byproduct), `docs/harness/` (the loop's own docs). **[F3] DOC_MAP holds ONLY role
   definitions** — its own "does NOT hold" cell must say it never carries project state /
   current focus / architecture (that's CONTEXT's job); it must not become a fourth truth
   surface. **Validation:** the table names all six surfaces; each has a non-empty "Does
   NOT hold" cell (the anti-overlap rule).

2. **Slim `DISPATCH_LOG.md` to a real index.** Replace the essay table with:
   `| # | Dispatch | Date | Level | Status | One-line |` — the "One-line" is ONE sentence
   max; all detail lives in the dispatch doc. Keep the status vocabulary. **Preserve the
   historical rows** (A8–A14) but compress each to the index shape — do NOT lose which
   dispatch shipped what; move any essential "what's left" into the dispatch doc if it isn't
   already there. **[F2] Open operator-TODOs are sacred:** before compressing any row,
   check whether it carries a pending operator action (e.g. A8 "set VAPID_* env / pip
   install .[push]", A11 "kanebra redeploy" — some are now DONE, verify against
   CONTEXT.md/git). If still open, **relocate it into that dispatch's own doc** before
   dropping it from the row — an open TODO may NOT vanish in the slim. Add a header line: "This is an INDEX. Full state per job lives in its
   `AGENT_N_*.md`. If a row needs a paragraph, it's in the wrong file." **Validation:**
   `DISPATCH_LOG.md` drops well under half its 11 KB; every row is one line; no row carries
   a multi-sentence "what was done" blob.

3. **Restore Current Focus to `CONTEXT.md`.** Add a short **"## Current Focus"** section
   near the top: the 1–3 things active right now (today: doc-structure work; harness proven
   on docs, not yet on a real code task), and a one-line pointer to DISPATCH_LOG for job
   state and the roadmap for forward priorities. This reclaims the role DISPATCH_LOG was
   absorbing. **Validation:** `CONTEXT.md` has a "Current Focus" section; DISPATCH_LOG no
   longer implies "what to work on next."

4. **Establish the roadmap home.** CONTEXT.md already has a "Current Priorities" table and
   deferred tables. Decide + document (in DOC_MAP.md) whether forward priorities live in
   CONTEXT.md's Priorities section (keep) or a dedicated `.ai/ROADMAP.md`. **Recommendation:
   keep the Priorities + Deferred tables in CONTEXT.md** (don't spawn a new file for its own
   sake) but state that decision explicitly in DOC_MAP so it's not ambiguous again. If you
   do split it out, update every cross-ref. **Validation:** DOC_MAP names exactly one home
   for forward priorities; no surface duplicates it.

5. **One dispatch = one living file (the litter fix — the important one).** Change the
   harness so a dispatch no longer spawns `.milestone.md` / `.closure.md` siblings:
   - `docs/harness/milestone_template.md` — change "WHERE IT LIVES" (L10): the burndown is
     a **`## Milestone / Burndown` section INSIDE the dispatch `AGENT_N_*.md`**, not a
     separate file. Keep the update-rule (it's the anti-hallucination dial).
   - `docs/harness/generators/closure_summary.md` — the closure is a **`## Closure` section
     appended to the same dispatch doc**, not a `.closure.md` file. Keep the honesty floor.
   - `docs/harness/dispatch_pipeline.md` — update steps 1/5/7 and any "WHERE IT LIVES"
     wording so the runbook tells the agent to grow ONE file through its lifecycle
     (packet → burndown → closure), and states plainly: **materially-important reference
     artifacts (maps, specs) go in `docs/`, never `.ai/dispatch/`.**
   - `docs/harness/loop_config_map.md` node table / dials — if any cell references the
     separate-file convention, update it to the one-file convention (grep for
     `.milestone` / `.closure`).
   **Validation:** grep across `docs/harness/` shows no instruction to create a standalone
   `<task-id>.milestone.md` or `.closure.md`; the pipeline explicitly states the one-file
   rule and the "reference artifacts live in docs/" rule.

6. **Do NOT retro-shred existing dispatch docs' content, but DO clean the obvious litter.**
   Delete nothing that carries unique history. You MAY fold the now-merged A13's
   `.milestone.md` + `.closure.md` into `AGENT_13_LOOP_CONFIG_MAP.md` as sections and remove
   the two siblings (they're the freshest example of the litter and A13 is closed) — but
   ONLY A13, and only because it's the demonstration case. Leave A8–A12 historical files
   untouched (rewriting closed history is out of scope). **[F1] The A13 siblings are
   removed FORWARD** — a normal new commit on `feat/doc-structure` that deletes the files.
   Do NOT rewrite the merged A13 commit / main's history. **Validation:** A13's two sibling
   files are folded + removed; A8–A12 files unchanged (`git status` shows only A13 siblings
   deleted); `git log main..feat/doc-structure` shows normal forward commits, no rebase of
   merged history.

### Validation (non-paid)
- `wc -c .ai/dispatch/DISPATCH_LOG.md` before/after (must shrink hard).
- `grep -rE '\.(milestone|closure)\.md' docs/harness/` returns nothing after step 5.
- link-target existence check for all new/changed cross-refs.
- NO pytest (no code); NO paid CLI.

### Definition of done
- `.ai/DOC_MAP.md` exists: six surfaces, each with a role + "does NOT hold" cell.
- `DISPATCH_LOG.md` is a lean index (`| # | Dispatch | Date | Level | Status | One-line |`),
  well under half its old size, every row one line, history preserved in compressed form.
- `CONTEXT.md` has a "Current Focus" section; forward-priorities home named unambiguously.
- Harness templates + pipeline updated so **one dispatch = one living file**; grep confirms
  no standalone-milestone/closure instruction remains; the "reference artifacts in `docs/`"
  rule is stated in the pipeline.
- A13's sibling files folded + removed; A8–A12 untouched.
- This dispatch doc carries its own **`## Milestone`** and (at close) **`## Closure`**
  sections inline — i.e. **dogfood the new one-file rule on THIS task.**
- `DISPATCH_LOG.md` A14 row present (in the new index shape). Committed on `feat/doc-structure`.
- Report back; HOLD on branch; do NOT merge (operator fork).

### Risks
- **R1 (history loss):** slimming DISPATCH_LOG could drop which dispatch shipped what. →
  compress, don't delete; if a row's detail isn't in its dispatch doc, move it there first.
- **R2 (scope creep into mkdocs/tooling):** design mkdocs-friendly, install NOTHING; no
  `mkdocs.yml`, no build, no nav config.
- **R3 (new-file sprawl):** the fix must REDUCE files. Do not create `ROADMAP.md`,
  `MILESTONES.md`, etc. unless a named home genuinely doesn't exist — prefer sections in
  existing surfaces. DOC_MAP.md is the one new file justified (it's the contract itself).

---

## Scope — DO NOT
- **No `src/` code, no gateway state, no migration.** Docs/prompt-artifact only.
- **No mkdocs, no rendering tooling, no `mkdocs.yml`.** mkdocs-ready structure only.
- **No rewriting closed dispatch history (A8–A12).** Only A13 gets folded (demo case).
- **No new index/roadmap/milestone files beyond `DOC_MAP.md`** unless a home truly doesn't
  exist — the goal is FEWER files, clearer roles.
- **No merge, no push, no `.env` edits, no gateway run.**

## Report format (hand back)
1. **DOC_MAP** — the six-surface table; did any two surfaces resist a clean role split?
2. **DISPATCH_LOG** — before/after byte size; the new index shape; any history you had to
   relocate to preserve it.
3. **Current Focus** — restored where; roadmap home decision.
4. **One-file rule** — the exact template/pipeline edits; grep-proof no `.milestone`/
   `.closure` instruction remains.
5. **A13 fold** — confirm the two siblings folded + removed, A8–A12 untouched.
6. **Friction note** — running THIS as a harness loop under the NEW one-file rule: did
   folding milestone+closure into the dispatch doc actually work better, or fight you?
   (This is live evidence on whether the new rule is right — be honest.)
7. Commit SHA + files changed.

---

## Milestone
<!-- ONE-FILE RULE (this dispatch dogfoods it): the burndown lives HERE, not in a sibling. -->
**Status:** closed
**Burndown:**
- [x] DOC_MAP.md authored (6 surfaces, anti-overlap cells)
- [x] DISPATCH_LOG slimmed to index, history preserved (11297 → 3365 B)
- [x] CONTEXT.md Current Focus restored + roadmap home named
- [x] harness templates/pipeline → one-file rule; grep-clean
- [x] A13 siblings folded + removed; A8–A12 untouched
- [x] cross-refs resolve

**Live Log:**
- 2026-07-03 — cut `feat/doc-structure` off main tip 1efa722; read DISPATCH_LOG (11297 B), CONTEXT, 3 harness templates, A13 siblings; grep-scoped `.milestone`/`.closure` instructions → only `milestone_template.md` L10 + `packet_template.xml` L11-13 mandate standalone files → author DOC_MAP next
- 2026-07-03 — authored `.ai/DOC_MAP.md` (6-surface role table + anti-overlap + one-file consequences) → contract set → slim DISPATCH_LOG
- 2026-07-03 — [F2] audited open operator-TODOs before slimming: A8 VAPID/install = DONE per CONTEXT; A9H/A12/A13 = op-merge (already in their docs); FX1 #41 gauge NOT recorded → relocated into FIX doc's new "Open follow-ups" → then rewrote DISPATCH_LOG to index shape (3365 B) → restore Current Focus
- 2026-07-03 — added `## Current Focus` to CONTEXT.md; roadmap home = CONTEXT Priorities+Deferred (stated in DOC_MAP, no ROADMAP.md) → wire one-file rule
- 2026-07-03 — edited milestone_template L10, closure_summary output, dispatch_pipeline (state def + 7-step + steps 1/4/5/7), packet_template L10-15, operating_model L98-99, draft_packet L1/L33 → grep: every residual `.milestone`/`.closure` hit is a prohibition, none instructs creating one → fold A13
- 2026-07-03 — folded A13 milestone+closure into AGENT_13 doc as `## Milestone`/`## Closure` sections; `git rm` both siblings (forward, no history rewrite) → all cross-refs resolve → close

## Closure
<!-- (executor fills at close, per closure_summary.md, IN THIS FILE — no .closure.md sibling) -->

### A14-doc-structure-contract — SHIPPED (2026-07-03)

**What changed (per file):**
- `.ai/DOC_MAP.md` — NEW (the one justified new file). Six-surface role-contract table,
  each with role / holds / **does NOT hold**; [F3] cell says DOC_MAP itself holds only
  role defs; consequences section states the one-file rule + "reference artifacts in
  `docs/`" + single roadmap home.
- `.ai/dispatch/DISPATCH_LOG.md` — rewritten from an 11 KB essay-table into a lean
  index (`# · Dispatch · Date · Level · Status · One-line`), 11297 → 3365 B (−70%). Old
  per-row "what was done / what's left" prose + the multi-paragraph "Milestone status"
  #9/T1 saga compressed into one-line rows (the CLOSED outcome is preserved in the A10/A11
  rows). "This is an INDEX" header added.
- `.ai/CONTEXT.md` — restored a `## Current Focus` section (doc-structure work; harness
  proven on docs not code; op-merge holds) with pointers to DISPATCH_LOG / Priorities / DOC_MAP.
- `.ai/dispatch/FIX_CLAUDE_ISERROR_PROMPT_TOO_LONG.md` — [F2] relocated the open "#41
  context-fill gauge" TODO into a new "Open follow-ups" section before dropping it from
  the log row.
- `docs/harness/milestone_template.md`, `generators/closure_summary.md`,
  `dispatch_pipeline.md`, `packet_template.xml`, `operating_model.md`,
  `generators/draft_packet.md` — one-file rule wired: milestone = a `## Milestone`
  SECTION and closure = a `## Closure` SECTION inside the ONE dispatch doc; no
  `.milestone.md`/`.closure.md`/`.packet.xml` siblings; "reference artifacts in `docs/`"
  rule stated in the pipeline.
- `.ai/dispatch/AGENT_13_LOOP_CONFIG_MAP.md` — A13's two siblings folded in as
  `## Milestone` + `## Closure` sections; the two sibling files removed (forward `git rm`).

**Verification (non-paid):**
- `wc -c DISPATCH_LOG.md`: 11297 → 3365 B.
- `grep -rInE '\.(milestone|closure)\.md' docs/harness/`: every hit is a *prohibition*
  ("NOT a separate", "no sibling", "do not spawn") — none instructs creating one.
- Cross-ref existence check: all new/changed relative links resolve (DOC_MAP, CONTEXT,
  DISPATCH_LOG, pipeline → DOC_MAP; DOC_MAP → 7 `docs/harness/` targets).
- `git status`: only A13's two siblings deleted in `.ai/dispatch/`; A8–A12 untouched.
- No pytest (docs-only, no `src/`); no paid Claude/Codex CLI; no gateway call.

**F-tag outcomes (packet scope guards):** F1 (A13 removed forward, no history rewrite)
→ done — `git rm` on this branch, merged A13 commit `1efa722` untouched. F2 (open
operator-TODOs relocated before slimming) → done — FX1 #41 gauge moved into the FIX doc;
A8/A9H/A12/A13 TODOs verified already-in-doc or done. F3 (DOC_MAP holds only role defs)
→ done — its "does NOT hold" cell says so explicitly.

**What follows (not code):**
- Operator merge decision on `feat/doc-structure` → `main` (HOLD on branch; no push).
- The one-file rule is now the standing convention; next real dispatch grows one file.
- Friction note (live evidence on the rule) is in the Report handed back, not here.
