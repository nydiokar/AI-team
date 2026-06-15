#!/usr/bin/env python3
"""
setup_mcp.py — One-time MCP setup for an ai-team worker machine.

Run this once on every machine (Windows or Linux) that will run agent tasks.
It writes ~/.config/ai-team/mcp.json pointing at mcp_jobs.py so that
Claude Code picks it up automatically on every invocation.

Usage:
    python setup_mcp.py
    python setup_mcp.py --url http://100.x.y.z:9002 --token mytoken --node Horse

If --url / --token / --node are omitted, reads from CONTROLLER_URL / WORKER_TOKEN / NODE_ID.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
from pathlib import Path


def _config_dir() -> Path:
    """Return the ai-team config directory, cross-platform."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home()
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "ai-team"


def _script_path() -> Path:
    """Absolute path to mcp_jobs.py, resolved relative to this file."""
    return (Path(__file__).parent / "mcp_jobs.py").resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Write ai-team MCP config for this machine.")
    parser.add_argument("--url",   default=None, help="Task server URL (CONTROLLER_URL)")
    parser.add_argument("--token", default=None, help="Worker token (WORKER_TOKEN)")
    parser.add_argument("--node",  default=None, help="Node ID (NODE_ID)")
    args = parser.parse_args()

    url   = args.url   or os.environ.get("CONTROLLER_URL", "http://127.0.0.1:9002")
    token = args.token or os.environ.get("WORKER_TOKEN",   "")
    node  = args.node  or os.environ.get("NODE_ID",        socket.gethostname())

    if not token:
        print("Error: WORKER_TOKEN is required (--token or env var).", file=sys.stderr)
        sys.exit(1)

    script = _script_path()
    if not script.exists():
        print(f"Error: mcp_jobs.py not found at {script}", file=sys.stderr)
        sys.exit(1)

    # Claude Code MCP config — server named "jobs", tool becomes mcp__jobs__watch_job
    mcp_config = {
        "mcpServers": {
            "jobs": {
                "command": sys.executable,   # exact Python that ran this script
                "args": [str(script)],
                "env": {
                    "CONTROLLER_URL": url,
                    "WORKER_TOKEN":   token,
                    "NODE_ID":        node,
                },
            }
        }
    }

    cfg_dir = _config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "mcp.json"
    cfg_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")

    print(f"Written: {cfg_path}")
    print(f"  server: jobs  (tool: mcp__jobs__watch_job)")
    print(f"  node:   {node}")
    print(f"  url:    {url}")
    print(f"  python: {sys.executable}")
    print()
    print("Claude Code will load this automatically on next invocation.")
    print("OpenCode: add the equivalent entry to your opencode MCP config.")


if __name__ == "__main__":
    main()
