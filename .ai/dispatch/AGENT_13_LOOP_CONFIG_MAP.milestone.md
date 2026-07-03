# Milestone: A13 Loop Configuration Map

## Objective
A person or a fresh Manager/Executor agent can open ONE document
(`docs/harness/loop_config_map.md`) and see the whole harness loop as a set of
configurable nodes — for each node: who drives it, which file programs it, its
input/output contract, and the dials that change its output quality. No loop
behavior is a blackbox; any bad output localizes to one named node + one named
dial. Plus: fill the one real gap — a Manager behavior spec.

## Current Status
closed

## Burndown
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

## Live Log
- 2026-07-03 — cut branch off main tip 2b26115 (confirmed not feat/task-harness) → clean base → ground
- 2026-07-03 — read all named sources (operating_model, dispatch_pipeline, draft/adversarial/closure generators, level_rubric, packet_template, milestone_template, spec §2.1/§3/§5/§7/§14/§16) → full control surface inventoried, every dial has a real source line → write the map
- 2026-07-03 — wrote loop_config_map.md sections (a)-(d) + Manager behavior spec as a headed section (chose in-file over separate manager_behavior.md: it must sit adjacent to the node table it references, and README already lists enough files) → all four sections present, 8-row node table → cross-link + verify
- 2026-07-03 — added README "Which file to use when" row; grep-verified every cross-ref target exists → all resolve → close
- 2026-07-03 — wrote closure summary; flipped DISPATCH_LOG A13 row to built; filled packet implementation log; committed on feat/harness-config-map → done

## Blockers
none

## Next Action
closed — none (HOLD on branch; do NOT merge — operator fork)
