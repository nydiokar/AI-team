#!/usr/bin/env python3
r"""
setup_mcp.py -- Register the ai-team jobs MCP server in all supported backends.

Run once per worker machine from anywhere in the AI-team repo.
No tokens, no URLs, no secrets needed -- mcp_jobs.py reads the project .env itself.

Supported backends:
  - Claude Code  (~/.claude.json)
  - OpenCode     (~/.config/opencode/config.json)
  - Codex CLI    (~/.codex/config.toml)  [only if config already exists]

Usage:
    python scripts/setup_mcp.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _script_path() -> Path:
    return (Path(__file__).parent / "mcp_jobs.py").resolve()


def _register_claude(script: Path) -> None:
    """Register in Claude Code's user config (~/.claude.json)."""
    cfg_path = Path.home() / ".claude.json"
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
    }
    cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"  [claude-code]  {cfg_path}")


def _register_opencode(script: Path) -> None:
    """Register in OpenCode's user config (~/.config/opencode/config.json).

    Creates the config file (and parent dir) if they don't exist yet.
    MCP is configured under the top-level "mcp" key; OpenCode loads it
    automatically — no per-run CLI flag is needed.
    """
    cfg_path = Path.home() / ".config" / "opencode" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Warning: could not parse {cfg_path}: {e}", file=sys.stderr)

    existing.setdefault("mcp", {})
    existing["mcp"]["jobs"] = {
        "command": sys.executable,
        "args": [str(script)],
    }
    cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"  [opencode]     {cfg_path}")


def _register_codex(script: Path) -> None:
    """Register in Codex CLI's user config (~/.codex/config.toml).

    Skipped silently if the config file doesn't exist (Codex not installed).
    The MCP server is appended as a [[mcp_servers]] TOML table if not already
    present.
    """
    cfg_path = Path.home() / ".codex" / "config.toml"
    if not cfg_path.exists():
        return

    content = cfg_path.read_text(encoding="utf-8")
    # Idempotent: skip if already registered.
    if 'name = "jobs"' in content:
        print(f"  [codex]        {cfg_path}  (already registered)")
        return

    entry = (
        "\n[[mcp_servers]]\n"
        'name = "jobs"\n'
        f'command = "{sys.executable}"\n'
        f'args = ["{script}"]\n'
    )
    with cfg_path.open("a", encoding="utf-8") as fh:
        fh.write(entry)
    print(f"  [codex]        {cfg_path}")


def main() -> None:
    script = _script_path()
    if not script.exists():
        print(f"Error: mcp_jobs.py not found at {script}", file=sys.stderr)
        sys.exit(1)

    print(f"Registering jobs MCP server (script: {script})\n")
    print("Updated configs:")
    _register_claude(script)
    _register_opencode(script)
    _register_codex(script)

    print()
    print("Tool available as:  mcp__jobs__watch_job")
    print()
    print("Backends load MCP automatically from their configs.")
    print("If a gateway worker was already running, restart it to pick up changes.")


if __name__ == "__main__":
    main()
