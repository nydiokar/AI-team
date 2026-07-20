#!/usr/bin/env python3
"""Dry-run/apply cleanup for empty closed gateway sessions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.session_store import SessionStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prune closed sessions with no tasks, turns, events, jobs, approvals, or Case links.",
    )
    parser.add_argument("--limit", type=int, default=100, help="Maximum candidate sessions to inspect.")
    parser.add_argument("--apply", action="store_true", help="Actually delete eligible rows and JSON mirrors.")
    args = parser.parse_args()

    result = SessionStore().prune_empty_closed_sessions(
        limit=max(1, args.limit),
        dry_run=not args.apply,
    )
    print(json.dumps(result, indent=2))
    if not args.apply and result["matched"]:
        print("Dry-run only. Re-run with --apply to delete these sessions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
