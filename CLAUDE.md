# AI-Team — project orientation for agents

You are running inside the **AI-Team gateway** repo — a UI-controlled gateway for local
coding agents (see `.ai/context/production_vision.md`). This file is the **project layer**:
*where to look* and *the rules of this project*. Your *behavior* (who you are, how you act)
comes from your role system prompt; this file gives you *this project's* context. The two are
separate on purpose — do not expect your role prompt to name files or repo rules; they live here.

## Read these first (canonical, in order)
1. **`.ai/CONTEXT.md`** — hot context: what is active NOW, the **Current Priorities** table,
   current state, and constraints. **START HERE.**
2. **`.ai/dispatch/DISPATCH_LOG.md`** — the job ledger: what has been dispatched, is in-flight,
   or is still open.
3. **`.ai/DOC_MAP.md`** — which document owns what.

Then verify against **git** (`git log`, `git status`, `git show`, `gh pr list`) — never trust
prose over what the repository actually contains. If they conflict, surface it.

## "Continue the work" — what it means here
When the objective is open-ended ("continue the project", "advance the work", "do what's next"):
1. Read CONTEXT.md's **Current Priorities** table + DISPATCH_LOG + recent git — orient yourself.
2. Identify the single highest-ranked **UNBLOCKED** item and drive it. As Manager: frame it as an
   outcome-based task, dispatch a worker, review the committed diff, and close on the evidence.
3. If nothing is genuinely unblocked, or the current milestone arc looks complete, **derive** the
   next direction instead of inventing busywork: propose 2-3 candidate directions with rationale,
   risk, and payoff, pick the one you would recommend, and **escalate the strategic choice to the
   operator**. Deciding the project's direction is a genuine fork — surface it, do not guess.

## Hard project rules (obey without exception)
- **TEST COST GUARD — safety-critical.** Tests can invoke the **paid** Claude CLI and have
  previously burned millions of tokens. Run **plain `pytest`** on the touched modules only. **NEVER**
  run the full or e2e suite "to verify" (real e2e is opt-in only: `AI_TEAM_ALLOW_OPENCODE_E2E=1
  pytest --run-e2e` — do not run it).
- **NEVER run `python main.py status`** — it takes the gateway lock and **kills the live gateway**.
  Check the running gateway with `curl http://127.0.0.1:9003/health`, nothing heavier.
- **Branch policy.** Docs-only work commits straight to `main`. Any `src/` / config / migration
  change cuts one `feat/<slug>` branch and opens a PR at close — never dangle a local branch, never
  carry another loop's edits. **Merging to `main`, deploys, and gateway restarts are the operator's
  decision — never merge or restart on your own; open the PR and hand back.**
- **Minimal diff / least action.** Change only what the task requires; preserve existing structure
  and formatting; no drive-by refactors.
- **Ground in git before you change; cross-layer honesty.** A green test on *your* layer does not
  prove the objective holds end-to-end — another layer can render a correct-looking change inert.
  Trace the value from where you changed it to where the goal is actually observed, and state which
  seams you verified and which you did not.
- Convert relative dates to absolute in anything you write (run `date` if unsure).

If you cannot see this project guidance in your context (no project `CLAUDE.md` loaded), **stop and
surface that** before running anything paid or destructive — do not guess your way past it.
