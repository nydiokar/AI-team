# Task Harness — prompt-and-artifact task-quality loop

The harness is a **small task-quality loop**, not a workflow engine. It adds
**zero new gateway state**: the XML task packet, the milestone burndown file, and
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

| File | Use it to… |
|------|-----------|
| [`level_rubric.md`](level_rubric.md) | pick the level (do this first) |
| [`packet_template.xml`](packet_template.xml) | lock objective + plan + execution rules (model-facing) |
| [`milestone_template.md`](milestone_template.md) | track resumable progress (executor keeps it current) |
| [`generators/draft_packet.md`](generators/draft_packet.md) | DRAFT: intent → packet + milestone |
| [`generators/adversarial_review.md`](generators/adversarial_review.md) | REVIEW: packet → F-tagged findings (≤2 rounds) |
| [`generators/closure_summary.md`](generators/closure_summary.md) | CLOSE: what changed, F-tag outcomes, doc updates |
| [`dispatch_pipeline.md`](dispatch_pipeline.md) | the end-to-end runbook (start here to run a task) |

## Cost guard (always)

No stage may invoke the **paid** Claude/Codex CLI to "verify". Use targeted
`pytest`, `--collect-only`, import smoke, `tsc -b`, `curl http://127.0.0.1:9003/health`.
Never run `python main.py status` (kills the live gateway).

## Spec

Full doctrine and rationale: [`../Task_harness_workflow.md`](../Task_harness_workflow.md) (v0.5).
