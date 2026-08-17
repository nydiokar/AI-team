#!/usr/bin/env python3
"""Probe Claude Code get_usage through the Python Agent SDK transport.

This script is intentionally outside production quota wiring. It starts
headless SDK sessions, sends only the ``get_usage`` control request, prints a
sanitized raw response, and closes every SDK client it opens.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from src.services.claude_usage_control import normalize_claude_quota, read_claude_usage_raw


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lower: str = str(key).lower()
            if any(
                marker in lower
                for marker in ("token", "secret", "authorization", "credential", "email")
            ):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _json_default(value: object) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())  # type: ignore[attr-defined]
    return str(value)


async def _read_once(label: str, *, cwd: Path, cli_path: str | None) -> dict[str, object]:
    options: ClaudeAgentOptions = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=[],
        permission_mode="dontAsk",
        env={
            **os.environ,
            "CLAUDE_AGENT_SDK_CLIENT_APP": "ai-team-python-get-usage-probe/0",
        },
        **({"cli_path": cli_path} if cli_path else {}),
    )
    client: ClaudeSDKClient = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        raw: dict[str, object] = await read_claude_usage_raw(client)
        snapshot = normalize_claude_quota(raw, sdk_version=version("claude-agent-sdk"))
        print(f"{label}_RAW_START")
        print(json.dumps(_sanitize(raw), indent=2, default=_json_default))
        print(f"{label}_RAW_END")
        print(f"{label}_SNAPSHOT_START")
        print(json.dumps(snapshot.model_dump(), indent=2, default=_json_default))
        print(f"{label}_SNAPSHOT_END")
        return raw
    finally:
        await client.disconnect()


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--twice", action="store_true", help="read from two independent sessions")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--cli-path", default=None)
    args = parser.parse_args()

    print(
        json.dumps(
            {
                "python_sdk_version": version("claude-agent-sdk"),
                "cwd": str(Path(args.cwd).resolve()),
                "cli_path": args.cli_path,
            },
            indent=2,
        )
    )
    await _read_once("SESSION_A", cwd=Path(args.cwd).resolve(), cli_path=args.cli_path)
    if args.twice:
        await _read_once("SESSION_B", cwd=Path(args.cwd).resolve(), cli_path=args.cli_path)


if __name__ == "__main__":
    asyncio.run(_main())
