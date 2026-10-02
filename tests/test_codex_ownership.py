"""Gateway-owned Codex queue and durable-claim invariants."""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from src.backends.codex_ownership import CodexOwnership
from src.core.interfaces import Task, TaskPriority, TaskStatus, TaskType
from src.core.session_task_queue import SessionTaskQueue


def _task(name: str, session_id: str) -> Task:
    return Task(
        name,
        TaskType.ANALYZE,
        TaskPriority.MEDIUM,
        TaskStatus.PENDING,
        "",
        name,
        [],
        name,
        [],
        "",
        {"session_id": session_id},
    )


@pytest.mark.asyncio
async def test_same_session_fifo_leaves_unrelated_session_runnable() -> None:
    queue = SessionTaskQueue(4, lambda item: item.metadata["session_id"])
    first, second, third, unrelated = [
        _task(name, session_id)
        for name, session_id in (
            ("first", "a"),
            ("second", "a"),
            ("third", "a"),
            ("other", "b"),
        )
    ]
    for item in (first, second, third, unrelated):
        queue.put_nowait(item)
    assert await queue.get() is first
    assert await queue.get() is unrelated
    queue.task_done(unrelated)
    waiting = asyncio.create_task(queue.get())
    await asyncio.sleep(0)
    assert not waiting.done()
    queue.task_done(first)
    assert await asyncio.wait_for(waiting, 1) is second
    queue.task_done(second)
    assert await queue.get() is third
    queue.task_done(third)
    await queue.join()


def test_cross_process_claim_and_crash_fail_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    script = (
        "from src.backends.codex_ownership import CodexOwnership; "
        "o=CodexOwnership(); o.acquire('session','exact-thread'); print('owned')"
    )
    child = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "owned"
    with pytest.raises(RuntimeError, match="codex_thread_busy"):
        CodexOwnership().acquire("session", "")
    with pytest.raises(RuntimeError, match="codex_thread_busy"):
        CodexOwnership().acquire("alias", "exact-thread")
    other = CodexOwnership()
    assert other.acquire("other", "different-thread") == "different-thread"
    other.release()


def test_cross_process_distinct_sessions_share_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    workspace = str(tmp_path / "workspace")
    owner = CodexOwnership()
    owner.acquire("first-session", "first-thread", workspace)
    script = (
        "from src.backends.codex_ownership import CodexOwnership; "
        f"CodexOwnership().acquire('second-session', 'second-thread', {workspace!r})"
    )
    try:
        competing = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
        )
        assert competing.returncode == 0, competing.stderr
    finally:
        owner.release()


def test_live_cross_process_thread_alias_is_excluded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    owner = CodexOwnership()
    owner.acquire("session", "exact-thread")
    script = (
        "from src.backends.codex_ownership import CodexOwnership; "
        "CodexOwnership().acquire('alias','exact-thread')"
    )
    try:
        competing = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
        )
        assert competing.returncode != 0
        assert "codex_thread_busy" in competing.stderr
    finally:
        owner.release()
    assert CodexOwnership().acquire("session", "") == "exact-thread"


# --------------------------------------------------------------------------- #
# [A82 pre-cutover, m2 sweep safety] the cutover sweep refuses while ANY
# `codex app-server` using the same CODEX_HOME is alive (fake /proc root).
# --------------------------------------------------------------------------- #
def _fake_proc(root, pid: int, argv: list[str], env: dict[str, str] | None) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if env is None:
        (d / "environ").write_bytes(b"")
        (d / "environ").chmod(0)
    else:
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


def _swept_legacy_owner(tmp_path, monkeypatch):
    from src.backends.codex_ownership import CodexOwnership

    home = tmp_path / "codexhome"
    monkeypatch.setenv("CODEX_HOME", str(home))
    legacy = CodexOwnership()
    legacy.acquire("sess-1", "thr-legacy", str(tmp_path))
    return home, legacy


def test_sweep_refuses_while_an_app_server_for_the_same_codex_home_is_alive(tmp_path, monkeypatch):
    from src.backends.codex_ownership import LegacySweepRefused, sweep_legacy_owners

    home, legacy = _swept_legacy_owner(tmp_path, monkeypatch)
    proc = tmp_path / "proc"
    _fake_proc(proc, 4242, ["/usr/bin/node", "/opt/codex/bin/codex", "app-server", "--stdio"],
               {"CODEX_HOME": str(home), "HOME": "/home/x"})
    with pytest.raises(LegacySweepRefused, match="4242"):
        sweep_legacy_owners(gone=lambda pid: True, proc_root=proc)
    assert legacy.thread_for("sess-1") == "thr-legacy"
    # Same CODEX_HOME through the default (HOME/.codex) also refuses.
    proc2 = tmp_path / "proc2"
    _fake_proc(proc2, 77, ["codex", "app-server"], {"HOME": str(home.parent)})
    monkeypatch.setenv("CODEX_HOME", str(home.parent / ".codex"))
    from src.backends.codex_ownership import CodexOwnership

    CodexOwnership().acquire("sess-2", "thr-2", str(tmp_path))
    with pytest.raises(LegacySweepRefused):
        sweep_legacy_owners(gone=lambda pid: True, proc_root=proc2)


def test_sweep_fails_closed_when_an_app_server_environ_is_unreadable(tmp_path, monkeypatch):
    import os as _os

    from src.backends.codex_ownership import LegacySweepRefused, sweep_legacy_owners

    if _os.geteuid() == 0:
        pytest.skip("root reads a 0-mode file")
    _home, _legacy = _swept_legacy_owner(tmp_path, monkeypatch)
    proc = tmp_path / "proc"
    _fake_proc(proc, 9001, ["/x/codex", "app-server", "--stdio"], None)
    with pytest.raises(LegacySweepRefused, match="unreadable"):
        sweep_legacy_owners(gone=lambda pid: True, proc_root=proc)


def test_sweep_ignores_other_codex_homes_and_other_processes(tmp_path, monkeypatch):
    from src.backends.codex_ownership import sweep_legacy_owners

    home, legacy = _swept_legacy_owner(tmp_path, monkeypatch)
    proc = tmp_path / "proc"
    _fake_proc(proc, 10, ["/x/codex", "app-server", "--stdio"], {"CODEX_HOME": str(tmp_path / "other")})
    _fake_proc(proc, 11, ["/usr/bin/python3", "worker.py", "app-server"], None)  # not codex
    _fake_proc(proc, 12, ["/x/codex", "exec", "hi"], {"CODEX_HOME": str(home)})  # not app-server
    (proc / "self").mkdir()
    assert sweep_legacy_owners(gone=lambda pid: True, proc_root=proc) == [legacy.owner]


def test_sweep_fails_closed_without_a_proc_root(tmp_path, monkeypatch):
    from src.backends.codex_ownership import LegacySweepRefused, sweep_legacy_owners

    _swept_legacy_owner(tmp_path, monkeypatch)
    with pytest.raises(LegacySweepRefused):
        sweep_legacy_owners(gone=lambda pid: True, proc_root=tmp_path / "no-proc")
