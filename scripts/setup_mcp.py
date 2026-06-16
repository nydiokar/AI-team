#!/usr/bin/env python3
"""
setup_mcp.py — Register the ai-team jobs MCP server in Claude Code's user config.

Run once per worker machine (Windows or Linux).
Merges the 'jobs' MCP server entry into ~/.claude.json  (%USERPROFILE%\.claude.json on Windows),
which is the location Claude Code reads automatically — no CLI flags needed.

Usage:
    python setup_mcp.py
    python setup_mcp.py --url http://100.x.y.z:9002 --token mytoken --node Horse

Omitted flags are read from CONTROLLER_URL / WORKER_TOKEN / NODE_ID env vars.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def _claude_json_path() -> Path:
    """~/.claude.json on Linux/Mac, %USERPROFILE%\\.claude.json on Windows."""
    return Path.home() / ".claude.json"


def _script_path() -> Path:
    return (Path(__file__).parent / "mcp_jobs.py").resolve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register ai-team jobs MCP server in Claude Code user config."
    )
    parser.add_argument("--url",   default=None, help="Task server URL (CONTROLLER_URL)")
    parser.add_argument("--token", default=None, help="Worker token (WORKER_TOKEN)")
    parser.add_argument("--node",  default=None, help="Node ID (NODE_ID)")
    args = parser.parse_args()

    url   = args.url   or os.environ.get("CONTROLLER_URL", "http://127.0.0.1:9002")
    token = args.token or os.environ.get("WORKER_TOKEN", "")
    node  = args.node  or os.environ.get("NODE_ID", socket.gethostname())

    if not token:
        print("Error: WORKER_TOKEN is required (--token or env var).", file=sys.stderr)
        sys.exit(1)

    script = _script_path()
    if not script.exists():
        print(f"Error: mcp_jobs.py not found at {script}", file=sys.stderr)
        sys.exit(1)

    cfg_path = _claude_json_path()

    # Read existing ~/.claude.json (may contain other settings / MCP servers).
    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Warning: could not parse {cfg_path}: {e}", file=sys.stderr)
            print("A backup will be written before modifying.", file=sys.stderr)
            backup = cfg_path.with_suffix(".json.bak")
            cfg_path.replace(backup)
            print(f"Backup: {backup}", file=sys.stderr)
            existing = {}

    # Merge in the jobs server — leaves all other entries untouched.
    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["jobs"] = {
        "command": sys.executable,
        "args": [str(script)],
        "env": {
            "CONTROLLER_URL": url,
            "WORKER_TOKEN":   token,
            "NODE_ID":        node,
        },
    }

    cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    print(f"Updated: {cfg_path}")
    print(f"  server: jobs  →  tool: mcp__jobs__watch_job")
    print(f"  node:   {node}")
    print(f"  url:    {url}")
    print(f"  python: {sys.executable}")
    print()
    print("Claude Code will load it automatically on next invocation.")


if __name__ == "__main__":
    main()
