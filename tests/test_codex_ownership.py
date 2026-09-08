import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.backends.codex import CodexBackend
from src.backends.codex_ownership import CodexOwnership
from src.core.interfaces import Task, TaskPriority, TaskStatus, TaskType
from src.core.session_task_queue import SessionTaskQueue
from src.core.telemetry import TelemetryContext
from src.orchestrator import TaskOrchestrator


def task(name: str, session_id: str) -> Task:
    return Task(name, TaskType.ANALYZE, TaskPriority.MEDIUM, TaskStatus.PENDING,
                "", name, [], name, [], "", {"session_id": session_id, "timeout_sec": 15})


async def wait_file(path: Path) -> None:
    async with asyncio.timeout(8):
        while not path.exists():
            await asyncio.sleep(0.02)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr("src.core.test_guard.assert_live_calls_allowed", lambda _: None)
    monkeypatch.setattr(CodexBackend, "_resolve_exe", lambda *args: sys.executable)
    script = (
        "import sys,json,time,os; from pathlib import Path; "
        "name=sys.stdin.read(); root=Path(sys.argv[2]); "
        "thread=sys.argv[1] or 'thread-'+name; "
        "print(json.dumps({'type':'thread.started','thread_id':thread}),flush=True); "
        "(root/(name+'.started')).write_text(str(os.getpid())); "
        "exec('while not (root/(name+\".release\")).exists(): time.sleep(0.02)'); "
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':name}}),flush=True)"
    )
    monkeypatch.setattr(CodexBackend, "_build_cmd", lambda self, resume_id, cwd, model=None, effort=None:
                        [sys.executable, "-c", script, resume_id or "", cwd])
    return tmp_path


def context(name: str, session: str = "same") -> TelemetryContext:
    return TelemetryContext(turn_id=name, invocation_id="inv-" + name,
                            node_id="test", session_id=session, backend="codex")


@pytest.mark.asyncio
async def test_process_exclusion_cancel_and_exact_resume(fake_cli):
    root = fake_cli
    first_backend = CodexBackend()
    first = asyncio.create_task(asyncio.to_thread(first_backend._run, str(root), "first", None, "same",
                                                telemetry_context=context("first")))
    await wait_file(root / "first.started")
    try:
        second = await asyncio.to_thread(CodexBackend()._run, str(root), "second", None, "same",
                                         telemetry_context=context("second"))
        assert not second.success
        assert "codex_thread_busy" in second.errors[0]
        assert not (root / "second.started").exists()
        assert not first.done()
        # Cancellation names the execution, so a delayed old cancel is harmless.
        first_backend.cancel_execution("first")
        result = await asyncio.wait_for(first, 8)
        assert result.errors == ["cancelled"]
        assert result.backend_session_id == "thread-first"
        with pytest.raises(ProcessLookupError):
            os.kill(int((root / "first.started").read_text()), 0)
        (root / "second.release").touch()
        first_backend.cancel_execution("first")
        resumed = await asyncio.to_thread(CodexBackend()._run, str(root), "second", None, "same",
                                          telemetry_context=context("second"))
        assert resumed.success
        assert resumed.backend_session_id == "thread-first"
    finally:
        (root / "first.release").touch()
        await asyncio.wait_for(first, 8)


@pytest.mark.asyncio
async def test_queue_fifo_leaves_unrelated_session_runnable():
    queue = SessionTaskQueue(4, lambda item: item.metadata["session_id"])
    first, second, third, unrelated = [task(n, s) for n, s in
                                      [("first", "a"), ("second", "a"), ("third", "a"), ("other", "b")]]
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


def test_cross_process_claim_and_crash_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    script = "from src.backends.codex_ownership import CodexOwnership; o=CodexOwnership(); o.acquire('session','exact-thread'); print('owned')"
    child = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "owned"
    # Process exit is not evidence that its mutation-capable descendants exited.
    with pytest.raises(RuntimeError, match="codex_thread_busy"):
        CodexOwnership().acquire("session", "")
    with pytest.raises(RuntimeError, match="codex_thread_busy"):
        CodexOwnership().acquire("alias", "exact-thread")
    other = CodexOwnership()
    assert other.acquire("other", "different-thread") == "different-thread"
    other.release()


@pytest.mark.asyncio
async def test_gateway_same_session_fifo_and_queued_cancel(fake_cli, monkeypatch):
    root = fake_cli
    monkeypatch.setattr("src.services.session_store._SESSIONS_DIR", root / "sessions")
    orch = TaskOrchestrator()
    session = orch.session_store.create("codex", str(root))
    first_task = task("first", session.session_id)
    cancelled_task = task("cancelled", session.session_id)
    next_task = task("next", session.session_id)
    for item in (first_task, cancelled_task, next_task):
        orch.active_tasks[item.id] = item
    first = asyncio.create_task(orch.process_task(first_task))
    await wait_file(root / "first.started")
    queued = asyncio.create_task(orch.process_task(cancelled_task))
    following = asyncio.create_task(orch.process_task(next_task))
    try:
        await asyncio.sleep(0.1)
        assert not (root / "next.started").exists()
        assert orch.cancel_task(cancelled_task.id)
        assert not first.done()
        assert orch.cancel_task(first_task.id)
        assert not (await asyncio.wait_for(first, 8)).success
        assert (await queued).errors == ["cancelled"]
        await wait_file(root / "next.started")
        assert not following.done()
        assert not (root / "cancelled.started").exists()
        (root / "next.release").touch()
        assert (await following).success
        assert orch.session_store.get(session.session_id).backend_session_id == "thread-first"
        from src.services.session_store import SessionStore
        assert SessionStore().get(session.session_id).backend_session_id == "thread-first"
    finally:
        for name in ("first", "cancelled", "next"):
            (root / (name + ".release")).touch()
        await asyncio.gather(first, queued, following, return_exceptions=True)


@pytest.mark.asyncio
async def test_different_sessions_run_real_children_concurrently(fake_cli):
    root = fake_cli
    backend = CodexBackend()
    runs = [asyncio.create_task(asyncio.to_thread(
        backend._run, str(root), name, None, name, telemetry_context=context(name, name)
    )) for name in ("alpha", "beta")]
    try:
        await asyncio.gather(*(wait_file(root / (name + ".started")) for name in ("alpha", "beta")))
        assert all(not run.done() for run in runs)
    finally:
        for name in ("alpha", "beta"):
            (root / (name + ".release")).touch()
        results = await asyncio.gather(*runs)
    assert all(result.success for result in results)


@pytest.mark.asyncio
async def test_mesh_cancel_control_targets_old_execution_only(fake_cli):
    from src.worker.agent import _execute_task
    root = fake_cli
    backend = CodexBackend()
    running = asyncio.create_task(asyncio.to_thread(
        backend._run, str(root), "remote", None, "same", telemetry_context=context("remote")
    ))
    await wait_file(root / "remote.started")
    try:
        result = await _execute_task({"action": "cancel_codex", "payload": {"target_task_id": "remote"}}, {})
        assert result["success"]
        assert (await asyncio.wait_for(running, 8)).errors == ["cancelled"]
        # Re-delivery is idempotent and cannot poison a follow-up task.
        await _execute_task({"action": "cancel_codex", "payload": {"target_task_id": "remote"}}, {})
        (root / "followup.release").touch()
        followup = await asyncio.to_thread(backend._run, str(root), "followup", None, "same",
                                          telemetry_context=context("followup"))
        assert followup.success
        assert followup.backend_session_id == "thread-remote"
    finally:
        (root / "remote.release").touch()
        await asyncio.wait_for(running, 8)


def test_live_cross_process_thread_alias_is_excluded(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    owner = CodexOwnership()
    owner.acquire("session", "exact-thread")
    script = "from src.backends.codex_ownership import CodexOwnership; CodexOwnership().acquire('alias','exact-thread')"
    try:
        competing = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
        assert competing.returncode != 0
        assert "codex_thread_busy" in competing.stderr
    finally:
        owner.release()
    assert CodexOwnership().acquire("session", "") == "exact-thread"
