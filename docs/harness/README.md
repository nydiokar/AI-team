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

> ⚠️ **Known drift (2026-08-01, see A64):** `packet_template.xml` and the three
> `generators/*.md` files below describe the XML-packet DRAFT/REVIEW ritual used through
> `AGENT_16`–`AGENT_29` (mid-July). No dispatch since `AGENT_36` (M2.5 onward) has used the XML
> shape — the actual, current house style is free-prose packets like `.ai/dispatch/AGENT_61_*`
> onward (`Why` / `TASK` / `TYPE` / `CONTEXT` / `ACCEPTANCE` / `RESERVED DECISIONS` / `SCOPE OUT`
> / `TRAIL` sections + a `## Milestone` burndown + `## Closure`, folded into one file per the
> ONE-FILE RULE). Nobody ever marked the XML ritual retired. Do not treat the table below as
> current practice for DRAFT/REVIEW until A64 reconciles it — `level_rubric.md`'s Level ladder and
> `dispatch_pipeline.md`'s stage doctrine + ONE-FILE RULE remain accurate and followed.
> `promotion_ladder.md` was already retired 2026-07-06 and is now deleted (git history only).

| File | Use it to… |
|------|-----------|
| [`level_rubric.md`](level_rubric.md) | pick the level (do this first) |
| [`packet_template.xml`](packet_template.xml) | ⚠️ stale — see drift note above; historical XML packet shape, not current practice |
| [`milestone_template.md`](milestone_template.md) | ⚠️ stale field shape — see drift note above; current practice: a `## Milestone` checkbox burndown folded into the dispatch doc |
| [`generators/draft_packet.md`](generators/draft_packet.md) | ⚠️ stale — see drift note above |
| [`generators/adversarial_review.md`](generators/adversarial_review.md) | REVIEW: packet → F-tagged findings (≤2 rounds) — the F-tag convention is still followed; the XML-packet framing is stale |
| [`generators/closure_summary.md`](generators/closure_summary.md) | CLOSE: what changed, F-tag outcomes, doc updates — still broadly accurate |
| [`dispatch_pipeline.md`](dispatch_pipeline.md) | the end-to-end runbook (start here to run a task) — stage doctrine + ONE-FILE RULE current; DRAFT/REVIEW artifact pointers stale |
| [`loop_config_map.md`](loop_config_map.md) | the loop's **control surface**: node table (driver/programmed-by/dials per stage), the "temperature" dials, Manager-vs-Executor behavior + Manager spec, and a failure→node→dial localization table (read this to debug a bad loop) |

## Cost guard (always)

No stage may invoke the **paid** Claude/Codex CLI to "verify". Use targeted
`pytest`, `--collect-only`, import smoke, `tsc -b`, `curl http://127.0.0.1:9003/health`.
Never run `python main.py status` (kills the live gateway).

## Spec

Full doctrine and rationale: [`../Task_harness_workflow.md`](../Task_harness_workflow.md) (v0.5).
