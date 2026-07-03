# Dispatch Pipeline — the end-to-end runbook (spec §14)

This is how a task moves from idea to executed change, repeatably. It is the
workflow the `.ai/dispatch/AGENT_*` files already follow, codified. A fresh
executor should be able to run a small task from **this file alone**.

> **Scope — two lanes.** This runbook governs the **authoring loop** and the
> **`.task.md` batch lane** (files dropped for the watcher to auto-pickup). It does
> **not** describe the **live `submit_instruction` lane** — the Telegram/Web path
> where an operator sends a turn to an existing session; that lane has no packet and
> no burndown, it just enqueues.
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

**Surface handling is intentionally NOT built in this pass (backend-only).** The
gate raises a typed signal at the choke point and stops there. How each surface
presents "blocked" — an HTTP status on the control API, a message on Telegram — is
a **separate, later, WebUI-first integration task**, not part of the backend gate.
Until then a raised `HarnessAdmissionBlocked` propagates to the caller as an
unhandled error (and the `.task.md` lane still releases its file-tracking state so
an `approved: true` re-write is re-picked-up). Do not wire per-surface UX here.

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
([`level_rubric.md`](level_rubric.md)). Fill [`packet_template.xml`](packet_template.xml):

```xml
<task_packet>
  <meta>
    <task_name>T-042-health-oneliner</task_name>
    <harness_level>1</harness_level>
  </meta>
  <objective_lock>
    <real_objective>A fresh operator can copy one command from the harness README to
      confirm the gateway is live, without grepping the codebase for the port.</real_objective>
    <literal_request>"add a curl health check line to the harness readme"</literal_request>
    <interpreted_task>Append one fenced curl http://127.0.0.1:9003/health line under a
      "Check the gateway is up" note in docs/harness/README.md. No script, no new endpoint.</interpreted_task>
    <constraints>Docs-only. No code. Must NOT tell the reader to run `python main.py status`
      (kills the live gateway — Test Cost Guard).</constraints>
    <non_goals>No healthcheck script, Makefile target, or CI probe. Does not document :9002
      (worker-facing, not the operator port).</non_goals>
    <assumptions>The operator port is 9003 — VERIFY against S1 before writing, not from memory.</assumptions>
    <drift_risks>Scope-creep into a shell script; suggesting main.py status; wrong port.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Edit docs/harness/README.md: add a "Check the gateway is up" line with the fenced
      curl http://127.0.0.1:9003/health command.</steps>
    <validation>Docs consistency check only — NO pytest needed.
      grep -n "9003/health" docs/harness/README.md returns the new line; port matches S1;
      grep -c "main.py status" docs/harness/README.md returns 0.</validation>
    <definition_of_done>README shows the copyable curl one-liner; no main.py status reference; port is 9003.</definition_of_done>
    <risks>None (single doc line).</risks>
  </approved_plan>
  <execution_rules>
    <do>Update the milestone Live Log after the edit; commit docs-only.</do>
    <do_not>No paid CLI. No python main.py status. No new script.</do_not>
    <report_format>closure_summary.md shape.</report_format>
  </execution_rules>
  <context_snippets>
    <snippet id="S1" source=".ai/CONTEXT.md Test Cost Guard">
      <quote>Check the running gateway with curl http://127.0.0.1:9003/health. Do NOT run python main.py status.</quote>
      <why_relevant>Pins the correct port and the command to avoid — guards the two drift risks.</why_relevant>
    </snippet>
  </context_snippets>
</task_packet>
```

And the initialized [`milestone_template.md`](milestone_template.md) (shown here at
its final, closed state — Burndown ticked, Live Log carrying the trail):

```md
# Milestone: T-042 health one-liner

## Objective
A fresh operator can copy one curl command from the harness README to confirm the gateway is live.

## Current Status
closed

## Burndown
- [x] README shows the copyable `curl http://127.0.0.1:9003/health` one-liner
- [x] no `main.py status` reference
- [x] port is 9003 (matches S1)

## Live Log
- 2026-07-03T17:20 — drafted packet + milestone (Level 1) → locked after 1 review round → edit README
- 2026-07-03T17:24 — edited README.md → curl line added, grep checks pass → close

## Blockers
none

## Next Action
closed — none
```

### Stage 2 — REVIEW ([`generators/adversarial_review.md`](generators/adversarial_review.md))

Adversarial pass over the packet above. Two genuine P1s (no P0 — docs-only):

```
### F1 (P1 — scope drift) — <validation> could be read as "run the test suite".
Failure scenario: executor runs full pytest "to be safe" on a docs-only change, burning time.
Resolution: fixed inline — <validation> now says "docs consistency check only — NO pytest needed".

### F2 (P1 — stale assumption) — port 9003 taken from memory, not verified.
Failure scenario: port changes to 9004; executor copies 9003 blindly; the one-liner is dead —
the exact opposite of <real_objective>.
Resolution: fixed inline — <assumptions> now says "VERIFY against S1 before writing"; <validation> greps that the port matches S1.
```

### Stage 3 — FIX (≤ 2 rounds)

Both F-tags fixed inline at the fields they guard (the packet above is already the
**locked, post-fix** copy). Re-review round 1: no new findings → **locked** (1
round, under the 2-cap). Nothing unresolved, so nothing spills to `<non_goals>`.

### Stage 4 — DISPATCH

Level 1, same-branch, hand-run: **no `.task.md` needed** (auto-pickup is for
dispatched batch tasks). The packet is executed inline on the working branch. A
`DISPATCH_LOG.md` row is still appended as `dispatched`.

### Stage 5 — EXECUTE

Make the one edit; update the milestone Live Log (done above). One line lands in
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

Then set the milestone `Current Status: closed` (done above), add the one-line
Shipped-Ledger entry to `.ai/CONTEXT.md`, and move the `DISPATCH_LOG.md` row to
`built`.

That is the whole loop. A fresh executor repeats it for any task, **scaling stages
by level** — a Level-0 typo collapses to stages 1+5; a Level-3 task adds the
operator-approval gate between stages 3 and 4.
