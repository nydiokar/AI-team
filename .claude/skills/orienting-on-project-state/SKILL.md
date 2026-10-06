---
name: orienting-on-project-state
description: Builds a grounded picture of where the AI-Team project stands right now — current priorities, open/stale dispatch jobs, recent merges, open PRs, unmerged branches, what is actually deployed — and reconciles the docs against git and the running system. Use this at the start of a session, when told to "continue the work", "what's next", "where are we", "pick up where we left off", before choosing what to work on, or when the docs and reality might disagree.
---

# Orienting on project state

The project's prose (`.ai/CONTEXT.md`, `DISPATCH_LOG.md`, packets) is written by many agents and
drifts; git and the running system don't. Orientation means reading both and naming where they
disagree, then picking work from the reconciled picture.

## Workflow

1. **Snapshot** (read-only, bounded, one call):
   ```bash
   .claude/skills/orienting-on-project-state/scripts/snapshot.sh
   ```
   CONTEXT Current Focus + Active Work, `dispatch_state.py --audit`, newest DISPATCH_LOG rows,
   git status / recent main / unmerged branches / worktrees, open PRs, and live state (containers,
   image age vs main, task-server health, PM2 worker).
2. **Reconcile.** List concrete mismatches, e.g.:
   - a job `active` in the audit but absent from CONTEXT (or `STALE_<n>d`) — abandoned or done?
     `git log --all --grep=A<N>` decides;
   - CONTEXT says shipped/live but `checking-live-state` shows main ahead of the image;
   - an open PR or unmerged branch nobody's ledger mentions — another loop's work: don't touch it,
     surface it;
   - a DISPATCH_LOG row `ready` whose dependency is still open.
3. **Read only what the choice needs.** Open a packet only for the candidate jobs. For code, resolve
   symbols with `python scripts/repo_index/symbol_lookup.py --defs-only <Symbol>` and read the span,
   not the file.
4. **Choose.** The highest-ranked **unblocked** item in CONTEXT's priorities that the audit
   agrees is open. Level 3 jobs (`docs/harness/level_rubric.md`) need operator approval before
   execution — they are not "unblocked" for you without it. If nothing is genuinely unblocked or
   the arc looks complete, propose 2–3 directions (rationale, risk, payoff), recommend one, and
   escalate the choice; don't invent busywork.

## Output

```
State:     <one line: focus + what is live vs merged>
Open:      <jobs genuinely in flight, with owner/age>
Drift:     <each doc-vs-reality mismatch, with the git/probe evidence>
Next:      <the pick + why it is unblocked>  |  <or the 2–3 options + recommendation>
```
Keep it to what a teammate needs to act; the raw snapshot stays in your context, not the report.
