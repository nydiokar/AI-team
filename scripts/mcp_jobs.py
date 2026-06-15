#!/usr/bin/env python3
"""
MCP server — ai-team watched jobs.

Exposes a single tool:  watch_job
Claude Code (and OpenCode) load this as a subprocess via stdio transport.
The agent calls it like any built-in tool — no bash scripts, no curl, no guessing.

Config comes from env vars set by the host process (worker or setup_mcp.py):
  CONTROLLER_URL   task server base URL  (default: http://127.0.0.1:9002)
  WORKER_TOKEN     shared mesh auth secret
  NODE_ID          this machine's node id  (default: hostname)
  SESSION_ID       gateway session id for routing the completion notification
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "watch_job",
        "description": (
            "Register a long-running shell command as a watched job. "
            "The worker spawns it detached, captures ALL stdout and stderr to a log file, "
            "and sends a Telegram notification when it finishes — including the label, "
            "exit code, and the last 20 lines of output. "
            "\n\n"
            "Use this whenever you are about to run a command that will take more than "
            "~30 seconds: training runs, builds, ETL jobs, data processing, deployments. "
            "The task turn ends immediately after registration; the job runs independently "
            "and does NOT hold the session busy or consume a task slot. "
            "\n\n"
            "Do NOT use for short commands — only for things the user should be notified about."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run (executed with shell=True on the worker node).",
                },
                "label": {
                    "type": "string",
                    "description": "Human-readable label shown in the Telegram notification, e.g. 'Training run epoch 100' or 'Nightly ETL'.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the command. Defaults to the current directory.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Gateway session ID for routing the notification. Leave blank — it is set automatically from the environment.",
                },
            },
            "required": ["command", "label"],
        },
    }
]

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _post_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    url = os.environ.get("CONTROLLER_URL", "http://127.0.0.1:9002").rstrip("/")
    token = os.environ.get("WORKER_TOKEN", "")
    if not token:
        raise RuntimeError("WORKER_TOKEN not set — cannot reach task server")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url}/jobs",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')}") from e
    except Exception as e:
        raise RuntimeError(f"Could not reach task server: {e}") from e

# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------

def _watch_job(args: Dict[str, Any]) -> str:
    node = os.environ.get("NODE_ID", socket.gethostname())
    session = args.get("session_id") or os.environ.get("SESSION_ID") or None
    cwd = args.get("cwd") or os.getcwd()
    command = args["command"]
    label = args["label"]

    result = _post_job({
        "node_id": node,
        "label": label,
        "command": command,
        "cwd": cwd,
        "session_id": session,
        "notify": True,
    })

    job_id = result.get("job_id", "?")
    return (
        f"Registered: {job_id}\n"
        f"Label:   {label}\n"
        f"Command: {command}\n"
        f"CWD:     {cwd}\n"
        f"Node:    {node}\n"
        f"\n"
        f"The worker will spawn it now and capture its output.\n"
        f"You (and the user) will be notified via Telegram when it finishes."
    )

# ---------------------------------------------------------------------------
# MCP protocol — JSON-RPC 2.0 over stdio
# ---------------------------------------------------------------------------

def _send(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj), flush=True)

def _reply(id_: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "result": result})

def _reply_error(id_: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})

def _dispatch(req: Dict[str, Any]) -> None:
    method: str = req.get("method", "")
    id_: Optional[Any] = req.get("id")

    if method == "initialize":
        _reply(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "jobs", "version": "1.0.0"},
        })

    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass  # fire-and-forget, no response

    elif method == "tools/list":
        _reply(id_, {"tools": _TOOLS})

    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name", "")
        arguments = params.get("arguments", {})

        if name == "watch_job":
            try:
                text = _watch_job(arguments)
                _reply(id_, {"content": [{"type": "text", "text": text}]})
            except Exception as exc:
                print(f"[mcp_jobs] watch_job failed: {exc}", file=sys.stderr, flush=True)
                _reply(id_, {
                    "content": [{"type": "text", "text": f"Error registering job: {exc}"}],
                    "isError": True,
                })
        else:
            _reply_error(id_, -32601, f"Unknown tool: {name!r}")

    else:
        if id_ is not None:  # only reply if it's a request, not a notification
            _reply_error(id_, -32601, f"Unknown method: {method!r}")


def main() -> None:
    print("[mcp_jobs] ready", file=sys.stderr, flush=True)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
            _dispatch(req)
        except json.JSONDecodeError as exc:
            _reply_error(None, -32700, f"Parse error: {exc}")
        except Exception as exc:
            print(f"[mcp_jobs] internal error: {exc}", file=sys.stderr, flush=True)
            _reply_error(None, -32603, f"Internal error: {exc}")


if __name__ == "__main__":
    main()
