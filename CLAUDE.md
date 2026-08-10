# AI-Team — project orientation for agents

You are running inside the **AI-Team gateway** repo — a UI-controlled gateway for local
coding agents (see `.ai/context/production_vision.md`). This file is the **project layer**:
*where to look* and *the rules of this project*. Your *behavior* (who you are, how you act)
comes from your role system prompt; this file gives you *this project's* context. The two are
separate on purpose — do not expect your role prompt to name files or repo rules; they live here.

## Read these first (canonical, in order)
1. **`.ai/CONTEXT.md`** — hot context: what is active NOW, the **Current Priorities** table,
   current state, and constraints. **START HERE.**
2. **`.ai/dispatch/DISPATCH_LOG.md`** — the job ledger (the **primary human index + closure
   surface**): what has been dispatched, is in-flight, or is still open. Job state is also
   **machine-tracked** — each `AGENT_N_*.md` carries a ` ```yaml ` state block (protocol in
   `.ai/dispatch/CLAUDE.md`); keep `status:` correct via
   `scripts/dispatch/dispatch_state.py --set`, and `--audit` is the honest queue. The
   generated `_DISPATCH_STATE.md` is a complementary query view, **never** a replacement for
   DISPATCH_LOG.
3. **`.ai/DOC_MAP.md`** — which document owns what.

Then verify against **git** (`git log`, `git status`, `git show`, `gh pr list`) — never trust
prose over what the repository actually contains. If they conflict, surface it.

## Orient cheaply — resolve symbols before reading whole files
Reading a whole module just to find where something is defined is the biggest avoidable token
sink. A prebuilt **ctags symbol index** resolves `symbol → file:line` in <1 ms; then read only
that span (±20 lines) instead of the file.
- Look up a definition: `python scripts/repo_index/symbol_lookup.py --defs-only <Symbol>`
  (drop `--defs-only` to also see imports/references).
- Do this **before** `Read`/`Grep` when you can name the class/function/method you want.
- **Freshness is automatic** — each lookup rebuilds the index first if any source file is newer,
  so you almost never need `--build` by hand (pass `--no-auto` to skip the check in a tight loop).
- **A miss suggests the real name.** `dispatch_worker` (wrong) prints `_dispatch_worker` and other
  near matches instead of a bare "no matches" — read the suggestion, don't jump straight to Grep.
The index is `.ctags_index` (gitignored, per-repo — each clone builds its own; never committed).
It is a short-lived CLI, not a service: zero idle cost, nothing to keep running. It is
regex-based, so it covers every Python/TS definition but can miss dynamically-generated names —
fall back to `Grep` for those, and for concept ("where do we handle X") rather than exact-symbol
searches.

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
- **Checking the running gateway.** Use `curl http://127.0.0.1:9003/health` — a read-only
  liveness/status probe that reflects the actual running gateway.
- **Branch policy.** Docs-only work commits straight to `main`. Any `src/` / config / migration
  change cuts one `feat/<slug>` branch and opens a PR at close — never dangle a local branch, never
  carry another loop's edits. **You own the full close: commit, push, open the PR, AND merge it to
  `main` yourself. Do NOT sit on an open PR "awaiting operator sign-off" — pushing, opening, and
  merging PRs are yours, not the operator's. `--force` still stays out (§0 above), and don't carry
  another loop's unmerged edits into your merge.**
- **Restart policy (restart the gateway freely; NEVER a worker reflexively).** Restarting the
  **gateway** (`pm2 restart ai-team-gateway`) to make merged code live is delegated to you — the
  operator is fine with it; just do it when a deploy needs it. **Do NOT restart a worker / node-carrier
  process (e.g. the `ai-team-worker` daemon or the worker on `Horse`) on your own** — that disrupts
  live worker sessions. If a worker restart is genuinely needed, surface it to the operator as a
  decision instead of doing it silently. (Still never take a gateway/global lock to "verify", e.g.
  `python main.py status`, which kills the live gateway as a side-effect — that is distinct from a
  clean, intentional `pm2 restart`.)
- **Minimal diff / least action.** Change only what the task requires; preserve existing structure
  and formatting; no drive-by refactors.
- **Ground in git before you change; cross-layer honesty.** A green test on *your* layer does not
  prove the objective holds end-to-end — another layer can render a correct-looking change inert.
  Trace the value from where you changed it to where the goal is actually observed, and state which
  seams you verified and which you did not.
- Convert relative dates to absolute in anything you write (run `date` if unsure).

If you cannot see this project guidance in your context (no project `CLAUDE.md` loaded), **stop and
surface that** before running anything paid or destructive — do not guess your way past it.
