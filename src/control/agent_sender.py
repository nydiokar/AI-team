"""[A82 Stage 5] Scoped agent sender — the ``send_instruction`` tool core.

One tool, ``send_instruction(target_session_id, body, operation_id)``, lets an
agent in an enrolled Case session durably queue an instruction into ANOTHER
session of the same open Case through the managed admission resource
(``POST /api/sessions/{id}/turn-requests``). It authenticates ONLY with the
per-session sender capability the carrier received privately at its managed
claim — never the shared dashboard/worker bearer (no ``_token_candidates``
fallback, no ``.env`` loading).

Two thin transports share this module:

* Claude SDK sessions: an in-process SDK MCP server (``build_claude_sender_server``)
  registered per session via ``ClaudeAgentOptions.mcp_servers``. The capability
  stays in carrier memory (a mutable ``SenderSlot``): the SDK serializes a stdio
  server's ``env`` into the CLI argv (``--mcp-config``), which every same-user
  process can read, so a stdio server would expose it there.
* Codex (and any stdio-capable backend): ``scripts/mcp_sender.py`` launched per
  thread with the capability in that server's own environment.

Result semantics: the tool reports the ACCEPTED turn id/status/queue position —
durably queued, not "the recipient read it".
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError, field_validator

SENDER_SERVER_NAME = "ai_team_sender"
SEND_TOOL_NAME = "send_instruction"
SENDER_TOOL_FQN = f"mcp__{SENDER_SERVER_NAME}__{SEND_TOOL_NAME}"
# Authorization scheme of the scoped credential. Distinct from ``Bearer`` so a
# shared admin/worker bearer can never be mistaken for an agent identity and a
# capability can never authenticate an operator route.
SENDER_AUTH_SCHEME = "AITeamSender"
CAPABILITY_ENV = "AI_TEAM_SENDER_CAPABILITY"
SENDER_URL_ENV = "AI_TEAM_SENDER_URL"
MAX_BODY_BYTES = 16 * 1024
MAX_CAPABILITY_CHARS = 256
HTTP_TIMEOUT_SEC = 20.0
STDIO_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "mcp_sender.py"

TOOL_DESCRIPTION = (
    "Queue an instruction for ANOTHER session of your current open Case (worker→Manager, "
    "worker→worker, Manager→worker). It is durably accepted into that session's turn queue "
    "and runs after the recipient's earlier turns; it never interrupts the recipient. The "
    "result is the accepted turn id/status — NOT confirmation the recipient has read it. "
    "Reuse the SAME operation_id when retrying the same instruction (a retry returns the "
    "same turn); a new instruction needs a new operation_id."
)
TOOL_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_session_id": {"type": "string", "description": "Recipient session id (same Case).",
                              "minLength": 1, "maxLength": 256},
        "body": {"type": "string", "description": "Instruction text (max 16 KiB UTF-8).",
                 "minLength": 1},
        "operation_id": {"type": "string", "description": "Stable idempotency key for this instruction.",
                         "minLength": 1, "maxLength": 256},
    },
    "required": ["target_session_id", "body", "operation_id"],
    "additionalProperties": False,
}


class SendInstructionArgs(BaseModel):
    """Strict tool input (validated before any network call)."""

    model_config = {"extra": "forbid"}

    target_session_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:\-]+$")
    body: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=256)

    @field_validator("body")
    @classmethod
    def _body_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("body exceeds 16 KiB UTF-8")
        return value


class SendInstructionOutcome(BaseModel):
    """What the tool reports back to the agent."""

    ok: bool
    status_code: int
    turn_id: Optional[str] = None
    status: Optional[str] = None
    queue_sequence: Optional[int] = None
    idempotent_replay: bool = False
    reason: Optional[str] = None
    message: str = ""


class SenderSlot(BaseModel):
    """Per-session, carrier-memory holder of the CURRENT capability. Shared by
    reference with a live backend instance, so a rotation/revocation reaches
    it without a respawn. The secret is excluded from repr."""

    token: Optional[str] = Field(default=None, repr=False)
    base_url: str = ""


def sender_base_url(env: Mapping[str, str]) -> str:
    """Control-API address for the tool (same resolution as the existing
    ``mcp_manager`` transport): DASHBOARD_URL, else CONTROLLER_URL host +
    DASHBOARD_PORT, else the local gateway."""
    explicit = str(env.get("DASHBOARD_URL", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = str(env.get("DASHBOARD_PORT", "9003") or "").strip() or "9003"
    controller = str(env.get("CONTROLLER_URL", "") or "").strip()
    if controller:
        host = urllib.parse.urlsplit(controller).hostname
        if host:
            return f"http://{host}:{port}"
    return f"http://127.0.0.1:{port}"


def _http_post(url: str, body: bytes, headers: Dict[str, str], timeout: float) -> Tuple[int, bytes]:
    """Single HTTP choke point (tests route it into the in-process app)."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read(64 * 1024)
    except urllib.error.HTTPError as e:
        return int(e.code), e.read(64 * 1024)


def send_instruction(capability: Optional[str], base_url: str, raw_args: Mapping[str, Any]) -> SendInstructionOutcome:
    """Validate the tool input and POST it to the admission resource with the
    scoped capability and a stable operation-id header. Never raises."""
    try:
        args = SendInstructionArgs.model_validate(dict(raw_args))
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        msg = str(first.get("msg") or "invalid arguments")
        return SendInstructionOutcome(ok=False, status_code=422, reason="invalid_arguments", message=msg[:300])
    if not capability:
        return SendInstructionOutcome(
            ok=False, status_code=401, reason="not_provisioned",
            message="sender capability not provisioned for this session (it is not an active "
                    "member of an open Case on this carrier)",
        )
    if len(capability) > MAX_CAPABILITY_CHARS:
        return SendInstructionOutcome(ok=False, status_code=401, reason="invalid_credential",
                                      message="malformed sender capability")
    url = f"{base_url.rstrip('/')}/api/sessions/{urllib.parse.quote(args.target_session_id, safe='')}/turn-requests"
    payload = json.dumps({"body": args.body, "operation_id": args.operation_id}).encode("utf-8")
    headers = {
        "Authorization": f"{SENDER_AUTH_SCHEME} {capability}",
        "Idempotency-Key": args.operation_id,
        "Content-Type": "application/json",
    }
    try:
        status, raw = _http_post(url, payload, headers, HTTP_TIMEOUT_SEC)
    except Exception as e:  # noqa: BLE001 — transport failure is a tool error
        return SendInstructionOutcome(ok=False, status_code=0, reason="unreachable",
                                      message=f"could not reach the gateway: {type(e).__name__}")
    try:
        data = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = {}
    if status == 202 and isinstance(data, dict) and data.get("turn_id"):
        return SendInstructionOutcome(
            ok=True, status_code=status, turn_id=str(data["turn_id"]),
            status=str(data.get("status") or ""),
            queue_sequence=data.get("queue_sequence") if isinstance(data.get("queue_sequence"), int) else None,
            idempotent_replay=bool(data.get("idempotent_replay")),
        )
    detail = data.get("detail") if isinstance(data, dict) else None
    reason = str(detail.get("reason") or "") if isinstance(detail, dict) else ""
    message = (str(detail.get("message") or "") if isinstance(detail, dict)
               else str(detail or "") if detail else "")
    return SendInstructionOutcome(ok=False, status_code=status, reason=reason or None, message=message[:300])


def render_outcome(outcome: SendInstructionOutcome, target_session_id: str = "") -> str:
    """Agent-facing result text."""
    if outcome.ok:
        replay = " (idempotent replay of an already-accepted operation)" if outcome.idempotent_replay else ""
        return (
            f"Accepted: turn_id={outcome.turn_id} status={outcome.status} "
            f"queue_sequence={outcome.queue_sequence}{replay}. Durably queued for session "
            f"{target_session_id}; it runs after that session's earlier turns. This does NOT "
            f"mean the recipient has read it."
        )
    return (f"send_instruction refused (HTTP {outcome.status_code}"
            f"{' ' + outcome.reason if outcome.reason else ''}): {outcome.message}")


def build_claude_sender_server(slot: SenderSlot) -> Dict[str, Any]:
    """In-process SDK MCP server (``ClaudeAgentOptions.mcp_servers`` value)
    exposing ONLY ``send_instruction``, reading the slot's CURRENT capability
    at call time. The HTTP call runs off the SDK loop."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(SEND_TOOL_NAME, TOOL_DESCRIPTION, TOOL_INPUT_SCHEMA)
    async def _send(args: Dict[str, Any]) -> Dict[str, Any]:
        outcome = await asyncio.to_thread(send_instruction, slot.token, slot.base_url, args)
        text = render_outcome(outcome, str(args.get("target_session_id") or ""))
        return {"content": [{"type": "text", "text": text}], "is_error": not outcome.ok}

    return dict(create_sdk_mcp_server(name=SENDER_SERVER_NAME, version="1.0.0", tools=[_send]))


def codex_sender_server(token: str, base_url: str) -> Dict[str, Any]:
    """Per-thread stdio server definition for Codex ``_thread_config``: the
    capability lives only in that server process's environment."""
    return {
        "command": sys.executable,
        "args": [str(STDIO_SCRIPT)],
        "env": {CAPABILITY_ENV: token, SENDER_URL_ENV: base_url},
    }


def stdio_capability() -> Tuple[Optional[str], str]:
    """(capability, base_url) for the stdio server: its OWN environment only."""
    return (os.environ.get(CAPABILITY_ENV) or None,
            os.environ.get(SENDER_URL_ENV) or sender_base_url(os.environ))
