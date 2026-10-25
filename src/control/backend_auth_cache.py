"""Bounded, process-local account observations; credentials stay on workers."""
from __future__ import annotations

import threading
import time
from _thread import LockType
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel


class AccountStatus(BaseModel):
    state: Literal["signed_in", "signed_out", "unavailable"]
    checked_at: str
    login_verified_at: str | None = None


class BackendAuthCache:
    """Bounded per-key locks coalesce checks without blocking unrelated nodes.

    This is an observation cache, not an auth authority or login audit ledger.
    Node incarnation/address is part of the key; restart loses all observations.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key_locks: dict[str, tuple[LockType, int]] = {}
        self._entries: OrderedDict[str, tuple[float, AccountStatus]] = OrderedDict()

    @contextmanager
    def _key_lock(self, key: str) -> Iterator[None]:
        with self._lock:
            if key not in self._key_locks:
                if len(self._key_locks) >= 256:
                    raise TimeoutError("backend_auth_check_busy")
                self._key_locks[key] = (threading.Lock(), 0)
            lock, users = self._key_locks[key]
            self._key_locks[key] = (lock, users + 1)
        acquired: bool = lock.acquire(timeout=5)
        try:
            if not acquired:
                raise TimeoutError("backend_auth_check_busy")
            yield
        finally:
            if acquired:
                lock.release()
            with self._lock:
                _, users = self._key_locks[key]
                if users == 1:
                    del self._key_locks[key]
                else:
                    self._key_locks[key] = (lock, users - 1)

    def read(self, key: str, load: Callable[[], dict[str, object]], *, refresh: bool = False) -> dict[str, object]:
        # A usable observation need not wait for a forced refresh of this key.
        if not refresh:
            with self._lock:
                entry = self._entries.get(key)
            if entry is not None and entry[0] > time.monotonic():
                return entry[1].model_dump()
        with self._key_lock(key):
            with self._lock:
                entry = self._entries.get(key)
            if entry is not None and entry[0] > time.monotonic() and not refresh:
                return entry[1].model_dump()
            result = load()
            previous = entry[1].login_verified_at if entry else None
            status = AccountStatus.model_validate({
                "state": result["state"],
                "checked_at": datetime.now(UTC).isoformat(),
                "login_verified_at": previous if result["state"] == "signed_in" else None,
            })
            self._put(key, status)
            return status.model_dump()

    def invalidate(self, key: str) -> None:
        with self._key_lock(key), self._lock:
            self._entries.pop(key, None)

    def verified(self, key: str, authenticated: bool | None) -> dict[str, object]:
        with self._key_lock(key):
            now: str = datetime.now(UTC).isoformat()
            with self._lock:
                previous = self._entries.get(key)
            verified_at = previous[1].login_verified_at if previous else None
            status = AccountStatus(
                state="signed_in" if authenticated is True else "signed_out" if authenticated is False else "unavailable",
                checked_at=now,
                login_verified_at=(verified_at or now) if authenticated is True else None,
            )
            self._put(key, status)
            return status.model_dump()

    def _put(self, key: str, status: AccountStatus) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + (30 if status.state == "unavailable" else 300), status)
            self._entries.move_to_end(key)
            while len(self._entries) > 256:
                self._entries.popitem(last=False)
