---
name: reviewing-worker-deliveries
description: Reviews a worker's (or any agent's) claimed delivery in the AI-Team repo against what git actually contains — mechanical delivery facts, Gate 0 relevance, acceptance-by-evidence, cross-layer tracing, this repo's known false-success patterns — and records the verdict with record_review. Use this whenever a dispatched worker finishes or wakes you, before accepting, merging or closing anything, when reviewing a PR or branch someone else produced, or when a hand-back says "done", "all tests pass" or "fixed".
---

# Reviewing worker deliveries

A hand-back is a claim; the committed diff is the evidence. The scoring rubric (Gate 0 + six
dimensions, ≥10/12, critical failures) lives in `docs/harness/roles/manager.md` — apply it; this
skill is the procedure for getting the evidence it scores.

## Workflow

```
- [ ] 1. Mechanical facts (script)
- [ ] 2. Gate 0: is it the task that was asked?
- [ ] 3. Each ACCEPTANCE item → evidence or gap
- [ ] 4. Trace across layers to where the goal is observed
- [ ] 5. Re-run the proof yourself
- [ ] 6. Verdict → record_review → act
```

**1. Facts first** — before reading the summary closely:
```bash
.claude/skills/reviewing-worker-deliveries/scripts/delivery_facts.sh <branch|sha|PR#> [base]
```
It resolves the claimed ref, lists commits/authors, diffstat, merge-cleanliness, and flags: ref
not in git, no commits / empty net diff, code without tests, new DB migrations (Level 3, deploy
needs backup), multiple authors (another loop's commits riding along). Any red flag is a
finding to resolve, not noise.

**2. Gate 0 (fail-closed).** Read the packet/objective's TASK and SCOPE OUT, then the diff.
Off-target work, or a named deliverable missing, is a critical failure regardless of quality.

**3. Acceptance by evidence.** For each ACCEPTANCE item, name the file:line, test, or command
output that proves it — or mark it unproven. "Tests pass" is not evidence for an item the tests
don't exercise; open the test and check it asserts the behavior, not a mock of it.

**4. Cross-layer trace.** Follow the changed value from where it was set to where the objective is
observed (API response, UI, worker payload, DB row). In this repo a correct-looking change has
repeatedly been made inert by another layer:
- a driver fix overwritten when the orchestrator re-classified `error_class`;
- `role_boot` dropped in node-payload serialisation, so the node-pinned path never saw it;
- a test whose fake session defaulted to `IDLE` hid a gate that was never true live
  (`AWAITING_INPUT`) — fakes that pick the convenient state prove nothing about the real one.
State which seams you verified and which you could not.

**5. Re-run.** Run the worker's tests yourself via `running-targeted-tests` (from the delivery's
tree: `git worktree add <scratch> <ref>`) — `.venv/bin/pytest <files>`, no extra `-q` (addopts
already has it; `-qq` hides the result line). Run every file the hand-back cites, plus the
selector's direct matches for the touched modules, not just the new test file. Whether it's live is a separate question —
`checking-live-state`.

**6. Verdict.** Call `mcp__manager__record_review` with `case_id`, `verdict`
(`accepted` | `rework_requested` | `waived`), a `reason` that cites the evidence, and the
worker's `task_id` (marks that finish consumed so it doesn't wake you again).
`rework_requested` blocks `close_case` until a later accept/waive. Outside a Case (plain PR
review), give the same verdict + evidence in the PR conversation.

## Writing a rework request

Specific and checkable: which acceptance item fails, the evidence (file:line, command output),
and what "fixed" will look like. Re-dispatch to the same warm worker (`session_id`) when the
context helps; start a fresh session if the worker died on quota or its context is poisoned.

## Repo-specific failure patterns

- **Success with nothing committed** — the script's "no commits / empty diff" flag; it has happened.
- **Parallel workers in one cwd** tangle commits and merges (one worker's commit shipped inside
  another's PR). `dispatch_worker` has no isolation flag: sequence them or give each its own
  worktree as `cwd`.
- **Scaffolding merged as done** — placeholders/TODO paths behind a green test. Grep the diff for
  `TODO|NotImplemented|placeholder|pass$` in new code.
- **Self-reported done, never reviewed** — direct commits to `main` for `src/` work bypass this
  gate; treat them as unreviewed until reviewed.
