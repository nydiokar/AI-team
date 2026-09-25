"""PATH-resolvable console entry points for the ai-team MCP stdio servers.

Why this exists (Docker worker/host integration, §7 "no host-specific absolute-path
proliferation in shared configuration"):

Backend MCP configs (``~/.claude.json`` ``mcpServers``, ``~/.codex/config.toml``
``[mcp_servers.*]``, ``~/.config/opencode/opencode.json`` ``mcp``) used to be written
as an OS/host-specific pair::

    "command": "<abs path to this machine's python>",
    "args":    ["<abs path to this checkout>/scripts/mcp_manager.py"]

That yields THREE incompatible configs for the same logical tool (Linux host,
Windows host, container) and leaks the repo checkout path into user config. The
authoritative Claude/Codex config is host-owned and projected into the worker
container by bind mount, so the paths baked into it MUST resolve identically on the
host and in the container.

The fix: register a *stable command name* (``ai-team-mcp-jobs`` /
``ai-team-mcp-manager``) that every environment provides on its own ``PATH`` via the
appropriate local launcher:

  * Linux host + worker container: the ``[project.scripts]`` console entry points
    below, installed by ``pip install .`` into the venv's ``bin`` (on ``PATH`` in the
    container because the Dockerfile puts ``/opt/venv/bin`` first).
  * Windows host: the same console entry points create ``ai-team-mcp-*.exe`` shims on
    ``PATH`` when the package is installed there.

The stdio server logic still lives in ``scripts/mcp_{jobs,manager}.py`` (unchanged).
These launchers only locate and exec those scripts under the current interpreter, so
there is a single source of truth for the tool behaviour.
"""
from __future__ import annotations

import os
import runpy
from pathlib import Path


def _scripts_dir() -> Path:
    """Locate the ``scripts/`` directory holding the MCP stdio servers.

    Resolution order (first hit wins), deliberately platform-neutral:
      1. ``AI_TEAM_SCRIPTS_DIR`` explicit override.
      2. Walk up from this module to a repo root (marked by ``pyproject.toml``) that
         has a sibling ``scripts/`` — the editable / from-checkout case (host).
      3. ``<cwd>/scripts`` — the container case (WORKDIR ``/app``, scripts at
         ``/app/scripts``; the package is installed into ``/opt/venv`` so (2) misses).
      4. ``/app/scripts`` — final container fallback.
    """
    override = os.environ.get("AI_TEAM_SCRIPTS_DIR")
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "scripts").is_dir():
            return parent / "scripts"

    cwd_scripts = Path.cwd() / "scripts"
    if cwd_scripts.is_dir():
        return cwd_scripts

    return Path("/app/scripts")


def _run(script_name: str) -> None:
    target = _scripts_dir() / script_name
    if not target.is_file():
        raise SystemExit(
            f"[mcp-launcher] cannot find {script_name} under {target.parent} — set "
            f"AI_TEAM_SCRIPTS_DIR to the ai-team scripts/ directory."
        )
    runpy.run_path(str(target), run_name="__main__")


def jobs_main() -> None:
    """Console entry point ``ai-team-mcp-jobs`` → scripts/mcp_jobs.py."""
    _run("mcp_jobs.py")


def manager_main() -> None:
    """Console entry point ``ai-team-mcp-manager`` → scripts/mcp_manager.py."""
    _run("mcp_manager.py")
