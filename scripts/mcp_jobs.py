#!/usr/bin/env python3
"""
MCP server — ai-team watched jobs.

Exposes watch_job, job_result and whoami.
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
            "Registration returns immediately; the job runs independently and does not "
            "consume a task slot. Continue independent work, or return control while it runs. "
            "By default, an unconsumed completion wakes this same session so you can continue. "
            "Use job_result to read status/results: a terminal read acknowledges consumption "
            "and prevents a redundant queued continuation. If you inspect a private log or "
            "artifact directly, call job_result before proceeding to acknowledge the result. "
            "For long waits tied to a Claude SDK session, the gateway can also observe "
            "or preserve the session prompt cache while the detached job runs. "
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
                    "description": "Whether an unconsumed completion wakes this same session when the job finishes. Defaults to true.",
                    "default": True,
                },
                "expected_runtime_sec": {
                    "type": "integer",
                    "description": "Optional expected runtime in seconds. Helps the gateway decide when a cache heartbeat is worth arming.",
                    "minimum": 0,
                    "maximum": 86400,
                },
                "cache_heartbeat": {
                    "type": "string",
                    "description": "Session cache heartbeat policy for this detached job: auto, on, or off. Defaults to auto.",
                    "enum": ["auto", "on", "off"],
                    "default": "auto",
                },
            },
            "required": ["command", "label"],
        },
    },
    {
        "name": "job_result",
        "description": (
            "Read one of your watched jobs. Running jobs return status without consuming "
            "the future completion. A terminal result returns exit status and captured tail "
            "and acknowledges it, preventing another unstarted continuation for the same job. "
            "Use this when checking a job or after inspecting its private output artifacts. "
            "Completion is based on process exit; redirected stdout can leave the tail empty. "
            "If a continuation has already started, the request returns an explicit conflict."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "whoami",
        "description": (
            "Report YOUR OWN identity (read-only): your gateway session id and node, resolved "
            "from the process environment. This is the id the gateway keys your session on — NOT "
            "the SessionStart/transcript uuid you may see at boot. Takes no arguments: it can only "
            "ever report YOU. Use it so you never have to guess or type your own session id."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _post_job(payload: Dict[str, Any], path: str = "/jobs") -> Dict[str, Any]:
    url = os.environ.get("CONTROLLER_URL", "http://127.0.0.1:9002").rstrip("/")
    token = os.environ.get("WORKER_TOKEN", "")
    if not token:
        raise RuntimeError("WORKER_TOKEN not set — cannot reach task server")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url}{path}",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read(65537)
            if len(body) > 65536:
                raise ValueError("job response too large")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read(2048).decode(errors='replace')}") from e
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


def _whoami(args: Dict[str, Any]) -> str:
    """Report the CALLER'S OWN identity from the process env — no network, no args.

    Env-only on purpose: a worker's MCP server reaches the task server, not the
    control API, and identity is the universally useful fact here ("what is my own
    session id?"). The id reported is the gateway session id the carrier injected
    (``AI_TEAM_SESSION_ID``/``SESSION_ID``) — the control-plane id, not the
    SessionStart/transcript uuid."""
    sid = (os.environ.get("AI_TEAM_SESSION_ID") or os.environ.get("SESSION_ID") or "").strip()
    node = (os.environ.get("NODE_ID") or os.environ.get("WORKER_NODE_ID") or "").strip()
    if not sid:
        return (
            "whoami: your session identity is not present in this process environment "
            "(AI_TEAM_SESSION_ID/SESSION_ID unset)."
        )
    lines = [
        f"session_id: {sid}",
        "  (this is YOUR gateway session id — the control-plane id, not a transcript uuid.)",
    ]
    if node:
        lines.append(f"node: {node}")
    return "\n".join(lines)


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
        or os.environ.get("AI_TEAM_SESSION_ID")
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
    expected_runtime_raw = args.get("expected_runtime_sec")
    expected_runtime_sec = None
    if expected_runtime_raw is not None:
        expected_runtime_sec = max(0, min(86400, int(expected_runtime_raw)))
    cache_heartbeat = str(args.get("cache_heartbeat") or "auto").strip().lower()
    if cache_heartbeat not in {"auto", "on", "off"}:
        raise ValueError("cache_heartbeat must be auto, on, or off")

    result = _post_job({
        "node_id": node,
        "label": label,
        "command": command,
        "cwd": cwd,
        "session_id": session,
        "notify": True,
        "notify_agent": notify_agent,
        "expected_runtime_sec": expected_runtime_sec,
        "cache_heartbeat": cache_heartbeat,
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
        f"Cache heartbeat: {cache_heartbeat}\n"
        f"\n"
        f"The worker will spawn it now and capture its output.\n"
        f"You can watch it in System > Jobs. When it finishes, the result is posted "
        f"to the session chat; agent follow-up runs when enabled and a session is available."
        f"\nContinue independent work or return control. Call job_result with {job_id} "
        f"when inspecting its result so it cannot trigger another unstarted continuation."
    )


def _job_result(args: Dict[str, Any]) -> str:
    job_id = _bounded_text(args.get("job_id"), "job_id", 128) or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise ValueError("invalid job_id")
    session = os.environ.get("AI_TEAM_SESSION_ID") or os.environ.get("SESSION_ID") or ""
    node = os.environ.get("NODE_ID") or os.environ.get("WORKER_NODE_ID") or socket.gethostname()
    if not session:
        raise ValueError("your session identity is not present; cannot consume a job result")
    result = _post_job({"session_id": session, "node_id": node}, f"/jobs/{job_id}/result")
    return json.dumps(result, ensure_ascii=False)

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

        impl = {"watch_job": _watch_job, "job_result": _job_result, "whoami": _whoami}.get(name)
        if impl is not None:
            try:
                text = impl(arguments)
                _reply(id_, {"content": [{"type": "text", "text": text}]})
            except Exception as exc:
                print(f"[mcp_jobs] {name} failed: {exc}", file=sys.stderr, flush=True)
                _reply(id_, {
                    "content": [{"type": "text", "text": f"Error in {name}: {exc}"}],
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
