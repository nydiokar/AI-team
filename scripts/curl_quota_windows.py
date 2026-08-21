#!/usr/bin/env python3
"""GET /api/quota-windows on the live gateway, authenticated.

Consumes the credential directly from the project .env file (the same source
pm2 feeds the gateway via AI_TEAM_ENV_FILE) instead of relying on any shell
environment being populated. Stdlib only; never prints the token.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:9003"
REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env_token(env_path: Path) -> str:
    """Parse KEY=VALUE lines and return DASHBOARD_TOKEN or WORKER_TOKEN."""
    order = ("DASHBOARD_TOKEN", "WORKER_TOKEN")
    found: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in order:
            value = value.strip().strip("'\"")
            found.setdefault(key, value)
    for key in order:
        if found.get(key):
            return found[key]
    raise SystemExit(f"no DASHBOARD_TOKEN/WORKER_TOKEN with a non-empty value in {env_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE, help="gateway base URL")
    parser.add_argument("--env-file", type=Path, default=None,
                        help="explicit .env path (default: repo root .env)")
    args = parser.parse_args()

    env_file = args.env_file or REPO_ROOT / ".env"
    if not env_file.exists():
        raise SystemExit(f"env file not found: {env_file}")
    token = load_env_token(env_file)

    url = f"{args.base.rstrip('/')}/api/quota-windows"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            print(f"HTTP {resp.status}", file=sys.stderr)
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
        return 1
    try:
        print(json.dumps(json.loads(body), indent=2))
    except json.JSONDecodeError:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
