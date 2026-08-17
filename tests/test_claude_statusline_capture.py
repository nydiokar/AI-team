from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_capture_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "claude_statusline_capture.py"
    spec = importlib.util.spec_from_file_location("claude_statusline_capture", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_statusline_sync_writes_expected_command_and_preserves_settings(tmp_path, monkeypatch):
    capture = _load_capture_module()
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    target_path = tmp_path / "claude_statusline_latest.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_JSON_PATH", str(settings_path))
    monkeypatch.setenv("CLAUDE_STATUS_LINE_JSON_PATH", str(target_path))

    rc = capture._sync_statusline_settings(apply=True)

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert data["theme"] == "dark"
    assert data["statusLine"] == {
        "type": "command",
        "command": f"CLAUDE_STATUS_LINE_JSON_PATH={target_path.resolve()} {(Path(__file__).resolve().parents[1] / 'scripts' / 'claude_statusline_capture.py').resolve()}",
        "refreshInterval": 60,
    }


def test_statusline_check_detects_drift_without_writing(tmp_path, monkeypatch):
    capture = _load_capture_module()
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"statusLine": {"type": "command", "command": "old"}}), encoding="utf-8")
    original = settings_path.read_text(encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SETTINGS_JSON_PATH", str(settings_path))
    monkeypatch.setenv("CLAUDE_STATUS_LINE_JSON_PATH", str(tmp_path / "latest.json"))

    rc = capture._sync_statusline_settings(apply=False)

    assert rc == 1
    assert settings_path.read_text(encoding="utf-8") == original


def test_statusline_check_passes_after_sync(tmp_path, monkeypatch):
    capture = _load_capture_module()
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("CLAUDE_SETTINGS_JSON_PATH", str(settings_path))
    monkeypatch.setenv("CLAUDE_STATUS_LINE_JSON_PATH", str(tmp_path / "latest.json"))

    assert capture._sync_statusline_settings(apply=True) == 0
    assert capture._sync_statusline_settings(apply=False) == 0
