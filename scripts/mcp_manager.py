#!/usr/bin/env python3
"""
MCP server — ai-team Manager tool surface (M3 Phase 3.0, F4 spike).

Exposes the two minimal tools a gateway-spawned *Manager* session needs to
orchestrate a *worker* session, per docs/M3_MANAGER_INVOCATION_SPEC.md §2.2 / §6:

  * dispatch_worker(objective, ...)  -> POST /api/instructions  (start a worker task)
  * wait_for_worker(task_id|flow_run_id, ...) -> long-poll GET /api/flows/{id}
        until the worker's flow reaches a terminal / attention status.

Modeled EXACTLY on scripts/mcp_jobs.py (stdio JSON-RPC MCP server; loads the
project .env; bearer-token urllib to the gateway). The one difference: these tools
talk to the CONTROL API (default 127.0.0.1:9003, DASHBOARD_TOKEN) — NOT the :9002
task server that mcp_jobs uses (WORKER_TOKEN).

Design invariants (why this is safe for the F4 spike):
  * wait_for_worker is a pure read-only long-poll from THIS subprocess. It never
    holds a worker task slot, so a Manager waiting on a child cannot starve the
    slot the child needs (the §6 anti-starvation criterion — verify live in 3.0).
  * dispatch_worker only wraps the existing, auth-guarded, Level-3-gated
    POST /api/instructions. It introduces no new dispatch path.

LINEAGE (A32): POST /api/instructions now accepts an optional parent_flow_run_id.
   dispatch_worker forwards it so a child dispatched here records a parent edge in
   /api/flows (the §6 "child flow visible ... with lineage" clause). This is a
   SHADOW record wired through the M2 substrate: it is persisted only when the
   gateway runs with HARNESS_FLOW_DRIVE ON (it is, in the live env); with the flag
   OFF the server no-ops the stamp and the field is silently ignored. Either way
   nothing reads the edge to drive execution — so a Manager should confirm lineage
   via /api/flows rather than assume it, and always review the child's committed
   git diff (never a self-reported summary).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Bootstrap: load the project .env the same way the worker / mcp_jobs does
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Load project .env into os.environ before anything else runs."""
    project_root = Path(__file__).resolve().parent.parent
    ai_team_env = os.environ.get("AI_TEAM_ENV_FILE", "")
    env_path = Path(ai_team_env) if ai_team_env else (project_root / ".env")

    if not env_path.exists():
        print(f"[mcp_manager] WARNING: .env not found at {env_path}", file=sys.stderr, flush=True)
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except ImportError:
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
# Config
# ---------------------------------------------------------------------------

def _base_url() -> str:
    """Control API base URL. DASHBOARD_URL wins; else 127.0.0.1:DASHBOARD_PORT."""
    explicit = os.environ.get("DASHBOARD_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("DASHBOARD_PORT", "9003").strip() or "9003"
    return f"http://127.0.0.1:{port}"


def _token() -> str:
    """Control API bearer token — DASHBOARD_TOKEN, falling back to WORKER_TOKEN
    (mirrors control_api._dashboard_token())."""
    return os.environ.get("DASHBOARD_TOKEN", "") or os.environ.get("WORKER_TOKEN", "")


# Terminal vocab mirrors src/control/work_read_model.py so we agree with the
# server on what "done" / "needs attention" means (kept in sync deliberately).
_DONE_STATUSES = {"closed", "superseded", "done", "complete", "completed",
                  "failed", "error", "cancelled", "canceled"}
_ATTENTION_STATUSES = {"blocked", "rework", "rework_requested", "needs_decision",
                       "awaiting_operator", "awaiting_approval", "review",
                       "in_review", "review_requested"}

_MAX_OBJECTIVE_CHARS = 8000
_MAX_PATH_CHARS = 1000
_MAX_ID_CHARS = 128
_MAX_FILES = 100
_MAX_FILE_CHARS = 1000

_WAIT_TIMEOUT_DEFAULT = 300.0
_WAIT_TIMEOUT_MAX = 3600.0
_POLL_INTERVAL_DEFAULT = 3.0
_POLL_INTERVAL_MIN = 1.0

# [A33] Transient-blip tolerance: a long wait must not abort on a single gateway
# hiccup. Tolerate this many CONSECUTIVE poll failures (a clean poll resets the
# streak) before giving up. Still bounded by the overall deadline.
_MAX_CONSECUTIVE_POLL_ERRORS = 5

# ---------------------------------------------------------------------------
# HTTP  (single choke point — tests monkeypatch this)
# ---------------------------------------------------------------------------

def _api_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
                 timeout: float = 20.0) -> Dict[str, Any]:
    """One bearer-authenticated JSON request to the control API.

    Raises RuntimeError with a clean message on any failure (never leaks a bare
    urllib traceback into the MCP reply)."""
    token = _token()
    if not token:
        raise RuntimeError("DASHBOARD_TOKEN/WORKER_TOKEN not set — cannot reach control API")
    url = f"{_base_url()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {method} {path}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"Could not reach control API at {url}: {e}") from e


# ---------------------------------------------------------------------------
# Validation helpers (pure — unit-tested directly)
# ---------------------------------------------------------------------------

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


def _bounded_files(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("files must be a list of strings")
    if len(value) > _MAX_FILES:
        raise ValueError(f"files has too many entries (max {_MAX_FILES})")
    out: List[str] = []
    for item in value:
        f = _bounded_text(item, "files entry", _MAX_FILE_CHARS)
        if f:
            out.append(f)
    return out or None


def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def classify_status(status: Optional[str]) -> str:
    """Map a raw flow status to one of: done | attention | active | unknown.

    Kept in sync with work_read_model's status sets."""
    s = (status or "").strip().lower()
    if not s:
        return "unknown"
    if s in _DONE_STATUSES:
        return "done"
    if s in _ATTENTION_STATUSES:
        return "attention"
    return "active"


# ---------------------------------------------------------------------------
# Tool: dispatch_worker
# ---------------------------------------------------------------------------

def _dispatch_worker(args: Dict[str, Any]) -> str:
    objective = _bounded_text(args.get("objective"), "objective", _MAX_OBJECTIVE_CHARS)
    session_id = _bounded_text(args.get("session_id"), "session_id", _MAX_ID_CHARS, required=False)
    cwd = _bounded_text(args.get("cwd"), "cwd", _MAX_PATH_CHARS, required=False)
    files = _bounded_files(args.get("files"))
    parent_flow_run_id = _bounded_text(
        args.get("parent_flow_run_id"), "parent_flow_run_id", _MAX_ID_CHARS, required=False)

    body: Dict[str, Any] = {"description": objective}
    if session_id:
        body["session_id"] = session_id
    if cwd:
        body["cwd"] = cwd
    if files:
        body["target_files"] = files
    if parent_flow_run_id:
        # [A32] The endpoint now accepts this and stamps it onto the child's
        # flow_runs row via the M2 substrate — but ONLY when the gateway runs with
        # HARNESS_FLOW_DRIVE ON (a SHADOW record; nothing reads it to drive work).
        body["parent_flow_run_id"] = parent_flow_run_id

    result = _api_request("POST", "/api/instructions", body)
    task_id = result.get("task_id", "?")
    session = result.get("session") or {}
    sess_id = session.get("session_id") if isinstance(session, dict) else None

    lines = [
        f"Dispatched worker task: {task_id}",
        f"Objective: {objective}",
        f"Session:   {sess_id or session_id or '(one-off, no session)'}",
        f"CWD:       {cwd or '(session/default)'}",
        f"Files:     {', '.join(files) if files else '(none)'}",
    ]
    if parent_flow_run_id:
        lines.append(
            f"parent_flow_run_id: {parent_flow_run_id} — sent as the Manager→worker "
            f"lineage edge (recorded when the gateway runs HARNESS_FLOW_DRIVE ON; it is a "
            f"SHADOW record — confirm via /api/flows, don't assume)."
        )
    lines.append("")
    lines.append(
        f"Next: call wait_for_worker(task_id='{task_id}') to block until the worker's "
        f"flow reaches a terminal/attention status. That poll does NOT hold a task slot."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: wait_for_worker
# ---------------------------------------------------------------------------

def _resolve_flow_run_id(task_id: str) -> Optional[str]:
    """Find the newest flow_run for a task via GET /api/flows?task_id=."""
    result = _api_request("GET", f"/api/flows?task_id={urllib.parse.quote(task_id)}&limit=1")
    flows = result.get("flows") or []
    if flows:
        return flows[0].get("flow_run_id")
    return None


def _terminal_task_event(
    flow_run_id: str, task_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    """[A37] Detect a dispatched worker's completion from the `task.finished` event.

    Post-A37, a task ending records an authoritative ``task.finished`` case event
    but NO LONGER writes ``flow_runs.status`` (a Case closes only via close_case).
    So a plain worker dispatch signals "the turn finished" via this event, not via
    status — poll the case timeline for it. Returns ``{"kind","outcome"}`` (kind:
    done|attention) for the matching event, or None if the turn has not finished.
    Read-only; a transport failure propagates as RuntimeError so the poll loop's
    blip tolerance handles it uniformly."""
    detail = _api_request(
        "GET", f"/api/work/{urllib.parse.quote(flow_run_id)}/timeline",
    )
    events = detail.get("events") or []
    matches = [
        e for e in events
        if e.get("event_type") == "task.finished"
        and (not task_id or e.get("entity_id") == task_id)
    ]
    if not matches:
        return None
    payload = matches[-1].get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = None
    outcome = str(payload.get("outcome")) if isinstance(payload, dict) and payload.get("outcome") else "success"
    return {"kind": "done" if outcome == "success" else "attention", "outcome": outcome}


def _wait_for_worker(args: Dict[str, Any]) -> str:
    task_id = _bounded_text(args.get("task_id"), "task_id", _MAX_ID_CHARS, required=False)
    flow_run_id = _bounded_text(args.get("flow_run_id"), "flow_run_id", _MAX_ID_CHARS, required=False)
    if not task_id and not flow_run_id:
        raise ValueError("wait_for_worker requires task_id or flow_run_id")

    timeout = _clamp_float(args.get("timeout"), _WAIT_TIMEOUT_DEFAULT, _POLL_INTERVAL_MIN, _WAIT_TIMEOUT_MAX)
    poll_interval = _clamp_float(args.get("poll_interval"), _POLL_INTERVAL_DEFAULT, _POLL_INTERVAL_MIN, 60.0)

    deadline = time.monotonic() + timeout
    resolved_id = flow_run_id
    last_status: Optional[str] = None
    polls = 0
    consecutive_errors = 0
    last_error: Optional[str] = None

    while True:
        polls += 1
        # [A33] Tolerate transient gateway blips: a single poll failure must not
        # abort a long wait. Only the HTTP transport (_api_request → RuntimeError)
        # is caught here; validation (ValueError) already ran before the loop.
        try:
            if resolved_id is None and task_id is not None:
                resolved_id = _resolve_flow_run_id(task_id)

            if resolved_id is not None:
                detail = _api_request("GET", f"/api/flows/{urllib.parse.quote(resolved_id)}")
                flow = detail.get("flow") or {}
                last_status = flow.get("status")
                kind = classify_status(last_status)
                if kind in ("done", "attention"):
                    stage = flow.get("current_stage")
                    return (
                        f"Worker flow {resolved_id} reached: {kind.upper()}\n"
                        f"status={last_status!r} current_stage={stage!r}\n"
                        f"task_id={task_id or '(unknown)'} polls={polls}\n"
                        + ("\nNeeds attention (blocked/review/decision) — not a clean completion; "
                           "the Manager should inspect the case before continuing."
                           if kind == "attention" else
                           "\nTerminal. Review the worker's committed diff in git before closing "
                           "the case (do NOT trust a self-reported summary).")
                    )
                # [A37] Honest closure: task-end no longer flips flow_runs.status
                # (a Case closes only via close_case). A plain worker dispatch
                # signals its turn finished via the `task.finished` event — poll for
                # it so wait_for_worker still terminates on real completion.
                tev = _terminal_task_event(resolved_id, task_id)
                if tev is not None:
                    stage = flow.get("current_stage")
                    ekind = tev["kind"]
                    return (
                        f"Worker flow {resolved_id} reached: {ekind.upper()} (task.finished)\n"
                        f"task_outcome={tev['outcome']!r} current_stage={stage!r}\n"
                        f"task_id={task_id or '(unknown)'} polls={polls}\n"
                        + ("\nWorker turn finished cleanly; the Case remains OPEN "
                           "(Task finished != Case completed). Review the committed diff "
                           "in git, then close the Case authoritatively via close_case "
                           "once the objective is truly met."
                           if ekind == "done" else
                           "\nWorker turn FAILED; the Case remains open for the Manager to "
                           "inspect and decide (rework / close).")
                    )
            consecutive_errors = 0  # a clean poll resets the streak
        except RuntimeError as exc:
            consecutive_errors += 1
            last_error = str(exc)
            if consecutive_errors >= _MAX_CONSECUTIVE_POLL_ERRORS:
                return (
                    f"ERROR: wait_for_worker gave up after {consecutive_errors} consecutive "
                    f"poll failures ({polls} polls). Last error: {last_error}. The worker may "
                    f"still be running — inspect /api/work manually and re-call if needed."
                )
            # Otherwise fall through: sleep and retry until the deadline.

        if time.monotonic() >= deadline:
            where = resolved_id or (f"(unresolved flow for task {task_id})" if task_id else "(no id)")
            err_note = f" last poll error={last_error!r}." if last_error else ""
            return (
                f"TIMEOUT after {timeout:.0f}s ({polls} polls). "
                f"Worker flow {where} last status={last_status!r} (still active/unresolved).{err_note} "
                f"The worker may still be running — re-call wait_for_worker or inspect "
                f"/api/work manually. NOTE: if the flow_run row never appears, confirm "
                f"HARNESS_FLOW_DRIVE is ON and that a plain worker dispatch writes a flow "
                f"row (open question for the live spike — see AGENT_31)."
            )

        # Sleep, but never past the deadline.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        time.sleep(min(poll_interval, remaining))


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------
_TOOLS = [
    {
        "name": "dispatch_worker",
        "description": (
            "Dispatch a bounded task to a WORKER as a real gateway task (separate from the "
            "Manager's own session — never a sub-agent). Thin wrapper over the existing, "
            "auth-guarded, Level-3-gated POST /api/instructions. Returns the worker's task_id; "
            "track it with wait_for_worker. Provide a professional, not-overstated objective. "
            "If session_id is given the work runs in that existing worker session (cheaper — "
            "reuses orientation); omit it for a one-off. Pass your own flow_run id as "
            "parent_flow_run_id to record the Manager→worker lineage edge (visible in /api/flows)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "The bounded task for the worker (becomes the instruction description). Ground it; do not overstate scope."},
                "session_id": {"type": "string", "description": "Existing worker session to run in. Omit for a one-off dispatch."},
                "cwd": {"type": "string", "description": "Working directory / repo path. Defaults to the session's repo or the request default."},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Target files to focus the worker on (optional)."},
                "parent_flow_run_id": {"type": "string", "description": "The Manager's own flow_run id (the case). Recorded as the child→parent lineage edge in /api/flows (a SHADOW record — persisted when the gateway runs HARNESS_FLOW_DRIVE ON)."},
            },
            "required": ["objective"],
        },
    },
    {
        "name": "wait_for_worker",
        "description": (
            "Block (read-only long-poll) until a dispatched worker's flow reaches a terminal "
            "status (done/failed/cancelled) or an attention status (blocked/review/needs-decision), "
            "or until timeout. Give task_id (preferred) or flow_run_id. This poll does NOT hold a "
            "worker task slot, so waiting here cannot starve the slot the child worker needs. "
            "On return, verify the worker's committed diff in git — never trust a self-reported summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task_id returned by dispatch_worker."},
                "flow_run_id": {"type": "string", "description": "The flow_run id, if already known (skips task->flow resolution)."},
                "timeout": {"type": "number", "description": f"Max seconds to wait (default {int(_WAIT_TIMEOUT_DEFAULT)}, max {int(_WAIT_TIMEOUT_MAX)})."},
                "poll_interval": {"type": "number", "description": f"Seconds between polls (default {int(_POLL_INTERVAL_DEFAULT)}, min {int(_POLL_INTERVAL_MIN)})."},
            },
        },
    },
]

_TOOL_IMPLS = {
    "dispatch_worker": _dispatch_worker,
    "wait_for_worker": _wait_for_worker,
}

# ---------------------------------------------------------------------------
# MCP protocol — JSON-RPC 2.0 over stdio  (identical shape to mcp_jobs.py)
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
            "serverInfo": {"name": "manager", "version": "1.0.0"},
        })

    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass  # fire-and-forget

    elif method == "tools/list":
        _reply(id_, {"tools": _TOOLS})

    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        impl = _TOOL_IMPLS.get(name)
        if impl is None:
            _reply_error(id_, -32601, f"Unknown tool: {name!r}")
            return
        try:
            text = impl(arguments)
            _reply(id_, {"content": [{"type": "text", "text": text}]})
        except Exception as exc:  # noqa: BLE001 — surface as an MCP tool error, never crash
            print(f"[mcp_manager] {name} failed: {exc}", file=sys.stderr, flush=True)
            _reply(id_, {
                "content": [{"type": "text", "text": f"Error in {name}: {exc}"}],
                "isError": True,
            })

    else:
        if id_ is not None:
            _reply_error(id_, -32601, f"Unknown method: {method!r}")


def main() -> None:
    print("[mcp_manager] ready", file=sys.stderr, flush=True)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
            _dispatch(req)
        except json.JSONDecodeError as exc:
            _reply_error(None, -32700, f"Parse error: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[mcp_manager] internal error: {exc}", file=sys.stderr, flush=True)
            _reply_error(None, -32603, f"Internal error: {exc}")


if __name__ == "__main__":
    main()
