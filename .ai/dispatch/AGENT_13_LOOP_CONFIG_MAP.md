# AGENT_13 — Loop Configuration Map: make the harness's control surface legible before we drive real work

**Dispatch created:** 2026-07-03
**Level:** 2 (Standard) — prompt/artifact docs work under `docs/harness/`; no code, no mesh, no migration, no Level-3 trigger.
**Branch:** cut `feat/harness-config-map` from `main` (harness is merged to main; we're on latest main).
**Spec:** `docs/Task_harness_workflow.md` v0.5 · `docs/Task_Harness_v0.4.md` (the deferred end-state, for the deferral map) · `docs/harness/operating_model.md`.

> ⚠️ **TEST COST GUARD.** Docs-only task. No `pytest` needed beyond a link/cross-ref
> sanity check. NEVER invoke the paid Claude/Codex CLI. Never run the full e2e suite.
> Never `python main.py status`. Check a live gateway only with
> `curl http://127.0.0.1:9003/health`.

---

## Why this — the operator's actual intent (grounded)

The harness v1 is built AND merged to `main` (A9H/A11/A12 all merged; A10 T1/#9 gate
closed). The v0.5 spec is **exhausted** — everything in §13 is `[x]`. The operator does
**not** want more machinery built (v0.4's `flow_runs`/stage-machine stays deferred per
v0.5 §16 + A12's evidenced "Phase 2 = NO" verdict).

What the operator DOES want, verbatim intent: *"we need to know all the configurable steps
— it's the 'fake' node graph … so we can see where the temperature is coming from and what
changes the quality of the loops before we go do it all together and establish a contract.
… we need to invoke proper behavior for the manager (it's the driving force) and for the
workers via the dispatch tasks … pre-configure it correctly so we don't debug later on a
blackbox."*

**The gap this fills:** today the loop's behavior is described **per-file** (each generator
documents its own stage). There is **no single document** that maps, across all stages, the
*configurable behavior surface* — who drives each node, what file "programs" its behavior,
and what dials its output quality. So when a real loop produces bad output, you can't
localize which knob to turn. This task produces that map. It is the **pre-condition** for
driving real forward work through the harness — not the driving itself.

**This is NOT the far-future work.** It is explicitly NOT: extracting the v0.4 milestone
ladder, running a real feature loop, or building any graph UI / driver / flow_runs. Those
come AFTER this map exists (operator: "this is far in the future … got to test if only 1
loop is even working correctly").

---

## Objective (locked)

- **Real objective:** a person (or a fresh Manager/Executor agent) can open ONE document
  and see the entire loop as a set of configurable nodes — for each node: who drives it,
  which file programs its behavior, its input/output contract, and the specific dials that
  change its output quality ("temperature"). No behavior of the loop is a blackbox: any bad
  output localizes to one named node + its named knob.
- **Literal request:** "know all the configurable steps — the fake node graph — where the
  temperature comes from and what changes loop quality; invoke proper behavior for the
  manager and for the workers."
- **Interpreted task:** author **one new document** `docs/harness/loop_config_map.md` that
  is the loop's configuration/behavior contract, AND fill the one real gap it exposes: a
  **Manager behavior spec** (the Manager's per-node driving behavior is described nowhere —
  `operating_model.md` gives the loop *shape*, not the Manager's per-node behavioral
  contract). Cross-link both from `README.md`. This is a MAP of existing behavior + the
  one missing behavior spec — it changes NO stage logic and adds NO machinery.

---

## Approved plan (concrete, each step independently checkable)

0. **Branch off `main`, not the stale local branch. [F4]** Run
   `git checkout main && git pull --ff-only && git checkout -b feat/harness-config-map`.
   Confirm `git log --oneline -1` shows the current `main` tip (harness already merged),
   NOT a `feat/task-harness` commit. This avoids carrying obsolete branch state forward.
1. **Inventory the real control surface** (read-only grounding, no writing yet). From the
   files that already exist, extract for EACH of the 7 stages: the driving role
   (Manager/Executor), the file(s) that program its behavior, and every configurable dial.
   Sources are ONLY these (do not invent): `operating_model.md`, `dispatch_pipeline.md`,
   `generators/draft_packet.md`, `generators/adversarial_review.md`,
   `generators/closure_summary.md`, `level_rubric.md`, `packet_template.xml`,
   `milestone_template.md`, and the spec §3/§5/§7/§14. **Validation:** every dial named in
   the map traces to a real line in one of these files (cite `file` per dial); no invented
   knobs.
2. **Write `docs/harness/loop_config_map.md`** — the config map. Required structure:
   - **(a) The node table** — one row per stage (0=level-select through 7=close), columns:
     `Node | Driver (Manager/Executor) | Programmed by (file) | Input contract | Output
     contract | Quality dials`. **[F2] A node with genuinely fixed behavior and no dial
     MUST show `none (fixed behavior)` in the Quality-dials cell — an empty/honest cell is
     a CORRECT result, never fill it with a fabricated knob to look complete.**
   - **(b) The quality dials section** — enumerate every "temperature" knob and, for each,
     say: what it controls, its default, and which direction trades cost vs. quality.
     MUST include at minimum: level selection (`level_rubric.md`), the ≤2-round review cap
     (§3), objective-lock granularity (real vs literal vs interpreted), F-tag severity floor
     (P0/P1-only), context injection (`<context_snippets>` + `continues:`/`load_compact_context`),
     and memory reuse (file-memory). If a dial isn't in that list but is real, add it; if one
     listed doesn't actually exist, say so and drop it (don't fabricate).
   - **(c) The two behavioral roles, explicitly separated** — a short contract for the
     **Manager** (the driving force: grounding, objective-lock, scope containment, review
     gate, iterate/close/derive decision) vs. the **Executor** (works the burndown, updates
     milestone, commits at checkpoints, honest closure). State exactly where each role's
     behavior is configured.
   - **(d) A failure-localization table** — "if the loop produced BAD output X → suspect
     node N → turn dial D." This is the payoff: it makes the blackbox debuggable. **[F3]
     It MUST have ≥1 concrete row for each named quality dial in (b), so the map is
     demonstrably load-bearing (you can trace every dial to a failure it fixes), not a
     decorative essay.**
   **Validation:** the doc has all four sections; the node table has exactly 8 rows
   (level-select + 7 stages) or clearly states why a stage is omitted; every dial in (b) is
   cited to a source file.
3. **Write the Manager behavior spec** — the one real gap. Either a new
   `docs/harness/manager_behavior.md` OR a clearly-headed section inside `loop_config_map.md`
   (executor's call; state which and why). It must cover the Manager's per-node driving
   behavior: the mandatory grounding reflex (`operating_model.md`), objective-lock ownership,
   the review GATE (verify claims in git, run `/code-review` on the committed diff), and the
   iterate/close/derive decision. **Validation:** it references `operating_model.md`'s
   grounding rule and the §5 checkpoint-review mechanism; it does NOT invent a new role.
4. **Cross-link** from `docs/harness/README.md` ("Which file to use when" table) so the map
   is discoverable. **Validation:** the README row resolves; all cross-refs in the new
   doc(s) resolve (grep the link targets exist).

### Validation (non-paid, per the guard)
- `grep`/file-read that every dial in the map cites a real source line.
- link-target existence check for all new cross-refs.
- NO pytest (no code touched); NO paid CLI; NO gateway call.

### Definition of done
- `docs/harness/loop_config_map.md` exists with sections (a)-(d); node table complete;
  every quality dial cited to a real source file.
- The Manager behavior spec exists (own file or headed section) and separates Manager vs
  Executor behavior explicitly.
- `README.md` cross-links the new map; all cross-refs resolve.
- A milestone file + closure summary produced (this dispatch is itself a Level-2 harness
  run — dogfood the loop).
- `DISPATCH_LOG.md` A13 row updated to `built`; committed on `feat/harness-config-map`.
- Report back; HOLD on branch; do NOT merge (operator fork).

### Risks
- **R1 (scope creep into machinery):** the map might tempt "let's add a config file the
  gateway reads." → HARD non-goal: the map is a *human/agent-facing document*, it configures
  nothing at runtime. Zero new gateway state (§0/§11). If a dial *could* be promoted to
  runtime config, that's a NOTED future item, not built here.
- **R2 (inventing knobs):** describing dials that don't actually exist would make the map
  lie. → every dial MUST cite a real source line; uncited = dropped.
- **R3 (Manager spec drifting into a new role):** the Manager already exists in
  `operating_model.md`; this is a behavior *contract*, not a new participant. Do not invent
  a supervisor/critic agent (v0.5 dropped the Supervisor on purpose).

---

## Scope — DO NOT
- **No machinery.** No `flow_runs`, no stage column, no driver, no graph UI, no gateway
  config the runtime reads. v0.4's platform stays deferred (§16); A12 verdict stands.
- **[F1] The "node graph" is a TABLE + prose contract, NOT a rendered graphic.** One
  optional small Mermaid *illustration* of the linear stage flow is fine, but the
  deliverable IS the node table + dials + localization table — never a graph render, an
  interactive diagram, or an HTML/JS artifact. If you catch yourself building a visual, stop.
- **No code change to `src/`.** Docs/prompt-artifact only.
- **No running a real feature loop, no v0.4 milestone-ladder extraction.** That is the
  operator's explicit "far in the future" — this task only makes the surface legible first.
- **No merge, no new gateway state, no `.env` edits, no gateway run.**

## Report format (hand back)
1. **Node table** — did all 8 nodes map cleanly? any stage that resisted mapping (that's a
   finding about the loop, report it).
2. **Quality dials** — the full list, each cited. Any dial you expected but couldn't find
   in the source (a gap in the loop's configurability = a real finding).
3. **Manager vs Executor separation** — where each is configured; was the Manager behavior
   genuinely undocumented before (confirm the gap was real)?
4. **Failure-localization table** — the payoff table.
5. **Friction note** — running THIS as a Level-2 harness loop, where did a
   generator/template do real work vs. get in the way? (Feeds the "is 1 loop even working"
   question the operator wants answered.)

---

## Implementation log (A13 run)

### A13 — SHIPPED (2026-07-03)

**Branch:** `feat/harness-config-map`, cut off `main` tip `2b26115` (confirmed NOT a
`feat/task-harness` commit).

**Inventory result.** Read all named sources read-only: `operating_model.md`,
`dispatch_pipeline.md`, the three generators, `level_rubric.md`, `packet_template.xml`,
`milestone_template.md`, spec §2.1/§3/§5/§7/§8/§9/§14/§16. The control surface mapped
cleanly to **8 nodes** (level-select + 7 stages) and **11 real, cited dials**. No dial
was invented; nodes with fixed behavior say so (e.g. the Level-3 wiki at node 7).

**Map sections' status.**
- (a) Node table — DONE, exactly 8 rows, every cell's dial cited inline (or fixed).
- (b) Quality dials — DONE, 11 dials, each cited to a real source line, cost↔quality
  direction each; the 6 packet-required dials all present; one honest gap noted (no
  provider/model temperature dial in-loop).
- (c) Manager-vs-Executor separation — DONE, each role's config location stated.
- (d) Failure-localization table — DONE, 12 rows, ≥1 per dial (coverage check inline).

**Manager-spec decision.** Placed as a **headed section inside `loop_config_map.md`**,
not a separate `manager_behavior.md`. Rationale: the Manager's per-node behavior is only
legible next to the node table/dials it references; a second file would split one
question across two docs, and README already lists seven files. **The gap was real** —
`operating_model.md` listed the Manager's *responsibilities* but no doc stated its
*per-node driving behavior* ordered into a contract; the generators describe
DRAFT/REVIEW/CLOSE as modes, never attributed to the standing Manager.

**Cross-ref check.** README "Which file to use when" row added. All 10 link targets in
`loop_config_map.md` resolve (grep-verified); cited source lines spot-checked
(adversarial_review round-cap, milestone update-rule, guard flag, grounding rule).

**Friction note (dogfooding this as a Level-2 loop).** What did real work: the
**milestone file** genuinely tracked the burndown and forced honest step-logging;
the **level rubric** (docs-only, no L3 trigger, >1 file → Level 2) picked cleanly; the
**packet's objective-lock + F-tags [F1]-[F4]** were load-bearing — they actively
fenced the two strongest drift risks (building a graph render; inventing knobs). What
got in the way: for a **single-author docs task the DRAFT→REVIEW→FIX split is
notional** — the packet was already Manager-reviewed before dispatch, so I ran no
separate adversarial round; the generators are shaped for the multi-turn
Manager↔Executor path, and a solo Level-2 docs run collapses stages 1–3 into "read the
locked packet." That is fine (level scales stages), but it means **one loop "working"
here mostly exercised objective-lock + milestone discipline, not the review gate** — the
review gate (node 6) is still only lightly tested until a loop produces a real code diff
for `/code-review` to bite on. Verdict for the operator's "is 1 loop working" question:
the *artifact discipline* half of the loop demonstrably works; the *adversarial-review*
half is under-exercised by a docs task and wants a real code loop to validate.

**Verification:** grep link-target existence (10/10 OK); cited-line spot checks. No
pytest (docs-only, no code touched); no paid CLI; no gateway call.

**F-tag outcomes:** the packet's pre-dispatch findings F1 (no rendered graphic —
table+prose only, one small Mermaid *illustration*), F2 (`none (fixed behavior)` for
dial-less nodes), F3 (≥1 localization row per dial), F4 (branch off main) were all
**honored in the deliverable**.

---

<!-- Folded in by A14 (2026-07-03) under the one-file rule: this Milestone section was
     `AGENT_13_LOOP_CONFIG_MAP.milestone.md`, now removed. -->
## Milestone

### Objective
A person or a fresh Manager/Executor agent can open ONE document
(`docs/harness/loop_config_map.md`) and see the whole harness loop as a set of
configurable nodes — for each node: who drives it, which file programs it, its
input/output contract, and the dials that change its output quality. No loop
behavior is a blackbox; any bad output localizes to one named node + one named
dial. Plus: fill the one real gap — a Manager behavior spec.

### Current Status
closed

### Burndown
- [x] Branch `feat/harness-config-map` cut off current `main` tip (2b26115), not `feat/task-harness`
- [x] Ground the real control surface (read-only): operating_model, dispatch_pipeline, 3 generators, level_rubric, packet_template, milestone_template, spec §2.1/§3/§5/§7/§14/§16
- [x] `loop_config_map.md` section (a): node table, exactly 8 rows (level-select + 7 stages), every dial cited or `none (fixed behavior)`
- [x] `loop_config_map.md` section (b): quality-dials enumeration, each cited to a source file, cost-vs-quality direction stated
- [x] `loop_config_map.md` section (c): Manager vs Executor roles separated, where each is configured
- [x] `loop_config_map.md` section (d): failure-localization table, ≥1 row per named dial
- [x] Manager behavior spec (decision: headed section inside the map; see log) — grounding reflex, objective-lock, review gate, iterate/close/derive
- [x] README cross-link added; all cross-refs resolve (grep check)
- [x] No code in src/; no machinery; no paid CLI; docs-only
- [x] Milestone + closure produced; DISPATCH_LOG A13 → built; packet implementation log filled; committed

### Live Log
- 2026-07-03 — cut branch off main tip 2b26115 (confirmed not feat/task-harness) → clean base → ground
- 2026-07-03 — read all named sources → full control surface inventoried, every dial has a real source line → write the map
- 2026-07-03 — wrote loop_config_map.md sections (a)-(d) + Manager behavior spec as a headed section (chose in-file over separate manager_behavior.md) → all four sections present, 8-row node table → cross-link + verify
- 2026-07-03 — added README "Which file to use when" row; grep-verified every cross-ref target exists → all resolve → close
- 2026-07-03 — wrote closure summary; flipped DISPATCH_LOG A13 row to built; filled packet implementation log; committed on feat/harness-config-map → done

### Blockers
none

### Next Action
closed — none (HOLD on branch; do NOT merge — operator fork)

---

<!-- Folded in by A14 (2026-07-03) under the one-file rule: this Closure section was
     `AGENT_13_LOOP_CONFIG_MAP.closure.md`, now removed. -->
## Closure

### A13-loop-config-map — SHIPPED (2026-07-03)

**What changed (per file):**
- `docs/harness/loop_config_map.md` — NEW. The loop's control-surface contract: (a) an
  8-row node table (level-select + 7 stages; driver / programmed-by / IO-contract /
  quality-dials, each cited); (b) 11 enumerated "temperature" dials, each cited to a
  real source line with cost↔quality direction; (c) Manager-vs-Executor separation +
  a headed **Manager behavior spec** (the filled gap); (d) a 12-row failure→node→dial
  localization table (≥1 row per dial). One small Mermaid *illustration* of the linear
  flow; no rendered/interactive graphic.
- `docs/harness/README.md` — added a "Which file to use when" row cross-linking the map.
- `.ai/dispatch/AGENT_13_LOOP_CONFIG_MAP.md` — implementation log filled.
- `.ai/dispatch/DISPATCH_LOG.md` — A13 row `dispatched` → `built — awaiting Manager review`.

**Verification (non-paid, per the guard):**
- `grep` link-target existence for all cross-refs in the new doc: **10/10 resolve**.
- Cited-line spot checks: adversarial_review round-cap, milestone update-rule,
  `HARNESS_LEVEL3_GUARD` flag, grounding rule — all match.
- README target `loop_config_map.md` exists.
- No pytest (docs-only, no `src/` touched); no paid Claude/Codex CLI; no gateway call.

**F-tag outcomes:** F1 (table+prose, not a graphic) → done; F2 (`none (fixed behavior)`
honesty) → done; F3 (≥1 localization row per dial) → done; F4 (branch off `main` tip) → done.

**Findings surfaced (not defects — map results):**
1. **No provider/model "temperature" dial exists inside the loop.** The loop's quality
   "temperature" is *entirely* prompt/artifact discipline (the 11 dials). §9's
   low-temperature sampling is onboarding-only. Cheap-DRAFT / strong-REVIEW is a *stated
   preference*, not a wired per-node dial. Promoting it would be Phase-2 machinery.
2. **The Manager behavior spec gap was real.** `operating_model.md` listed the Manager's
   responsibilities but not its ordered per-node driving behavior; now filled.
3. **Dogfood friction:** a solo Level-2 docs run collapses DRAFT→REVIEW→FIX into
   "read the locked packet," and node 6's review gate is under-exercised without a real
   code diff. Milestone + objective-lock discipline demonstrably worked.

**What follows (not code):**
- Manager reviews the committed diff; then operator decides merge.
- HOLD on `feat/harness-config-map` — no merge, no push (operator fork).
- A future real *code* loop is the true test of the review-gate half of the harness.
