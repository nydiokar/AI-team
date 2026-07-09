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


def _manager_script_path() -> Path:
    return (Path(__file__).parent / "mcp_manager.py").resolve()


def _register_claude_manager(script: Path) -> None:
    """[M3 A34] OPT-IN: register the ai-team 'manager' MCP server in Claude Code's
    user config (~/.claude.json). Only runs with `--with-manager`. Gives a session
    the dispatch_worker / wait_for_worker tools — but the gateway ALSO requires
    MANAGER_TOOLS_ENABLED=1 in its env before those tools are actually granted
    (see claude_driver._manager_tools_enabled), so registering here alone is inert."""
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
    existing["mcpServers"]["manager"] = {
        "command": sys.executable,
        "args": [str(script)],
    }
    cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"  [claude-code]  manager → {cfg_path}")


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
    """Register in OpenCode's user config (~/.config/opencode/opencode.json).

    OpenCode reads ``opencode.json`` / ``opencode.jsonc`` from this dir — NOT
    a file named ``config.json`` (that one is silently ignored, and any stray
    ``config.json`` can make OpenCode report "Configuration is invalid"). If a
    ``.jsonc`` config already exists we update that in place; otherwise we
    write ``opencode.json``. A leftover ``config.json`` is removed.

    OpenCode's MCP schema differs from Claude Code's: each entry needs
    ``type: "local"`` and a single ``command`` array combining the executable
    and its args (there is no separate ``args`` key).
    """
    cfg_dir = Path.home() / ".config" / "opencode"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # Remove the stale, ignored file written by older versions of this script.
    stale = cfg_dir / "config.json"
    if stale.exists():
        try:
            stale.unlink()
            print(f"  [opencode]     removed stale {stale}")
        except Exception as e:
            print(f"Warning: could not remove {stale}: {e}", file=sys.stderr)

    # Prefer an existing .jsonc config; otherwise use opencode.json.
    jsonc_path = cfg_dir / "opencode.jsonc"
    cfg_path = jsonc_path if jsonc_path.exists() else cfg_dir / "opencode.json"

    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Warning: could not parse {cfg_path}: {e}", file=sys.stderr)

    existing.setdefault("$schema", "https://opencode.ai/config.json")
    existing.setdefault("mcp", {})
    existing["mcp"]["jobs"] = {
        "type": "local",
        "command": [sys.executable, str(script)],
        "enable": True,
    }
    cfg_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"  [opencode]     {cfg_path}")


def _register_codex(script: Path) -> None:
    """Register in Codex CLI's user config (~/.codex/config.toml).

    Skipped silently if the config file doesn't exist (Codex not installed).
    The MCP server is appended as a [mcp_servers.jobs] TOML table if not
    already present.
    """
    cfg_path = Path.home() / ".codex" / "config.toml"
    if not cfg_path.exists():
        return

    content = cfg_path.read_text(encoding="utf-8")
    # Idempotent: skip if already registered.
    if "[mcp_servers.jobs]" in content:
        print(f"  [codex]        {cfg_path}  (already registered)")
        return

    # Use forward slashes so Windows paths don't trip TOML's backslash
    # escape parsing (e.g. "\Users" -> invalid \U unicode escape).
    command = sys.executable.replace("\\", "/")
    script_path = str(script).replace("\\", "/")
    entry = (
        "\n[mcp_servers.jobs]\n"
        f'command = "{command}"\n'
        f'args = ["{script_path}"]\n'
    )
    with cfg_path.open("a", encoding="utf-8") as fh:
        fh.write(entry)
    print(f"  [codex]        {cfg_path}")


def main() -> None:
    with_manager = "--with-manager" in sys.argv[1:]

    script = _script_path()
    if not script.exists():
        print(f"Error: mcp_jobs.py not found at {script}", file=sys.stderr)
        sys.exit(1)

    print(f"Registering jobs MCP server (script: {script})\n")
    print("Updated configs:")
    _register_claude(script)
    _register_opencode(script)
    _register_codex(script)

    if with_manager:
        manager_script = _manager_script_path()
        if not manager_script.exists():
            print(f"Error: mcp_manager.py not found at {manager_script}", file=sys.stderr)
            sys.exit(1)
        print(f"\n[--with-manager] Registering manager MCP server (Claude Code only): {manager_script}")
        _register_claude_manager(manager_script)

    print()
    print("Tool available as:  mcp__jobs__watch_job")
    if with_manager:
        print("Manager tools:      mcp__manager__dispatch_worker, mcp__manager__wait_for_worker")
        print("  ⚠️  Also set MANAGER_TOOLS_ENABLED=1 in the gateway env — the tools stay")
        print("      inert until that flag is on (see docs/ENV_FEATURE_FLAGS.md).")
    print()
    print("Backends load MCP automatically from their configs.")
    print("If a gateway worker was already running, restart it to pick up changes.")


if __name__ == "__main__":
    main()
