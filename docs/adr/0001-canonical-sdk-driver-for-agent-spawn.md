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

## Decision

**Any agent we spawn — Manager or Worker — MUST run as a persistent SDK session.** The legacy
`ClaudePrintResumeDriver` / `run_oneoff` (`claude -p`) is reserved for genuine non-agent
one-shots and for the SDK-unavailable fallback. It must never be the path an agent lands on.

Concretely:

1. **`dispatch_worker` refuses a sessionless dispatch** (implemented, PR on
   `feat/canonical-sdk-agent-spawn`). With neither `session_id` nor `cwd` it raises rather than
   POST a sessionless instruction — the Manager must pass `cwd` (opens an observable SDK worker
   session) or `session_id` (reuse a warm worker). This closes the agent-level leak at the
   dispatch seam without touching the running orchestrator/driver, so it needs no restart to be
   safe.
2. **(Phase 2, restart-gated — NOT yet built)** Route `run_oneoff` through the active driver:
   when the primary driver is the SDK client, run a transient SDK session (open → one turn →
   close) instead of shelling out to `claude -p`. Keep `ClaudePrintResumeDriver.run_oneoff` only
   as the fallback when the SDK is unavailable. Requires care around SDK session lifecycle
   (§7 service-boundary: no leaked transient sessions) and a gateway restart to take effect.
3. **New agent-spawn code obeys this by default.** Whenever we add a path that starts an
   agent (worker, manager, or any future role), it goes through the SDK session seam
   (`create_session`/`submit_instruction(session_id=…)`), never `run_oneoff`/`claude -p`.

## Consequences

- A `dispatch_worker` that omits `cwd`/`session_id` now fails fast with a clear message
  instead of silently burning quota on the CLI driver. This is a deliberate behavior change.
- The deeper `run_oneoff`→SDK rewrite (Phase 2) is tracked but intentionally deferred to a
  restart-gated change so we don't disrupt live Manager/Worker sessions.
- The CLI driver remains the honest fallback for SDK-unavailable environments; the existing
  degradation WARNINGs stay as the tripwire.
