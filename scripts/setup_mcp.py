#!/usr/bin/env python3
r"""
setup_mcp.py -- Register the ai-team jobs MCP server in Claude Code's user config.

Run once per worker machine (Windows or Linux) from anywhere in the AI-team repo.
No tokens, no URLs, no secrets needed -- mcp_jobs.py reads the project .env itself.

Usage:
    python scripts\setup_mcp.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def _script_path() -> Path:
    return (Path(__file__).parent / "mcp_jobs.py").resolve()


def main() -> None:
    script = _script_path()
    if not script.exists():
        print(f"Error: mcp_jobs.py not found at {script}", file=sys.stderr)
        sys.exit(1)

    cfg_path = _claude_json_path()
    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            backup = cfg_path.with_suffix(".json.bak")
            print(f"Warning: could not parse {cfg_path}: {e}", file=sys.stderr)
            print(f"Backing up to {backup}", file=sys.stderr)
            cfg_path.replace(backup)
            existing = {}

    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["jobs"] = {
        "command": sys.executable,
        "args": [str(script)],
        # No env section -- mcp_jobs.py loads the project .env itself.
    }

    cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    print(f"Done. Updated: {cfg_path}")
    print(f"  server: jobs  ->  tool: mcp__jobs__watch_job")
    print(f"  script: {script}")
    print(f"  python: {sys.executable}")
    print()
    print("Claude Code will load it automatically. No restart needed for first-time setup.")
    print("If the worker was already running, restart it so it picks up the updated backend.")


if __name__ == "__main__":
    main()
