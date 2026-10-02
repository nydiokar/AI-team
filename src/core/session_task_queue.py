"""Bounded FIFO queue that leaves busy Codex sessions pending, without using a worker."""
import asyncio
from collections.abc import Callable
from typing import Any

from src.core.interfaces import Task


class SessionTaskQueue(asyncio.Queue[Task]):
    def __init__(self, maxsize: int, key: Callable[[Task], str]) -> None:
        super().__init__(maxsize=maxsize)
        self._key = key
        self._owned: dict[str, str] = {}
        self._keys: dict[str, str] = {}
        self._changed = asyncio.Event()
        # [A82 Stage 4a] Optional shared legacy+managed waiting allowance. None
        # (the default, and whenever no managed queue exists) keeps the plain
        # asyncio.Queue capacity check byte-identical.
        self._shared: Any = None

    def share_allowance(self, shared: Any) -> None:
        """Count managed waiting turns against this queue's ``maxsize`` so the
        legacy and managed paths draw on ONE allowance (design §8)."""
        self._shared = shared

    def full(self) -> bool:
        shared = self._shared
        if shared is None:
            return super().full()
        return shared.legacy_blocked(self.qsize(), self.maxsize)

    async def put(self, item: Task) -> None:
        """Unshared ⇒ plain ``asyncio.Queue.put``. Shared ⇒ retry until the ONE
        allowance has room; the waiting caller's own timeout (``wait_for``)
        bounds it, so a racing managed reservation can never surface as a raw
        ``QueueFull`` from the blocking put (managed room frees on other
        threads/loops, which do not wake asyncio putters)."""
        if self._shared is None:
            return await super().put(item)
        while True:
            try:
                return self.put_nowait(item)
            except asyncio.QueueFull:
                await asyncio.sleep(0.05)

    def put_nowait(self, item: Task) -> None:
        shared = self._shared
        if shared is None:
            return super().put_nowait(item)
        # Check + put atomically against a concurrent managed reservation.
        with shared.lock:
            if shared.legacy_blocked(self.qsize(), self.maxsize):
                raise asyncio.QueueFull
            return super().put_nowait(item)

    def _put(self, item: Task) -> None:
        self._keys[item.id] = self._key(item)
        super()._put(item)
        self._changed.set()

    def _get(self) -> Task:
        for item in self._queue:
            key = self._keys[item.id]
            if not key or key not in self._owned.values():
                self._queue.remove(item)
                self._keys.pop(item.id)
                if key:
                    self._owned[item.id] = key
                return item
        raise asyncio.QueueEmpty

    async def get(self) -> Task:
        while True:
            self._changed.clear()
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                await self._changed.wait()

    def task_done(self, task: Task | None = None) -> None:
        if task is not None:
            self._owned.pop(task.id, None)
            self._changed.set()
        super().task_done()
