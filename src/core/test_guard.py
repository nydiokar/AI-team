"""
Live-CLI cost guard.

Backends spawn real CLI subprocesses (Claude/Codex/OpenCode). Claude in
particular is billed per token, and several tests historically constructed a
real ``TaskOrchestrator`` (with its file watcher) which then dispatched task
files to the **live Claude backend** — silently burning money during a plain
``pytest`` run.

This module is the single choke point that prevents that. Every backend calls
``assert_live_calls_allowed(backend_name)`` immediately before spawning its CLI
subprocess. Behaviour:

  * Normal runtime (no test env vars): always allowed — production is unchanged.
  * Test mode (``AI_TEAM_TEST_MODE=1``, set automatically by tests/conftest.py):
    every paid backend is blocked with a clear RuntimeError. Tests physically
    cannot reach the Claude CLI.
  * Explicit OpenCode e2e opt-in (``AI_TEAM_ALLOW_OPENCODE_E2E=1``): only the
    ``opencode`` backend is permitted, so a deliberate end-to-end test runs
    through the cheap/free backend and never through Claude.

The contract is intentionally fail-closed: in test mode the default is BLOCK.
"""

import os

# Backends considered "paid" / never allowed under test mode without an explicit
# per-backend opt-in. (opencode is opt-in-able; claude/codex are not.)
_OPENCODE = "opencode"


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def live_calls_blocked() -> bool:
    """True when we're in a mode that should block paid live CLI spawns."""
    return _truthy("AI_TEAM_TEST_MODE") or _truthy("AI_TEAM_BLOCK_LIVE_CLI")


class LiveCallBlockedError(RuntimeError):
    """Raised when a backend tries to spawn a live CLI under test mode."""


def assert_live_calls_allowed(backend_name: str) -> None:
    """Raise if spawning a live CLI for ``backend_name`` is not permitted.

    No-op in normal runtime. Under test mode, blocks everything except an
    explicitly opted-in OpenCode e2e run.
    """
    if not live_calls_blocked():
        return

    name = (backend_name or "").strip().lower()

    # OpenCode is the only backend a test may opt into, and only explicitly.
    if name.startswith(_OPENCODE) and _truthy("AI_TEAM_ALLOW_OPENCODE_E2E"):
        return

    raise LiveCallBlockedError(
        f"Live '{backend_name}' CLI call blocked: AI_TEAM_TEST_MODE is set. "
        "Tests must not invoke paid backends. For a deliberate end-to-end test, "
        "use OpenCode and set AI_TEAM_ALLOW_OPENCODE_E2E=1 (and run with "
        "--run-e2e). Claude/Codex are never allowed from tests."
    )
