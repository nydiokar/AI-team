"""A82 Stage 4a — transport-neutral managed admission service (design §4, §8).

One entry for every producer that targets an ENROLLED session. It adds the
process-level service bounds around the strict ``MeshDB.enqueue_turn``
transaction (which itself enforces idempotency, enrollment, count/byte caps and
commit-before-acknowledge):

  * **Admission concurrency** — at most ``MAX_CONCURRENT_ADMISSIONS`` (4)
    executing queue mutations process-wide, a ``threading`` permit so it holds
    across the gateway's control-API and task-server event loops. Excess is
    refused at once with a typed 429 (``retry_after``). The permit is taken and
    released INSIDE the worker thread, so cancelling the awaiting coroutine can
    never release it while the DB work is still running.
  * **Shared waiting allowance** — ``config.system.max_queue_size`` is ONE fleet
    allowance for legacy (in-memory ``SessionTaskQueue``) + managed (DB
    queued+pending) work in one gateway. ``SharedWaitingAllowance`` makes the
    two admission sides see each other: a managed admission reserves a slot
    (visible to legacy puts) and passes the legacy occupancy into the DB
    transaction; a legacy put counts managed rows + in-flight reservations.

No HTTP objects here: callers receive typed ``TurnQueueError`` outcomes.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Generator, Optional

from pydantic import BaseModel, Field

from .turn_queue import BackingStoreError, CapacityError, TurnAdmission

logger = logging.getLogger(__name__)

MAX_CONCURRENT_ADMISSIONS = 4
_ADMISSION_PERMITS = threading.BoundedSemaphore(MAX_CONCURRENT_ADMISSIONS)


class SharedWaitingAllowance:
    """The single legacy+managed waiting allowance of one gateway process.

    Invariant: ``managed_cache`` never UNDER-counts committed managed waiting
    rows (it is raised under the lock in the same step that releases the
    admitting reservation, and a DB refresh is applied only when no admission
    raced it). Every legacy put therefore sees committed managed rows plus
    in-flight reservations, and every managed admission sees the legacy queue.
    The lock only guards integer arithmetic — never I/O."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._reserved = 0
        self._managed_cache = 0
        self._generation = 0
        self._legacy_probe: Optional[Callable[[], int]] = None

    def register_legacy_probe(self, probe: Optional[Callable[[], int]]) -> None:
        with self.lock:
            self._legacy_probe = probe

    def legacy_waiting(self) -> int:
        probe = self._legacy_probe
        if probe is None:
            return 0
        try:
            return max(0, int(probe()))
        except Exception:  # noqa: BLE001 — a broken probe must not admit blindly
            logger.warning("event=turn_admission_legacy_probe_failed", exc_info=True)
            raise CapacityError("legacy queue occupancy unavailable", retry_after=1)

    def legacy_blocked(self, legacy_qsize: int, cap: int) -> bool:
        """Called by the legacy queue UNDER ``self.lock`` before a put."""
        if cap <= 0:
            return False
        return legacy_qsize + self._managed_cache + self._reserved >= cap

    @contextmanager
    def reserve(self, cap: int) -> Generator[Dict[str, int], None, None]:
        """Reserve one waiting slot for a managed admission. Yields
        ``{"legacy": n}`` (legacy occupancy at reservation) for the DB
        transaction; the caller sets ``slot["committed"] = 1`` after a NEW row
        committed so the cache is raised in the same critical section that
        drops the reservation."""
        with self.lock:
            legacy = self.legacy_waiting()
            if legacy + self._managed_cache + self._reserved >= cap:
                raise CapacityError(
                    "fleet waiting capacity reached (shared legacy+managed allowance)",
                    legacy_waiting=legacy, managed_waiting=self._managed_cache,
                    reserved=self._reserved, cap=cap, retry_after=1,
                )
            self._reserved += 1
        slot: Dict[str, int] = {"legacy": legacy, "committed": 0}
        try:
            yield slot
        finally:
            with self.lock:
                if slot.get("committed"):
                    self._managed_cache += 1
                    self._generation += 1
                self._reserved -= 1

    def snapshot_generation(self) -> int:
        with self.lock:
            return self._generation

    def refresh_managed(self, db_count: int, generation: int) -> bool:
        """Apply a DB-read managed waiting count, unless an admission committed
        or is in flight since ``generation`` was taken (then the read may be
        stale-low; keep the conservative cache)."""
        with self.lock:
            if self._generation != generation or self._reserved:
                return False
            self._managed_cache = max(0, int(db_count))
            return True

    def managed_cached(self) -> int:
        with self.lock:
            return self._managed_cache


ALLOWANCE = SharedWaitingAllowance()


class AdmissionRequest(BaseModel):
    """Strict input of the admission service (not the public HTTP DTO)."""

    model_config = {"extra": "forbid", "frozen": True}

    session_id: str = Field(min_length=1, max_length=256)
    task_id: Optional[str] = Field(default=None, max_length=128)
    body: str = Field(max_length=262144)
    payload: Dict[str, Any]
    backend: Optional[str] = Field(default=None, max_length=64)
    action: str = Field(default="resume_session", max_length=64)
    turn_source: str = Field(min_length=1, max_length=64)
    turn_kind: str = Field(default="instruction", max_length=32)
    operation_id: str = Field(min_length=1, max_length=256)
    idempotency_scope: str = Field(min_length=1, max_length=512)
    admission_hash: str = Field(min_length=1, max_length=128)
    flow_run_id: Optional[str] = Field(default=None, max_length=128)
    coalesce_key: Optional[str] = Field(default=None, max_length=256)
    sender_session_id: Optional[str] = Field(default=None, max_length=256)
    machine_id: Optional[str] = Field(default=None, max_length=256)
    # Durable "lineage pending" writer token (Case lineage written after the
    # commit, then finalized under CAS); None ⇒ no post-admission lineage.
    lineage_token: Optional[str] = Field(default=None, max_length=64, repr=False)
    # [A82 Stage 4c] Producer trigger token (a Case continuation token id)
    # linked to the admitted turn in the admission txn, with its durable facts.
    producer_token: Optional[str] = Field(default=None, max_length=256)
    producer_meta: Optional[Dict[str, Any]] = None


def admit_turn(
    db: Any,
    request: AdmissionRequest,
    *,
    fleet_cap: int,
    allowance: Optional[SharedWaitingAllowance] = None,
) -> TurnAdmission:
    """Synchronous admission (run it in a worker thread). Raises typed
    ``TurnQueueError``s; returns only after the durable row committed."""
    if not _ADMISSION_PERMITS.acquire(blocking=False):
        raise CapacityError(
            "admission concurrency limit reached", limit=MAX_CONCURRENT_ADMISSIONS,
            retry_after=1,
        )
    try:
        shared = allowance if allowance is not None else ALLOWANCE
        with shared.reserve(fleet_cap) as slot:
            admission: TurnAdmission = db.enqueue_turn(
                task_id=request.task_id,
                session_id=request.session_id,
                backend=request.backend,
                action=request.action,
                payload=request.payload,
                body=request.body,
                operation_id=request.operation_id,
                idempotency_scope=request.idempotency_scope,
                admission_hash=request.admission_hash,
                turn_source=request.turn_source,
                turn_kind=request.turn_kind,
                flow_run_id=request.flow_run_id,
                coalesce_key=request.coalesce_key,
                sender_session_id=request.sender_session_id,
                machine_id=request.machine_id,
                lineage_token=request.lineage_token,
                producer_token=request.producer_token,
                producer_meta=request.producer_meta,
                require_enrolled=True,
                external_waiting=slot["legacy"],
                fleet_cap=fleet_cap,
            )
            if not admission.idempotent_replay:
                slot["committed"] = 1
        return admission
    finally:
        _ADMISSION_PERMITS.release()


async def admit_turn_async(
    db: Any,
    request: AdmissionRequest,
    *,
    fleet_cap: int,
    allowance: Optional[SharedWaitingAllowance] = None,
) -> TurnAdmission:
    """Offload the bounded admission transaction from the event loop."""
    return await asyncio.to_thread(
        admit_turn, db, request, fleet_cap=fleet_cap, allowance=allowance,
    )


def _read_marker(db: Any, session_id: str) -> bool:
    try:
        return bool(db.is_session_enrolled(session_id))
    except Exception as e:
        raise BackingStoreError(
            f"turn-queue enrollment marker unreadable: {e}", session_id=session_id,
        )


async def session_enrollment(db: Any, session_id: str) -> bool:
    """[A82 Stage 4a rework] Is ``session_id`` enrolled in the managed queue?

    No mesh DB, or the process-level presence flag says NO session is enrolled
    ⇒ False with NO marker read (the legacy path behaves exactly as before).
    Otherwise ONE marker read, offloaded from the event loop; an unreadable
    marker then FAILS CLOSED (typed 503) — an enrolled session must never get
    unmanaged execution because the marker could not be read."""
    if db is None:
        return False
    if db.any_session_enrolled() is False:
        return False
    return await asyncio.to_thread(_read_marker, db, session_id)


def session_enrollment_sync(db: Any, session_id: str) -> bool:
    """[A82 Stage 4b] Synchronous twin of :func:`session_enrollment` for sync
    callers (session close, operator stop/cancel). Same contract: no DB or no
    enrollment anywhere ⇒ False with NO marker read (legacy byte-identical);
    otherwise one marker read; unreadable ⇒ typed 503 (fail closed)."""
    if db is None:
        return False
    if db.any_session_enrolled() is False:
        return False
    return _read_marker(db, session_id)
