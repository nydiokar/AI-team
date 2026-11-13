#!/usr/bin/env python3
"""Deterministic SDK wire peer. No agent, credentials, network, or model call."""

import json
import sys


def emit(message: dict) -> None:
    print(json.dumps(message), flush=True)


def main() -> None:
    if "--version" in sys.argv or "-v" in sys.argv:
        print("2.1.300 (offline protocol peer)")
        return
    session_id = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "offline-session"
    for line in sys.stdin:
        message = json.loads(line)
        if message["type"] == "control_request":
            emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": message["request_id"], "response": {},
            }})
        elif message["type"] == "user":
            emit({"type": "system", "subtype": "init", "session_id": session_id,
                  "claude_code_version": "2.1.300"})
            emit({**message, "session_id": session_id})
            emit({"type": "assistant", "message": {
                "model": "offline", "content": [{"type": "text", "text": "offline reply"}],
            }})
            emit({"type": "result", "subtype": "success", "duration_ms": 1,
                  "duration_api_ms": 0, "is_error": False, "num_turns": 1,
                  "session_id": session_id, "total_cost_usd": 0, "result": "offline reply"})


if __name__ == "__main__":
    main()
