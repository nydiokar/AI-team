"""A82 Stage 2 — managed session turn queue: typed outcomes + request/result models.

This module holds the *narrow* contract surface for the protocol-1 (managed)
turn queue described in ``docs/SESSION_TURN_QUEUE_DESIGN.md`` §§3-4 and §6.

Design decisions honoured here (A82 §15):

  * The managed path NEVER shares helpers with the legacy protocol-0 path. The
    strict DB helpers (``MeshDB.claim_turn`` / ``start_turn`` / ``complete_turn``
    / ``resolve_recovery`` / ``update_session_fields`` / ``get_active_turn``)
    RAISE these typed errors instead of swallowing a losing predicate the way
    legacy ``complete_task`` / ``fail_task`` do.
  * Every typed error carries the HTTP-ish status code the design's §6 table maps
    it to (401/403/404/409/413/422/429/503). Internal producers read the code;
    the control API turns it into an HTTP response — this module builds NO HTTP
    objects (design §6: "Internal producers get the corresponding typed
    outcome, not HTTP objects").

Nothing here is wired into admission/scheduler/sender/UI — those are Stages 4-6
and stay out of scope. This is the schema + transaction contract layer only.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Managed protocol-1 turn states (design §3 state machine)
# --------------------------------------------------------------------------- #
#   queued --activate--> pending --claim--> claimed --start--> running
#      |                     |                 |                  |
#   withdrawn                +----------- terminal outcome -------+
#                                              |
#                                        recovery_required
QUEUE_PROTOCOL_LEGACY = 0
QUEUE_PROTOCOL_MANAGED = 1

# Non-terminal states that hold a session's single active slot (design §3 index
# ``idx_mesh_turns_one_active_session``). recovery_required is INTENTIONALLY
# here — it is NOT terminal and retains the slot until quiescence is proven.
ACTIVE_SLOT_STATUSES = ("pending", "claimed", "running", "recovery_required")

# Every non-terminal managed state (adds the pre-activation 'queued').
OPEN_STATUSES = ("queued",) + ACTIVE_SLOT_STATUSES

# Terminal outcomes (design §3). recovery_required is deliberately absent.
TERMINAL_STATUSES = (
    "completed",
    "failed",
    "cancelled",
    "failed_node_offline",
    "withdrawn",
)


# --------------------------------------------------------------------------- #
# Typed errors — internal producers get these; the API layer maps .status_code
# to an HTTP response (design §6). No HTTP objects are constructed here.
# --------------------------------------------------------------------------- #
class TurnQueueError(Exception):
    """Base for every managed-path typed failure. Carries a stable status code.

    ``status_code`` mirrors the design §6 mapping so a producer can branch on the
    integer without importing FastAPI. ``detail`` is a bounded human string.
    ``code`` is a stable machine token for structured logging/telemetry.
    """

    status_code: int = 409
    code: str = "turn_queue_error"

    def __init__(self, detail: str = "", **context: Any) -> None:
        self.detail = detail or self.__class__.__name__
        self.context: Dict[str, Any] = context
        super().__init__(self.detail)


class InvalidCredentialError(TurnQueueError):
    """401 — invalid credential (design §6)."""

    status_code = 401
    code = "invalid_credential"


class ScopeForbiddenError(TurnQueueError):
    """403 — a valid credential with a disallowed scope/target (design §6)."""

    status_code = 403
    code = "scope_forbidden"


class TurnNotFoundError(TurnQueueError):
    """404 — unknown / inaccessible resource per existing access policy."""

    status_code = 404
    code = "turn_not_found"


class OwnershipConflictError(TurnQueueError):
    """409 — state / revision / idempotency mismatch, a losing ownership or
    claim-token predicate, or missing recovery evidence (design §6).

    This is the typed conflict the MANAGED SDK send returns on a busy session
    lock (A82 §15 decision 1) and that the strict DB helpers raise when their
    compare-and-swap predicate loses. It NEVER triggers ``cancel_inflight``.
    """

    status_code = 409
    code = "ownership_conflict"


class RecoveryRequiredError(OwnershipConflictError):
    """409 — the managed backend outcome is uncertain (design §3.3: uncertainty
    retains the active slot). Raised by the managed SDK path when a result cannot
    be correlated to the managed query, or the turn deadline expires without a
    terminal result. The carrier must hold the session (``recovery_required``);
    it is NEVER a reason to interrupt the backend or to report success."""

    code = "recovery_required"


class ByteCapError(TurnQueueError):
    """413 — byte cap exceeded (design §8)."""

    status_code = 413
    code = "byte_cap"


class MalformedTurnError(TurnQueueError):
    """422 — malformed model data (design §6)."""

    status_code = 422
    code = "malformed_turn"


class CapacityError(TurnQueueError):
    """429 — capacity / rate limit (design §8)."""

    status_code = 429
    code = "capacity"


class BackingStoreError(TurnQueueError):
    """503 — DB unavailable / deadline exceeded (design §6/§8). Fails closed."""

    status_code = 503
    code = "backing_store"


# --------------------------------------------------------------------------- #
# Request / result models (Pydantic v2). These are the strict validation seam
# for the strict DB helpers; they are NOT the public HTTP DTOs (Stage 6).
# --------------------------------------------------------------------------- #
class ClaimToken(str):
    """The fresh opaque claim token returned by ``MeshDB.claim_turn``.

    A ``str`` subclass so it compares equal to the persisted ``claim_token``
    column and can be passed straight back as ``claim_token=`` to start/complete/
    release — while ALSO carrying the authoritative claim metadata the carrier
    needs (design §6: task + carrier kind/process incarnation + session + the
    frozen execution payload). Stage 3 freezes the payload at activation; Stage 2
    surfaces the current row payload.
    """

    task_id: str
    session_id: Optional[str]
    node_id: str
    carrier_kind: str
    incarnation_id: Optional[str]
    status: str
    payload: Dict[str, Any]

    def __new__(
        cls,
        token: str,
        *,
        task_id: str,
        node_id: str,
        carrier_kind: str,
        session_id: Optional[str] = None,
        incarnation_id: Optional[str] = None,
        status: str = "claimed",
        payload: Optional[Dict[str, Any]] = None,
    ) -> "ClaimToken":
        self = super().__new__(cls, token)
        self.task_id = task_id
        self.session_id = session_id
        self.node_id = node_id
        self.carrier_kind = carrier_kind
        self.incarnation_id = incarnation_id
        self.status = status
        self.payload = payload or {}
        return self


class StartAuthorization(BaseModel):
    """Result of a ``claimed -> running`` start authorization (design §6).

    An exact repeated start with the SAME live token returns an EQUAL
    authorization (idempotent), never a second executor. Equality is by value so
    a caller can compare two authorizations directly.
    """

    model_config = {"extra": "forbid"}

    task_id: str
    claim_token: str
    started_at: str
    status: str = "running"


class CompletionResult(BaseModel):
    """Result of an atomic managed completion (design §6, A82 §15 decision 2).

    Terminal status + native session id + active identity are committed in ONE
    transaction. An identical repeated completion for the current token returns
    an EQUAL result (idempotent, OWN06); a superseded/foreign token raises
    ``OwnershipConflictError``.

    Note: the outcome carries NO "was this a replay" marker precisely because a
    replay must be INDISTINGUISHABLE from the first commit (byte-equal result),
    which is the idempotency guarantee. A caller that needs to know whether it
    re-committed reads the row state (already-terminal), not this object.
    """

    model_config = {"extra": "forbid"}

    task_id: str
    status: str
    native_session_id: Optional[str] = None


class RecoveryResolution(BaseModel):
    """Result of an operator recovery resolution (design §6).

    Requires the current token PLUS recorded authenticated quiescence evidence
    (or a durable terminal result). A bare boolean / offline label is refused
    with ``OwnershipConflictError`` (409, missing evidence).
    """

    model_config = {"extra": "forbid"}

    task_id: str
    resolved_status: str
