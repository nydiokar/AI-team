# Dispatch Pipeline — the end-to-end runbook (spec §14)

This is how a task moves from idea to executed change, repeatably. It is the
workflow the `.ai/dispatch/AGENT_*` files already follow, codified. A fresh
executor should be able to run a small task from **this file alone**.

> **Scope — two lanes.** This runbook governs the **authoring loop** and the
> **`.task.md` batch lane** (files dropped for the watcher to auto-pickup). It does
> **not** describe the **live `submit_instruction` lane** — the Telegram/Web path
> where an operator sends a turn to an existing session; that lane has no packet and
> no burndown, it just enqueues.
> Level-3 operator approval is an **authoring convention**. The former runtime
> admission predicate and `HARNESS_LEVEL3_GUARD` were removed (`3bf0317`); neither
> lane provides a general consequential-setting certification gate. See
> [ENV_FEATURE_FLAGS](../backend/ENV_FEATURE_FLAGS.md) and the
> [A113 proof boundary](consequential_decision_control.md).

**Zero new gateway state.** The free-prose dispatch packet, the `## Milestone` burndown
section, and the dispatch convention *are* the state. No `flow_runs` table, no stage
column (that is Phase 2, spec §16).

> **ONE-FILE RULE (the doc contract — [`.ai/DOC_MAP.md`](../../.ai/DOC_MAP.md)).** A
> dispatch grows **one living file**, `.ai/dispatch/AGENT_N_*.md`, through its whole
> life: **packet → `## Milestone` burndown → `## Closure`**, all folded into that one
> file. Do **not** spawn `.milestone.md` / `.closure.md` siblings. Materially-important
> **reference artifacts (maps, specs, runbooks) go in `docs/`, NEVER `.ai/dispatch/`** —
> the dispatch folder holds job packets and their inline lifecycle only.

Necessary separate supporting reports/proposals belong under
`.ai/dispatch/evidence/A<N>_<DESCRIPTION>.md`, linked both ways with the parent packet.
List proof in that parent's YAML `evidence:`. Never create top-level support-file siblings:
the registry scans every unexcluded top-level Markdown file as a job. Existing legacy
reports stay at their excluded paths to preserve references; new artifacts use `evidence/`.

> ⚠️ **Test Cost Guard.** No stage invokes the paid Claude/Codex CLI to "verify".
> Use targeted `pytest`, `--collect-only`, import smoke, `tsc -b`,
> `curl http://127.0.0.1:9003/health`. Never run `python main.py status` (kills the
> live gateway).

---

## The seven steps

```
(1) DRAFT      intent + curated context → free-prose packet + a `## Milestone` section in the dispatch doc
(2) REVIEW     adversarial pass → F-tagged P0/P1 findings
(3) FIX        revise packet inline per F-tag; cap 2 rounds; unresolved → non-goal/risk
(4) DISPATCH   write .ai/dispatch/AGENT_N_<NAME>.md (+ optional .task.md for auto-pickup)
(5) EXECUTE    executor works the burndown, updates the `## Milestone` section, commits at checkpoints
(6) CHECKPOINT reviewer reviews the COMMITTED diff (/code-review + /security-review)
(7) CLOSE      append `## Closure` + set milestone closed; update CONTEXT.md + DISPATCH_LOG.md
```

Every stage lives in the **one dispatch file** (§ ONE-FILE RULE above): the packet, the
`## Milestone` burndown, and the `## Closure` are sections of `AGENT_N_*.md`, not siblings.

Which steps run depends on the **level** — pick it FIRST with
[`level_rubric.md`](level_rubric.md). Level 0 is just `intent → execute`; Level 3
runs all seven plus the operator-approval gate.

---

## Step-by-step

### 1. DRAFT — [`generators/draft_packet.md`](generators/draft_packet.md)
Pick the level. Turn intent + curated context into a **free-prose dispatch packet** in
the current house style (the shape in
[`packet_template.md`](packet_template.md) — grounded in real packets `AGENT_52`–`AGENT_64`),
plus a `## Milestone (burndown)` checkbox section (body from
[`milestone_template.md`](milestone_template.md)) **inside the dispatch doc** — one file,
no `.milestone.md` sibling. Resume context, if any, comes from
`load_compact_context(task_id)` + file-memory — invent no memory store.

### 2. REVIEW — [`generators/adversarial_review.md`](generators/adversarial_review.md)
Adversarial pass over the packet. Emit F-tags (`[Fn]`, one-line defect, concrete
failure scenario) in the house style. P0/P1 only. Zero findings is a valid result.

### 3. FIX (≤ 2 rounds)
Revise the packet inline at each `[Fn]`. Stop after 2 rounds. Anything unresolved
becomes an explicit `## SCOPE OUT` item or a logged risk — never silently dropped. Record
each tag's outcome (`fixed` / `accepted` / `no change needed`) for the closure log.

### 4. DISPATCH — the auto-pickup handoff
Write the finalized packet to `.ai/dispatch/AGENT_N_<NAME>.md` — the one file that will
also carry this job's `## Milestone` and `## Closure` sections. Append a **one-line** row
to [`../../.ai/dispatch/DISPATCH_LOG.md`](../../.ai/dispatch/DISPATCH_LOG.md) as
`dispatched` (index shape: `# · Dispatch · Date · Level · Status · One-line` — the log is
an index, not a place for paragraphs). Any materially-important reference artifact the job
produces (a map, a spec, a runbook) goes in `docs/`, **never `.ai/dispatch/`**.

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
The executor picks up the task, works the Burndown, and **updates the `## Milestone`
section (in the dispatch doc) after every meaningful step** (this is what kills
hallucinated success). It commits at checkpoints. For rote/fragile extraction, use the **Single-Item
Long-Running lane** (spec §6): one item → verify → log → next; never batch and
claim success.

### 6. CHECKPOINT review (sequential, on the committed diff)
The executor commits, **then** the reviewer runs `/code-review` +
`/security-review` on the committed diff (spec §5). P0/P1 only. There is no
live-tailing reviewer and no second agent on the working tree — dispatches are
sequential single turns. Executor fixes bounded findings, then next slice.

### 7. CLOSE — [`generators/closure_summary.md`](generators/closure_summary.md)
Append a `## Closure` section **to the dispatch doc** (not a `.closure.md` sibling):
honest summary of what changed (per file), verification commands + results, F-tag
outcomes, what follows. Set the `## Milestone` burndown fully ticked. Update
`.ai/CONTEXT.md` (Shipped Ledger / Priorities) and advance the one-line `DISPATCH_LOG.md`
row to `built`/`reviewed`/`merged`. The Level-3 wiki is optional and never a gate.

---

## The auto-pickup safety boundary (Level-3 guard)

**Rule (convention — the dispatch prompt obeys this):** auto-enqueue via `.task.md`
is allowed for **Level ≤ 2** only. A `harness_level: 3` task
(migration / security / mesh / trading / autonomy / destructive / >~5 files) must
clear the **operator-approval stage before dispatch**. Approval is expressed as
`approved: true` in the frontmatter.

**Current enforcement:** the old runtime backstop was removed in `3bf0317`.
`approved: true` expresses the authoring decision; it is not authenticated proof
of a per-value choice. Do not set the retired flag or rely on the former predicate.
`HarnessAdmissionBlocked` remains for other admission failures such as disabled
local execution. A113 proves an isolated consumer guard; it activates no replacement
gate in `.task.md`, Web, Telegram or internal runtime admission.

Covered by `tests/test_harness_level3_guard.py` (24 cases: the pure predicate + the
`_enqueue_task` admission behavior). When the flag is unset the gate is a pure
pass-through — enable it on a host that wants the hard boundary; the convention is
the primary control everywhere else.

---

## Worked example — the whole loop, copyable, on one tiny task

This runs all seven stages on a **fictitious Level-1 task** with **real filled
artifacts** (not placeholders), so a fresh executor can copy the shapes. The task:
*"add a `curl /health` liveness one-liner to `docs/harness/README.md`."* No paid
call anywhere.

### Stage 1 — DRAFT ([`generators/draft_packet.md`](generators/draft_packet.md))

Pick the level first: single doc file, no Level-3 trigger fires → **Level 1**
([`level_rubric.md`](level_rubric.md)). Fill [`packet_template.md`](packet_template.md)
— free-prose, the shape real recent packets use:

```markdown
# DISPATCH — T-042 · health-oneliner in the harness README

**Level:** 1 (single low-risk doc file) · **Type:** docs
**Authored:** 2026-07-03 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — · **Branch:** `main` (docs-only)

> **Read this first — why this packet exists.** Operators keep grepping the codebase for
> the gateway's health port. One copyable one-liner in the harness README removes that
> friction — and pins the correct port so nobody suggests the gateway-killing status
> command.

## Why (intent)
A fresh operator can copy one command from the harness README to confirm the gateway is
live, without grepping the codebase for the port.

## TASK
1. Append one fenced `curl http://127.0.0.1:9003/health` line under a "Check the gateway
   is up" note in `docs/harness/README.md`. No script, no new endpoint. Verification:
   `grep -n "9003/health" docs/harness/README.md` shows the new line, and the port matches
   the live value (`curl http://127.0.0.1:9003/health` — read-only, allowed).

## TYPE
docs — commits straight to `main` per branch policy.

## CONTEXT (reuse verbatim)
- Test Cost Guard (`.ai/CONTEXT.md`): never run `python main.py status` (kills the live
  gateway); check liveness with `curl http://127.0.0.1:9003/health`.

## ACCEPTANCE (proof, not vibes)
1. README shows the copyable curl one-liner; `grep -c "main.py status" docs/harness/README.md`
   returns 0 as an instruction.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — which port to document.** 9003 is the operator port; 9002 is worker-facing.
  Default: 9003; flag if the live value differs.

## SCOPE OUT
No healthcheck script, Makefile target, or CI probe. Does not document :9002.

## TRAIL / EVIDENCE (fill at close)
- README diff · grep results.

---
## Milestone (burndown)
- [ ] README shows the copyable `curl http://127.0.0.1:9003/health` one-liner
- [ ] no `main.py status` reference
- [ ] port is 9003 (matches the live health check)

## Closure (fill on completion)
<filled at close>
```

> The `## Milestone (burndown)` is a checkbox list — that is the shape real packets use
> (`AGENT_52`–`AGENT_64`), not the older Objective/Current Status/Live Log field shape.

### Stage 2 — REVIEW ([`generators/adversarial_review.md`](generators/adversarial_review.md))

Adversarial pass over the packet above. Two genuine P1s (no P0 — docs-only):

```
### F1 (P1 — scope drift) — the TASK step could be read as "run the test suite".
Failure scenario: executor runs full pytest "to be safe" on a docs-only change, burning time.
Resolution: fixed inline — the TASK step now says "docs consistency check only — NO pytest needed".

### F2 (P1 — stale assumption) — port 9003 taken from memory, not verified.
Failure scenario: port changes to 9004; executor copies 9003 blindly; the one-liner is dead —
the exact opposite of the Why outcome.
Resolution: fixed inline — CONTEXT now says "VERIFY against the live value"; ACCEPTANCE greps that the port matches.
```

### Stage 3 — FIX (≤ 2 rounds)

Both F-tags fixed inline at the sections they guard (the packet above is already the
**locked, post-fix** copy). Re-review round 1: no new findings → **locked** (1
round, under the 2-cap). Nothing unresolved, so nothing spills to `## SCOPE OUT`.

### Stage 4 — DISPATCH

Level 1, same-branch, hand-run: **no `.task.md` needed** (auto-pickup is for
dispatched batch tasks). The packet is executed inline on the working branch. A
`DISPATCH_LOG.md` row is still appended as `dispatched`.

### Stage 5 — EXECUTE

Make the one edit; tick the `## Milestone (burndown)` box. One line lands in
`README.md`:

```md
**Check the gateway is up:** `curl http://127.0.0.1:9003/health` — do not run
`python main.py status` (it kills the live gateway).
```

### Stage 6 — CHECKPOINT

Review the **committed diff**, not the working tree. Docs-only, so the consistency
grep is the whole check — no `/security-review`, no `pytest`:

```
$ grep -n "9003/health" docs/harness/README.md   # → the new line
$ grep -c "main.py status" docs/harness/README.md # → 1 (the "do not" warning), 0 as an instruction
```

### Stage 7 — CLOSE ([`generators/closure_summary.md`](generators/closure_summary.md))

```
### T-042-health-oneliner — SHIPPED (2026-07-03)
What changed: docs/harness/README.md → adds a copyable `curl .../health` liveness line.
Verification: grep for "9003/health" (present) and "main.py status" as an instruction (absent). No pytest (docs-only).
F-tag outcomes: F1 → fixed; F2 → fixed.
What follows: none. No follow-up task; no `continues:`.
```

Then tick the remaining `## Milestone (burndown)` boxes, add the one-line
Shipped-Ledger entry to `.ai/CONTEXT.md`, and move the `DISPATCH_LOG.md` row to
`built`.

That is the whole loop. A fresh executor repeats it for any task, **scaling stages
by level** — a Level-0 typo collapses to stages 1+5; a Level-3 task adds the
operator-approval gate between stages 3 and 4.
