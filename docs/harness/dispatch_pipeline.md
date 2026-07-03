# Dispatch Pipeline — the end-to-end runbook (spec §14)

This is how a task moves from idea to executed change, repeatably. It is the
workflow the `.ai/dispatch/AGENT_*` files already follow, codified. A fresh
executor should be able to run a small task from **this file alone**.

> **Scope.** This runbook governs the authoring loop and the `.task.md` batch lane.
> The **Level-3 admission gate**, however, is NOT limited to `.task.md`: it runs in
> `orchestrator._enqueue_task`, the choke point every ingestion lane shares
> (`submit_instruction` from Telegram/Web, `.task.md` auto-pickup, and internal
> runtime tasks). So an un-approved Level-3 task is refused on the main door too,
> not just the batch lane.

**Zero new gateway state.** The XML packet, the milestone file, and the dispatch
convention *are* the state. No `flow_runs` table, no stage column (that is Phase 2,
spec §16).

> ⚠️ **Test Cost Guard.** No stage invokes the paid Claude/Codex CLI to "verify".
> Use targeted `pytest`, `--collect-only`, import smoke, `tsc -b`,
> `curl http://127.0.0.1:9003/health`. Never run `python main.py status` (kills the
> live gateway).

---

## The seven steps

```
(1) DRAFT      intent + curated context → XML packet + milestone file
(2) REVIEW     adversarial pass → F-tagged P0/P1 findings
(3) FIX        revise packet inline per F-tag; cap 2 rounds; unresolved → non-goal/risk
(4) DISPATCH   write .ai/dispatch/<NAME>.md (+ optional .task.md for auto-pickup)
(5) EXECUTE    executor works the burndown, updates milestone, commits at checkpoints
(6) CHECKPOINT reviewer reviews the COMMITTED diff (/code-review + /security-review)
(7) CLOSE      closure summary + milestone→closed; update CONTEXT.md + DISPATCH_LOG.md
```

Which steps run depends on the **level** — pick it FIRST with
[`level_rubric.md`](level_rubric.md). Level 0 is just `intent → execute`; Level 3
runs all seven plus the operator-approval gate.

---

## Step-by-step

### 1. DRAFT — [`generators/draft_packet.md`](generators/draft_packet.md)
Pick the level. Turn intent + curated `<context_snippets>` into a filled
[`packet_template.xml`](packet_template.xml) and an initialized
[`milestone_template.md`](milestone_template.md). Resume context, if any, comes
from `load_compact_context(task_id)` + file-memory — invent no memory store.

### 2. REVIEW — [`generators/adversarial_review.md`](generators/adversarial_review.md)
Adversarial pass over the packet. Emit F-tags (`[Fn]`, one-line defect, concrete
failure scenario) in the house style. P0/P1 only. Zero findings is a valid result.

### 3. FIX (≤ 2 rounds)
Revise the packet inline at each `[Fn]`. Stop after 2 rounds. Anything unresolved
becomes an explicit `<non_goal>` or a logged risk — never silently dropped. Record
each tag's outcome (`fixed` / `accepted` / `no change needed`) for the closure log.

### 4. DISPATCH — the auto-pickup handoff
Write the finalized packet to `.ai/dispatch/<NAME>.md`. Append a row to
[`../../.ai/dispatch/DISPATCH_LOG.md`](../../.ai/dispatch/DISPATCH_LOG.md) as
`dispatched`.

Optionally drop a `.task.md` (YAML frontmatter) into the watched directory so the
file-watcher auto-enqueues it. **The existing auto-pickup primitive:**

```
src/services/file_watcher.py::TaskFileHandler._is_task_file   (matches *.task.md)
  → orchestrator.py::_handle_new_task_file                    (validate → parse → guard → enqueue)
    → src/services/task_parser.py::parse_task_file            (frontmatter → Task.metadata)
      → orchestrator.py::_enqueue_task                        (the enqueue point)
```

`.task.md` frontmatter carries the harness fields:

```yaml
---
id: T-014-slice-2
type: fix
priority: medium
harness_level: 2          # from level_rubric.md — REQUIRED for a dispatched task
continues: task_99bc7bec  # optional: prior task id to resume context from (spec §7)
# approved: true          # REQUIRED to auto-enqueue a harness_level: 3 file
---
```

### 5. EXECUTE
The executor picks up the task, works the Burndown, and **updates the milestone
file after every meaningful step** (this is what kills hallucinated success). It
commits at checkpoints. For rote/fragile extraction, use the **Single-Item
Long-Running lane** (spec §6): one item → verify → log → next; never batch and
claim success.

### 6. CHECKPOINT review (sequential, on the committed diff)
The executor commits, **then** the reviewer runs `/code-review` +
`/security-review` on the committed diff (spec §5). P0/P1 only. There is no
live-tailing reviewer and no second agent on the working tree — dispatches are
sequential single turns. Executor fixes bounded findings, then next slice.

### 7. CLOSE — [`generators/closure_summary.md`](generators/closure_summary.md)
Honest summary: what changed (per file), verification commands + results, F-tag
outcomes, what follows. Set the milestone `Current Status: closed`. Update
`.ai/CONTEXT.md` (Shipped Ledger / Priorities) and move the `DISPATCH_LOG.md` row
to `built`/`reviewed`/`merged`. The Level-3 wiki is optional and never a gate.

---

## The auto-pickup safety boundary (Level-3 guard)

**Rule (convention — the dispatch prompt obeys this):** auto-enqueue via `.task.md`
is allowed for **Level ≤ 2** only. A `harness_level: 3` task
(migration / security / mesh / trading / autonomy / destructive / >~5 files) must
clear the **operator-approval stage before dispatch**. Approval is expressed as
`approved: true` in the frontmatter.

**Enforcement backstop (code, flag-guarded, OFF by default):** the decision
predicate `orchestrator.py::_harness_level3_allows_autopickup` is invoked at
admission in `_enqueue_task` — the shared choke point — so it covers **every**
lane (`submit_instruction` from Telegram/Web, `.task.md` auto-pickup, internal
tasks). It is opt-in via the `HARNESS_LEVEL3_GUARD` env flag:

| `HARNESS_LEVEL3_GUARD` | `harness_level` | `approved` | Result |
|------------------------|-----------------|-----------|--------|
| unset / falsey         | *(anything)*    | —         | **allow** (byte-identical legacy behavior) |
| on (`1`/`true`/`yes`/`on`) | absent      | —         | allow (unchanged) |
| on                     | 0 / 1 / 2       | —         | allow (enqueue) |
| on                     | 3               | absent / false | **BLOCK** — emits `task_blocked`, raises `HarnessAdmissionBlocked`, nothing queued |
| on                     | 3               | `true`    | allow |
| on                     | unparseable     | —         | allow (never invents a block) |

On a block, `_enqueue_task` raises `HarnessAdmissionBlocked(task_id, reason)`
**instead of returning a task_id** — so no caller can mistake a blocked task for
an accepted one, and no side effect (queue / `active_tasks`) leaks past the gate.
Operator-facing callers catch it and surface it honestly: the control API returns
**HTTP 409** (`harness_level3_needs_approval`); Telegram replies "needs operator
approval, not started". The `.task.md` lane releases its file-tracking state so an
`approved: true` re-write is picked up on the next watch event. Covered by
`tests/test_harness_level3_guard.py` (24 cases: the pure predicate + the
`_enqueue_task` admission behavior). When the flag is unset the gate is a pure
pass-through — enable it on a host that wants the hard boundary; the convention is
the primary control everywhere else.

---

## Worked example (a real small task — this dispatch's own doc update)

A Level-1 run, to show the loop end-to-end without a paid call:

1. **DRAFT** — intent: *"tick the §13 checklist in `docs/Task_harness_workflow.md`
   for what shipped."* Level = **1** (single doc file, low-risk; no Level-3
   trigger). Packet `<real_objective>`: the spec's build-scope checklist reflects
   reality. Milestone Burndown = the checklist items to tick.
2. **REVIEW** — one plausible F-tag: *"[F1] don't tick item 9 (`continues:`) if it
   was shipped by a PRIOR dispatch, not this one — ticking implies this task did
   it."* Resolution: tick only what this dispatch delivered; note the rest as
   already-shipped.
3. **FIX** — packet updated inline; 1 round; done.
4. **DISPATCH** — Level 1, no `.task.md` needed; done inline on the branch.
5. **EXECUTE** — edit the checklist; milestone Live Log updated.
6. **CHECKPOINT** — `git diff` reviewed; docs-only, no `/security-review` needed.
7. **CLOSE** — summary in the dispatch Implementation log; `DISPATCH_LOG.md` row →
   `built`.

That is the whole loop. A fresh executor repeats it for any task, scaling stages by
level.
