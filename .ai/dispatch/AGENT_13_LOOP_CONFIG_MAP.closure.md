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
- `.ai/dispatch/AGENT_13_LOOP_CONFIG_MAP.milestone.md` — NEW. Level-2 burndown, closed.
- `.ai/dispatch/AGENT_13_LOOP_CONFIG_MAP.md` — implementation log filled.
- `.ai/dispatch/DISPATCH_LOG.md` — A13 row `dispatched` → `built — awaiting Manager review`.

**Verification (non-paid, per the guard):**
- `grep` link-target existence for all cross-refs in the new doc: **10/10 resolve**.
- Cited-line spot checks: adversarial_review round-cap (L46–56), milestone update-rule
  (L4–8), `HARNESS_LEVEL3_GUARD` flag (L125), grounding rule (L46) — all match.
- README target `loop_config_map.md` exists.
- No pytest (docs-only, no `src/` touched); no paid Claude/Codex CLI; no gateway call.

**F-tag outcomes:** packet pre-dispatch findings all honored in the deliverable —
F1 (table+prose, not a graphic) → done; F2 (`none (fixed behavior)` honesty) → done;
F3 (≥1 localization row per dial) → done; F4 (branch off `main` tip) → done.

**Findings surfaced (not defects — map results):**
1. **No provider/model "temperature" dial exists inside the loop.** The loop's quality
   "temperature" is *entirely* prompt/artifact discipline (the 11 dials). §9's
   low-temperature sampling is onboarding-only, not a per-task stage. Cheap-DRAFT /
   strong-REVIEW is a *stated preference* (§14, `draft_packet.md`), not a wired
   per-node dial. Promoting model-route-per-node would be Phase-2 machinery — noted,
   not built.
2. **The Manager behavior spec gap was real.** `operating_model.md` listed the
   Manager's responsibilities but not its ordered per-node driving behavior; now filled.
3. **Dogfood friction:** a solo Level-2 docs run collapses DRAFT→REVIEW→FIX into
   "read the locked packet," and node 6's review gate is under-exercised without a real
   code diff. Milestone + objective-lock discipline demonstrably worked; the
   adversarial-review half wants a real code loop to validate.

**What follows (not code):**
- Manager reviews the committed diff (`/code-review` on the docs diff is thin, but the
  cross-ref/citation audit is the real check); then operator decides merge.
- HOLD on `feat/harness-config-map` — no merge, no push (operator fork).
- A future real *code* loop is the true test of the review-gate half of the harness.
- Possible `continues:` handoff for that code loop, resuming this map's node/dial names.
