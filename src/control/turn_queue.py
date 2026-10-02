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

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


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


class ManagedUnsupportedError(TurnQueueError):
    """422 — a managed (protocol-1) turn reached a backend method that has no
    managed execution path. Raised BEFORE anything runs; never silently falls
    back to the legacy send (fail-closed)."""

    status_code = 422
    code = "managed_unsupported"


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


class CarrierUnavailableError(TurnQueueError):
    """503 — no registered managed-capable carrier for the turn's assignment
    (A82 Stage 4a rework): refused rather than queued for nobody to claim."""

    status_code = 503
    code = "carrier_unavailable"


class CarrierOfflineError(CarrierUnavailableError):
    """[A82 pre-cutover] The assigned carrier REGISTERED ``backend`` as managed
    but is offline / heart-beat stale right now. Under the offline-carrier
    admission policy the turn is admitted queued and this is its operator-
    visible ``blocked_reason`` until the carrier returns (host affinity:
    never relocated); otherwise it is a plain 503 refusal."""

    code = "carrier_offline"

    @property
    def blocked_reason(self) -> str:
        return f"carrier_offline: {self.context.get('node_id') or ''}"


class LegacyExecutionRefusedError(TurnQueueError):
    """409 — a protocol-0 (legacy) EXECUTION row for a session enrolled in the
    managed turn queue, refused at the DB insert / claim boundary (design §3
    item 2). Control rows (close / cancel) are never refused."""

    status_code = 409
    code = "legacy_execution_refused"


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


class ManagedTurnOwnership(BaseModel):
    """[A82 Stage 3 rework] The typed ownership a carrier passes to
    ``CodingBackend.run_managed_turn``: the claimed protocol-1 attempt the
    backend call executes on behalf of. The claim token is an execution
    credential — excluded from repr so it never lands in logs/tracebacks."""

    model_config = {"extra": "forbid", "frozen": True}

    task_id: str
    session_id: str
    node_id: str
    claim_token: str = Field(repr=False)
    incarnation_id: Optional[str] = None
    # [A82 Stage 3 rework 5] The carrier-chosen managed turn identity (a UUID
    # persisted write-ahead in the carrier's claim record). The backend submits
    # the prompt under it, and a late reply is bound back to EXACTLY this
    # attempt by it — never by session.
    turn_uuid: Optional[str] = None


class StagedFileRef(BaseModel):
    """[A82 pre-cutover P2] A gateway-staged upload carried by a managed turn
    (or a file-only delivery). The carrier GETs ``/files/{file_id}`` and writes
    ``<repo>/uploads/<filename>``, so both are validated as single plain path
    segments at admission — a traversal is refused (422), never stored."""

    model_config = {"extra": "forbid", "frozen": True}

    file_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    filename: str = Field(min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def _plain_file_name(cls, v: str) -> str:
        if "/" in v or "\\" in v or "\x00" in v or not v.strip(". "):
            raise ValueError("filename must be a plain file name")
        return v


class SenderIdentity(BaseModel):
    """[A82 Stage 5] Server-validated agent sender, derived from the scoped
    capability's canonical binding; never contains the bearer capability."""

    model_config = {"extra": "forbid", "frozen": True}

    session_id: str
    case_id: str
    role: str
    # [A82 Stage 5 rework] sha256 of the validated capability (never the raw
    # secret) so the admission txn re-selects the SAME live capability row.
    capability_hash: Optional[str] = Field(default=None, max_length=128, repr=False, exclude=True)


class SenderCapabilityGrant(BaseModel):
    """[A82 Stage 5] The sender binding a carrier receives in its PRIVATE managed
    claim response. ``token`` is the raw capability only when one was minted
    for this claim (``None`` ⇒ the carrier's held generation is still current).
    The secret is excluded from repr; the DB stores only its hash."""

    model_config = {"extra": "forbid", "frozen": True}

    session_id: str
    case_id: str
    role: str
    generation: int
    operations: List[str] = Field(default_factory=lambda: ["send_instruction"])
    token: Optional[str] = Field(default=None, repr=False)


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


# --------------------------------------------------------------------------- #
# A82 Stage 4b — producer 2: operator cancel of the ACTIVE managed turn, and
# session close with managed rows.
# --------------------------------------------------------------------------- #
# Protocol-0 control action that carries a cancel request to the carrier that
# holds the attempt (outside the turn slot, like close_session/cancel_codex).
CANCEL_MANAGED_ACTION = "cancel_managed"


class TurnCancelOutcome(BaseModel):
    """Result of ``MeshDB.request_turn_cancel`` (one transaction).

    ``outcome``:
      * ``cancelled``     — pending (unclaimed) or claimed-not-started: made
                            terminal ``cancelled`` directly (no backend ran);
      * ``requested``     — running / recovery_required: the cancel is recorded
                            against the CURRENT attempt token and a control row
                            ``control_task_id`` was delivered to ``node_id``; the
                            attempt's own failed/interrupted result commits as
                            ``cancelled``;
      * ``already_terminal`` / ``not_active`` (queued) — nothing changed.
    """

    model_config = {"extra": "forbid"}

    task_id: str
    outcome: str
    status: str
    node_id: Optional[str] = None
    control_task_id: Optional[str] = None


class SessionCloseTurns(BaseModel):
    """Result of ``MeshDB.close_session_turns``: the session is durably closed
    and every queued row withdrawn in ONE transaction. ``withdrawn`` lists the
    withdrawn turn ids; ``active_task_id`` is the slot holder (if any), which
    the caller then cancels through the fenced path."""

    model_config = {"extra": "forbid"}

    session_id: str
    withdrawn: list[str] = Field(default_factory=list)
    active_task_id: Optional[str] = None


# --------------------------------------------------------------------------- #
# A82 Stage 4a — admission bounds (design §8) + the admission result.
# --------------------------------------------------------------------------- #
# Per-session waiting cap (queued + pending managed rows of one session).
PER_SESSION_WAITING_CAP = 20
# Stored waiting-intent byte caps (persisted `intent_bytes`, design §8).
MAX_INTENT_BYTES_PER_ROW = 2 * 1024 * 1024
MAX_INTENT_BYTES_FLEET = 100 * 1024 * 1024
# Queue-mutation total lock/DB deadline (design §8 "Time").
ADMISSION_DEADLINE_SEC = 5.0
# Statuses that occupy the fleet waiting allowance (design §8: queued + pending).
WAITING_STATUSES = ("queued", "pending")


class TurnAdmission(str):
    """The acknowledged managed admission: a ``str`` equal to the turn id (so it
    can be passed straight to ``get_task`` and compared across replays, like
    ``ClaimToken``), carrying the committed row summary.

    ``admission["id"]`` / ``["status"]`` / ``["revision"]`` /
    ``["queue_sequence"]`` / ``["idempotent_replay"]`` / ``["coalesced"]`` keep
    the Stage-2 mapping shape; any other subscript is ordinary ``str`` indexing.
    Only ever constructed AFTER the admitting transaction committed."""

    _FIELDS = ("id", "status", "revision", "queue_sequence", "idempotent_replay", "coalesced",
               "lineage_pending")

    id: str
    status: str
    revision: int
    queue_sequence: Optional[int]
    idempotent_replay: bool
    coalesced: bool
    lineage_pending: bool

    def __new__(
        cls,
        task_id: str,
        *,
        status: str,
        revision: int,
        queue_sequence: Optional[int],
        idempotent_replay: bool,
        coalesced: bool = False,
        lineage_pending: bool = False,
    ) -> "TurnAdmission":
        self = super().__new__(cls, task_id)
        self.id = task_id
        self.status = status
        self.revision = int(revision)
        self.queue_sequence = queue_sequence
        self.idempotent_replay = idempotent_replay
        self.coalesced = coalesced
        self.lineage_pending = lineage_pending
        return self

    def __getitem__(self, key: Any) -> Any:  # type: ignore[override]
        if isinstance(key, str) and key in self._FIELDS:
            return getattr(self, key)
        return super().__getitem__(key)


# --------------------------------------------------------------------------- #
# [A82 Stage 4e] Producer 5 — the fixed A/B/R retry rule (design §7, packet §3.9)
# --------------------------------------------------------------------------- #
#: Only a turn that genuinely RAN and FAILED is retried automatically. Not
#: ``failed_node_offline`` (the carrier vanished — the backend may have executed),
#: not ``cancelled`` (operator stop), not ``withdrawn`` (obsolete / closed / the
#: operator withdrew it), not an open / ``recovery_required`` turn (uncertain).
RETRYABLE_FAILED_STATUSES = ("failed",)


class RetryDecision(BaseModel):
    """The A/B/R decision for failed turn A once its automatic pause is
    eligible to end. ``supersede`` releases ONLY that producer's pause and
    leaves the earlier-accepted real instruction B as head; ``retry`` admits R
    as head (the pause is closed at R's terminal commit, not now); ``drop``
    releases the pause without a retry (A is not retryable); ``wait`` changes
    nothing (pause not yet eligible, or another hold applies)."""

    model_config = {"extra": "forbid", "frozen": True}

    action: str
    supersede_retry: bool = False
    admit_retry: bool = False
    release_pause: bool = False
    head: Optional[str] = None
    reason: str = ""


def decide_retry(
    *,
    failed_task_id: str,
    earlier_waiting: Any,
    pause_eligible: bool,
    failed_status: str = "failed",
    held: bool = False,
) -> RetryDecision:
    """Pure A/B/R rule. ``earlier_waiting`` = real (non-automation) turns B of
    the same session accepted after A and before any retry, oldest first.
    Other holds (operator stop, an ineligible pause — approval pending, a
    future quota/backoff deadline) always win: nothing is cleared."""
    if held:
        return RetryDecision(action="wait", reason="held")
    if not pause_eligible:
        return RetryDecision(action="wait", reason="pause_not_eligible")
    if str(failed_status or "") not in RETRYABLE_FAILED_STATUSES:
        return RetryDecision(
            action="drop", release_pause=True, reason=f"not_retryable:{failed_status}",
        )
    waiting = [str(b) for b in (earlier_waiting or []) if b]
    if waiting:
        return RetryDecision(
            action="supersede", supersede_retry=True, release_pause=True,
            head=waiting[0], reason="superseded_by_real_instruction",
        )
    return RetryDecision(action="retry", admit_retry=True, head=None, reason="retry_head")


# --------------------------------------------------------------------------- #
# [A82 Stage 6] Post-commit UI invalidation signal
# --------------------------------------------------------------------------- #
TURN_QUEUE_CHANGED_EVENT = "turn_queue_changed"


def emit_turn_queue_changed(
    session_id: Optional[str],
    change: str,
    *,
    turn_id: Optional[str] = None,
    status: Optional[str] = None,
) -> None:
    """Append one ``turn_queue_changed`` event AFTER a queue commit so the
    single SSE stream (A81) invalidates ``["session-turn-queue", id]`` and the
    session list. Carries ``turn_id`` (not ``task_id``) so a queue-only change
    never fans out to the task/job lists. A hint, never authority: it is
    called only once the transaction committed and never raises."""
    sid = (session_id or "").strip()
    if not sid:
        return
    try:
        from src.core.observability import emit_event

        emit_event(
            TURN_QUEUE_CHANGED_EVENT, session_id=sid, task_id=None,
            turn_id=turn_id, change=change, status=status,
        )
    except Exception:  # noqa: BLE001 — the UI safety net still converges
        pass
