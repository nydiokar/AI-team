"""[A88] Worker-side controller-state client.

The worker reads controller-owned state (runtime-flag registry, Manager boot
reconcile) from the task-server over its existing authenticated HTTP plane —
never from a mesh.db file (docs/backend/DATABASE_AUTHORITY.md).

Flags are held as an immutable last-known-good snapshot swapped atomically, so
reads are memory-only and safe from any thread or the event loop. A failed or
malformed refresh keeps the previous snapshot; a worker that never fetched one
has no registry rows, so callers fall back to env → default (the same contract
as a process without a DB).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Protocol
from urllib.error import HTTPError

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

logger = logging.getLogger(__name__)

_FLAGS_PATH = "/control/runtime-flags"
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_REQUEST_TIMEOUT_SEC = 5


class _ControllerHTTP(Protocol):
    def get(self, path: str, params: dict[str, str] | None = None, timeout: int = 10) -> object: ...

    def post(self, path: str, body: object = None, timeout: int = 10) -> object: ...


class RuntimeFlagRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    flag_name: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=32)
    set_at: str = Field(default="", max_length=64)


class RuntimeFlagSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: str = Field(min_length=1, max_length=128)
    flags: list[RuntimeFlagRow] = Field(max_length=256)


def _index(snapshot: RuntimeFlagSnapshot) -> dict[str, RuntimeFlagRow]:
    return {row.flag_name.strip().upper(): row for row in snapshot.flags}


def _changed_flags(old: dict[str, RuntimeFlagRow], new: dict[str, RuntimeFlagRow]) -> list[str]:
    return sorted(
        name for name in set(old) | set(new)
        if (old.get(name) and old[name].value) != (new.get(name) and new[name].value)
    )


class RemoteControllerState:
    """Implements ``src.control.controller_state.ControllerStateClient``."""

    def __init__(
        self,
        http: _ControllerHTTP,
        *,
        refresh_interval_sec: float = 30.0,
        retry_interval_sec: float = 5.0,
        stale_after_intervals: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self.refresh_interval_sec: float = refresh_interval_sec
        self.retry_interval_sec: float = retry_interval_sec
        self._stale_after_sec: float = refresh_interval_sec * stale_after_intervals
        self._clock = clock
        self._started_at: float = clock()
        self._snapshot: RuntimeFlagSnapshot | None = None
        self._rows: dict[str, RuntimeFlagRow] = {}
        self._fetched_at: float | None = None
        self._stale_reported: bool = False
        self._route_missing: bool = False

    @property
    def revision(self) -> str | None:
        snapshot = self._snapshot
        return snapshot.revision if snapshot is not None else None

    def runtime_flag_row(self, flag_name: str) -> dict[str, str] | None:
        row = self._rows.get((flag_name or "").strip().upper())
        if row is None:
            return None
        return {"flag_name": row.flag_name, "value": row.value, "set_at": row.set_at}

    def boot_reconcile_case(self, case_id: str) -> dict[str, JsonValue]:
        if not _CASE_ID_RE.match(case_id or ""):
            return {"ok": False, "reason": "invalid_case_id"}
        result = self._http.post(
            f"/control/cases/{case_id}/boot-reconcile", None, _REQUEST_TIMEOUT_SEC,
        )
        if not isinstance(result, dict):
            return {"ok": False, "reason": "malformed_response"}
        return result

    def refresh(self) -> bool:
        """Fetch the controller's flag snapshot once. True on success; on any
        failure the last-known-good snapshot is kept."""
        try:
            raw = self._http.get(_FLAGS_PATH, None, _REQUEST_TIMEOUT_SEC)
            snapshot = RuntimeFlagSnapshot.model_validate(raw)
        except HTTPError as exc:
            if exc.code == 404 and not self._route_missing:
                self._route_missing = True
                # Deployment ordering: this worker is newer than the task-server.
                # Logged once per episode; retries continue at the normal interval.
                logger.error(
                    "event=controller_state_route_missing path=%s — deploy the task-server "
                    "before the worker; flags fall back to last-known-good/env until then",
                    _FLAGS_PATH,
                )
            return False
        except ValidationError as exc:
            logger.warning(
                "event=controller_flags_malformed errors=%d kept_revision=%s",
                exc.error_count(), self.revision,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — a refresh failure must never kill the loop; LKG is kept
            logger.debug("event=controller_flags_refresh_failed err_class=%s", type(exc).__name__)
            return False

        rows = _index(snapshot)
        previous = self._snapshot
        if previous is None:
            logger.info(
                "event=controller_flags_refreshed revision=%s rows=%d",
                snapshot.revision, len(rows),
            )
        elif snapshot.revision != previous.revision:
            logger.info(
                "event=controller_flags_changed revision=%s previous=%s changed=%s",
                snapshot.revision, previous.revision, ",".join(_changed_flags(self._rows, rows)),
            )
        if self._stale_reported:
            logger.info("event=controller_state_recovered revision=%s", snapshot.revision)
            self._stale_reported = False
        self._route_missing = False
        # Rows before snapshot: a reader seeing the new revision sees its rows.
        self._rows = rows
        self._snapshot = snapshot
        self._fetched_at = self._clock()
        return True

    @property
    def route_missing(self) -> bool:
        return self._route_missing

    def ready_for_work(self) -> bool:
        """True once the controller's flags are known — or the controller predates
        the route (404), where waiting would stall all work; that case runs on
        env/default flags and is logged at ERROR by ``refresh``."""
        return self._snapshot is not None or self._route_missing

    def check_stale(self) -> None:
        """Log once when the snapshot (or the first fetch) is overdue."""
        if self._stale_reported:
            return
        since = self._fetched_at if self._fetched_at is not None else self._started_at
        age = self._clock() - since
        if age <= self._stale_after_sec:
            return
        self._stale_reported = True
        logger.warning(
            "event=controller_state_stale age_sec=%d revision=%s",
            int(age), self.revision or "none",
        )

    def next_delay(self) -> float:
        if self._snapshot is not None or self._route_missing:
            return self.refresh_interval_sec
        return self.retry_interval_sec

    async def run(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            await asyncio.to_thread(self.refresh)
            self.check_stale()
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=self.next_delay())
            except TimeoutError:
                pass
