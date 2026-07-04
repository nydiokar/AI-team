# AGENT_15 — Harness Promotion Ladder: the evidence-gated roadmap from v0.5 → v0.4 end-state

**Dispatch created:** 2026-07-03
**Level:** 2 (Standard) — reference-doctrine authoring under `docs/harness/`; no `src/` code, no gateway state.
**Branch:** cut `feat/harness-ladder` from `main`.
**Spec driving this loop:** `docs/Task_Harness_v0.4.md` (the fuller end-state) vs `docs/Task_harness_workflow.md` v0.5 (what shipped) + §16 (Phase-2 deferral).

> ⚠️ **TEST COST GUARD.** Docs-only. No `pytest` beyond a cross-ref check. NEVER the paid
> Claude/Codex CLI. Never the full e2e suite. Never `python main.py status`.
> Live gateway = `curl http://127.0.0.1:9003/health` (not needed).

---

## Why this (Manager loop-0 framing, grounded)

This is the **first real harness loop** run via the driver (`docs/harness/manager_invocation.md`).
The spec is the harness's own future. Grounding (verified this session against v0.5 §16 +
A12's friction verdict): **v0.4 is a someday-maybe, not a build list.** v0.5 deliberately
stripped v0.4 to prompt-and-artifact discipline and deferred the rest as Phase 2, and
A12's evidenced verdict was **"Phase 2 = NO."** So dumping v0.4 elements into build
dispatches would violate the standing "no speculative machinery" rule.

**The right loop-0 output is therefore NOT "build v0.4."** It is the **evidence-gated
promotion ladder**: for each element v0.4 has that v0.5 deferred, the *specific, concrete
strain* that would justify promoting it — in priority order. This converts a vague
end-goal into a **decision instrument**: "when strain X is observed N times, promote
element Y." It builds nothing prematurely and it governs every future harness loop
(including whether `deferred/AGENT_11_WEBUI_HARNESS_NODEGRAPH.md` ever un-defers).

**The v0.4→v0.5 delta (the deferred elements to rank):**
1. **Gateway-owned flow state** — `flow_runs` record + `current_stage` stage machine (v0.4 §11/§13-item-1; v0.5 §16).
2. **Wiki / HTML dashboard** layer (v0.4 §2.3; v0.5 §12 optional).
3. **Live tailing reviewer** — concurrent reviewer agent (v0.4 §5; v0.5 §5 reframed to sequential-checkpoint).
4. **Async memory compression** — cheap-model summarizer service (v0.4 §7; v0.5 §7 "invent no store").
5. **Per-task provider smoke** — model identity/quality check per task (v0.4 §9; v0.5 §9 onboarding-only).
6. **Automatic model routing** — per-node cheap-DRAFT/strong-REVIEW as a wired dial (v0.4 §13 "do not build"; surfaced as the missing dial in `loop_config_map.md` §b).

---

## Objective (locked)

- **Real objective:** the project has a single, durable decision instrument that says, for
  each deferred harness capability, **the exact evidence that would justify building it** —
  so promotion is triggered by observed strain, never by speculation or a slow session, and
  a future Manager can check "have we hit the gate yet?" instead of re-litigating scope.
- **Literal request:** "Manager picks up the v0.4 spec, attacks it from the large view,
  produces the plan (the ladder) to test the harness on its own end-goal."
- **Interpreted task:** author `docs/harness/promotion_ladder.md` — one row per deferred
  element with: what it is, why v0.5 deferred it, **the concrete promotion trigger
  (evidence, not vibes)**, the priority rank, and the cheapest thing to do *instead* until
  the gate trips. Cross-link from README + loop_config_map. This is reference doctrine
  (`docs/harness/`), NOT a build dispatch — it authorizes no machinery.

---

## Approved plan (each step independently checkable)

0. **Branch off `main`.** `git checkout main && git pull --ff-only && git checkout -b feat/harness-ladder`. Confirm `git log --oneline -1` is the current main tip.

1. **Ground each deferred element in the two specs** (read-only). For each of the 6
   elements, confirm from the actual spec text: what v0.4 proposed and where v0.5
   deferred/reframed it. **Validation:** each ladder row cites both a v0.4 section and the
   v0.5 section that deferred it (no invented capabilities; if an element isn't really in
   v0.4, drop it and say so). **[F1] If NO falsifiable trigger can be written for an
   element** (you genuinely cannot name evidence that would ever justify it), record it as
   **"drop candidate — no realistic trigger"** — do NOT give it a mushy trigger like "when
   the team decides." An honest "we'd never need this" is a valid, valuable output.

2. **Author `docs/harness/promotion_ladder.md`.** Required structure:
   - **Intro:** state the rule plainly — nothing here is a build authorization; each row
     stays deferred until its trigger is *observed and recorded* (link A12's "Phase 2 = NO"
     as the current standing verdict).
   - **The ladder table:** `| Rank | Element | v0.4 §/ v0.5 § | What it is | Promotion
     trigger (concrete, observed evidence) | Cheaper interim move |`. The **trigger** is
     the load-bearing column: it must be a *falsifiable, observable condition*, e.g. "a
     multi-slice task loses handoff state across ≥2 resumes such that the milestone file
     alone can't recover it" — NOT "when it feels needed." One row per deferred element.
   - **Priority reasoning:** 2–3 sentences on why the ranking is what it is, tied to the
     project's real scars (false-success, burned tokens, doc-litter) and end-goal.
   **Validation:** 6 rows (or fewer with a stated reason); every trigger is a concrete
   observable condition, not a feeling; every "cheaper interim move" is something that
   needs no new machinery.

3. **Sanity-check the ranking against evidence we already have.** A12 already produced one
   data point (a real docs loop held with zero lost state → flow-state gate NOT tripped);
   A13/A14 added two more docs loops. The ladder must be **consistent** with that recorded
   evidence — e.g. it may NOT rank "gateway flow state" as trippable-now when three loops
   just ran without needing it. **Validation:** the doc explicitly notes the A12/A13/A14
   evidence and confirms no trigger is already-satisfied-but-ignored (or, if one is, says
   so loudly — that would be a real finding). **[F2] "Identified as a gap" ≠ "trigger
   satisfied":** element 6 (model routing) was flagged as a missing dial in
   `loop_config_map.md`, but three loops ran fine single-model — a named-but-unneeded
   capability stays COLD. Do not rank it hot just because it's already been named.

4. **Cross-link** from `docs/harness/README.md` and note in `loop_config_map.md` §"What
   this map does NOT do" that the model-routing dial's promotion is governed by this ladder.
   **Validation:** README row + loop_config_map note resolve; all new cross-refs resolve.

### Validation (non-paid)
- `grep`/read that each row cites real v0.4 + v0.5 sections.
- link-target existence check.
- NO pytest, NO paid CLI.

### Definition of done
- `docs/harness/promotion_ladder.md` exists: intro (no-build rule), the 6-row evidence-
  gated table (every trigger a concrete observable), priority reasoning.
- Consistency-checked against the A12/A13/A14 loop evidence; no trigger silently
  already-tripped.
- README + loop_config_map cross-links resolve.
- This dispatch doc carries its own inline `## Milestone` and (at close) `## Closure` —
  **dogfood the one-file rule** (A14's contract).
- DISPATCH_LOG A15 row present (index shape). Committed on `feat/harness-ladder`.
- Report back; HOLD on branch; do NOT merge.

### Risks
- **R1 (turns into a build plan):** the ladder must AUTHORIZE NOTHING. Every row is
  deferred-with-a-gate. If a reader could mistake it for a backlog, it failed. → the intro
  states this in bold; triggers are gates, not tasks.
- **R2 (vague triggers):** "when needed" is not a trigger. Each must be falsifiable and
  observable in the loop's own artifacts/logs. → validation rejects any non-observable
  trigger.
- **R3 (contradicting recorded evidence):** ranking a gate as hot when 3 loops just ran
  without it. → step 3 forces consistency with A12/A13/A14.

---

## Scope — DO NOT
- **No `src/` code, no gateway state, no machinery.** This is the *plan for* possible
  future machinery, gated — it builds none of it.
- **No new files beyond `promotion_ladder.md`.** (Reference doctrine → `docs/harness/`,
  per DOC_MAP — NOT `.ai/dispatch/`.)
- **No mkdocs/tooling.** No merge, no push, no `.env`, no gateway run.

## Report format (hand back)
1. **The ladder** — the 6-row table; did any element resist a concrete trigger (that's a
   finding — a capability we can't say when we'd ever want is a capability to drop)?
2. **Evidence consistency** — is any trigger already satisfied by A12/A13/A14, or are all
   correctly still-deferred?
3. **Priority reasoning** — the ranking and its tie to real scars/end-goal.
4. **Cross-refs** — resolve?
5. **Friction note (elevated — this is the point) [F3]** — this is the FIRST loop run via
   the new driver `docs/harness/manager_invocation.md`. Name **at least one concrete thing
   the driver prompt made easy** AND **one thing it left ambiguous or missing** (or state
   "nothing ambiguous" with the reason). Also: did the one-file rule hold under a real
   worker (not just the Manager dogfooding it)? This is live calibration of the driver I
   was just handed — vague "worked fine" is a failure to report.
6. Commit SHA + files.

---

## Milestone
**Status:** closed
**Burndown:**
- [x] each of 6 deferred elements grounded in real v0.4 + v0.5 sections
- [x] `promotion_ladder.md` authored: intro + evidence-gated table + priority reasoning
- [x] consistency-checked vs A12/A13/A14 evidence (no trigger silently tripped)
- [x] README + loop_config_map cross-links resolve
- [x] one-file rule dogfooded (this doc holds milestone + closure)

**Live Log:**
- 2026-07-03 — Manager drafted + self-reviewed + locked packet → dispatching worker → next: worker executes
- 2026-07-03 — Worker: branched `feat/harness-ladder` off main tip `1efa722` (A13 merged); carried A15 packet + `manager_invocation.md` + DISPATCH_LOG A15 row across; did NOT carry A14's unrelated doc moves.
- 2026-07-03 — Worker: read both specs (v0.4 §§5/7/9/11/13 + §2.3/§12; v0.5 §§5/7/9/11/16 + §2.3/§12) + loop_config_map §(b) + A12 friction report. All 6 elements grounded in real section text; none invented.
- 2026-07-03 — Worker: authored `docs/harness/promotion_ladder.md` — 6-row evidence-gated table (2 writable triggers, 3 drop candidates, 1 flow-state). Consistency-checked vs A12/A13/A14: no trigger already-satisfied; A12's concurrency caveat flagged loudly as NOT a Row-1 trip.
- 2026-07-03 — Worker: added README row + loop_config_map "does NOT do" note (model-routing promotion governed by ladder). All cross-refs verified resolving. → next: commit + DISPATCH_LOG built.

## Closure

**Result: SHIPPED (docs-only).** One new file `docs/harness/promotion_ladder.md` + two
cross-link edits (README, loop_config_map). No `src/` code, no machinery, no other new
files. Held on `feat/harness-ladder`; not merged, not pushed.

**What shipped:**
- `docs/harness/promotion_ladder.md` — intro (bold "authorizes NOTHING" rule + standing
  "Phase 2 = NO" verdict), the 6-row evidence-gated table, the drop-candidate finding,
  the evidence-consistency section, priority reasoning, cross-refs.
- README.md — one row for the ladder in the "Which file to use when" table.
- loop_config_map.md — note in "What this map deliberately does NOT do": model-routing
  promotion is governed by the ladder (row 2), stays COLD until its trigger is observed.

**The ladder verdict (6 elements):**
- **2 have writable falsifiable triggers, both correctly still-COLD:** Row 1 flow-state
  (needs ≥2 lost-handoff resumes; 3 loops lost zero), Row 2 model-routing (named ≠ needed;
  3 loops ran fine single-model).
- **3 are drop candidates — no realistic trigger:** Row 4 live-tailing-reviewer
  (structurally unsafe — concurrent agents share a git tree), Row 5 async-memory-compression
  (superseded by two existing memory systems), Row 6 per-task-provider-smoke (forbidden by
  the Test Cost Guard on the hot path). Reported as a finding, not papered over.
- Row 3 wiki/HTML dashboard: gated (writable-ish trigger) but ranked low as doc-litter risk.

**Consistency finding (flagged loudly, not folded):** A12's friction report surfaced a real
*lock-contention* observation (two Executors race on the shared DISPATCH_LOG), which is NOT
the lost-*handoff*-state condition Row 1's trigger names and is already mitigated by the
"one worker per branch/tree" rule. Recorded in the ladder as a caveat to re-log if it recurs
after that rule is honored — it does NOT trip Row 1.

**F-tags honored:** [F1] drop candidates recorded honestly (rows 4/5/6) rather than given
mushy triggers. [F2] model routing kept COLD despite being a named gap. [F3/R3] no gate
ranked trippable-now; the A12/A13/A14 zero-lost-state evidence is stated explicitly and no
trigger is silently already-satisfied.

**Cross-refs:** all resolve (README→ladder, loop_config_map→ladder, ladder→both + both specs
+ A12; internal anchor defined+used). Verified by file-existence + grep, no paid CLI.

**Friction note (first loop via `manager_invocation.md`):** see the report handed back. In
short — the driver made the one-file rule and the "authorizes nothing / Phase 2 = NO"
grounding unambiguous and easy; the one ambiguity was branch hygiene when the loop's own
setup files (the A15 packet + `manager_invocation.md` itself) live only in a dirty non-main
working tree, which the "branch off main" step does not address. The one-file rule held:
this dispatch grew packet → `## Milestone` → `## Closure` with zero sibling files.
