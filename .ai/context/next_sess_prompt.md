# Handoff prompt — finish Phase 9 Step D (D4) + merge

**Read `.ai/CONTEXT.md` and `.ai/NEXT_TASKS.md` first.** Then read this whole file.

> ⚠️ **TEST COST GUARD — READ BEFORE RUNNING ANYTHING.** This project's tests can
> invoke the **live, paid Claude CLI** and previously burned millions of tokens.
> A guard now prevents it, but you must respect the rules:
> - Run tests with plain `pytest` only. Claude is physically unreachable from tests.
> - **NEVER** run the full e2e suite "to verify." Prefer cheap targeted checks
>   (import smoke, direct function calls, `--collect-only`, single skipped-test).
> - Real e2e is OpenCode-only: `AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`.
> - **Do NOT run `python main.py status`** — it acquires the gateway lock and
>   KILLS the live PM2 gateway. Use `curl http://<tailscale-ip>:9002/health`
>   (or `/metrics` with the WORKER_TOKEN bearer) to check the running gateway.

---

## Current state

- **Branch:** `feat/mesh-d1-observability-guard` (3 commits ahead of `main`, clean tree).
- **The live PM2 gateway is running** with `MESH_ENABLED=true` on Tailscale IP
  `100.112.245.29:9002`. Leave it running. It's on the OLD code until restarted.
- **Done + committed this session:** D1 (embedded task server), D1.5 (observability
  spine), D2 (worker traceback → Telegram), D3 (`/nodes` + `/node`), and the test
  cost guard. See NEXT_TASKS.md for the per-item detail.

## What's LEFT to do

### 1. D4 — status + session-list UX overhaul (the actual remaining feature)
Current `/status` and `/session_list` are walls of text. Make them compact.
Handlers live in `src/telegram/interface.py`:
- `_handle_status_command` (~line 1044)
- `_handle_session_list` (~line 1480, search for `def _handle_session_list`)
- Reuse helpers already there: `_mesh_online_nodes()`, `_heartbeat_age()` (added
  in D3), `_format_session_overview()`.

Target `/status`:
```
✅ Gateway running — 3 workers, 1 active session
Session: b52d0b06 | claude | LP-1 | awaiting_input
Path: AI-team
```
Target `/session_list`:
```
Sessions (3)
• b52d0b06 — claude — LP-1 — awaiting_input — AI-team
• ae01d054 — claude — this server — idle — narrative-engine
• [closed] f6e22e5d — claude — main-pc
```
Show the node column only when mesh is enabled and workers exist. Collapse closed
sessions to one line each. Keep it backward-compatible — don't remove data users
rely on, just tighten the layout.

### 2. D5 — `scripts/fix_session_machine_ids.py` (small, needed before VPS migration)
Per AGENT_MESH_SPEC.md §3.2. Reads all `state/sessions/*.json`, finds sessions
whose `machine_id == socket.gethostname()` (old server) and rewrites them to the
correct `WORKER_NODE_ID`. Dry-run by default, `--apply` to write. Idempotent.

### 3. Merge to main and finish
Once D4 (and ideally D5) are done and verified with cheap checks:
```bash
git checkout main
git merge --no-ff feat/mesh-d1-observability-guard
# delete the branch after merge
git branch -d feat/mesh-d1-observability-guard
```
Do NOT push unless the owner asks. Do NOT auto-restart the live gateway —
tell the owner to restart it (`pm2 restart ai-team-gateway`) to pick up the new
embedded-server + observability code, and remind them the separate
`ai-team-task-server` PM2 entry is gone (run `pm2 delete ai-team-task-server` if
it's still lingering).

## Verification (all CHEAP — no Claude, no full suite)
- `python -m py_compile src/telegram/interface.py` after edits.
- Format the new `/status` + `/session_list` output by calling the formatting
  logic directly against the live `state/mesh.db` / session store in a one-off
  `python -c` (use `PYTHONIOENCODING=utf-8` on Windows for emoji).
- `pytest tests/test_telegram_session_flow.py -q` (cheap, no backend) — note 1
  pre-existing failure in that file is NOT yours (it fails on baseline `main` too).
- Embedded server still good: `python scripts/test_embedded_server.py` (7/7).

## Known pre-existing test failures (NOT regressions — verified on baseline `main`)
`test_git_cli_integration` (3), `test_opencode_backend::test_resume_session_rejects...`,
`test_session_cancellation::test_session_timeout_calls_backend_cancel` (stale
assertion string), `test_telegram_session_flow` (some), `test_claude_session_backend`
(2), `test_git_automation::test_init_not_git_repo`. Don't chase these as part of D4.

## .env hygiene the owner still needs to confirm (don't edit .env — it's secret/blocked)
- `MESH_TAILSCALE_IP` was a literal comment string at one point; owner says the
  real IP is set now (gateway is serving on 100.112.245.29, so it's fine).
- dotenv loads with `override=True`, so `.env` beats process env vars — this is
  why `scripts/test_mesh_local.py` fails (hardcoded test token loses to real
  WORKER_TOKEN); unrelated to any D-work.
