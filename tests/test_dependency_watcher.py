"""Offline dependency contracts; no agent CLI or live service is started."""

import asyncio
from pathlib import Path

import pytest

from src.services.file_watcher import FileWatcher


@pytest.mark.parametrize("publication", ["existing", "create", "rename", "modify"])
async def test_linux_watcher_delivers_task_and_stops(
    tmp_path: Path, publication: str,
) -> None:
    """Exercise the real observer, thread handoff, debounce and cleanup."""
    target: Path = tmp_path / "example.task.md"
    if publication == "existing":
        target.write_text("task", encoding="utf-8")
    received: list[str] = []
    delivered: asyncio.Event = asyncio.Event()

    def callback(path: str) -> None:
        received.append(path)
        delivered.set()

    watcher: FileWatcher = FileWatcher(str(tmp_path))
    observer = None
    emitters = ()
    try:
        watcher.start(callback)
        observer = watcher.observer
        emitters = tuple(observer.emitters)
        assert watcher.is_running()
        if publication == "rename":
            staging: Path = tmp_path / "upload.tmp"
            staging.write_text("task", encoding="utf-8")
            staging.rename(target)
        elif publication != "existing":
            target.write_text("task", encoding="utf-8")
            if publication == "modify":
                target.write_text("updated task", encoding="utf-8")
        (tmp_path / "ignored.md").write_text("not a task", encoding="utf-8")
        await asyncio.wait_for(delivered.wait(), timeout=5)
        # Existing code can schedule several callbacks from the initial burst.
        # Once settled, processed_files must suppress subsequent modifications.
        await asyncio.sleep(0.6)
        initial_count: int = len(received)
        assert set(received) == {str(target)}
        target.write_text("duplicate modification", encoding="utf-8")
        await asyncio.sleep(0.6)
        assert len(received) == initial_count
    finally:
        watcher.stop()
    assert not watcher.is_running()
    assert observer is not None
    assert not observer.is_alive()
    assert all(not emitter.is_alive() for emitter in emitters)
