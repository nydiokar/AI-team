#!/usr/bin/env python3
"""Enroll / rotate a per-node mesh credential (A71).

Run on the gateway host:
    .venv/bin/python scripts/enroll_node.py <node_id>            # mint + print once
    .venv/bin/python scripts/enroll_node.py --revoke <node_id>   # revoke a credential

Only the SHA-256 is stored in state/mesh.db (node_credentials table); the
plaintext is printed exactly once and must be written into that node's .env as
``NODE_CRED``. Revoking forces the node to be redeployed with a fresh credential.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    env_file = os.environ.get("AI_TEAM_ENV_FILE") or str(ROOT / ".env")
    os.environ["AI_TEAM_ENV_FILE"] = env_file
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    except Exception:
        pass


def _db() -> "object":
    from config import config as cfg
    from src.control.db import MeshDB

    db_path = cfg.mesh.db_path
    if not os.path.isabs(db_path):
        db_path = str(ROOT / db_path)
    return MeshDB(db_path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("node_id", nargs="?", help="node id to mint a credential for")
    ap.add_argument("--revoke", metavar="NODE_ID", help="revoke this node's credential")
    args = ap.parse_args()

    _load_env()
    db = _db()

    if args.revoke:
        ok = db.revoke_node_credential(args.revoke)
        print(f"revoked credential for node {args.revoke!r}: {'yes' if ok else 'not found'}")
        return 0

    if not args.node_id:
        ap.error("node_id is required (or use --revoke <node_id>)")

    token = secrets.token_hex(32)
    cred_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db.enroll_node_credential(args.node_id, cred_hash)
    print(f"Minted credential for node {args.node_id!r} (SHA-256 stored in mesh.db).")
    print("Put this EXACT value in the node's .env as NODE_CRED:")
    print("")
    print(token)
    print("")
    print("It is not stored in plaintext anywhere — save it now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
