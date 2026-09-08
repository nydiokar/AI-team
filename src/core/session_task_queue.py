"""Bounded FIFO queue that leaves busy Codex sessions pending, without using a worker."""
import asyncio
from collections.abc import Callable

from src.core.interfaces import Task


class SessionTaskQueue(asyncio.Queue[Task]):
    def __init__(self, maxsize: int, key: Callable[[Task], str]) -> None:
        super().__init__(maxsize=maxsize)
        self._key = key
        self._owned: dict[str, str] = {}
        self._keys: dict[str, str] = {}
        self._changed = asyncio.Event()

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
