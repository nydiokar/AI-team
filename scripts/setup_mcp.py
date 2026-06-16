#!/usr/bin/env python3
r"""
setup_mcp.py -- Register the ai-team jobs MCP server in Claude Code's user config.

Run once per worker machine (Windows or Linux) from the AI-team project directory.
Reads WORKER_NODE_ID, WORKER_TOKEN, and CONTROLLER_URL from the project .env file
(the same file the worker process uses), then merges the 'jobs' MCP server entry
into ~/.claude.json (%USERPROFILE%\.claude.json on Windows).

SECURITY: WORKER_TOKEN is NOT written to ~/.claude.json.
The MCP server subprocess inherits it from Claude Code's process environment.
Make sure WORKER_TOKEN is a persistent system/user environment variable:
  Windows: System Properties -> Advanced -> Environment Variables
  Linux:   Add to ~/.profile or ~/.bashrc: export WORKER_TOKEN=...

Usage (run from the AI-team project root):
    python scripts/setup_mcp.py
    python scripts/setup_mcp.py --url http://100.x.y.z:9002 --node Horse
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def _load_dotenv(project_root: Path) -> None:
    """Load the project .env into os.environ without overriding existing values."""
    env_path = Path(os.environ.get("AI_TEAM_ENV_FILE", "")) or (project_root / ".env")
    if not env_path.exists():
        env_path = project_root / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    # Fallback: manual parse (no python-dotenv installed)
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


def _claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def _script_path() -> Path:
    return (Path(__file__).parent / "mcp_jobs.py").resolve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register ai-team jobs MCP server in ~/.claude.json"
    )
    parser.add_argument("--url",  default=None, help="Task server URL (CONTROLLER_URL)")
    parser.add_argument("--node", default=None, help="Node ID as registered in the mesh (WORKER_NODE_ID)")
    args = parser.parse_args()

    # Load .env from project root so we get the same vars the worker uses.
    project_root = Path(__file__).resolve().parent.parent
    _load_dotenv(project_root)

    url  = args.url  or os.environ.get("CONTROLLER_URL", "")
    node = args.node or os.environ.get("WORKER_NODE_ID", "")

    if not url:
        print(
            "Error: task server URL not found.\n"
            "Pass --url http://<pi-tailscale-ip>:9002  or set CONTROLLER_URL in .env",
            file=sys.stderr,
        )
        sys.exit(1)

    if not node:
        detected = socket.gethostname()
        print(
            f"Warning: WORKER_NODE_ID not found in .env or env. Detected hostname: {detected!r}\n"
            f"If your worker is registered under a different name, pass --node <name>",
            file=sys.stderr,
        )
        node = detected

    if "127.0.0.1" in url or "localhost" in url:
        print(
            f"Warning: URL {url!r} points to localhost.\n"
            "The MCP server runs on this worker machine and needs the Pi's Tailscale IP "
            "to reach the task server.",
            file=sys.stderr,
        )

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
        "env": {
            # Non-secrets only. WORKER_TOKEN must be in the OS environment.
            "CONTROLLER_URL": url,
            "NODE_ID":        node,
        },
    }

    cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    print(f"Updated: {cfg_path}")
    print(f"  server: jobs  ->  tool: mcp__jobs__watch_job")
    print(f"  node:   {node}")
    print(f"  url:    {url}")
    print(f"  python: {sys.executable}")
    print()

    if os.environ.get("WORKER_TOKEN"):
        print("WORKER_TOKEN: found in environment - will be inherited by Claude Code.")
    else:
        print("ACTION REQUIRED: WORKER_TOKEN not set as an environment variable.")
        print("The MCP server needs it at runtime but it should NOT be stored in ~/.claude.json.")
        if sys.platform == "win32":
            print("\nWindows: System Properties -> Advanced -> Environment Variables")
            print("         Add WORKER_TOKEN under 'User variables'")
        else:
            print("\nLinux: Add to ~/.profile:  export WORKER_TOKEN=<your-token>")
        print("Then restart Claude Code so it picks up the new variable.")


if __name__ == "__main__":
    main()
