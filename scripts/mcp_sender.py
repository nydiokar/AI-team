#!/usr/bin/env python3
"""
MCP server — ai-team scoped agent sender (A82 Stage 5).

Exposes exactly ONE tool:

  * send_instruction(target_session_id, body, operation_id)
        -> POST /api/sessions/{target}/turn-requests  (managed admission)

Authenticated ONLY by the per-session sender capability the owning carrier
placed in THIS process's environment (``AI_TEAM_SENDER_CAPABILITY``), sent as
``Authorization: AITeamSender <capability>``. Unlike ``mcp_manager.py`` this
server deliberately does NOT load the project ``.env`` and has NO
DASHBOARD_TOKEN/WORKER_TOKEN fallback: a shared admin/worker bearer is never
an agent identity. No capability ⇒ the tool reports "not provisioned".

Same stdio JSON-RPC conventions as ``mcp_manager.py``; the request/validation
logic is shared with the Claude in-process server (``src/control/agent_sender``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Repo root importable regardless of the launching interpreter / cwd (same
# reason as mcp_manager._bootstrap) — but NO .env loading here.
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.control import agent_sender  # noqa: E402

_TOOLS = [{
    "name": agent_sender.SEND_TOOL_NAME,
    "description": agent_sender.TOOL_DESCRIPTION,
    "inputSchema": agent_sender.TOOL_INPUT_SCHEMA,
}]


def _send(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj), flush=True)


def _reply(id_: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _reply_error(id_: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _call_send_instruction(arguments: Dict[str, Any]) -> Dict[str, Any]:
    capability, base_url = agent_sender.stdio_capability()
    outcome = agent_sender.send_instruction(capability, base_url, arguments)
    text = agent_sender.render_outcome(outcome, str(arguments.get("target_session_id") or ""))
    return {"content": [{"type": "text", "text": text}], "isError": not outcome.ok}


def _dispatch(req: Dict[str, Any]) -> None:
    method: str = req.get("method", "")
    id_: Optional[Any] = req.get("id")
    if method == "initialize":
        _reply(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": agent_sender.SENDER_SERVER_NAME, "version": "1.0.0"},
        })
    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass
    elif method == "tools/list":
        _reply(id_, {"tools": _TOOLS})
    elif method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if name != agent_sender.SEND_TOOL_NAME or not isinstance(arguments, dict):
            _reply_error(id_, -32601, f"Unknown tool: {name!r}")
            return
        _reply(id_, _call_send_instruction(arguments))
    elif id_ is not None:
        _reply_error(id_, -32601, f"Unknown method: {method!r}")


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        if len(raw) > 256 * 1024:
            _reply_error(None, -32600, "request too large")
            continue
        try:
            req = json.loads(raw)
            if isinstance(req, dict):
                _dispatch(req)
            else:
                _reply_error(None, -32600, "invalid request")
        except json.JSONDecodeError as exc:
            _reply_error(None, -32700, f"Parse error: {exc.msg}")
        except Exception as exc:  # noqa: BLE001 — never crash the stdio loop
            _reply_error(None, -32603, f"Internal error: {type(exc).__name__}")


if __name__ == "__main__":
    main()
