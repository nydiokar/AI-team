<!--
  MILESTONE BURNDOWN FILE — the resumable progress ledger (spec §2.2).

  UPDATE RULE (read first): The executor updates this file after every meaningful
  step. On resume, THIS FILE + `orchestrator.load_compact_context(task_id)` is
  ground truth — NOT model memory. If it isn't written here, it didn't happen.
  This is what replaces vague "keep working" behavior with visible milestone
  pressure and directly targets the recorded overbatch/hallucinate-success scar.

  WHERE IT LIVES (ONE-FILE RULE): the burndown is a `## Milestone` SECTION *inside*
  the dispatch doc `.ai/dispatch/AGENT_N_*.md` — NOT a separate `.milestone.md`
  sibling. One dispatch = one living file (packet → milestone → closure). Paste this
  template's body under a `## Milestone` heading in the dispatch doc and keep it
  current there. Nothing parses it — it is a human/model-readable record, not a schema.
  (Contract: `.ai/DOC_MAP.md`.)
-->
# Milestone: <TASK-ID> <short title>

## Objective
<!-- One or two sentences: the real objective from the packet's <objective_lock>.
     What outcome closes this task. -->

## Current Status
<!-- One word, kept current: drafting / executing / reviewing / blocked / closed -->
drafting

## Burndown
<!-- The definition_of_done from the packet, as checkable items. Tick as you go.
     For a Single-Item Long-Running lane (spec §6), each item is one unit:
     one item → verify → log → next. Do NOT batch and claim success. -->
- [ ] item 1
- [ ] item 2

## Live Log
<!-- Append-only. One line per meaningful step:
     `<timestamp> — action taken → result → next action`.
     This is the trail a resuming agent (or the checkpoint reviewer) reads. -->
- <timestamp> — <action> → <result> → <next action>

## Blockers
<!-- What is stopping progress right now, or "none". For a Level-3 task awaiting
     operator approval before dispatch, record it here. -->
none

## Next Action
<!-- The single next concrete step. On resume, start here. -->
<the one next step>
