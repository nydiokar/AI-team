"""
Retag session machine_id before the VPS migration (AGENT_MESH_SPEC §3.2).

Sessions set ``machine_id`` to ``socket.gethostname()`` at creation time. Once
the gateway moves to the VPS, those existing sessions must keep pointing at the
machine that actually owns their backend session state (the old main PC, now a
worker). This script rewrites ``machine_id`` on the affected session JSON files
to a stable ``WORKER_NODE_ID`` so session affinity survives the migration.

Dry-run by default — prints what would change and writes nothing. Pass
``--apply`` to write. Idempotent: re-running after a successful apply is a no-op.

Usage:
    python scripts/fix_session_machine_ids.py                 # dry run
    python scripts/fix_session_machine_ids.py --apply         # write
    python scripts/fix_session_machine_ids.py --node-id LP-1  # explicit target
    python scripts/fix_session_machine_ids.py --from-host OLD-PC --node-id LP-1

By default the "old server" hostname is the current machine's
``socket.gethostname()`` (run this on the old PC before migrating), and the
target node id comes from ``WORKER_NODE_ID`` in the environment / ``.env``.
Override either with ``--from-host`` / ``--node-id``.
"""

import argparse
import json
import logging
import os
import socket
import sys
from pathlib import Path

# Ensure project root is on the path so config and src imports work.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SESSIONS_DIR = PROJECT_ROOT / "state" / "sessions"


def _resolve_target_node_id(explicit: str | None) -> str:
    """Determine the WORKER_NODE_ID to write. CLI flag wins, then env/.env."""
    if explicit:
        return explicit.strip()
    # Load .env the same way the worker does, so this matches production config.
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env", override=True)
    except Exception:
        pass
    node_id = (os.environ.get("WORKER_NODE_ID") or "").strip()
    return node_id


def main(apply: bool, from_host: str, node_id: str) -> int:
    if not node_id:
        logger.error(
            "No target node id. Set WORKER_NODE_ID in .env / environment, "
            "or pass --node-id <id>."
        )
        return 2

    if node_id == from_host:
        logger.error(
            "Target node id (%r) equals the old hostname (%r) — nothing to "
            "migrate and writing would be a no-op. Pass a distinct --node-id.",
            node_id,
            from_host,
        )
        return 2

    if not SESSIONS_DIR.is_dir():
        logger.error("Sessions directory not found: %s", SESSIONS_DIR)
        return 1

    logger.info("Sessions dir : %s", SESSIONS_DIR)
    logger.info("From host    : %s", from_host)
    logger.info("To node_id   : %s", node_id)
    logger.info("Mode         : %s", "APPLY (writing files)" if apply else "DRY RUN (no writes)")
    logger.info("")

    files = sorted(SESSIONS_DIR.glob("*.json"))
    scanned = 0
    matched = 0
    skipped_already = 0
    errors = 0

    for path in files:
        scanned += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            errors += 1
            logger.warning("Skipping unreadable %s: %s", path.name, e)
            continue

        current = (data.get("machine_id") or "").strip()
        if current == node_id:
            skipped_already += 1
            continue
        if current != from_host:
            # Belongs to a different machine; leave it untouched.
            continue

        matched += 1
        sid = data.get("session_id", path.stem)
        logger.info("%s  %s : %r -> %r", "WOULD FIX" if not apply else "FIX", sid, current, node_id)

        if apply:
            data["machine_id"] = node_id
            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(path)  # atomic on same filesystem
            except Exception as e:
                errors += 1
                logger.error("Failed to write %s: %s", path.name, e)
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

    logger.info("")
    logger.info(
        "Scanned %d | %s %d | already %r %d | errors %d",
        scanned,
        "fixed" if apply else "would fix",
        matched,
        node_id,
        skipped_already,
        errors,
    )
    if not apply and matched:
        logger.info("Re-run with --apply to write these changes.")
    return 1 if errors else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retag session machine_id for VPS migration.")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run).")
    parser.add_argument(
        "--from-host",
        default=socket.gethostname(),
        help="Old machine_id to match (default: this machine's hostname).",
    )
    parser.add_argument(
        "--node-id",
        default=None,
        help="Target WORKER_NODE_ID (default: WORKER_NODE_ID from env/.env).",
    )
    args = parser.parse_args()
    sys.exit(
        main(
            apply=args.apply,
            from_host=args.from_host.strip(),
            node_id=_resolve_target_node_id(args.node_id),
        )
    )
