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

    # --- filesystem helpers ---------------------------------------------- #
    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass

    def _path(self, task_id: str, claim_token: str) -> Path:
        return self.dir / f"{_validate_id('task_id', task_id)}.{_validate_id('claim_token', claim_token)}.json"

    def _retained_bytes(self) -> int:
        if not self.dir.exists():
            return 0
        total = 0
        for p in self.dir.glob("*.json"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

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
        # Reserve against the current retained footprint plus this envelope.
        if self._retained_bytes() + max(nbytes, 1) > self.max_retained_bytes:
            return None
        return SpoolReservation(task_id=task_id, claim_token=claim_token, nbytes=nbytes)

    # --- commit (spool BEFORE the result POST) --------------------------- #
    def commit(self, task_id: str, claim_token: str, envelope: Dict[str, Any]) -> Path:
        """Durably write the result envelope 0600 via atomic temp+replace.

        Raises :class:`OversizeResultError` if the serialized envelope exceeds the
        per-envelope cap (the caller preserves the full artifact elsewhere and
        holds the recovery obligation), or :class:`ResultSpoolError` on a disk
        write failure (a visible recovery obligation — never a false ack).
        """
        self._ensure_dir()
        body = {
            "task_id": _validate_id("task_id", task_id),
            "claim_token": _validate_id("claim_token", claim_token),
            "envelope": envelope,
        }
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > self.max_envelope_bytes:
            raise OversizeResultError(
                f"managed result envelope {len(data)}B exceeds cap "
                f"{self.max_envelope_bytes}B (task={task_id})"
            )
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
    def list_spooled(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Return ``(task_id, claim_token, envelope)`` for every retained result.

        Reads one envelope at a time (bounded replay batches — the caller iterates
        and delivers). A malformed/foreign file is skipped, not fatal.
        """
        out: List[Tuple[str, str, Dict[str, Any]]] = []
        if not self.dir.exists():
            return out
        for p in sorted(self.dir.glob("*.json")):
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
