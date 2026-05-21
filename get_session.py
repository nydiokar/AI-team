#!/usr/bin/env python3
"""
Lookup the backend (original) session ID for a given gateway session ID.

Usage:
    python get_session.py <session_id>
    python get_session.py s_4585499f6033
    python get_session.py 4585499f6033
"""

import json
import sys
from pathlib import Path

SESSIONS_DIR = Path(__file__).parent / "state" / "sessions"


def find_session(raw_id: str) -> dict | None:
    session_id = raw_id.removeprefix("s_").strip()

    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    # Fallback: partial match (prefix)
    matches = list(SESSIONS_DIR.glob(f"{session_id}*.json"))
    if len(matches) == 1:
        return json.loads(matches[0].read_text(encoding="utf-8"))
    if len(matches) > 1:
        print(f"Ambiguous prefix '{session_id}', matches:")
        for m in matches:
            print(f"  {m.stem}")
        sys.exit(1)

    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python get_session.py <session_id>")
        print("       python get_session.py s_4585499f6033")
        sys.exit(1)

    raw_id = sys.argv[1]
    session = find_session(raw_id)

    if session is None:
        print(f"Session not found: {raw_id}")
        sys.exit(1)

    session_id = session.get("session_id", "")
    backend_session_id = session.get("backend_session_id", "")
    backend = session.get("backend", "")
    status = session.get("status", "")
    repo_path = session.get("repo_path", "")

    if not backend_session_id:
        print(f"Session s_{session_id} exists but has no backend_session_id yet (no task run yet).")
        sys.exit(1)

    print(f"Gateway session : s_{session_id}")
    print(f"Backend         : {backend}")
    print(f"Status          : {status}")
    print(f"Repo path       : {repo_path}")
    print()
    print(f"Backend session ID: {backend_session_id}")
    print()

    if backend == "claude":
        print(f"Resume with:")
        print(f"  claude --resume {backend_session_id}")
    elif backend == "codex":
        print(f"Resume with:")
        print(f"  codex --thread {backend_session_id}")


if __name__ == "__main__":
    main()
