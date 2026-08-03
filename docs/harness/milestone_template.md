<!--
  MILESTONE BURNDOWN — the resumable progress ledger inside the dispatch doc.

  UPDATE RULE (read first): The executor updates this section after every meaningful
  step. On resume, THIS + `orchestrator.load_compact_context(task_id)` is ground truth —
  NOT model memory. If it isn't written here, it didn't happen. This is what replaces
  vague "keep working" behavior with visible milestone pressure and directly targets the
  recorded overbatch/hallucinate-success scar.

  WHERE IT LIVES (ONE-FILE RULE): the burndown is a `## Milestone (burndown)` SECTION
  *inside* the dispatch doc `.ai/dispatch/AGENT_N_*.md` — NOT a separate `.milestone.md`
  sibling. One dispatch = one living file (packet → milestone → closure). The actual
  shape in current use is the plain checkbox list below — the field-heavy
  Objective/Current Status/Live Log shape was never used by real packets and is retired
  (A64, 2026-08-01). Nothing parses it — it is a human/model-readable record, not a
  schema. (Contract: `.ai/DOC_MAP.md`.)
-->
## Milestone (burndown)

The `definition_of_done` as checkable items. Tick as you go. For a Single-Item
Long-Running lane (spec §6), each item is one unit: one item → verify → log → next. Do
NOT batch and claim success.

- [ ] item 1
- [ ] item 2

_(The trail a resuming agent reads is the git history + this list; if a step needs
explaining, add a dated line above the box it belongs to.)_
