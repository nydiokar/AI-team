#!/usr/bin/env python3
"""Capture Claude Code status-line JSON for the quota coordinator.

Configure this script as the Claude Code statusLine command. Claude Code pipes
status data to stdin; the script stores sanitized rate-limit fields and prints a
small status line. It does not call Claude or spend model tokens.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


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


def main() -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("quota unavailable")
        return 0

    sanitized = {
        "rate_limits": {
            "five_hour": _bucket(data, "five_hour"),
            "seven_day": _bucket(data, "seven_day"),
        },
        "version": data.get("version"),
    }
    target = Path(os.getenv("CLAUDE_STATUS_LINE_JSON_PATH", "state/claude_statusline_latest.json"))
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
