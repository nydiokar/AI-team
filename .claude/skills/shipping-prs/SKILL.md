---
name: shipping-prs
description: Takes finished work in the AI-Team repo from working tree to merged on main — branch policy, scoped staging, conventional commit, push, gh PR, CI check, self-merge, and the post-merge hand-off to deploy or dispatch closure. Use this whenever work is done and needs committing, a PR opened or merged, a branch closed out, or the user says "ship it", "open a PR", "merge it", "close this out" — including at the end of any feat/fix task.
---

# Shipping PRs

You own the whole close: commit, push, PR, **and the merge**. Leaving a PR open "awaiting
sign-off" or a local branch dangling is an unfinished task here, not caution.

## Checklist

```
- [ ] 1. Right branch for the change
- [ ] 2. Stage only this task's files
- [ ] 3. Targeted tests green
- [ ] 4. Commit (conventional, scoped)
- [ ] 5. Push + open PR
- [ ] 6. CI green, main merged in if behind
- [ ] 7. Merge + delete branch, sync local main
- [ ] 8. Post-merge hand-off
```

**1. Branch.** Docs-only (`.ai/**`, `docs/**`, `*.md`) commits straight to `main`. Anything
touching `src/`, `scripts/`, `web/`, config, `compose.yaml`, `Dockerfile`, `.claude/`, or a
migration gets one `feat/<slug>` (or `fix/<slug>`) branch off fresh `origin/main`. The live PM2
`ai-team-worker` runs from the main checkout, so do code work in a worktree rather than switching
branches under it:
`git fetch -q && git worktree add .worktrees/<slug> -b feat/<slug> origin/main`
(remove it after merge: `git worktree remove .worktrees/<slug>`).

**2. Stage by path.** `git status --short` first. Other loops and agents work in this tree; their
edits are not yours to ship. `git add <paths>` explicitly — never `git add -A` / `git add .`
(`.worktrees/` and other loops' files ride along). If an unrelated file is modified, leave it and
mention it.

**3. Tests.** Use the `running-targeted-tests` skill. Keep the exact command + result line for the PR.

**4. Commit.** Conventional, scoped, imperative — matches `git log`:
```
fix(heartbeat): renew lease before the timeout window closes
feat(control-api): add /api/flags explain view
docs(dispatch): close A89 with PR #171 evidence
```
Body: why, not what. Reference the dispatch id (`A97`) when there is one.

**5. PR.** `git push -u origin HEAD`, then
`gh pr create --base main --title "<commit subject>" --body-file <tmpfile>` with sections:
**What / Why**, **Verification** (exact commands + results), **Not verified** (seams you could
not exercise — be specific; another layer can make a correct-looking change inert),
**Deploy** (needs gateway rebuild? migrations? flags?).

**6. CI + freshness.** `gh pr checks <n> --watch`. On red: `gh run view <run-id> --log-failed`,
fix, push. If `main` moved: `git merge origin/main` on the branch (history uses merge commits —
do not rebase a pushed branch, and `--force` is never used here), re-test, push.

**7. Merge.** `gh pr merge <n> --merge --delete-branch`, then in the main checkout
`git pull --ff-only`. Confirm: `git log --oneline -1` shows the merge commit.

**8. Hand-off.**
- Code that runs in the gateway/task-server changed → `deploying-the-gateway` (merged ≠ live).
- Worker-side code (`worker_main.py`, `src/worker/**`) → the worker needs a restart; that is an
  **operator decision** — say so explicitly instead of doing it.
- A dispatch job is complete → `managing-dispatch-jobs` (closure + ledger).

## Report

PR URL, merge SHA, test command + result, and what still has to happen (deploy, worker restart,
ledger closure) with who owns it.
