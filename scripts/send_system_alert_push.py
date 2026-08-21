#!/usr/bin/env python3
"""Send a Web Push notification for a gateway liveness alert.

Invoked by ~/scripts/aiteam-healthcheck.sh (a process OUTSIDE the gateway, on
purpose — it must be able to notify even when the gateway itself is
unresponsive). Reuses the real PushService/pywebpush path so alert pushes get
the same encryption, VAPID signing, and gone-subscription handling as every
other push the app sends — never a hand-rolled HTTP call. Best-effort: must
never raise into the caller, so a caller can always safely background it.
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--url", default="/")
    args = parser.parse_args()

    try:
        from pathlib import Path

        from config import config
        from src.control.db import MeshDB
        from src.services.push_service import PushService, build_task_payload

        project_root = Path(__file__).resolve().parent.parent
        db_path = Path(config.mesh.db_path)
        if not db_path.is_absolute():
            db_path = project_root / db_path

        db = MeshDB(str(db_path))
        try:
            svc = PushService(config, db)
            payload = build_task_payload(
                title=args.title, body=args.body, task_id=None, session_id=None, url=args.url,
            )
            asyncio.run(svc.fanout(payload))
        finally:
            db.close()
        return 0
    except Exception as e:  # best-effort — never fail the caller
        print(f"send_system_alert_push failed: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
