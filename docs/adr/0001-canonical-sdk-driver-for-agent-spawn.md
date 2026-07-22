# ADR-0001 — Agents are always spawned on the canonical SDK driver, never the CLI driver

- **Status:** Accepted
- **Date:** 2026-07-22
- **Deciders:** operator + Manager (Case `88143385…`)

## Context

The gateway can drive a Claude backend two ways (`src/backends/claude_driver.py`):

1. **`ClaudeSDKClientDriver`** — a *persistent* `claude-agent-sdk` client held per session in
   a background event loop. Context stays resident; turns hit the prompt cache. This is the
   **canonical** driver and the whole reason the gateway SDK layer was built. Default
   `config.claude.driver_type = "sdk"` (`config/settings.py:110`), and `build_driver("sdk")`
   **raises** if the SDK is not importable — it does **not** silently degrade.
2. **`ClaudePrintResumeDriver`** — the legacy `claude -p [--resume]` **CLI subprocess** path.
   Stateless: every turn reconstructs full context from disk. No persistent client, no prompt
   cache. Kept **only** as (a) an SDK-unavailable fallback (`driver_type="auto"`/`"print_resume"`)
   and (b) the implementation behind `run_oneoff` (genuine one-shots).

The gateway already logs loud WARNINGs on any CLI-driver activity
(`event=backend_degraded`, `event=legacy_driver_active`, `event=driver_fallback`).

### What was verified (Case `88143385…`, 2026-07-22)

- Live gateway (`main.py`, pid observed) runs under `.venv/bin/python`; `.venv` has
  `claude_agent_sdk==0.2.110`. Default `driver_type="sdk"` ⇒ **the running gateway is on the
  SDK driver.** The whole backend is **not** silently on `claude -p`.
- **Managers** are spawned SDK-side: `/api/manager` → `orchestrator.invoke_manager()` →
  `create_session()` + `submit_instruction(session_id=…)` (a *sessionful* turn). ✓
- **Workers** dispatched with `cwd` open a real observable session (PR #19 + #23, merged) →
  sessionful → SDK. ✓
- **The leak:** the orchestrator routes a **sessionless** task via
  `action = "run_oneoff"` (`orchestrator.py`), and `ClaudeCodeBackend.run_oneoff` delegates to
  `self._fallback.run_oneoff` = `ClaudePrintResumeDriver` (`claude_code.py:365`) — the CLI
  driver — **even when the SDK driver is healthy.** A `dispatch_worker` call with neither a
  reused `session_id` **nor** a `cwd` produces exactly such a sessionless task: the worker then
  runs on `claude -p`, off the persistent client and its cache.

> Not conflated: an **interactive Claude Code console** (a human at a terminal, or a forked
> conversation) is Anthropic's own CLI harness — off the gateway substrate entirely. That is a
> separate surface from the two gateway drivers above and is out of scope for this ADR.

## Primary root cause — the Manager's MCP tools silently vanished (`setting_sources`)

Separate from the driver question, this is *why a Manager stopped seeing `dispatch_worker`*:

- A role session's MCP tool grant (`manager_tool_names()` → `mcp__manager__dispatch_worker`, …)
  only puts the tool **names** in `allowed_tools`. The `manager` (and `jobs`) MCP **servers**
  that back those names are registered in **user scope** (`~/.claude.json`).
- The Claude Agent SDK loads user-scope MCP servers only when `"user"` is in `setting_sources`
  (it defaults to `["user","project"]` when `setting_sources` is `None` —
  `claude_agent_sdk/_internal/transport/subprocess_cli.py`).
- Commit **`703faf5` (PR #28, 2026-07-18)** set role sessions to `setting_sources=["project"]`
  (to load the repo `CLAUDE.md`). That **dropped `"user"`**, so `--setting-sources=project`
  launched the CLI without user scope → the `manager` server never connected → the Manager
  booted with its role prompt and tool *names* but **zero working MCP tools**. This is exactly
  why A42/A44 (2026-07-13/14) had `dispatch_worker` but Managers after 2026-07-18 did not — on
  the in-gateway path **and** the node path (nodes now carry `case_role`/`role_boot` via PR
  #18/#25 and hit the identical regression).

**Fix:** role sessions use `setting_sources=["user", "project"]` — user scope restores the MCP
wiring; project scope still loads the repo `CLAUDE.md`.

## Decision

**Any agent we spawn — Manager or Worker — MUST run as a persistent SDK session.** The legacy
`ClaudePrintResumeDriver` / `run_oneoff` (`claude -p`) is reserved for genuine non-agent
one-shots and for the SDK-unavailable fallback. It must never be the path an agent lands on.

**A role session MUST load `"user"` setting scope** so the MCP servers backing its scoped tool
grant actually connect. Never narrow a role session's `setting_sources` to `["project"]` alone.

Concretely:

1. **`dispatch_worker` refuses a sessionless dispatch** (implemented, PR on
   `feat/canonical-sdk-agent-spawn`). With neither `session_id` nor `cwd` it raises rather than
   POST a sessionless instruction — the Manager must pass `cwd` (opens an observable SDK worker
   session) or `session_id` (reuse a warm worker). This closes the agent-level leak at the
   dispatch seam without touching the running orchestrator/driver, so it needs no restart to be
   safe.
2. **Phase 2 — `run_oneoff` now roots on the SDK client (IMPLEMENTED).**
   `ClaudeSDKClientDriver.run_oneoff` runs a genuine one-off in a **transient** SDK session
   (open → one turn → close, always torn down in `finally` — no leaked client/thread) and
   `ClaudeCodeBackend.run_oneoff` selects it whenever the active driver is `sdk`, falling back
   to `ClaudePrintResumeDriver` (`claude -p`) only when the SDK is unavailable. So even the
   legacy sessionless path no longer touches the CLI driver when the SDK is present. (Takes
   effect on gateway/worker restart — server-side code.)
3. **New agent-spawn code obeys this by default.** Whenever we add a path that starts an
   agent (worker, manager, or any future role), it goes through the SDK session seam
   (`create_session`/`submit_instruction(session_id=…)`), never `run_oneoff`/`claude -p`.

## What a Manager on another node needs to see `dispatch_worker`

With the `setting_sources` fix, a node Manager gets its MCP tools when ALL hold on that node:

1. **`claude_agent_sdk` in the node's venv** → the node runs the SDK driver (not `print_resume`,
   which has no `_role_boot` at all).
2. **`MANAGER_ROLE_ENABLED=1`** in the node worker's env (gates `_role_boot`).
3. **The `manager` server in the node's `~/.claude.json`** — run `python scripts/setup_mcp.py
   --with-manager` on the node. `_role_boot` allow-lists the tool names; user-scope
   `setting_sources` now loads the server that backs them.
4. `case_role`/`role_boot` travel the mesh (already handled — PR #18/#25).

Follow-up worth considering (not in this change): inject `mcp_servers` **programmatically** into
`ClaudeAgentOptions` for role sessions so a node needs no per-node `~/.claude.json` at all — the
gateway hands the SDK the server config directly. That removes step 3 as a manual per-node step.

## Consequences

- A `dispatch_worker` that omits `cwd`/`session_id` now fails fast with a clear message
  instead of silently burning quota on the CLI driver. This is a deliberate behavior change.
- Role sessions load `["user","project"]` scope — same as every non-role session's default —
  so their MCP servers connect. This restores `dispatch_worker` for all Managers on restart.
- `run_oneoff` no longer touches `claude -p` when the SDK is available (Phase 2 done).
- The CLI driver remains the honest fallback for SDK-unavailable environments; the existing
  degradation WARNINGs stay as the tripwire.
- **Activation requires a restart** of the gateway AND every node worker (server-side driver
  code): until then, running sessions keep the old behavior.
