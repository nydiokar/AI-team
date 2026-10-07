"""Control API — the gateway's own in-process HTTP interface.

U1 of docs/CONTROL_SURFACE_UNIFICATION.md. This replaces the standalone
``dashboard.py`` + ``dashboard_main.py`` process. Built by ``build_control_api``
with the **live orchestrator**, so read handlers call the orchestrator's
in-process services and singletons — never a second ``SessionStore`` and never a
file/DB side-read where an in-process source exists.

Why in-process matters (same argument as ``embedded_server.py``): the gateway's
``get_registry()`` singleton and ``SessionService`` are populated in *this*
process. A separate dashboard process could only re-read ``state/mesh.db`` and
re-derive node liveness by hand; sharing the process removes that whole class of
staleness. Telegram and this HTTP surface are now siblings over the same services.

U3 adds the write surface: thin HTTP adapters over the SAME services Telegram
calls (``submit_instruction`` / ``SessionService`` / ``cancel_task`` /
``compact_session``) — no new business logic. Web sessions are tagged
``SessionOrigin(channel="web")``. WS/SSE push is U4; static serving is U5.

All ``/api/*`` endpoints require ``Authorization: Bearer {DASHBOARD_TOKEN}``
(falls back to ``WORKER_TOKEN``).
"""
from __future__ import annotations

import asyncio
import functools
import hmac
import ipaddress
import json
import logging
import socket
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError, field_validator

from src.core import observability
from src.control.app_metrics import RequestTimingMiddleware
# [A82 Stage 5] Authorization scheme of the scoped agent sender capability.
from src.control.agent_sender import SENDER_AUTH_SCHEME as _SENDER_AUTH_SCHEME



def _scrub_surrogates(obj: Any) -> Any:
    """Recursively replace lone UTF-16 surrogates in any string within ``obj``.

    A lone surrogate (e.g. ``\\udc81`` from mojibake) cannot be encoded to UTF-8, so
    it crashes ``JSONResponse.render`` (``ensure_ascii=False`` → ``.encode("utf-8")``).
    ``backslashreplace`` keeps the audit faithful (the offending code point survives as
    a printable escape) while making every string safely encodable. Clean strings
    round-trip byte-identically."""
    if isinstance(obj, str):
        return obj.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(obj, dict):
        return {_scrub_surrogates(k): _scrub_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_surrogates(v) for v in obj]
    return obj

# Map a CommandResult.reason (stable machine code) to an HTTP status. The body
# always still carries {ok, reason} so the client owns the wording (no prose here).
_REASON_STATUS = {
    "unknown_backend": 400,
    # [A82 Stage 8a] retired backend (e.g. the OpenCode CLI): use opencode-server
    "backend_retired": 410,
    "unknown_model": 400,
    "unknown_effort": 400,
    "invalid_repo_path": 400,
    "session_not_found": 404,
    "not_closed": 409,
    # [A82 Stage 4b] managed close could not reach the turn ledger (fail closed)
    "turn_queue_unavailable": 503,
    # Move H — approvals
    "not_found": 404,
    "already_resolved": 409,
    "invalid_decision": 400,
    "missing_action": 400,
    # Upload
    "no_repo_path": 400,
    "dangerous_extension": 400,
    "file_too_large": 413,
    # Task-harness Level-3 admission gate (A13) — task refused before enqueue.
    "harness_level3_needs_approval": 409,
}

# Human copy for the admission block. The backend owns a single sentence; the
# client may still map the stable `reason` to its own richer UI treatment.
_HARNESS_BLOCK_DETAIL = (
    "This is a Level-3 task and needs operator approval before it can run "
    "(HARNESS_LEVEL3_GUARD is armed). Retrying will not help."
)


def _harness_blocked_http(blocked: Exception) -> HTTPException:
    """Build the 409 for a task-harness Level-3 admission block.

    Mirrors the ``invalid_repo_path`` envelope shape: a stable machine ``reason``
    plus a human ``detail`` the client can surface verbatim. ``task_id`` echoes the
    refused task's id for logs/telemetry — no queue side-effect happened.
    """
    reason = getattr(blocked, "reason", "harness_level3_needs_approval")
    return HTTPException(
        status_code=_REASON_STATUS.get(reason, 409),
        detail={
            "ok": False,
            "reason": reason,
            "detail": _HARNESS_BLOCK_DETAIL,
            "task_id": getattr(blocked, "task_id", None),
        },
    )


# [Session-fork] Max chars for a `continue_inline` fork digest across the API. A
# generous working budget (~12k tokens) — a real forked conversation carries in
# full — while still bounding the field so an oversized payload can't be a DoS
# vector (§7). Mirrors web `FORK_DIGEST_MAX_CHARS` + the orchestrator prior-context
# clamp; override the clamp at runtime via AI_TEAM_COMPACT_PREFIX_MAX_CHARS.
_CONTINUE_INLINE_MAX = 48000
# [A72/P2-3] Generous server-side bound on the control-API write surface. The MCP
# tool caps objective at 8k and the compact-context budget is 48k — 256 KB is 32x
# that, so no realistic caller (web composer, MCP Manager, Manager-internal
# dispatch) can hit it; it only blunts runaway/accidental oversized posts.
_MAX_INSTRUCTION_CHARS = 262144
# [A82 Stage 4a] Compatibility-route serialized request ceiling (design §8),
# DERIVED from the existing character limits so no previously valid request is
# refused: the worst JSON encoding of one character is 12 bytes (a non-BMP char
# ASCII-escaped as a surrogate pair, e.g. \ud83d\ude00), applied to the prompt and
# carry limits, plus a 256 KiB allowance for the envelope/other fields. ≈3.8 MiB
# — a documented deviation from design §8's 2 MiB (which would refuse valid
# 262144-char prompts of escaped non-BMP text).
_JSON_MAX_BYTES_PER_CHAR = 12
_INSTRUCTIONS_ENVELOPE_ALLOWANCE = 256 * 1024
_INSTRUCTIONS_MAX_REQUEST_BYTES = (
    _JSON_MAX_BYTES_PER_CHAR * (_MAX_INSTRUCTION_CHARS + _CONTINUE_INLINE_MAX)
    + _INSTRUCTIONS_ENVELOPE_ALLOWANCE
)
# New turn-request route (Stage 6) whole-request cap (design §8: 256 KiB).
_TURN_REQUESTS_MAX_REQUEST_BYTES = 256 * 1024
# Body-read deadline for the capped routes (design §8 "Time").
_BODY_READ_DEADLINE_SEC = 5.0


async def _session_turn_queue_enrolled(session_id: str) -> bool:
    """[A82 Stage 4a] Durable enrollment marker. No read while no session is
    enrolled; else one offloaded read; unreadable ⇒ typed 503 (fail closed)."""
    from src.control.db import get_db
    from src.control.turn_admission import session_enrollment
    from src.control.turn_queue import TurnQueueError

    try:
        return await session_enrollment(get_db(), session_id)
    except TurnQueueError as err:
        raise _turn_queue_http(err)


def _turn_queue_http(err: Exception) -> HTTPException:
    """[A82 Stage 4a] Map a typed managed-queue outcome to a structured HTTP
    error (design §6 table); 429 carries Retry-After."""
    status = int(getattr(err, "status_code", 503) or 503)
    code = str(getattr(err, "code", "turn_queue_error"))
    ctx = getattr(err, "context", {}) or {}
    headers = None
    if status == 429:
        headers = {"Retry-After": str(int(ctx.get("retry_after", 1) or 1))}
    return HTTPException(
        status_code=status,
        detail={"ok": False, "reason": code, "message": str(getattr(err, "detail", err))[:300]},
        headers=headers,
    )


# [A82 Stage 4b rework 2] Caller self-declaration on /api/instructions. An
# in-repo automation caller (Manager MCP dispatch_worker) sends
# `X-AI-Team-Principal: automation`; its enrolled turn is then non-human and
# never releases an operator stop hold. Absent ⇒ operator (web UI). This is a
# trust-model LABEL, not authentication: both callers hold the same bearer token.
PRINCIPAL_HEADER = "X-AI-Team-Principal"
AUTOMATION_PRINCIPAL = "automation"


async def _submit_managed_instruction(
    orchestrator: Any, body: Any, session: Any, idempotency_key: Optional[str],
    principal: Optional[str] = None,
) -> str:
    """[A82 Stage 4a] Producer 1 (web) → managed admission. The web
    Idempotency-Key is the durable operation id (replay-safe across restarts)."""
    from src.control.turn_queue import TurnQueueError
    from src.orchestrator import HarnessAdmissionBlocked

    try:
        return await orchestrator.submit_instruction(
            description=body.description,
            session_id=session.session_id,
            cwd=session.repo_path or body.cwd,
            target_files=body.target_files,
            source=(
                "automation_session"
                if (principal or "").strip().lower() == AUTOMATION_PRINCIPAL
                else "web_session"
            ),
            parent_flow_run_id=body.parent_flow_run_id,
            join_case_id=body.case_id,
            extra_metadata=_instruction_extra_metadata(body),
            operation_id=idempotency_key,
            turn_queue_enrolled=True,
        )
    except HarnessAdmissionBlocked as blocked:
        raise _harness_blocked_http(blocked)
    except TurnQueueError as err:
        raise _turn_queue_http(err)


async def _validate_agent_sender(raw: str, target_session_id: str) -> Any:
    """[A82 Stage 5] Validate a scoped sender capability for ``target_session_id``
    (bounded offload; read-only). 401 unknown/revoked, 403 wrong scope, 503 DB."""
    from src.control.db import get_db
    from src.control.turn_queue import TurnQueueError

    db = get_db()
    if db is None:
        raise HTTPException(status_code=503, detail={"ok": False, "reason": "mesh_db_unavailable"})
    try:
        return await asyncio.to_thread(db.validate_sender_capability, raw, target_session_id)
    except TurnQueueError as err:
        http = _turn_queue_http(err)
        if http.status_code == 401:
            http.headers = {"WWW-Authenticate": _SENDER_AUTH_SCHEME}
        raise http


async def _submit_agent_instruction(orchestrator: Any, body: Any, session: Any, sender: Any) -> Any:
    """[A82 Stage 5] Agent sender → the SAME managed admission path. Source,
    sender and Case come from the validated capability, never the request."""
    from src.control.turn_queue import TurnQueueError
    from src.orchestrator import HarnessAdmissionBlocked

    try:
        return await orchestrator.submit_instruction(
            description=body.body,
            session_id=session.session_id,
            cwd=session.repo_path,
            source="agent_session",
            operation_id=body.operation_id,
            sender_session_id=sender.session_id,
            sender_case_id=sender.case_id,
            sender_capability_hash=sender.capability_hash,
            turn_queue_enrolled=True,
        )
    except HarnessAdmissionBlocked as blocked:
        raise _harness_blocked_http(blocked)
    except TurnQueueError as err:
        http = _turn_queue_http(err)
        if http.status_code == 401:  # stale capability at admission (rework F3)
            http.headers = {"WWW-Authenticate": _SENDER_AUTH_SCHEME}
        raise http


def _is_json_content_type(value: Optional[str]) -> bool:
    """FastAPI's strict declared-body rule: ``application/json`` or any
    ``+json`` subtype (parameters such as charset allowed); missing ⇒ False."""
    if not value:
        return False
    import email.message

    msg = email.message.Message()
    msg["content-type"] = value
    if msg.get_content_maintype() != "application":
        return False
    subtype: str = msg.get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


def _parse_turn_request_body(raw: bytes, content_type: Optional[str] = None) -> "TurnRequestCreateBody":
    """[A82 Stage 5 rework] Validate the create-route body AFTER the auth
    dependency ran (FastAPI decodes a declared body before dependencies, so
    unauthenticated garbage would be 422 instead of 401). Same 422 shape —
    including FastAPI's strict content-type rule: a non-JSON (or missing)
    media type is never decoded as JSON ⇒ 422, as on every declared body."""
    if not _is_json_content_type(content_type):
        raise RequestValidationError([{
            "type": "content_type", "loc": ("body",),
            "msg": "Content-Type must be application/json", "input": content_type,
        }])
    try:
        return TurnRequestCreateBody.model_validate_json(raw)
    except ValidationError as e:
        raise RequestValidationError(
            [{**err, "loc": ("body", *err.get("loc", ()))} for err in e.errors(include_url=False)]
        )


def _preparse_byte_guard(app: Any) -> None:
    """[A82 Stage 4a] Install the streamed pre-parse byte gate (design §8):
    bytes are counted as RECEIVED (chunked included) and a structured 413 is
    returned before JSON parsing for the operator recovery route and the
    ``/api/instructions`` admission route; a stalled body read fails at the
    deadline instead of holding the request open."""
    from src.control.body_cap import BodyCapMiddleware

    app.add_middleware(
        BodyCapMiddleware,
        rules=[
            (r"/api/turn-requests/[^/]+/resolve-recovery", 16 * 1024),
            (r"/api/turn-requests/[^/]+/withdraw", 16 * 1024),
            (r"/api/turn-requests/[^/]+", _TURN_REQUESTS_MAX_REQUEST_BYTES),
            (r"/api/instructions", _INSTRUCTIONS_MAX_REQUEST_BYTES),
            (r"/api/sessions/[^/]+/turn-requests", _TURN_REQUESTS_MAX_REQUEST_BYTES),
            (r"/api/sessions/[^/]+/turn-requests/(pause|resume|enroll|unenroll)", 16 * 1024),
        ],
        read_deadline_sec=_BODY_READ_DEADLINE_SEC,
    )
# [A72 review] Smaller semantic fields on the case write surface. The MCP client
# already bounds spec body ≤ 8k / title ≤ 512 / uri ≤ 1000 / reviewer ≤ 64, so the
# server bounds below sit at-or-above every legit caller and only reject bulk that
# no legit path can produce.
_REASON_MAX = 8000
_TITLE_MAX = 1024
_URI_MAX = 2000
_ID_STR_MAX = 128
_KEEP_NOTE_MAX = 4000
_KEEP_BODY_MAX_BYTES = 4096


class UploadedFileAttachment(BaseModel):
    path: str
    staged_file: Optional[Dict[str, str]] = None


class InstructionBody(BaseModel):
    description: str = Field(max_length=_MAX_INSTRUCTION_CHARS)
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    target_files: Optional[List[str]] = None
    upload_attachment: Optional[UploadedFileAttachment] = None
    # [A32] Optional Manager→worker lineage. When a Manager session dispatches a
    # worker via mcp_manager, it passes its own flow_run id (the case). Stamped
    # onto the child's flow_runs row ONLY when HARNESS_FLOW_DRIVE is ON (else a
    # no-op ⇒ byte-identical). Absent/None on every normal Telegram/Web request.
    parent_flow_run_id: Optional[str] = None
    # [A38] Manager→worker MEMBERSHIP. When set, the worker task JOINS this existing
    # Case (a `task` link on it) instead of birthing a child Case — the M3.1 default
    # for a Manager dispatching into its own Case. Attach only happens when
    # HARNESS_FLOW_DRIVE is ON; absent/None on every normal request ⇒ byte-identical.
    case_id: Optional[str] = None
    # [Session-fork] Verbatim digest of the marked messages carried over from a
    # forked session. When present it is injected ONCE, fence-defused and re-clamped
    # (keeping the most recent tail) as a reference-only `<prior_context>` block on
    # THIS turn's prompt (see orchestrator._maybe_inject_compact_context). Bounded
    # here (§7) so an oversized payload cannot be a DoS vector — but generously, so a
    # real fork carries in full. Absent on every normal turn ⇒ byte-identical.
    continue_inline: Optional[str] = Field(default=None, max_length=_CONTINUE_INLINE_MAX)


class TurnRequestCreateBody(BaseModel):
    """Small human instruction accepted by the managed queue resource."""

    model_config = {"extra": "forbid"}

    body: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=256)

    @field_validator("body")
    @classmethod
    def _body_byte_limit(cls, value: str) -> str:
        try:
            byte_count = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("body is not valid UTF-8") from exc
        if byte_count > 16 * 1024:
            raise ValueError("body exceeds 16 KiB UTF-8")
        return value


class TurnRequestEditBody(BaseModel):
    model_config = {"extra": "forbid"}

    body: str = Field(min_length=1)

    @field_validator("body")
    @classmethod
    def _body_byte_limit(cls, value: str) -> str:
        return TurnRequestCreateBody._body_byte_limit(value)


class TurnRequestSummaryOut(BaseModel):
    """[A82 Stage 6] One queue card (design §9). A read model of the managed
    ledger — NOT the telemetry turn DTO of ``/api/turns``. Never carries the
    full prompt, payload, claim token or sender capability."""

    model_config = {"extra": "ignore"}

    id: str
    turn_id: str
    session_id: str
    status: str
    revision: int
    queue_sequence: int
    queue_position: Optional[int] = None
    turn_source: Optional[str] = None
    turn_kind: Optional[str] = None
    sender_session_id: Optional[str] = None
    blocked_reason: Optional[str] = None
    created_at: Optional[str] = None
    activated_at: Optional[str] = None
    started_at: Optional[str] = None
    preview: str = ""

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "TurnRequestSummaryOut":
        return cls.model_validate({**row, "turn_id": row["id"]})


class TurnRequestPageOut(BaseModel):
    """[A82 Stage 6] Cursor page of one session's open managed turns, in run
    order, plus the queue-level state (counts, active slot, operator pause)."""

    turns: List[TurnRequestSummaryOut]
    count: int
    queued: int
    active_turn_id: Optional[str] = None
    active_status: Optional[str] = None
    next_cursor: Optional[int] = None
    enrolled: bool
    paused: bool
    hold: Optional[str] = None
    # [A82 Stage 8a] Finished turns of this session whose post-commit effects
    # (notification / history / telemetry) ended ``failed`` + the latest one.
    effects_failed: int = 0
    effects_failed_turn_id: Optional[str] = None
    # [A101] Read-only projection of why the head managed turn is held, and the
    # Case a quota pause can be resumed on. Pure mirror of the scheduler gates —
    # it carries NO new behaviour; it only lets the session window surface the
    # hold reason + the (unchanged) resume decision co-located with the composer.
    # ``pause_reason`` ∈ {operator_hold, operator_pause, quota, transient, retry,
    # manager_rebound, carrier_offline, backoff, legacy_draining, lineage}.
    blocked: bool = False
    pause_reason: Optional[str] = None
    resume_case_id: Optional[str] = None


class TurnRequestDetailOut(TurnRequestSummaryOut):
    """[A82 Stage 6] The one-item read: the full editable intent (body)."""

    body: str = ""
    completed_at: Optional[str] = None
    flow_run_id: Optional[str] = None
    # [A82 Stage 8a] A84 post-commit effects outcome of a finished turn
    # (``failed`` = the reply/notification may never have reached the user).
    effects_state: Optional[str] = None
    effects_error: Optional[str] = None

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "TurnRequestDetailOut":
        body: str = str(row.get("body") or "")
        preview: str = body.encode("utf-8")[:2048].decode("utf-8", errors="ignore")
        return cls.model_validate({**row, "turn_id": row["id"], "preview": preview})


class TurnRequestReceiptOut(BaseModel):
    """[A82 Stage 6] 202 acknowledgement of a durable admission: stable id,
    current status/revision, acceptance time and a queue-position SNAPSHOT
    (not a start-time promise)."""

    turn_id: str
    task_id: str
    status: str
    revision: int
    queue_sequence: Optional[int] = None
    queue_position: Optional[int] = None
    accepted_at: Optional[str] = None
    idempotent_replay: bool = False
    source: Optional[str] = None
    sender_session_id: Optional[str] = None


class TurnQueueControlOut(BaseModel):
    """[A82 Stage 6] Operator queue pause/resume outcome."""

    session_id: str
    paused: bool
    hold: Optional[str] = None


class CreateSessionBody(BaseModel):
    backend: str
    repo_path: str
    model: Optional[str] = None
    node_id: Optional[str] = None
    # [Worker role] Explicit opt-in role-boot signal ('worker'). Absent ⇒ tier-0
    # (byte-identical). dispatch_worker(role='worker') threads it to here so the
    # created worker session boots role-ful; nothing else sets it.
    role_boot: Optional[str] = None
    # [Session-fork] Session→session lineage. When this create is a FORK (continue a
    # stalled session as a fresh one), the source session id is passed here and
    # stamped on the new session for a navigable thread. Purely session-axis — it
    # does NOT touch Case membership or role. Absent on a normal create.
    continued_from: Optional[str] = None


class KeepSessionBody(BaseModel):
    keep_pinned: bool
    keep_note: Optional[str] = Field(default="", max_length=_KEEP_NOTE_MAX)


class ManagerInvokeBody(BaseModel):
    """[A38] Boot a Manager session bound to one new Case (M3 Phase 3.1)."""
    objective: str = Field(max_length=_MAX_INSTRUCTION_CHARS)
    repo_path: str
    backend: str = "claude"
    model: Optional[str] = None
    node_id: Optional[str] = None
    completion_criteria: Optional[str] = Field(default=None, max_length=_MAX_INSTRUCTION_CHARS)
    context_refs: Optional[List[str]] = None
    branch: Optional[str] = None
    # [Manager-fork] Seed the Manager boot turn with a prior conversation. All three
    # default None ⇒ byte-identical legacy boot (no lineage, no prior-context block).
    #   continued_from — session→session lineage pointer (navigable thread; no context carry).
    #   continue_inline — a client-held, marked-message digest injected once as a fenced,
    #     reference-only <prior_context> block on the Manager's first assignment turn.
    #   continues — a prior task_id; the gateway builds the bounded prior-context server-side.
    # continue_inline / continues reuse the proven compact-context injector verbatim.
    continued_from: Optional[str] = None
    continue_inline: Optional[str] = Field(default=None, max_length=_CONTINUE_INLINE_MAX)
    continues: Optional[str] = None


class CaseCloseBody(BaseModel):
    """[A38] Authoritatively close a Case (A37 close_case). ``outcome`` must be a
    terminal status; ``criteria_reconciliation`` records each completion_criterion
    as met or waived-with-reason so an honest close can proceed.
    ``continuation_plan`` records the Manager's next-priority handoff."""
    outcome: str = "closed"
    criteria_reconciliation: Optional[List[Dict[str, Any]]] = None
    continuation_plan: Optional[str] = Field(default=None, max_length=8000)
    # Records why a lane is exhausted against the objective when the Case did not
    # earn advancement evidence (a second dispatch or a rework). Satisfies the
    # MANAGER_ADVANCEMENT_GATE close gate; ignored when the flag is OFF.
    exhaustion_attestation: Optional[str] = Field(default=None, max_length=8000)
    # Operator escape hatch: cancel the Case's dangling pending approvals (an
    # ignored respawn/resume proposal) so an explicit close is not wedged by
    # "unresolved required approval". Default False ⇒ Manager/auto close unchanged.
    resolve_pending_approvals: bool = False


class CaseReviewBody(BaseModel):
    """[M3.2] Record a Manager review verdict on a Case. ``verdict`` must be one of
    accepted|rework_requested|waived; ``reason`` is an optional short note.
    ``task_id`` optionally TAGS the verdict to a specific worker task — richer audit,
    and the Wake-Dispatcher reads it as a consumption signal so an out-of-band review
    is not re-surfaced as a redundant continuation wake."""
    verdict: str
    reason: Optional[str] = Field(default=None, max_length=_REASON_MAX)
    task_id: Optional[str] = Field(default=None, max_length=_ID_STR_MAX)


class CaseWaitBody(BaseModel):
    """[A46/M3.3] Record a durable pending-wait marker for a dispatched worker so a
    resumed Manager can reconcile its outstanding waits from the ledger. ``task_id``
    is the dispatched worker's task; ``timeout`` is the optional wait bound (seconds),
    carried so a re-armed wait keeps the original deadline shape."""
    task_id: str
    timeout: Optional[float] = None


class CaseWaitGroupBody(BaseModel):
    """[M3.4] Arm a Manager wait-GROUP over a dispatch set so the Wake-Dispatcher
    re-enters the Case when it is satisfied. ``wait_group_id`` names the group;
    ``condition`` ∈ ANY|ALL|NAMED; ``member_task_ids`` are the group's dispatched
    worker tasks. A single-worker default is ANY over one member."""
    wait_group_id: str
    condition: str = "ANY"
    member_task_ids: List[str]


class CasePublishArtifactBody(BaseModel):
    """[A56/M4] Publish a durable artifact onto a Case. ``artifact_id`` names the
    artifact; ``kind`` is a free label (defaults to 'artifact'); ``title``/``uri`` are
    optional; ``metadata`` is verbatim JSON evidence."""
    artifact_id: str = Field(max_length=_ID_STR_MAX)
    kind: str = Field(default="artifact", max_length=_ID_STR_MAX)
    title: Optional[str] = Field(default=None, max_length=_TITLE_MAX)
    uri: Optional[str] = Field(default=None, max_length=_URI_MAX)
    metadata: Optional[Dict[str, Any]] = None


class CaseSpecBody(BaseModel):
    """[A56/M4] Author a spec onto a Case (durable evidence). ``spec_id`` names it;
    ``body`` is the authored spec text; ``title`` is an optional short label."""
    spec_id: str = Field(max_length=_ID_STR_MAX)
    body: str = Field(max_length=_MAX_INSTRUCTION_CHARS)
    title: Optional[str] = Field(default=None, max_length=_TITLE_MAX)


class CaseSpecReviewBody(BaseModel):
    """[A56/M4] Score a spec against R1 by a SEPARATE plan-reviewer seat. ``spec_id``
    is the spec being scored; ``scores`` maps each rubric dimension to 0–2; ``reason``
    is an optional note; ``reviewer`` names the (separate) reviewing seat."""
    spec_id: str = Field(max_length=_ID_STR_MAX)
    scores: Dict[str, Any]
    reason: Optional[str] = Field(default=None, max_length=_REASON_MAX)
    reviewer: str = Field(default="reviewer", max_length=_ID_STR_MAX)


class CaseDecomposeBody(BaseModel):
    """[A56/M4] Decompose an APPROVED objective into a task-DAG on ONE Case.
    ``spec_id`` is the approved spec; ``tasks`` is the list of task nodes, each
    ``{task_key, objective, depends_on: [...], ...hints}``."""
    spec_id: str
    tasks: List[Dict[str, Any]]


class CaseInterruptBody(BaseModel):
    """[A53] Kill a Case: cancel its in-flight worker task(s), mark it blocked
    (resumable), record flow.interrupted, escalate once. ``reason`` is an optional
    short label for the interruption (defaults to 'operator_kill')."""
    reason: Optional[str] = Field(default=None, max_length=_REASON_MAX)


class CaseOrphanSweepBody(BaseModel):
    """Operator cleanup for open Cases whose Manager session is gone or inactive.

    ``dry_run`` reports candidates without changing state. A real sweep
    force-closes TERMINAL orphans (Manager CLOSED/CANCELLED/gone) as 'cancelled'
    when ``close_terminal_orphans`` (default True), and marks resumable ones
    (pinned-node-offline) blocked through the interrupt path.
    """
    dry_run: bool = False
    limit: int = Field(default=200, ge=1, le=500)
    reason: Optional[str] = Field(default="manager_session_unavailable", max_length=64)
    close_terminal_orphans: bool = True


class CaseStateBody(BaseModel):
    """Operator Case state control. Non-terminal Cases can be moved between
    ``open`` and ``blocked``. Terminal close/cancel semantics stay on their
    dedicated paths."""
    state: str = Field(max_length=32)
    reason: Optional[str] = Field(default="operator_state_change", max_length=256)


class CaseResumeBody(BaseModel):
    """[quota-resume] Operator-triggered Case resume.

    ``mode`` is optional: omitted (or unknown) means "let the harness pick"
    (``_recommended_resume_mode``). Bounded tiny body — the resume itself is
    single-flight leased server-side, so a double-click cannot start two
    Managers."""
    mode: Optional[str] = Field(default=None, max_length=32)


class CaseOperatorCloseBody(BaseModel):
    """Operator manual Case closure.

    ``reason`` is written into the audit trail. Stored completion criteria are
    waived by default with that reason, so stale blocked Cases can be closed
    deliberately without weakening the Manager close contract.
    """
    reason: Optional[str] = Field(default="operator_manual_close", max_length=256)
    waive_completion_criteria: bool = True


class CaseOpenBody(BaseModel):
    """[M3.3] Open a NEW Case on an EXISTING Manager session — so one long-lived
    Manager session can own many Cases sequentially instead of spawning a fresh
    session per Case (the token-inflation the operator flagged). ``session_id`` is
    the Manager's own session; ``completion_criteria`` is the checkable done-gate
    that ``close_case`` later demands."""
    objective: str = Field(max_length=_MAX_INSTRUCTION_CHARS)
    session_id: str
    completion_criteria: Optional[str] = Field(default=None, max_length=_MAX_INSTRUCTION_CHARS)
    role: str = "manager"
    # [M3.4/A52] Optional autonomous-continuation round cap; folded into
    # completion_criteria as {"round_cap": N} by db.open_case (no new column).
    # Must be positive — a non-positive cap is rejected (422) rather than silently
    # widened to the engine default, mirroring the MCP tool's own validation.
    round_cap: Optional[int] = Field(default=None, gt=0)


class BindBody(BaseModel):
    chat_id: Optional[int] = None


class TurnRecoveryResolveBody(BaseModel):
    """[A82 Stage 3 rework] Operator unwedge for a held managed turn. Minimal
    (full UI is Stage 6): the decision + note are recorded on the row."""

    model_config = {"extra": "forbid"}

    decision: str = Field(pattern="^(failed|cancelled|requeue)$")
    note: str = Field(default="", max_length=500)
    # The operator explicitly accepts that the backend outcome is unproven
    # (required for a started/held turn; not needed to requeue an unstarted one).
    acknowledge_uncertain: bool = False


class RuntimeFlagBody(BaseModel):
    value: bool
    set_by: Optional[str] = Field(default=None, max_length=128)


class CacheHeartbeatBody(BaseModel):
    reason: str = Field(max_length=500)
    duration_sec: Optional[int] = Field(default=None, gt=0, le=86400)
    max_beats: int = Field(default=6, gt=0, le=15)


class ModelBody(BaseModel):
    model: Optional[str] = None


class EffortBody(BaseModel):
    effort: Optional[str] = Field(default=None, max_length=16)


class ApprovalRequestBody(BaseModel):
    action: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    risk: str = "medium"
    reversible: bool = True
    requested_by: str = ""


class ApprovalResolveBody(BaseModel):
    decision: str  # "approved" | "rejected"
    resolved_by: str = ""


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeBody(BaseModel):
    endpoint: str
    keys: PushKeys
    label: Optional[str] = None


class PushUnsubscribeBody(BaseModel):
    endpoint: str


class InspectBody(BaseModel):
    op: str
    path: Optional[str] = None
    limit: Optional[int] = None
    sort_by_recent: Optional[bool] = None


class GitCommitBody(BaseModel):
    task_id: str
    task_description: Optional[str] = Field(default=None, max_length=_REASON_MAX)
    create_branch: bool = True
    push_branch: bool = False


class UploadResult(BaseModel):
    ok: bool
    filename: str
    size: int
    path: str


def _session_payload(session, *, with_queue: bool = False) -> Optional[Dict[str, Any]]:
    """Render a Session as the canonical SessionView dict (or None).
    [A82 Stage 6] ``with_queue`` adds the ledger-derived turn-queue overlay
    (enrolled admission responses: the truthful session state)."""
    if session is None:
        return None
    from src.core.view_models import SessionView
    view = SessionView.from_session(session)
    if with_queue:
        from src.services.session_service import session_turn_queue_overlay
        view = view.with_turn_queue(
            session_turn_queue_overlay(_db(), [session.session_id]).get(session.session_id),
        )
    return view.to_dict()


def _fork_carry_meta(continue_inline: Optional[str]) -> Optional[Dict[str, str]]:
    """[Session-fork] Wrap a fork carry-over digest as task extra_metadata, or None.

    Returns ``{"continue_inline": <digest>}`` only when a non-blank string is present
    (so the orchestrator injects it once as a reference-only prior-context block);
    None otherwise, keeping every normal turn byte-identical (no metadata added)."""
    if isinstance(continue_inline, str) and continue_inline.strip():
        return {"continue_inline": continue_inline}
    return None


def _command_envelope(result) -> Dict[str, Any]:
    """Uniform JSON for a CommandResult: {ok, reason, session}."""
    env = {
        "ok": result.ok,
        "reason": result.reason,
        "session": _session_payload(result.session),
    }
    detail = getattr(result, "detail", "")
    if detail:
        env["detail"] = detail
    return env

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth — reuse the mesh secret; dashboard-specific override allowed
# ---------------------------------------------------------------------------

def _dashboard_token() -> str:
    """The web-UI credential — injected into the served HTML so the browser can
    authenticate. This is the dashboard token when set, else the mesh secret."""
    try:
        from config import config as _cfg
        return _cfg.mesh.dashboard_token or _cfg.mesh.worker_token
    except Exception:
        import os
        return os.getenv("DASHBOARD_TOKEN", "") or os.getenv("WORKER_TOKEN", "")


def _worker_token() -> str:
    """The shared mesh secret (WORKER_TOKEN), accepted as an ALTERNATE control-API
    credential alongside the dashboard token.

    The mesh WORKER_TOKEN is already provisioned on EVERY mesh node at setup (it is
    how a node's worker authenticates to the task server on :9002). Honoring it here
    lets a Manager/worker session running on ANY mesh node (e.g. Horse) reach the
    control API with a credential it already holds — the operator never has to copy
    the gateway-local dashboard token to each node, nor expose it anywhere. Both are
    trusted mesh-internal secrets and the control API binds loopback + tailnet only
    (never the public/LAN interface), so the trust boundary is unchanged. Empty when
    unconfigured, in which case only the dashboard token is accepted."""
    try:
        from config import config as _cfg
        return _cfg.mesh.worker_token or ""
    except Exception:
        import os
        return os.getenv("WORKER_TOKEN", "")


def _control_api_bind_host() -> str:
    """The configured tailnet bind host (CONTROL_API_HOST), empty when unset."""
    try:
        from config import config as _cfg
        return _cfg.mesh.control_api_host or ""
    except Exception:
        import os
        return os.getenv("CONTROL_API_HOST", "")


_TAILNET_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),        # Tailscale CGNAT range
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),  # Tailscale IPv6 ULA range
)


def _ui_request_trusted(host_header: str, client_ip: str) -> bool:
    """Whether the served UI may carry the DASHBOARD_TOKEN for this request.

    Both must hold:
    - Host is a name an attacker cannot point at us: the configured bind host, a
      Tailscale MagicDNS ``*.ts.net`` name (not publicly resolvable), or a tailnet
      IP literal. This defeats DNS rebinding, where a malicious site re-resolves ITS
      OWN name to our tailnet IP and then reads ``/`` as same-origin.
    - The peer is a REMOTE tailnet device. ``tailscale serve`` proxies from
      127.0.0.1 and uvicorn's proxy-headers middleware (trusts X-Forwarded-For from
      127.0.0.1 only) swaps in the real remote IP, so a peer that is loopback or
      any of THIS host's addresses (v4/v6, incl. our own tailnet IP when a local
      process goes through serve) is a process on this host — a host-networked
      container, an SSRF through a local service — never trusted.
    Residual (accepted): a local process that forges X-Forwarded-For over loopback.
    """
    host = host_header.strip().lower()
    if host.startswith("["):  # [fd7a::1]:9003
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    bind = _control_api_bind_host().strip().lower()
    host_ok = (
        (bool(bind) and host == bind)
        or host.endswith(".ts.net")
        # An IP-literal Host is never a rebinding vector (that needs the attacker's
        # own domain in Host), so any tailnet IP literal is trusted.
        or _is_tailnet(host)
    )
    return host_ok and _is_tailnet(client_ip) and not _is_local_address(client_ip)


@functools.lru_cache(maxsize=256)
def _is_local_address(addr: str) -> bool:
    """True iff ``addr`` is assigned to this host: the kernel only lets us bind a
    local address. Covers every interface and IPv6 with no config to drift."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((str(ip), 0))
        return True
    except OSError:
        return False


def _is_tailnet(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _TAILNET_NETS)


def _token_accepted(supplied: Optional[str]) -> bool:
    """True iff ``supplied`` matches either accepted mesh-internal credential:
    the dashboard token (web-UI / gateway-local) or the shared mesh WORKER_TOKEN.
    Callers must first confirm a token is configured (``_dashboard_token()``);
    an empty ``supplied`` never matches an empty accepted value."""
    if not supplied:
        return False
    if hmac.compare_digest(supplied.encode(), _dashboard_token().encode()):
        return True
    worker = _worker_token()
    return bool(worker) and hmac.compare_digest(supplied.encode(), worker.encode())


def _control_api_docs_enabled() -> bool:
    """Whether to expose the interactive Swagger/ReDoc/OpenAPI endpoints.

    Off by default — those endpoints are unauthenticated and leak the full API
    shape. Set CONTROL_API_DOCS=true to re-enable them for local development.
    """
    from src.control.db import control_api_docs_enabled
    return control_api_docs_enabled()


async def event_stream_frames(
    *,
    since: int = 0,
    is_disconnected=None,
    sleep=None,
    max_iterations: Optional[int] = None,
    keepalive_every: int = 20,
):
    """Async generator of SSE frame strings tailing events.ndjson (U4).

    Extracted from the route so it is testable without the HTTP transport
    (Starlette's TestClient buffers streaming responses and can't drive an endless
    stream). ``is_disconnected`` is an async predicate to stop on client hangup;
    ``sleep`` is the inter-poll await (injectable); ``max_iterations`` bounds the
    loop in tests. Each frame is ``data: {...}\\n\\n`` or an SSE comment keep-alive.
    """
    import asyncio as _asyncio

    if is_disconnected is None:
        async def is_disconnected():  # pragma: no cover - default never disconnects
            return False
    if sleep is None:
        sleep = lambda: _asyncio.sleep(1.0)  # noqa: E731

    offset = since
    data = observability.read_recent_events(since_offset=offset)
    offset = data.get("offset", offset)
    if data.get("events"):
        yield f"data: {json.dumps(data)}\n\n"
    else:
        yield ": connected\n\n"

    # Idle keep-alive cadence is decoupled from the event-poll cadence. We still
    # read the log AND check for client hangup every cycle (~1s), so event-delivery
    # latency and disconnect detection are UNCHANGED; we only withhold the idle
    # ": keep-alive" filler until `keepalive_every` consecutive idle cycles pass
    # (~20s at the 1s default). An every-second keep-alive frame keeps each client's
    # mobile radio awake for nothing. A longer idle gap is safe: EventSource has no
    # client idle timeout, X-Accel-Buffering:no already defeats proxy buffering, and
    # any intermediary that idle-closes just triggers the client's existing
    # reconnect + tail-replay (no lost events).
    idle_cycles = 0
    iterations = 0
    while True:
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        if await is_disconnected():
            break
        data = observability.read_recent_events(since_offset=offset)
        if data.get("events"):
            yield f"data: {json.dumps(data)}\n\n"
            offset = data.get("offset", offset)
            idle_cycles = 0
        else:
            idle_cycles += 1
            if idle_cycles >= keepalive_every:
                yield ": keep-alive\n\n"
                idle_cycles = 0
        await sleep()


def _bearer_from_header(request) -> Optional[str]:
    """Extract a Bearer token from the Authorization header, if present."""
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return None


def _heartbeat_timeout_sec() -> int:
    try:
        from config import config as _cfg
        return int(_cfg.mesh.node_heartbeat_timeout_sec)
    except Exception:
        return 90


def _annotate_node_liveness(node: Dict[str, Any]) -> None:
    """Derive ``live`` + ``heartbeat_age_sec`` from ``last_heartbeat`` (DB fallback).

    Only used when the in-process registry is empty (standalone-mesh / fallback
    mode), i.e. when we *must* read the shared DB and cannot trust an in-process
    ``status``. When the registry is populated we use its live nodes directly and
    this is never called — the whole reason it existed (separate-process staleness)
    is gone in the embedded path.
    """
    from datetime import datetime, timezone

    node["live"] = False
    node["heartbeat_age_sec"] = None
    raw = node.get("last_heartbeat")
    if not raw:
        return
    try:
        hb = datetime.fromisoformat(str(raw))
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        age = (now - hb).total_seconds()
        node["heartbeat_age_sec"] = round(age, 1)
        node["live"] = age <= _heartbeat_timeout_sec()
    except Exception:
        return


def _db():
    try:
        from src.control.db import get_db
        return get_db()
    except Exception:
        return None


def _telemetry_store():
    db = _db()
    if db is None:
        return None
    from src.control.telemetry_store import TelemetryStore
    return TelemetryStore(db)


def _results_dir() -> "Path":
    """The artifact directory (config.system.results_dir), as a Path."""
    from pathlib import Path
    try:
        from config import config as _cfg
        return Path(_cfg.system.results_dir)
    except Exception:
        return Path("results")


def _upload_staging_root() -> "Path":
    """Gateway staging directory for files that must be pulled by remote workers."""
    from pathlib import Path
    return Path(__file__).resolve().parents[2] / "state" / "uploads"


def _instruction_extra_metadata(body: InstructionBody) -> Optional[Dict[str, Any]]:
    metadata: Dict[str, Any] = dict(_fork_carry_meta(body.continue_inline) or {})
    if body.upload_attachment and body.upload_attachment.staged_file:
        metadata["staged_file"] = dict(body.upload_attachment.staged_file)
    # None (not {}) for an empty carry-over: submit_instruction treats
    # extra_metadata=None as a byte-identical legacy boot turn, and callers
    # (e.g. tests / fork-continuity) rely on that exact distinction.
    return metadata or None


def _upload_attached_instruction(instruction: str, file_path: str) -> str:
    clean_instruction: str = instruction.strip()
    return f"{clean_instruction}\n\n📎 File: `{file_path}`" if clean_instruction else ""


def _upload_error(status_code: int, reason: str, detail: str = "") -> HTTPException:
    """Structured upload failure: the repo-wide ``{"ok": False, "reason": ...}`` envelope
    (stable machine ``reason`` + optional human ``detail``) instead of a bare 500."""
    body: Dict[str, Any] = {"ok": False, "reason": reason}
    if detail:
        body["detail"] = detail
    return HTTPException(status_code=status_code, detail=body)


async def _store_session_upload(
    orchestrator: Any,
    session: Any,
    raw_name: str,
    content: bytes,
    instruction: Optional[str] = None,
) -> Dict[str, Any]:
    import os as _os
    import re as _re
    import shutil as _shutil
    import uuid as _uuid
    from pathlib import Path as _Path
    from src.control.node_inspector import session_node

    # [A82 pre-cutover P2] Producer 8: an ENROLLED session's file is always
    # staged for its managed carrier (which fetches it before invoking — the
    # gateway never writes the repo), and an attached instruction is a managed
    # turn: durable before ack, never BUSY / last_task_id.
    enrolled: bool = await _session_turn_queue_enrolled(session.session_id)

    ext = _os.path.splitext(raw_name)[1].lower()
    blocked_exts: set[str] = {
        ".exe", ".bat", ".cmd", ".com", ".msi", ".msp", ".scr", ".pif",
        ".vbs", ".vbe", ".ps1", ".psm1", ".psd1", ".wsf", ".wsh", ".hta",
        ".jar", ".dll", ".reg", ".lnk",
    }
    if ext in blocked_exts:
        raise _upload_error(400, "dangerous_extension")

    try:
        from config import config as _cfg
        max_mb: int = getattr(_cfg.telegram, "upload_max_mb", 0)
    except Exception:
        max_mb = 0
    if max_mb > 0 and len(content) > max_mb * 1024 * 1024:
        raise _upload_error(413, "file_too_large")

    safe_name: str = _re.sub(r"[^\w.\-]", "_", raw_name)[:200] or "upload"
    if not safe_name.strip("._"):
        safe_name = "upload"
    file_path: str = f"uploads/{safe_name}"
    attached_instruction: str = _upload_attached_instruction(instruction or "", file_path)

    remote_node = session_node(session)
    if remote_node is not None or enrolled:
        stage_id: str = _uuid.uuid4().hex[:16]
        stage_dir = _upload_staging_root() / stage_id
        dest = (stage_dir / safe_name).resolve()
        try:
            dest.relative_to(stage_dir.resolve())
        except ValueError:
            raise _upload_error(400, "dangerous_extension")
        try:
            stage_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        except OSError as exc:
            logger.error(
                "event=web_upload_stage_failed session=%s file=%s node=%s error=%s",
                session.session_id, safe_name, remote_node, exc,
            )
            raise _upload_error(
                503, "upload_staging_unavailable",
                "Gateway could not stage the file for the remote worker.",
            ) from exc
        staged_file_meta: dict[str, str] = {"file_id": stage_id, "filename": safe_name}
        if not attached_instruction:
            logger.info(
                "event=web_upload_staged_pending session=%s file=%s size=%d node=%s",
                session.session_id,
                safe_name,
                len(content),
                remote_node,
            )
            return {
                "ok": True,
                "filename": safe_name,
                "size": len(content),
                "path": file_path,
                "delivery": "pending_instruction",
                "staged_file": staged_file_meta,
            }

        if enrolled:
            from src.control.turn_queue import TurnQueueError
            from src.orchestrator import HarnessAdmissionBlocked

            try:
                admitted = await orchestrator.submit_instruction(
                    description=attached_instruction,
                    session_id=session.session_id,
                    cwd=session.repo_path,
                    source="web_session",
                    extra_metadata={"staged_file": staged_file_meta},
                    turn_queue_enrolled=True,
                )
            except Exception as exc:
                _shutil.rmtree(stage_dir, ignore_errors=True)
                if isinstance(exc, TurnQueueError):
                    raise _turn_queue_http(exc) from exc
                if isinstance(exc, HarnessAdmissionBlocked):
                    raise _harness_blocked_http(exc) from exc
                logger.error(
                    "event=web_upload_attached_enqueue_failed session=%s file=%s error=%s",
                    session.session_id, safe_name, exc,
                )
                raise _upload_error(500, "delivery_enqueue_failed") from exc
            return {
                "ok": True,
                "filename": safe_name,
                "size": len(content),
                "path": file_path,
                "delivery": "attached",
                "task_id": str(admitted),
                "instruction": attached_instruction,
                "staged_file": staged_file_meta,
            }

        orchestrator.session_service.mark_busy(
            session.session_id, last_user_message=attached_instruction)
        session = orchestrator.session_service.store.get(session.session_id) or session
        try:
            task_id: str = await orchestrator.submit_instruction(
                description=attached_instruction,
                session_id=session.session_id,
                cwd=session.repo_path,
                source="web_session",
                extra_metadata={
                    "staged_file": staged_file_meta,
                },
            )
        except Exception as exc:
            orchestrator.session_service.mark_idle(session.session_id)
            _shutil.rmtree(stage_dir, ignore_errors=True)
            logger.error(
                "event=web_upload_attached_enqueue_failed session=%s file=%s node=%s error=%s",
                session.session_id,
                safe_name,
                remote_node,
                exc,
            )
            raise _upload_error(500, "delivery_enqueue_failed") from exc
        logger.info(
            "event=web_upload_attached session=%s file=%s size=%d node=%s task=%s",
            session.session_id,
            safe_name,
            len(content),
            remote_node,
            task_id,
        )
        session.last_task_id = task_id
        orchestrator.session_service.store.save(session)
        return {
            "ok": True,
            "filename": safe_name,
            "size": len(content),
            "path": file_path,
            "delivery": "attached",
            "task_id": task_id,
            "instruction": attached_instruction,
        }

    upload_dir = _Path(session.repo_path) / "uploads"
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = (upload_dir / safe_name).resolve()

        try:
            dest.relative_to(upload_dir.resolve())
        except ValueError:
            raise _upload_error(400, "dangerous_extension")

        if dest.exists():
            stem, suffix = _os.path.splitext(safe_name)
            counter: int = 1
            while dest.exists():
                dest = (upload_dir / f"{stem}_{counter}{suffix}").resolve()
                counter += 1
            safe_name = dest.name
            file_path = f"uploads/{safe_name}"
            attached_instruction = _upload_attached_instruction(instruction or "", file_path)

        dest.write_bytes(content)
    except OSError as exc:
        # The session's repo is not writable from the gateway. For a session pinned
        # to a worker node this means the node is unknown to the gateway, so the
        # remote (staged) path was not taken — never surface it as an unhandled 500.
        pinned: bool = bool(getattr(session, "machine_id", ""))
        logger.error(
            "event=web_upload_local_write_failed session=%s machine_id=%s repo=%s error=%s",
            session.session_id, getattr(session, "machine_id", ""), session.repo_path, exc,
        )
        raise _upload_error(
            409 if pinned else 500,
            "session_repo_unreachable" if pinned else "upload_write_failed",
            f"Gateway cannot write to the session repo path: {exc.strerror or exc}."
            + (" The session's node is not registered with this gateway." if pinned else ""),
        ) from exc
    logger.info(
        "event=web_upload session=%s file=%s size=%d", session.session_id, safe_name, len(content)
    )
    if attached_instruction:
        orchestrator.session_service.mark_busy(
            session.session_id, last_user_message=attached_instruction)
        session = orchestrator.session_service.store.get(session.session_id) or session
        try:
            task_id = await orchestrator.submit_instruction(
                description=attached_instruction,
                session_id=session.session_id,
                cwd=session.repo_path,
                target_files=[str(dest)],
                source="web_session",
            )
        except Exception as exc:
            orchestrator.session_service.mark_idle(session.session_id)
            logger.error(
                "event=web_upload_attached_enqueue_failed session=%s file=%s error=%s",
                session.session_id,
                safe_name,
                exc,
            )
            raise _upload_error(500, "delivery_enqueue_failed") from exc
        session.last_task_id = task_id
        orchestrator.session_service.store.save(session)
        return {
            "ok": True,
            "filename": safe_name,
            "size": len(content),
            "path": file_path,
            "delivery": "attached",
            "task_id": task_id,
            "instruction": attached_instruction,
        }

    return {
        "ok": True,
        "filename": safe_name,
        "size": len(content),
        "path": file_path,
        "delivery": "local",
    }


def _sessions_dir() -> "Path":
    """The per-session record directory (``state/sessions/<id>.json``).

    Anchored the same way SessionStore does (``<project_root>/state/sessions``).
    This is the conversation source of truth — each record's ``task_history`` holds
    the full per-turn user_message + result_summary the transcript reader serves.
    """
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent.parent
    return project_root / "state" / "sessions"


def _list_projects_for_node(node_id: str, limit: int = 20) -> list:
    """List discoverable repos for a node. Mirrors TelegramInterface._repo_choices_for_node.

    Local (__local__): scans PathResolver root for git dirs (same logic as the Telegram
    wizard). Remote: reads the DB node row's `repos` JSON (populated by the worker's
    heartbeat). Returns [{name, path}].
    """
    if node_id == "__local__":
        try:
            from src.services.path_resolver import PathResolver
            from pathlib import Path as _Path
            resolver = PathResolver.from_config()
            root = resolver.base_cwd or resolver.allowed_root
            if not root:
                return []
            root_path = _Path(root).resolve()
            children = [
                c for c in root_path.iterdir()
                if c.is_dir() and not c.name.startswith(".")
            ]
            children.sort(key=lambda c: c.stat().st_mtime, reverse=True)
            repos = [c for c in children if (c / ".git").exists()]
            if len(repos) < limit:
                seen = {c.resolve() for c in repos}
                for c in children:
                    if c.resolve() not in seen:
                        repos.append(c)
                        seen.add(c.resolve())
                        if len(repos) >= limit:
                            break
            return [{"name": c.name, "path": str(c.resolve())} for c in repos[:limit]]
        except Exception as e:
            logger.warning("_list_projects_for_node local err=%s", e)
            return []
    else:
        try:
            import json as _json
            db = _db()
            if db is None:
                return []
            row = db.get_node(node_id)
            if row:
                repos = _json.loads(row.get("repos") or "[]")
                return [{"name": r["name"], "path": r["path"]} for r in repos[:limit]]
        except Exception as e:
            logger.warning("_list_projects_for_node remote node=%s err=%s", node_id, e)
        return []


def build_control_api(orchestrator) -> FastAPI:
    """Build the gateway's read API bound to the live orchestrator.

    ``orchestrator`` must expose ``session_service`` (M1 SessionService). Node and
    task reads use the in-process registry / DB. No state is constructed here.
    """
    # Disable FastAPI's built-in (unauthenticated) docs endpoints. /docs, /redoc and
    # /openapi.json leak the full API shape to anyone who can reach the port and have
    # no reason to be open even on the tailnet (defense in depth). The human-facing
    # map lives in docs/backend/ARCHITECTURE.md; a developer who wants live Swagger can flip
    # CONTROL_API_DOCS=true to re-enable them locally.
    _docs_on = _control_api_docs_enabled()
    app = FastAPI(
        title="AI-Team Control API",
        version="1.0",
        docs_url="/docs" if _docs_on else None,
        redoc_url="/redoc" if _docs_on else None,
        openapi_url="/openapi.json" if _docs_on else None,
    )
    app.add_middleware(RequestTimingMiddleware, component="gateway")
    # [A82 Stage 3 rework 4, m2 / Stage 4a] Streamed pre-parse byte caps.
    _preparse_byte_guard(app)

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Render 422s safely even when the offending body carries un-encodable text.

        FastAPI's default handler echoes the rejected ``input`` verbatim into a
        ``JSONResponse``; starlette renders that with ``ensure_ascii=False`` and then
        ``.encode("utf-8")``. A request body containing a lone UTF-16 surrogate (e.g.
        mojibake in a Manager review ``reason``) is itself what pydantic rejects — but
        the echoed surrogate then makes the encode step raise ``UnicodeEncodeError``,
        turning a would-be 422 into a 500 across the whole write surface. Scrub
        surrogates from the error payload so malformed input returns a structured 422
        (§7 service boundary), never a panic."""
        safe = _scrub_surrogates(jsonable_encoder(exc.errors()))
        return JSONResponse(status_code=422, content={"detail": safe})

    # auto_error=False: we own the missing-credential status (always 401) instead of
    # inheriting it from FastAPI, which changed it from 403 to 401 across versions.
    _bearer = HTTPBearer(auto_error=False)

    async def _require_auth(
        creds: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    ) -> None:
        if creds is None:
            raise HTTPException(
                status_code=401,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not _dashboard_token():
            raise HTTPException(status_code=500, detail="DASHBOARD_TOKEN not configured")
        if not _token_accepted(creds.credentials):
            raise HTTPException(status_code=401, detail="Invalid token")

    # Bounded in-process idempotency cache {(<route>, <key>) -> response dict}.
    # In-process is sufficient: the gateway is a single process (the point of U1).
    #
    # CONC-1: the prior implementation was check-then-act (get → miss → execute →
    # put) with no locking, which assumed retries arrive sequentially. FastAPI runs
    # sync (and awaits async) endpoints concurrently, so two requests sharing a key
    # could both miss the cache and both execute the side effect. Fixing get/put
    # atomicity alone is NOT enough — the whole get→execute→put sequence must be
    # serialized PER KEY. We hand out a per-key lock (created under a small registry
    # lock); same-key requests serialize on it so the second caller blocks until the
    # first stores its response, then hits the cache. Different keys never contend.
    _idem: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
    _IDEM_MAX = 512
    _idem_registry_lock = threading.Lock()
    _idem_key_locks: "OrderedDict[tuple, threading.Lock]" = OrderedDict()

    def _idem_put(route: str, key: Optional[str], resp: Dict[str, Any]) -> None:
        if not key:
            return
        with _idem_registry_lock:
            _idem[(route, key)] = resp
            while len(_idem) > _IDEM_MAX:
                _idem.popitem(last=False)

    def _lock_is_free(lock: Any) -> bool:
        """True if the lock is not currently held. Both threading.Lock and
        asyncio.Lock expose .locked(); default to 'held' if some exotic lock
        doesn't, so we never evict something we can't prove is free."""
        try:
            return not lock.locked()
        except Exception:
            return False

    def _idem_lock_for(route: str, key: str, factory) -> Any:
        """Fetch-or-create the per-key lock for (route, key) under the registry lock.

        ``factory`` builds the lock (threading.Lock for sync endpoints, asyncio.Lock
        for the async one) so the same registry serves both without holding the wrong
        lock type across an ``await``."""
        rk = (route, key)
        with _idem_registry_lock:
            lock = _idem_key_locks.get(rk)
            if lock is None:
                lock = factory()
                # Move-to-end so a freshly created/used lock is the youngest.
                _idem_key_locks[rk] = lock
                # Bound the registry, but NEVER evict a lock that is currently held
                # — evicting it would let a concurrent same-key request mint a fresh
                # lock and run in parallel, defeating the guard. We scan from oldest
                # and drop only free locks; a registry full of held locks is allowed
                # to exceed _IDEM_MAX transiently (it drains as work completes).
                if len(_idem_key_locks) > _IDEM_MAX:
                    for ek in list(_idem_key_locks.keys()):
                        if len(_idem_key_locks) <= _IDEM_MAX:
                            break
                        if ek == rk:
                            continue
                        el = _idem_key_locks[ek]
                        if _lock_is_free(el):
                            del _idem_key_locks[ek]
            else:
                _idem_key_locks.move_to_end(rk)
            return lock

    @contextmanager
    def _idem_guard(route: str, key: Optional[str]):
        """Serialize the check-execute-store sequence for a single (route, key) in a
        SYNC endpoint (FastAPI runs these in a threadpool, so a blocking lock is safe).

        Yields the cached response if one already exists (caller returns it), else
        None (caller executes, then calls _idem_put). Holding the per-key lock across
        the caller's work is what makes idempotency concurrency-safe."""
        if not key:
            yield None
            return
        lock = _idem_lock_for(route, key, threading.Lock)
        with lock:
            with _idem_registry_lock:
                cached = _idem.get((route, key))
            yield cached

    @asynccontextmanager
    async def _idem_guard_async(route: str, key: Optional[str]):
        """Async counterpart of _idem_guard for ``async def`` endpoints, which run ON
        the event loop. A threading.Lock held across ``await`` would block the loop
        thread and deadlock a second same-key request; an asyncio.Lock yields instead."""
        if not key:
            yield None
            return
        lock = _idem_lock_for(route, key, asyncio.Lock)
        async with lock:
            with _idem_registry_lock:
                cached = _idem.get((route, key))
            yield cached

    # --- route areas: one APIRouter per area (src/control/routes/) -----------
    from src.control.routes import admin, cases, cost, monitoring, sessions, turn_requests, work

    auth = {"require_auth": _require_auth}
    idem = {"idem_put": _idem_put, "idem_guard": _idem_guard}
    app.include_router(monitoring.build_router(orchestrator, **auth))
    app.include_router(turn_requests.build_router(orchestrator, **auth, bearer=_bearer))
    app.include_router(admin.build_router(orchestrator, **auth, **idem))
    app.include_router(
        sessions.build_router(orchestrator, **auth, **idem, idem_guard_async=_idem_guard_async)
    )
    app.include_router(work.build_router(orchestrator, **auth))
    app.include_router(
        cases.build_router(orchestrator, **auth, **idem, idem_guard_async=_idem_guard_async)
    )
    app.include_router(cost.build_router(orchestrator, **auth))

    # --- serve the built Web UI from the gateway (U5) ---------------------
    _mount_web_ui(app)

    return app


def _web_dist_dir() -> "Path":
    """Path to the built Web UI (web/dist), relative to the repo root."""
    from pathlib import Path
    # control_api.py is src/control/control_api.py → repo root is parents[2].
    return Path(__file__).resolve().parents[2] / "web" / "dist"


def _mount_web_ui(app: FastAPI) -> None:
    """Serve web/dist at / (U5), with the DASHBOARD_TOKEN injected for trusted requests.

    The tailnet is the trust boundary, so the operator's devices get the token baked
    in as ``window.__DASHBOARD_TOKEN__`` (no pairing). Injection happens ONLY when
    ``_ui_request_trusted`` holds (Host allowlist + loopback/tailnet peer), which
    defeats DNS rebinding from a malicious site; the injected page is ``no-store``.
    Any other request gets the plain page and pairs via ``/#token=...`` or the
    TokenGate. /api/* enforces the token regardless.
    A built UI is optional: if web/dist is absent (dev — vite serves the UI and
    proxies /api here), the mount is skipped silently.
    """
    from pathlib import Path
    from fastapi.responses import HTMLResponse, FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = _web_dist_dir()
    index_file = dist / "index.html"
    if not index_file.exists():
        logger.info("event=web_ui_not_mounted reason=no_dist dir=%s", dist)
        return

    # The dashboard is auto-authenticated on trusted devices: never frameable
    # (clickjacking from a malicious page the operator visits).
    _NO_FRAME = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}

    def _index_response(request: Request) -> HTMLResponse:
        html = index_file.read_text(encoding="utf-8")
        client_ip = request.client.host if request.client else ""
        token = _dashboard_token()
        if not token or not _ui_request_trusted(request.headers.get("host", ""), client_ip):
            return HTMLResponse(html, headers=_NO_FRAME)
        # Inject BEFORE the first <script> so the global exists before the app boots.
        inject = f"<script>window.__DASHBOARD_TOKEN__ = {json.dumps(token)};</script>"
        html = html.replace("<head>", "<head>" + inject, 1) if "<head>" in html else inject + html
        return HTMLResponse(html, headers={**_NO_FRAME, "Cache-Control": "no-store"})

    # Static assets (JS/CSS/img) served directly from web/dist/assets.
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    # include_in_schema=False: these serve HTML, not JSON, and their HTMLResponse
    # return annotation breaks OpenAPI schema generation (the /openapi.json 500 seen
    # when CONTROL_API_DOCS=true). Excluding them keeps the schema buildable.
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def _web_index(request: Request) -> HTMLResponse:
        return _index_response(request)

    # SPA fallback: any non-/api, non-asset path returns index (client-side routing).
    dist_resolved = dist.resolve()

    @app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
    def _web_spa(full_path: str, request: Request) -> HTMLResponse:
        # DX-1: an unmatched GET under /api/ is a missing endpoint, not a client
        # route — return a real 404 JSON error instead of letting it fall through
        # to the SPA index (which would 200 with HTML and mask the bug). The named
        # /api/* routes above are registered first and still win; only genuinely
        # unknown /api paths reach here.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        # Let real files (favicon, manifest, …) resolve if present; else SPA index.
        # SECURITY: confine the resolved path to web/dist. ``full_path`` is
        # attacker-controlled and may contain ``..`` / percent-encoded ``..`` that
        # the router does not normalize; without this check, a request like
        # ``/%2e%2e/%2e%2e/.env`` would escape web/dist and serve arbitrary files
        # (unauthenticated — this route has no token). On any escape, fall through
        # to the SPA index rather than serving the file.
        if full_path:
            candidate = (dist / full_path).resolve()
            if (candidate == dist_resolved or dist_resolved in candidate.parents) \
                    and candidate.is_file():
                return FileResponse(str(candidate))  # type: ignore[return-value]
        return _index_response(request)

    logger.info("event=web_ui_mounted dir=%s", dist)


def _live_nodes() -> List[Dict[str, Any]]:
    """Prefer the in-process registry; fall back to the DB read with liveness.

    Registry-populated (embedded task server running): every node is live with a
    fresh heartbeat the expiry loop maintains *in this process* — no annotation
    needed. Registry empty (standalone-mesh / fallback): read the shared DB and
    annotate, exactly as the old dashboard did, so behavior is preserved.
    """
    # The gateway's own hostname self-node (empty backends, no tailscale IP) is a
    # liveness-only stub for local self-claims — not a selectable worker. Hide it
    # from operator-facing listings (System tab cards + New Session machine
    # picker, which both consume this). Presentation-only: task_server still
    # registers/heartbeats/reaps it via the registry and DB.
    from src.control.db import _is_gateway_self_node

    try:
        from src.control.node_registry import get_registry
        reg = get_registry()
        if not reg.is_empty():
            out: List[Dict[str, Any]] = []
            for info in reg.list_all():
                d = info.to_dict()
                if _is_gateway_self_node(d):
                    continue
                d["live"] = d.get("status") == "online"
                age = get_registry()._live_state_age_sec(info)
                d["heartbeat_age_sec"] = round(age, 1) if age is not None else None
                out.append(d)
            return out
    except Exception as e:
        logger.warning("control_api_registry_read_failed err=%s", e)

    db = _db()
    nodes = db.list_nodes() if db is not None else []
    nodes = [n for n in nodes if not _is_gateway_self_node(n)]
    for n in nodes:
        _annotate_node_liveness(n)
    return nodes
