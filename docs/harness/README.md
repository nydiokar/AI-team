# Task Harness — prompt-and-artifact task-quality loop

The harness is a **small task-quality loop**, not a workflow engine. Its task-quality
process adds **zero new gateway state**: the free-prose dispatch packet, the inline
milestone burndown, and
the dispatch convention *are* the state. It rides on what the gateway already has
(`mesh_tasks` ledger, `load_compact_context`, file-memory). If you feel the urge to
add a migration or a stage machine — stop; that is Phase 2 (spec §16), out of scope.

**Why it exists:** the #1 scar in this project is burned tokens and false-success
from ungrounded execution. The fix is a *locked packet* (intent can't drift) and a
*visible ledger* (progress can't be hallucinated) — not machinery.

## The loop

```
intent → objective lock → plan → adversarial review → execution → checkpoint review → closure
```

Every stage is skippable by level. Tiny tasks bypass the whole thing.

## The level ladder (pick with `level_rubric.md`)

| Level | When | Runs |
|------:|------|------|
| **0** tiny | one-liner, typo, obvious local fix | just execute |
| **1** small | single file, low-risk | short plan → execute → optional review |
| **2** standard | normal localized change | full packet → plan review → execute → review → close |
| **3** strict | any Level-3 trigger (migration, security, mesh, autonomy, destructive, >~5 files) | + adversarial review + **operator approval** + checkpoint review + fix loop |

**Level 3 is never auto-picked-up without operator approval.** When in doubt,
escalate one level.

## Which file to use when

> **Current packet shape (A64, 2026-08-04):** XML was used through `AGENT_29`; the
> first post-cutover packet, `AGENT_31`, and all recent packets use free prose:
> `Why` / `TASK` / `TYPE` / `CONTEXT` / `ACCEPTANCE` / `RESERVED DECISIONS` / `SCOPE OUT`
> / `TRAIL`, followed by `## Milestone (burndown)` and `## Closure` in the same file.
> Use the template and generators below. Historical packets stay unchanged in
> `.ai/dispatch/`.

| File | Use it to… |
|------|-----------|
| [`level_rubric.md`](level_rubric.md) | pick the level (do this first) |
| [`packet_template.md`](packet_template.md) | current free-prose dispatch-packet shape, grounded in current packets |
| [`milestone_template.md`](milestone_template.md) | current inline `## Milestone (burndown)` checkbox shape |
| [`generators/draft_packet.md`](generators/draft_packet.md) | DRAFT: intent + level + curated context → free-prose packet |
| [`generators/adversarial_review.md`](generators/adversarial_review.md) | REVIEW: packet → F-tagged P0/P1 findings (≤2 rounds) |
| [`generators/closure_summary.md`](generators/closure_summary.md) | CLOSE: what changed, F-tag outcomes, evidence, and tracker updates |
| [`dispatch_pipeline.md`](dispatch_pipeline.md) | the end-to-end runbook (start here to run a task) — current stage doctrine + ONE-FILE RULE |
| [`loop_config_map.md`](loop_config_map.md) | the loop's **control surface**: node table (driver/programmed-by/dials per stage), the "temperature" dials, Manager-vs-Executor behavior + Manager spec, and a failure→node→dial localization table (read this to debug a bad loop) |

## Cost guard (always)

No stage may invoke the **paid** Claude/Codex CLI to "verify". Use targeted
`pytest`, `--collect-only`, import smoke, `tsc -b`, `curl http://127.0.0.1:9003/health`.
Never run `python main.py status` (kills the live gateway).

## Spec

Full doctrine and rationale: [`../Task_harness_workflow.md`](../Task_harness_workflow.md) (v0.5).
