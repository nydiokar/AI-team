"""A82 Stage 3 — managed (protocol-1) result spool for the carrier/worker.

Design §6 / packet §7: a managed turn's result is a **completed delivery
obligation**, not queued execution intent. It must be persisted to a bounded,
carrier-local store BEFORE the result POST, replayed on boot and after transient
failures, and removed ONLY on a durable accepted/stale **receipt that matches the
task AND claim token** — never merely because an HTTP timeout elapsed or any 2xx
arrived. Oversized results and disk failures leave a VISIBLE recovery obligation;
they never truncate-and-claim-success.

Initial bounds (design §6):
  * 8 MiB serialized envelope per result;
  * 128 MiB retained envelopes per carrier;
  * 2 concurrent result-delivery requests (enforced by the caller's semaphore).

Storage: under carrier state (``<state_dir>/managed_result_spool``), each envelope
a file ``<task_id>.<claim_token>.json`` written 0600 via atomic temp+replace. Only
VALIDATED task/token identifiers are used for paths (never client-supplied paths).

This module is deliberately transport-free: it knows nothing about HTTP. The
worker reserves an envelope allowance before start, spools before POST, and calls
:meth:`prune_on_receipt` only with a receipt it has independently parsed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("worker.managed_result_spool")

# --- bounds (design §6) --------------------------------------------------- #
MAX_ENVELOPE_BYTES = 8 * 1024 * 1024          # 8 MiB per serialized result
MAX_RETAINED_BYTES = 128 * 1024 * 1024        # 128 MiB retained per carrier
MAX_CONCURRENT_DELIVERIES = 2                  # informational; caller enforces
MAX_DEAD_LETTERS = 256                         # m3: bounded dead-letter count

# Validated identifier syntax for spool paths — reject anything that could
# escape the spool dir or collide. Task/token ids are server-minted opaque
# strings; a defensive whitelist is cheap insurance against a poisoned payload.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


class ResultSpoolError(RuntimeError):
    """A spool operation could not be completed durably (disk full, oversize,
    invalid id). NEVER swallowed into a false 'accepted' — the caller must hold
    the ownership obligation visible for recovery."""


class OversizeResultError(ResultSpoolError):
    """The serialized result exceeds the per-envelope byte cap. The full backend
    artifact is preserved elsewhere; the spool keeps only a bounded reference and
    the ownership hold — never a truncated success."""


@dataclass(frozen=True)
class SpoolReservation:
    """An allowance to spool exactly one result envelope of up to ``nbytes``.

    Reserved BEFORE the backend turn starts (design §6: "reserve one envelope
    allowance before start; if reservation is unavailable, leave pending rather
    than run and discard"). Holds no OS resource — it is a byte-budget grant the
    caller redeems with :meth:`ManagedResultSpool.commit`.
    """

    task_id: str
    claim_token: str
    nbytes: int


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a completed rename survives power loss (m4)."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(dir_path: Path, final: Path, body: Dict[str, Any]) -> None:
    """0600 temp + fsync + atomic replace + dir fsync."""
    dir_path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dir_path, 0o700)
    except OSError:
        pass
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(dir_path), prefix=".spool-", suffix=".tmp")
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, final)
    _fsync_dir(dir_path)


def _clean_orphan_tmps(dir_path: Path) -> int:
    """Remove ``.spool-*.tmp`` left by a crash mid-write (never a committed
    envelope — those are only visible after the atomic rename). Returns count."""
    n = 0
    if not dir_path.exists():
        return 0
    for p in dir_path.glob(".spool-*.tmp"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def _rotate(paths: List[Path], after: Optional[str]) -> List[Path]:
    """[M2] Start AFTER the given id (task id prefix of the file name) and wrap
    around — bounded batches then cycle through every record, so a stuck head
    can never starve later ones."""
    if not after:
        return paths
    split = next((i for i, p in enumerate(paths) if p.name.split(".", 1)[0] > after), len(paths))
    return paths[split:] + paths[:split]


def _validate_id(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.match(value):
        raise ResultSpoolError(f"invalid {kind} for spool path: {value!r}")
    return value


class ManagedResultSpool:
    """Bounded carrier-local spool of managed result envelopes.

    Thread-affinity: intended to be driven from the worker's asyncio loop thread;
    file operations are synchronous and cheap. It keeps no in-memory copy of
    envelope bodies beyond the current operation (design §8: worker spool contents
    are not all loaded in memory).
    """

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        max_envelope_bytes: int = MAX_ENVELOPE_BYTES,
        max_retained_bytes: int = MAX_RETAINED_BYTES,
    ) -> None:
        self.dir = Path(state_dir) / "managed_result_spool"
        self.max_envelope_bytes = int(max_envelope_bytes)
        self.max_retained_bytes = int(max_retained_bytes)
        # [A82 Stage 3 rework, M2] Outstanding pre-start reservations, keyed by
        # (task_id, claim_token). Counted against the retained budget so N
        # concurrent turns cannot each pass the check and then overrun it.
        self._reserved: Dict[Tuple[str, str], int] = {}

    # --- filesystem helpers ---------------------------------------------- #
    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def _path(self, task_id: str, claim_token: str) -> Path:
        return self.dir / f"{_validate_id('task_id', task_id)}.{_validate_id('claim_token', claim_token)}.json"

    @property
    def dead_dir(self) -> Path:
        return self.dir.parent / "managed_result_dead"

    def _retained_bytes(self) -> int:
        total = 0
        for d in (self.dir, self.dead_dir):
            if not d.exists():
                continue
            for p in d.glob("*.json"):
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    def clean_orphan_tmps(self) -> int:
        """Boot hygiene (m4): drop crash-orphaned temp files; returns count."""
        return _clean_orphan_tmps(self.dir)

    def discard(self, task_id: str, claim_token: str) -> None:
        """Drop a spooled envelope whose attempt the server definitively refused
        and whose recovery hold is acknowledged (it backs no obligation)."""
        try:
            self._path(task_id, claim_token).unlink(missing_ok=True)
            _fsync_dir(self.dir)
        except OSError:
            logger.warning("event=managed_spool_discard_failed task_id=%s", task_id)

    def retire_dead_letter(self, task_id: str, claim_token: str) -> None:
        """[M2] Exit for a dead letter: once the server has ACKNOWLEDGED the
        attempt's recovery hold, the refused envelope no longer backs any
        obligation — delete it (it leaves the retained budget). The small
        ``.reason`` record stays as the audit trace, itself bounded."""
        try:
            (self.dead_dir / self._path(task_id, claim_token).name).unlink(missing_ok=True)
            reasons = sorted(self.dead_dir.glob("*.json.reason"), key=lambda p: p.stat().st_mtime)
            for old in reasons[:-MAX_DEAD_LETTERS]:
                old.unlink(missing_ok=True)
        except OSError:
            logger.warning("event=managed_dead_letter_retire_failed task_id=%s", task_id)

    def dead_letter(self, task_id: str, claim_token: str, reason: str) -> bool:
        """[m3] Move an envelope the server DEFINITIVELY refused (4xx) out of the
        replay queue into a bounded dead-letter dir (it still counts against the
        retained budget, so a pile-up stops new managed claims rather than
        growing without bound). Returns False if the dead-letter dir is full
        (the envelope then stays in the spool — never deleted)."""
        src = self._path(task_id, claim_token)
        if not src.exists():
            return True
        try:
            self.dead_dir.mkdir(parents=True, exist_ok=True)
            if len(list(self.dead_dir.glob("*.json"))) >= MAX_DEAD_LETTERS:
                logger.error("event=managed_dead_letter_full task_id=%s", task_id)
                return False
            os.replace(src, self.dead_dir / src.name)
            _atomic_write_json(
                self.dead_dir, self.dead_dir / (src.name + ".reason"),
                {"task_id": task_id, "reason": (reason or "")[:500]},
            )
            _fsync_dir(self.dir)
        except OSError:
            logger.warning("event=managed_dead_letter_failed task_id=%s", task_id, exc_info=True)
            return False
        return True

    # --- reservation (design §6: reserve BEFORE start) ------------------- #
    def reserve(self, task_id: str, claim_token: str, nbytes: int) -> Optional[SpoolReservation]:
        """Reserve a single-envelope allowance, or ``None`` if unavailable.

        Returns ``None`` (leave the turn PENDING, do not run) when either the
        estimated size exceeds the per-envelope cap or the retained budget cannot
        fit it. Never raises for a full spool — a missing reservation is a normal
        back-pressure signal, not an error.
        """
        _validate_id("task_id", task_id)
        _validate_id("claim_token", claim_token)
        nbytes = max(0, int(nbytes))
        if nbytes > self.max_envelope_bytes:
            return None
        key = (task_id, claim_token)
        if key in self._reserved:
            # Idempotent for the same attempt (a repeated start of one claim).
            return SpoolReservation(task_id=task_id, claim_token=claim_token, nbytes=self._reserved[key])
        # Reserve against the retained footprint PLUS every outstanding
        # reservation plus this envelope.
        outstanding = sum(self._reserved.values())
        if self._retained_bytes() + outstanding + max(nbytes, 1) > self.max_retained_bytes:
            return None
        self._reserved[key] = max(nbytes, 1)
        return SpoolReservation(task_id=task_id, claim_token=claim_token, nbytes=nbytes)

    def release_reservation(self, task_id: str, claim_token: str) -> None:
        """Return an unused reservation (the turn never started / was released)."""
        self._reserved.pop((task_id, claim_token), None)

    def reserved_bytes(self) -> int:
        return sum(self._reserved.values())

    # --- commit (spool BEFORE the result POST) --------------------------- #
    def commit(self, task_id: str, claim_token: str, envelope: Dict[str, Any]) -> Path:
        """Durably write the result envelope 0600 via atomic temp+replace.

        Raises :class:`OversizeResultError` if the serialized envelope exceeds the
        per-envelope cap (the caller preserves the full artifact elsewhere and
        holds the recovery obligation), or :class:`ResultSpoolError` on a disk
        write failure (a visible recovery obligation — never a false ack).
        """
        body = {
            "task_id": _validate_id("task_id", task_id),
            "claim_token": _validate_id("claim_token", claim_token),
            "envelope": envelope,
        }
        # m3: size with the SAME serialization the carrier puts on the wire
        # (`_HTTP.post` = `json.dumps(body)`: ASCII escapes, default separators),
        # so an envelope that fits here also fits the server's byte cap.
        wire_bytes = len(json.dumps(envelope).encode("utf-8"))
        if wire_bytes > self.max_envelope_bytes:
            raise OversizeResultError(
                f"managed result envelope {wire_bytes}B exceeds cap "
                f"{self.max_envelope_bytes}B (task={task_id})"
            )
        data = json.dumps(body).encode("utf-8")
        try:
            self._ensure_dir()
        except OSError as e:
            raise ResultSpoolError(f"failed to create spool dir for {task_id}: {e}") from e
        final = self._path(task_id, claim_token)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".spool-", suffix=".tmp")
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)
            os.replace(tmp, final)  # atomic on POSIX
            _fsync_dir(self.dir)  # m4: make the rename itself durable
            # The envelope now occupies retained bytes; drop its reservation.
            self._reserved.pop((task_id, claim_token), None)
        except OSError as e:
            # Disk-write failure leaves a visible recovery obligation; it CANNOT
            # produce a successful ack (design §6).
            raise ResultSpoolError(f"failed to spool managed result for {task_id}: {e}") from e
        return final

    # --- receipt-matched pruning (NEVER on bare timeout/2xx) ------------- #
    @staticmethod
    def receipt_matches(receipt: Any, task_id: str, claim_token: str) -> bool:
        """True iff ``receipt`` is a durable accepted/stale receipt that matches
        BOTH the task id and the claim token (design §6).

        A bare HTTP timeout, a 2xx with no body, or a receipt for a different
        task/token does NOT match — the spooled envelope survives for replay.
        """
        if not isinstance(receipt, dict):
            return False
        status = str(receipt.get("status", "")).lower()
        if status not in ("accepted", "accepted (stale)", "stale", "acknowledged"):
            return False
        if str(receipt.get("task_id", "")) != str(task_id):
            return False
        # Token match is mandatory: a receipt must prove it acknowledged THIS
        # attempt, not merely that the task id is terminal from another attempt.
        return str(receipt.get("claim_token", "")) == str(claim_token)

    def prune_on_receipt(self, task_id: str, claim_token: str, receipt: Any) -> bool:
        """Remove the spooled envelope ONLY if ``receipt`` matches task+token.

        Returns True if a matching receipt pruned the envelope; False otherwise
        (the envelope is retained for replay). Idempotent — a missing file is a
        no-op success once the receipt matched.
        """
        if not self.receipt_matches(receipt, task_id, claim_token):
            return False
        try:
            self._path(task_id, claim_token).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "event=managed_spool_prune_failed task_id=%s (retained for replay)",
                task_id,
            )
            return False
        return True

    # --- boot / transient-failure replay --------------------------------- #
    def list_spooled(
        self, limit: Optional[int] = None, after: Optional[str] = None
    ) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Return ``(task_id, claim_token, envelope)`` for every retained result.

        Reads one envelope at a time (bounded replay batches — the caller iterates
        and delivers). A malformed/foreign file is skipped, not fatal.
        """
        out: List[Tuple[str, str, Dict[str, Any]]] = []
        if not self.dir.exists():
            return out
        for p in _rotate(sorted(self.dir.glob("*.json")), after):
            if limit is not None and len(out) >= limit:
                break
            try:
                body = json.loads(p.read_text(encoding="utf-8"))
                tid = str(body["task_id"])
                tok = str(body["claim_token"])
                env = body["envelope"]
            except Exception:
                logger.warning("event=managed_spool_unreadable path=%s (skipped)", p.name)
                continue
            out.append((tid, tok, env))
        return out


class ManagedClaimStore:
    """[A82 Stage 3 rework, B2] Durable record of every managed attempt this
    carrier holds, written AT CLAIM TIME (before start) under carrier state
    (``<state_dir>/managed_claims/<task_id>.json``, 0600, atomic + dir fsync)
    and removed ONLY on a durable receipt / acknowledged release / resolved
    recovery. It survives a crash so a restarted carrier can always move every
    held row (no token lives only in memory).

    ``invoked`` is a WRITE-AHEAD flag: set durably BEFORE the backend is
    called, so ``invoked == False`` is proof the backend never ran for that
    attempt."""

    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.dir = Path(state_dir) / "managed_claims"

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{_validate_id('task_id', task_id)}.json"

    def put(self, task_id: str, record: Dict[str, Any]) -> None:
        _validate_id("claim_token", str(record.get("claim_token", "")))
        try:
            _atomic_write_json(self.dir, self._path(task_id), {**record, "task_id": task_id})
        except OSError as e:
            raise ResultSpoolError(f"failed to persist managed claim {task_id}: {e}") from e

    def update(self, task_id: str, **fields: Any) -> Dict[str, Any]:
        rec = self.get(task_id) or {}
        rec.update(fields)
        self.put(task_id, rec)
        return rec

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self._path(task_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def remove(self, task_id: str) -> None:
        try:
            self._path(task_id).unlink(missing_ok=True)
            _fsync_dir(self.dir)
        except OSError:
            logger.warning("event=managed_claim_remove_failed task_id=%s", task_id)

    def list(self, limit: Optional[int] = None, after: Optional[str] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.dir.exists():
            return out
        for p in _rotate(sorted(self.dir.glob("*.json")), after):
            if limit is not None and len(out) >= limit:
                break
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                logger.warning("event=managed_claim_unreadable path=%s", p.name)
        return out

    def clean_orphan_tmps(self) -> int:
        return _clean_orphan_tmps(self.dir)
