#!/usr/bin/env python3
"""
PM2 entry point for the mesh worker daemon.

PM2 treats `script` as a real file path, so it cannot run `python -m
src.worker.agent` directly (it would look for a file literally named "-m").
This thin launcher mirrors main.py: it puts ./src on sys.path and hands off to
src.worker.agent.main(), which loads .env, inits the shared observability spine,
and runs the WorkerAgent.

Run directly (no PM2 required):
    python worker_main.py
"""
import os
import sys
from pathlib import Path

# [A102] Scrub any agent-identity vars inherited from the parent process.
# When the worker is restarted from inside an agent turn (e.g. a PM2/OOM restart
# triggered while an agent is running), the child process inherits that agent's
# SESSION_ID/TURN_ID/etc.  Every session subsequently served by this worker would
# then inherit the dead identity — misaddressing MCP tool calls and losing watched-
# job notifications.  Purge them here, once, before any session is ever served.
# codex_native.py mirrors this discipline for the Codex runtime (see line ~292).
_STALE_AGENT_VARS = (
    "SESSION_ID",
    "AI_TEAM_SESSION_ID",
    "AI_TEAM_TURN_ID",
    "AI_TEAM_INVOCATION_ID",
    "CLAUDECODE",
)
for _var in _STALE_AGENT_VARS:
    os.environ.pop(_var, None)

# Match main.py: append (not prepend) src so we don't shadow third-party
# packages (e.g. telegram) that share a top-level name with our modules.
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.append(str(src_path))

def main() -> None:
    """Load the role environment before importing worker configuration."""
    try:
        from dotenv import load_dotenv

        configured_env = os.getenv("AI_TEAM_ENV_FILE")
        env_path = Path(configured_env) if configured_env else Path(__file__).parent / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=bool(configured_env))
    except ImportError:
        pass

    from src.worker.agent import main as worker_main

    worker_main()


if __name__ == "__main__":
    main()
