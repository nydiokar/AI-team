#!/usr/bin/env python3
"""Probe canonical Claude quota through the quota coordinator path."""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from src.services.quota_window_coordinator import (
    ClaudeGetUsageQuotaAdapter,
    QuotaWindowCoordinator,
    QuotaWindowStore,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=None, help="Working directory for the SDK client")
    parser.add_argument("--cli-path", default=None, help="Optional Claude Code binary path")
    parser.add_argument("--timeout", type=float, default=60.0, help="get_usage timeout seconds")
    return parser


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("token", "secret", "authorization", "credential", "email")):
                out[str(key)] = "<redacted>"
            else:
                out[str(key)] = _sanitize(item)
        return out
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


async def _run(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="ai-team-quota-") as tmp:
        store = QuotaWindowStore(Path(tmp) / "quota.db")
        adapter = ClaudeGetUsageQuotaAdapter(
            cwd=args.cwd,
            cli_path=args.cli_path,
            timeout_sec=args.timeout,
        )
        coordinator = QuotaWindowCoordinator(store=store, adapters=[adapter], enabled=True)
        await coordinator.observe_once()
        print(json.dumps(_sanitize(coordinator.read_status()), indent=2, sort_keys=True))


def main() -> int:
    args = _parser().parse_args()
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
