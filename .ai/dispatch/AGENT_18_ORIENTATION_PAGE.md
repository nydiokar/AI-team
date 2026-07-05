# A18 — Orientation / Overview Page

> ONE living file (DOC_MAP one-file rule): packet → `## Milestone` → `## Closure`.

```xml
<task_packet>
  <meta>
    <task_name>A18-orientation-overview-page</task_name>
    <harness_level>2</harness_level>
    <continues></continues>
  </meta>

  <objective_lock>
    <real_objective>A person who lands in this repo cold (or an agent) can, from ONE
      page, understand in under a minute what the system is, see its as-it-runs shape,
      and be routed to the exact owning doc for anything deeper (current state,
      job status, architecture, how-to-run, the harness). It is the "you are here"
      front door — it orients and routes; it does not become a new place where facts
      live.</real_objective>

    <literal_request>Build a human-facing orientation / overview page — the v0.4
      "wiki page" need (user orientation), as a static, readable Markdown page
      (mkdocs-friendly, no renderer/tooling), NOT the deferred wiki renderer.</literal_request>

    <interpreted_task>Create ONE new static Markdown file `docs/OVERVIEW.md`: a curated
      newcomer's map. It states what the project is (3 lines), shows the one-process
      topology at a glance, and carries a "Where things live / where to go next" router
      table pointing at the OWNING surfaces (CONTEXT, DISPATCH_LOG, DOC_MAP,
      ARCHITECTURE, QUICK_START, production_vision, docs/harness/). It holds NO
      canonical state of its own. Then add a single one-line link to it from
      `docs/README.md` so it is discoverable. This is the v0.4 §2.3 "Human Artifact"
      NEED met by a hand-written page — the renderer/automation stays deferred.</interpreted_task>

    <constraints>Docs-only. ZERO code, ZERO gateway state, ZERO new machinery. No
      `mkdocs.yml`, no nav, no rendering/build tooling (mkdocs is not installed and out
      of scope — DOC_MAP mkdocs note). No new truth surface: the page must not restate
      priorities, shipped-status, architecture-of-record, or job status as if it owned
      them — it LINKS to the owner (DOC_MAP anti-overlap rule [F3]). Relative links
      only. Must not tell anyone to run `python main.py status` (Test Cost Guard); the
      liveness check is `curl http://127.0.0.1:9003/health`.</constraints>

    <non_goals>Not the deferred wiki RENDERER / wiki automation (v0.4 §12/§13 — stays
      COLD per promotion_ladder). Not a rewrite of README/ARCHITECTURE/QUICK_START/
      CONTEXT/ROADMAP — those keep their roles; OVERVIEW only links to them. Not a new
      roadmap (ROADMAP.md already routes to `.ai/`; do NOT duplicate priorities). The
      shape thumbnail is a PLAIN FENCED ASCII BOX ONLY — no Mermaid, no diagram tooling,
      nothing that could tempt installing/running a renderer to "preview" it [F3].
      Not a `.ai/` file — this is durable reference doctrine, so it lives in `docs/`
      (DOC_MAP: docs/ owns reference; .ai/dispatch/ is job packets only).</non_goals>

    <assumptions>These are taken as true but MUST be re-verified against the live files
      before writing any link or claim (see validation): (a) the owning surfaces are
      `.ai/CONTEXT.md`, `.ai/dispatch/DISPATCH_LOG.md`, `.ai/DOC_MAP.md`,
      `.ai/context/production_vision.md`, `docs/ARCHITECTURE.md`, `docs/QUICK_START.md`,
      `docs/README.md`, `docs/harness/` (dispatch_pipeline.md is the harness entry).
      (b) the gateway is one process (`python main.py`) with Telegram + Control API
      (:9003) + mesh task server (:9002) as in-process coroutines (ARCHITECTURE.md §1).
      (c) the product is a session-first Telegram/Web gateway for local coding agents,
      NOT a generic autonomous framework (README, production_vision anti-goals).
      A wrong path here makes a dead link — so every link is grep-verified, not
      remembered.</assumptions>

    <drift_risks>(1) Scope-creep into a SECOND source of truth — copying the priorities
      table or shipped ledger into OVERVIEW so it goes stale the next day (the exact
      failure DOC_MAP exists to stop). Guard: OVERVIEW states facts that change slowly
      (what/shape) and LINKS everything that changes (state/status/priorities).
      (2) Drift into building the renderer / mkdocs tooling because the word "wiki"
      appears. Guard: non_goals + constraints forbid it; it's a hand-written .md.
      (3) Dead relative links from writing paths from memory. Guard: every link
      grep-verified to an existing file before commit.
      (4) Telling the reader to run `python main.py status`. Guard: forbidden; use the
      curl /health one-liner.</drift_risks>
  </objective_lock>

  <approved_plan>
    <steps>
      1. Create `docs/OVERVIEW.md` with these sections and nothing that duplicates an
         owning surface:
         - Title + a 1-line "what this page is" (a front door / router; not a source of
           truth — see the owner links).
         - "What this is" — 3 lines max: session-first Telegram/Web gateway for local
           coding agents; native backend resume (Claude Code/Codex) is the runtime;
           NOT a generic autonomous framework (cite production_vision anti-goals via link).
         - "The shape (as it runs)" — a PLAIN FENCED ASCII topology block (no Mermaid): one process
           `python main.py` hosting Telegram + Control API (:9003, serves Web UI) + mesh
           task server (:9002); workers are separate processes on other machines.
           Keep it a *thumbnail* and point to `docs/ARCHITECTURE.md` for the full map.
         - "Where things live / where to go next" — the router table (see below).
         - "Check it's alive" — the one-liner `curl http://127.0.0.1:9003/health`, with an
           explicit "do NOT run `python main.py status`" note (Test Cost Guard).
      2. The router table maps intent → owning doc (relative links), e.g.:
         | You want… | Read |
         current state / priorities / shipped → `.ai/CONTEXT.md`
         state of every dispatched job → `.ai/dispatch/DISPATCH_LOG.md`
         which doc owns which info (the doc contract) → `.ai/DOC_MAP.md`
         full process/HTTP architecture → `docs/ARCHITECTURE.md`
         install + first run → `docs/QUICK_START.md`
         strategic intent + anti-goals → `.ai/context/production_vision.md`
         the task-quality harness (how work gets dispatched) → `docs/harness/dispatch_pipeline.md`
         completed-work history → `docs/archive/progress/_archive_PROGRESS_LOG.md`
      3. Add ONE line to `docs/README.md` (under "Canonical Internal Docs" →
         "Supporting reference") linking to `OVERVIEW.md` as the newcomer front door.
      4. Update the `## Milestone` Live Log after each meaningful step.
    </steps>

    <validation>Docs-only — NO pytest, NO paid CLI, NO `python main.py`. Checks:
      - Every relative link target exists:
        `for f in .ai/CONTEXT.md .ai/dispatch/DISPATCH_LOG.md .ai/DOC_MAP.md .ai/context/production_vision.md docs/ARCHITECTURE.md docs/QUICK_START.md docs/README.md docs/harness/dispatch_pipeline.md docs/archive/progress/_archive_PROGRESS_LOG.md; do test -e "$f" && echo "OK $f" || echo "MISSING $f"; done`
        (run from repo root; each must print OK).
      - Anti-Test-Cost-Guard: `grep -n "main.py status" docs/OVERVIEW.md` returns only
        the "do NOT run" warning line (0 as an instruction to run it).
      - Liveness one-liner present: `grep -n "9003/health" docs/OVERVIEW.md` returns a line.
      - No duplicated state: `grep -nE "Current Priorities|Shipped Ledger" docs/OVERVIEW.md`
        returns nothing (the page links, it does not restate).
      - README anchor exists before edit: `grep -n "Supporting reference" docs/README.md`
        returns a line (insert the OVERVIEW link into that list) [F2].
      - README link lands: `grep -n "OVERVIEW.md" docs/README.md` returns the new line.
      - Router cells are labels not values: no router row restates a target's contents —
        manual read-through (the `Current Priorities|Shipped Ledger` grep above catches the
        common paraphrase) [F1].</validation>

    <definition_of_done>
      - [ ] `docs/OVERVIEW.md` exists: what-it-is (≤3 lines) + shape thumbnail + router
            table + liveness one-liner.
      - [ ] Every router link resolves to a real file (grep loop all OK).
      - [ ] Page restates NO canonical state (no priorities/shipped/architecture-of-record
            copied in — it links to owners).
      - [ ] `docs/README.md` links to OVERVIEW.md (one line, Supporting reference).
      - [ ] No `python main.py status` as an instruction; `curl .../9003/health` present.
      - [ ] No mkdocs.yml / renderer / code / gateway state added.
    </definition_of_done>

    <risks>Low (single new doc + one README line). Main risk = becoming a stale second
      source of truth → mitigated by the "links, never restates" rule + the
      no-duplicated-state grep. Dead links → mitigated by the grep-verify loop as a
      gating check before commit.</risks>
  </approved_plan>

  <execution_rules>
    <do>Write the page as a router that links to owners. Router "Read" cells contain
      ONLY a relative link + a ≤6-word CATEGORY label (e.g. "current state + priorities")
      — NEVER a value or summary of the target's contents (no "the top priority is X")
      [F1]. Grep-verify every link target exists BEFORE committing. Before editing
      README, grep that the "Supporting reference" list exists there and insert into it
      [F2]. Keep "what it is" to 3 lines. Update the Milestone Live Log after each step.
      Commit docs-only.</do>
    <do_not>No paid CLI. No `python main.py status` (as an instruction). No mkdocs.yml,
      no renderer, no build tooling, no code, no gateway state, no new migration. Do NOT
      copy the priorities table / shipped ledger / full architecture into OVERVIEW —
      link to them. Do NOT rewrite README/ARCHITECTURE/CONTEXT/ROADMAP.</do_not>
    <report_format>Closure summary shape (docs/harness/generators/closure_summary.md):
      per-file what-changed, the verification commands + their output, F-tag outcomes,
      what follows. State SHIPPED/PARTIAL/BLOCKED honestly.</report_format>
  </execution_rules>

  <context_snippets>
    <snippet id="S1" source=".ai/DOC_MAP.md — the anti-overlap rule [F3] + mkdocs note">
      <quote>prefer a section in an existing surface over a new file… A new file is
        justified only when no surface below owns the information… mkdocs is not
        installed and out of scope — no mkdocs.yml, no nav, no rendering tooling.</quote>
      <why_relevant>Justifies OVERVIEW as a NEW file only because it owns a distinct job
        (newcomer routing) no current surface holds — and bans the renderer/tooling. It
        also forces the "links, never restates" discipline so OVERVIEW isn't a 7th truth.</why_relevant>
    </snippet>
    <snippet id="S2" source="docs/Task_Harness_v0.4.md §2.3 + §12/§13">
      <quote>§2.3 Human Artifact: Wiki Page — a readable wiki-style summary… human
        command center. Markdown files remain source-of-truth. §13 Do not build yet:
        … wiki automation. §12 wiki renderer — Required: no.</quote>
      <why_relevant>The NEED (human orientation) is real and un-deferred; only the
        RENDERER/automation is deferred. Confirms the operator's split: build the page,
        not the tooling.</why_relevant>
    </snippet>
    <snippet id="S3" source="docs/ROADMAP.md — the pattern to copy">
      <quote>This file is a pointer, not the source of truth… This page exists so anyone
        landing here from docs/ is redirected to the right place. | You want… | Read |</quote>
      <why_relevant>ROADMAP.md is already exactly the species OVERVIEW should be — a pure
        router that holds no state. OVERVIEW is the broader whole-system version. Copy
        this shape; do NOT duplicate ROADMAP's roadmap rows.</why_relevant>
    </snippet>
    <snippet id="S4" source="docs/ARCHITECTURE.md §1 — the one-process topology">
      <quote>There is one long-running process on the gateway box: python main.py.
        Telegram, the Control API (which also serves the Web UI), and the mesh task
        server are all coroutines inside it… workers are separate processes on other
        machines… → port 9003 (Web UI/Control) · → 9002 (mesh).</quote>
      <why_relevant>The source for the shape THUMBNAIL. OVERVIEW shows a small version and
        links here for the full map — it does not become the architecture-of-record.</why_relevant>
    </snippet>
    <snippet id="S5" source=".ai/CONTEXT.md — Test Cost Guard">
      <quote>Do NOT run python main.py status — it acquires the gateway lock and KILLS
        the live PM2 gateway. Check the running gateway with curl http://127.0.0.1:9003/health.</quote>
      <why_relevant>Pins the correct liveness one-liner and the command OVERVIEW must warn
        AGAINST — guards drift_risk (4).</why_relevant>
    </snippet>
  </context_snippets>
</task_packet>
```

## Milestone: A18 orientation / overview page

## Objective
A newcomer (or agent) can, from ONE static page, grasp what the system is, see its
as-it-runs shape, and be routed to the owning doc for anything deeper — without the
page becoming a new source of truth.

## Current Status
shipped

## Burndown
- [x] `docs/OVERVIEW.md` created: what-it-is (≤3 lines) + shape thumbnail + router table + liveness one-liner
- [x] Every router link grep-verified to a real file (all OK)
- [x] Page restates NO canonical state (no priorities/shipped/architecture copied; links only)
- [x] `docs/README.md` links to OVERVIEW.md (one line, Supporting reference)
- [x] No `python main.py status` instruction; `curl .../9003/health` present
- [x] No mkdocs.yml / renderer / code / gateway state added

## Live Log
- 2026-07-04T00:00 — Manager: grounded intent vs v0.4 §2.3/§12/§13 + DOC_MAP + promotion_ladder → page-not-renderer confirmed spec-faithful; branch `docs/orientation-overview` cut from main; packet drafted (Level 2).
- 2026-07-04T00:05 — Manager: adversarial review → 3 P1s (F1 router cells could restate state; F2 README anchor unverified; F3 mermaid invites renderer tooling) all fixed inline; locked round 1; DISPATCH_LOG row added as dispatched.
- 2026-07-05T00:00 — Executor: verified all 9 link targets exist (grep loop all OK) + README "Supporting reference" anchor present before edit.
- 2026-07-05T00:05 — Executor: created `docs/OVERVIEW.md` (what-it-is ≤3 lines + plain ASCII shape thumbnail + 8-row router table + curl /health liveness, with explicit do-NOT-run `python main.py status` warning). No Mermaid, no renderer, no code.
- 2026-07-05T00:07 — Executor: added ONE line to `docs/README.md` under "Supporting reference" linking to OVERVIEW.md.
- 2026-07-05T00:10 — Executor: all validation greps pass (9003/health present; no Current Priorities/Shipped Ledger restated; OVERVIEW.md link lands in README). Committed docs-only. SHIPPED.

## Blockers
none

## Next Action
Done — docs-only commit on `docs/orientation-overview`. Closure reported.
