# DROP — Canonical SDK driver for agent spawn + restore Manager MCP tools

- **Date:** 2026-07-22
- **Branch / PR:** `feat/canonical-sdk-agent-spawn` → PR #37
- **Case:** `88143385d73344eaab2809b0eff71658` (operator: "confirm Managers see the MCP tools;
  make sure we spawn agents via the SDK driver, not the CLI")
- **Status:** built + tested; merge/deploy pending (see "Restart required")

## The question that started it
"Do I (a Manager) see `dispatch_worker`? What do managers on other nodes need to see it?"

## What was actually wrong (three findings, grounded in code + git)

1. **PRIMARY — Managers lost their MCP tools to a `setting_sources` regression.**
   Role sessions were pinned to `setting_sources=["project"]` by `703faf5` (PR #28, 2026-07-18).
   The SDK loads user-scope `~/.claude.json` MCP servers only when `"user"` is in
   `setting_sources` (default `["user","project"]` when `None`). Dropping `"user"` meant the
   `manager` server never connected: the Manager booted with its role prompt + tool *names* in
   `allowed_tools` but **no working `dispatch_worker`**. Hit in-gateway AND on nodes (nodes now
   carry `case_role`/`role_boot` via PR #18/#25, so they reach the same code). Explains why
   A42/A44 (2026-07-13/14) had the tool but Managers after 2026-07-18 didn't.
   **Fix:** role sessions → `setting_sources=["user","project"]`.

2. **`run_oneoff` was hardwired to the CLI driver.** `ClaudeCodeBackend.run_oneoff` always
   delegated to `ClaudePrintResumeDriver` (`claude -p`), even with a healthy SDK driver. Only
   reachable by *sessionless* tasks — but a `dispatch_worker` without `cwd`/`session_id`
   produced exactly one.
   **Fix (Phase 2):** `ClaudeSDKClientDriver.run_oneoff` runs a transient SDK session
   (open→turn→close, torn down in `finally`); `ClaudeCodeBackend.run_oneoff` uses it whenever
   the active driver is `sdk`, CLI only as SDK-unavailable fallback.

3. **`dispatch_worker` could silently emit a sessionless (CLI) worker.**
   **Fix:** it now refuses a dispatch with neither `cwd` nor `session_id`.

### Verified NOT broken (killed assumptions)
- Managers are already spawned as **persistent SDK sessions** (`invoke_manager` → `create_session`
  + `submit_instruction(session_id=…)`); both the local (`orchestrator.py:3447`) and node
  (`agent.py:692`) dispatchers route sessionful tasks to `create_session`/`resume_session` (SDK)
  and only sessionless tasks to `run_oneoff`. The Manager never used `run_oneoff`.
- The live gateway runs the SDK driver (`.venv` has `claude_agent_sdk 0.2.110`; default
  `driver_type="sdk"` hard-errors rather than degrading). Whole-backend CLI degradation is NOT
  happening.
- `case_role`/`role_boot` already travel the mesh (PR #18/#25) — the old CONTEXT.md note about
  node-path role-boot is stale.

## Changes
- `src/backends/claude_driver.py` — role `setting_sources=["user","project"]`; new
  `ClaudeSDKClientDriver.run_oneoff` (transient session).
- `src/backends/claude_code.py` — `run_oneoff` prefers the SDK driver; warns on CLI fallback.
- `scripts/mcp_manager.py` — refuse sessionless `dispatch_worker` (coexists with #36 model tiering).
- `docs/adr/0001-canonical-sdk-driver-for-agent-spawn.md` — the canonical rule + root cause.
- Tests: `test_claude_driver.py` (setting_sources lock, transient one-off), `test_mcp_manager.py`
  (refusal), `test_manager_role.py`. Touched-module suites green (159 tests).

## Restart required (activation)
The `setting_sources` + `run_oneoff` fixes are **server-side driver code** — they take effect only
on restart:
- **Gateway restart** — required for in-gateway (`__local__`) Managers to get their MCP tools.
- **Node worker restart** — required for node-pinned Managers/Workers (same driver code runs there).
Operator-gated (live Managers are running). The `dispatch_worker` refusal (mcp_manager) is a
per-session subprocess and needs no restart — new Manager sessions pick it up on boot.

## For a node Manager to see `dispatch_worker`
node venv has `claude_agent_sdk` + `MANAGER_ROLE_ENABLED=1` + `setup_mcp.py --with-manager` on the
node + (already) `case_role` travels. Follow-up: inject `mcp_servers` programmatically to drop the
per-node `~/.claude.json` step.
