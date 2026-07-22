#!/usr/bin/env python3
"""
MCP server — ai-team watched jobs.

Exposes a single tool:  watch_job
Claude Code (and OpenCode) load this as a subprocess via stdio transport.
The agent calls it like any built-in tool — no bash scripts, no curl, no guessing.

No env vars or secrets need to be configured externally. This script loads the
project .env at startup (same file the worker process uses) and reads
WORKER_TOKEN, CONTROLLER_URL, and WORKER_NODE_ID from there.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Bootstrap: load the project .env the same way the worker does
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Load project .env into os.environ before anything else runs."""
    project_root = Path(__file__).resolve().parent.parent
    ai_team_env = os.environ.get("AI_TEAM_ENV_FILE", "")
    env_path = Path(ai_team_env) if ai_team_env else (project_root / ".env")

    if not env_path.exists():
        print(f"[mcp_jobs] WARNING: .env not found at {env_path}", file=sys.stderr, flush=True)
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except ImportError:
        # Fallback: manual parse
        with open(env_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val

_bootstrap()

# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------
_TOOLS = [
    {
        "name": "watch_job",
        "description": (
            "Register a long-running shell command as a watched job. "
            "The worker spawns it detached, captures ALL stdout and stderr to a log file, "
            "records it in System > Jobs, and posts the terminal result into the owning "
            "session when it finishes. "
            "\n\n"
            "Use this whenever you are about to run a command that will take more than "
            "~30 seconds: training runs, builds, ETL jobs, data processing, deployments. "
            "The task turn ends immediately after registration; the job runs independently "
            "and does NOT hold the session busy or consume a task slot. By default, the "
            "agent is notified in-session when the job finishes so it can continue. "
            "\n\n"
            "Commands run on the worker OS shell. On Windows workers, use cmd/PowerShell "
            "syntax, not POSIX shell syntax. Do NOT use for short commands."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run (executed with shell=True on the worker node). Use worker-native syntax.",
                },
                "label": {
                    "type": "string",
                    "description": "Human-readable label shown in System > Jobs and the terminal session result, e.g. 'Training run epoch 100' or 'Nightly ETL'.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the command. Defaults to the current directory.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Gateway session ID for routing the notification. Leave blank; it is set automatically from the environment.",
                },
                "notify_agent": {
                    "type": "boolean",
                    "description": "Whether to submit a follow-up instruction to the same session when the job finishes. Defaults to true.",
                    "default": True,
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

_WINDOWS_SLEEP_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])sleep\s+([0-9]+(?:\.[0-9]+)?)(?![A-Za-z0-9_./-])"
)
_MAX_COMMAND_CHARS = 8000
_MAX_LABEL_CHARS = 160
_MAX_PATH_CHARS = 1000
_MAX_SESSION_ID_CHARS = 128


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _bounded_text(value: Any, name: str, max_chars: int, *, required: bool = True) -> Optional[str]:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{name} cannot be empty")
    if len(text) > max_chars:
        raise ValueError(f"{name} is too long (max {max_chars} characters)")
    return text or None


def _is_windows() -> bool:
    return os.name == "nt"


def _normalize_worker_command(command: str) -> str:
    if not _is_windows():
        return command

    def _replace_sleep(match: re.Match[str]) -> str:
        seconds = match.group(1)
        return f'powershell -NoProfile -Command "Start-Sleep -Seconds {seconds}"'

    return _WINDOWS_SLEEP_RE.sub(_replace_sleep, command)


def _watch_job(args: Dict[str, Any]) -> str:
    node = os.environ.get("NODE_ID") or os.environ.get("WORKER_NODE_ID") or socket.gethostname()
    session = (
        _bounded_text(args.get("session_id"), "session_id", _MAX_SESSION_ID_CHARS, required=False)
        or os.environ.get("SESSION_ID")
        or None
    )
    if session is not None:
        session = _bounded_text(session, "session_id", _MAX_SESSION_ID_CHARS)
    cwd = _bounded_text(args.get("cwd"), "cwd", _MAX_PATH_CHARS, required=False) or os.getcwd()
    command = _normalize_worker_command(
        _bounded_text(args.get("command"), "command", _MAX_COMMAND_CHARS) or ""
    )
    label = _bounded_text(args.get("label"), "label", _MAX_LABEL_CHARS) or ""
    notify_agent = _coerce_bool(args.get("notify_agent"), True)

    result = _post_job({
        "node_id": node,
        "label": label,
        "command": command,
        "cwd": cwd,
        "session_id": session,
        "notify": True,
        "notify_agent": notify_agent,
    })

    job_id = result.get("job_id", "?")
    return (
        f"Registered: {job_id}\n"
        f"Label:   {label}\n"
        f"Command: {command}\n"
        f"CWD:     {cwd}\n"
        f"Node:    {node}\n"
        f"Session: {session or '(none)'}\n"
        f"Agent follow-up: {'yes' if notify_agent else 'no'}\n"
        f"\n"
        f"The worker will spawn it now and capture its output.\n"
        f"You can watch it in System > Jobs. When it finishes, the result is posted "
        f"to the session chat; agent follow-up runs when enabled and a session is available."
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
