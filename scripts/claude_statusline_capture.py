#!/usr/bin/env python3
"""Render the operator's Claude Code terminal status line.

Configure this script as the Claude Code statusLine command. Claude Code pipes
status data to stdin; the script prints a small status line and stores the
sanitized rate-limit fields alongside it for local inspection. It does not call
Claude or spend model tokens.

This is NOT a quota source. The quota coordinator reads only the canonical
`get_usage` SDK control request (`src/services/claude_usage_control.py`); it
never reads the file this script writes.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


CAPTURE_SCHEMA_VERSION = "claude-statusline-capture-v1"
DEFAULT_REFRESH_INTERVAL_SEC = 60


def _bucket(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    rate_limits = data.get("rate_limits") if isinstance(data.get("rate_limits"), dict) else {}
    bucket = rate_limits.get(key) if isinstance(rate_limits.get(key), dict) else {}
    return {
        "used_percentage": bucket.get("used_percentage"),
        "resets_at": bucket.get("resets_at"),
    }


def _percent_text(value: Any) -> str:
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return "?"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _target_path(repo_root: Path) -> Path:
    configured = os.getenv("CLAUDE_STATUS_LINE_JSON_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (repo_root / "state" / "claude_statusline_latest.json").resolve()


def _statusline_command(repo_root: Path) -> str:
    target = _target_path(repo_root)
    script = (repo_root / "scripts" / "claude_statusline_capture.py").resolve()
    return f"CLAUDE_STATUS_LINE_JSON_PATH={target} {script}"


def _settings_path() -> Path:
    configured = os.getenv("CLAUDE_SETTINGS_JSON_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".claude" / "settings.json").resolve()


def _load_json_object(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _desired_statusline(repo_root: Path) -> Dict[str, Any]:
    return {
        "type": "command",
        "command": _statusline_command(repo_root),
        "refreshInterval": DEFAULT_REFRESH_INTERVAL_SEC,
    }


def _statusline_synced(settings: Dict[str, Any], repo_root: Path) -> bool:
    return settings.get("statusLine") == _desired_statusline(repo_root)


def _sync_statusline_settings(*, apply: bool) -> int:
    repo_root = _repo_root()
    settings_path = _settings_path()
    settings = _load_json_object(settings_path)
    desired = _desired_statusline(repo_root)
    if _statusline_synced(settings, repo_root):
        print(f"claude statusLine synced: {settings_path}")
        return 0
    print(f"claude statusLine drift: {settings_path}")
    print(json.dumps({"desired": desired, "current": settings.get("statusLine")}, sort_keys=True))
    if not apply:
        return 1
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings["statusLine"] = desired
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"claude statusLine updated: {settings_path}")
    return 0


def _command_arg(args: list[str], name: str) -> Optional[str]:
    try:
        idx = args.index(name)
    except ValueError:
        return None
    if idx + 1 >= len(args):
        raise ValueError(f"{name} requires a value")
    return args[idx + 1]


def main() -> int:
    if "--check-statusline-settings" in sys.argv:
        return _sync_statusline_settings(apply=False)
    if "--sync-statusline-settings" in sys.argv:
        return _sync_statusline_settings(apply=True)
    explicit_target = _command_arg(sys.argv, "--target")
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("quota unavailable")
        return 0

    sanitized = {
        "captured_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "capture_schema_version": CAPTURE_SCHEMA_VERSION,
        "source": "claude_code_status_line",
        "source_schema_version": "claude-code-status-line-input-v1",
        "rate_limits": {
            "five_hour": _bucket(data, "five_hour"),
            "seven_day": _bucket(data, "seven_day"),
        },
        "source_version": data.get("version"),
    }
    target = Path(explicit_target or os.getenv("CLAUDE_STATUS_LINE_JSON_PATH", "state/claude_statusline_latest.json"))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(sanitized, sort_keys=True), encoding="utf-8")
    except OSError:
        pass

    five = sanitized["rate_limits"]["five_hour"].get("used_percentage")
    seven = sanitized["rate_limits"]["seven_day"].get("used_percentage")
    print(f"5h {_percent_text(five)}% | 7d {_percent_text(seven)}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
