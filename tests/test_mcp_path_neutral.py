"""MCP config must be platform-neutral (Docker worker/host integration §7).

The host-authoritative backend config (~/.claude.json, ~/.codex/config.toml,
~/.config/opencode/opencode.json) is projected into the worker container by bind
mount, so the command it names MUST resolve identically on the host and in the
container. That rules out "<abs python> <abs script path>" and requires a stable
PATH-resolvable launcher command.

These tests assert the registration writes the neutral launcher name and that the
launcher can locate the stdio server scripts without a hard-coded absolute path.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_setup_mcp():
    spec = importlib.util.spec_from_file_location(
        "setup_mcp_under_test", REPO_ROOT / "scripts" / "setup_mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _assert_neutral(command) -> None:
    """A neutral command is a bare launcher name — not an absolute path and not
    this interpreter's path."""
    cmd = command[0] if isinstance(command, list) else command
    assert cmd in ("ai-team-mcp-jobs", "ai-team-mcp-manager"), cmd
    assert not os.path.isabs(cmd), cmd
    assert "/" not in cmd and "\\" not in cmd, cmd
    assert cmd != sys.executable, cmd


def test_claude_registration_is_path_neutral(tmp_path, monkeypatch):
    setup_mcp = _load_setup_mcp()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    setup_mcp._register_claude(REPO_ROOT / "scripts" / "mcp_jobs.py")
    setup_mcp._register_claude_manager(REPO_ROOT / "scripts" / "mcp_manager.py")

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    jobs = cfg["mcpServers"]["jobs"]
    manager = cfg["mcpServers"]["manager"]

    assert jobs == {"command": "ai-team-mcp-jobs", "args": []}
    assert manager == {"command": "ai-team-mcp-manager", "args": []}
    _assert_neutral(jobs["command"])
    _assert_neutral(manager["command"])


def test_opencode_registration_is_path_neutral(tmp_path, monkeypatch):
    setup_mcp = _load_setup_mcp()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    setup_mcp._register_opencode(REPO_ROOT / "scripts" / "mcp_jobs.py")

    cfg = json.loads((tmp_path / ".config" / "opencode" / "opencode.json").read_text())
    entry = cfg["mcp"]["jobs"]
    assert entry["command"] == ["ai-team-mcp-jobs"]
    _assert_neutral(entry["command"])


def test_codex_registration_is_path_neutral(tmp_path, monkeypatch):
    setup_mcp = _load_setup_mcp()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text("# existing codex config\n")

    setup_mcp._register_codex(REPO_ROOT / "scripts" / "mcp_jobs.py")

    content = (codex_dir / "config.toml").read_text()
    assert '[mcp_servers.jobs]' in content
    assert 'command = "ai-team-mcp-jobs"' in content
    # No absolute path / interpreter path leaked into the TOML.
    assert sys.executable not in content
    assert str(REPO_ROOT) not in content


def _load_launchers():
    spec = importlib.util.spec_from_file_location(
        "mcp_launchers_under_test", REPO_ROOT / "src" / "mcp_launchers.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_launcher_scripts_dir_env_override(tmp_path, monkeypatch):
    launchers = _load_launchers()
    monkeypatch.setenv("AI_TEAM_SCRIPTS_DIR", str(tmp_path))
    assert launchers._scripts_dir() == tmp_path


def test_launcher_scripts_dir_finds_repo_scripts(monkeypatch):
    launchers = _load_launchers()
    monkeypatch.delenv("AI_TEAM_SCRIPTS_DIR", raising=False)
    resolved = launchers._scripts_dir()
    # From the real checkout the walk-up must land on the repo's scripts/ holding
    # the actual stdio servers — never a hard-coded absolute literal.
    assert (resolved / "mcp_jobs.py").is_file()
    assert (resolved / "mcp_manager.py").is_file()
