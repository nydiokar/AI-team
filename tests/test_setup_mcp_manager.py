"""[M3 A34] setup_mcp.py --with-manager opt-in registration.

HERMETIC: Path.home() is redirected to a tmp dir; no real user config is touched.
Proves the manager server is registered ONLY on demand and merges without clobbering
an existing 'jobs' entry.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "setup_mcp", Path(__file__).resolve().parent.parent / "scripts" / "setup_mcp.py")
setup_mcp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(setup_mcp)


def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_mcp.Path, "home", staticmethod(lambda: tmp_path))


def test_register_manager_adds_entry(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    script = Path("/x/scripts/mcp_manager.py")
    setup_mcp._register_claude_manager(script)

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "manager" in cfg["mcpServers"]
    assert cfg["mcpServers"]["manager"]["args"] == [str(script)]


def test_register_manager_preserves_jobs(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"jobs": {"command": "python", "args": ["j.py"]}},
                    "otherKey": 1}))

    setup_mcp._register_claude_manager(Path("/x/mcp_manager.py"))

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert cfg["mcpServers"]["jobs"]["args"] == ["j.py"]   # untouched
    assert "manager" in cfg["mcpServers"]                   # added
    assert cfg["otherKey"] == 1                             # unrelated keys survive


def test_default_main_does_not_register_manager(tmp_path, monkeypatch):
    """Without --with-manager, main() must NOT add a manager server (byte-identical
    to pre-A34 behavior)."""
    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(setup_mcp.sys, "argv", ["setup_mcp.py"])
    setup_mcp.main()

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "jobs" in cfg["mcpServers"]
    assert "manager" not in cfg["mcpServers"]


def test_main_with_manager_flag_registers(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(setup_mcp.sys, "argv", ["setup_mcp.py", "--with-manager"])
    setup_mcp.main()

    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "jobs" in cfg["mcpServers"]
    assert "manager" in cfg["mcpServers"]
