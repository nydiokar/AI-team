"""Gap 1/2/3 — remote /close propagation, worker close_session action, and the
pooled-session count. No paid backend: fakes stand in for the SDK driver.

Context: a mesh /close used to be a no-op on the owning worker
(event=session_backend_close_remote_skipped), leaking the claude process. These
tests pin the new behaviour: the gateway dispatches a close_session task, the
worker executes it via backend.close, and the pool count is observable.
"""
import asyncio
import socket

import pytest

from src.services.session_store import SessionStore
from src.services.session_service import SessionService
from src.core.interfaces import SessionStatus


class _FakeBackend:
    def __init__(self, count: int = 0):
        self.closed = []
        self._count = count

    def close(self, session):
        self.closed.append(session.session_id)

    def live_session_count(self) -> int:
        return self._count


def _svc(dispatcher=None):
    return SessionService(
        SessionStore(),
        repo_path_validator=lambda _p: None,
        remote_close_dispatcher=dispatcher,
    )


def _make(svc, **kw):
    res = svc.create_session(backend=kw.pop("backend", "claude"),
                             repo_path=kw.pop("repo_path", "/tmp"), **kw)
    assert res.ok
    return res.session


# --- Gap 1: remote /close dispatch -----------------------------------------

def test_close_remote_invokes_dispatcher():
    dispatched = []
    svc = _svc(dispatcher=lambda s: dispatched.append(s.session_id))
    s = _make(svc)
    s.backend_session_id = "bsid-remote"
    s.machine_id = "Horse"
    svc.store.save(s)
    fake = _FakeBackend()

    res = svc.close_session(s.session_id, backends={"claude": fake}, host=socket.gethostname())
    assert res.ok
    assert dispatched == [s.session_id]          # remote → dispatcher fired
    assert fake.closed == []                      # local backend NOT called
    assert svc.store.get(s.session_id).status == SessionStatus.CLOSED


def test_close_remote_without_dispatcher_is_legacy_skip():
    svc = _svc(dispatcher=None)
    s = _make(svc)
    s.backend_session_id = "bsid-remote"
    s.machine_id = "Horse"
    svc.store.save(s)
    fake = _FakeBackend()

    res = svc.close_session(s.session_id, backends={"claude": fake}, host=socket.gethostname())
    assert res.ok
    assert fake.closed == []                      # still skipped, no crash
    assert svc.store.get(s.session_id).status == SessionStatus.CLOSED


def test_close_local_still_calls_backend_not_dispatcher():
    dispatched = []
    svc = _svc(dispatcher=lambda s: dispatched.append(s.session_id))
    s = _make(svc)
    s.backend_session_id = "bsid-local"
    s.machine_id = socket.gethostname()
    svc.store.save(s)
    fake = _FakeBackend()

    res = svc.close_session(s.session_id, backends={"claude": fake}, host=socket.gethostname())
    assert res.ok
    assert fake.closed == [s.session_id]          # local → backend.close
    assert dispatched == []                       # dispatcher NOT used for local


def test_dispatcher_failure_does_not_break_close():
    def boom(_s):
        raise RuntimeError("enqueue down")
    svc = _svc(dispatcher=boom)
    s = _make(svc)
    s.backend_session_id = "bsid-remote"
    s.machine_id = "Horse"
    svc.store.save(s)

    res = svc.close_session(s.session_id, backends={"claude": _FakeBackend()}, host=socket.gethostname())
    assert res.ok                                 # close still succeeds locally
    assert svc.store.get(s.session_id).status == SessionStatus.CLOSED


# --- Gap 1 worker side: close_session action reaps the pool -----------------

def test_worker_execute_close_session_calls_backend_close():
    from src.worker.agent import _execute_task
    fake = _FakeBackend()
    task_row = {
        "id": "close-1",
        "action": "close_session",
        "payload": {"session": {"session_id": "s-99", "backend": "claude", "machine_id": "Horse"}},
    }
    result = asyncio.run(_execute_task(task_row, {"claude": fake}))
    assert fake.closed == ["s-99"]
    assert result["success"] is True
    assert result["output"] == "session closed"


def test_worker_execute_close_session_no_live_session():
    from src.worker.agent import _execute_task
    task_row = {"id": "close-2", "action": "close_session", "payload": {}}
    result = asyncio.run(_execute_task(task_row, {"claude": _FakeBackend()}))
    assert result["success"] is True             # no session payload → benign


# --- Gap 3: pooled-session count -------------------------------------------

def test_backend_live_session_count_reported():
    from src.backends.claude_driver import ClaudeSDKClientDriver
    assert ClaudeSDKClientDriver().live_session_count() == 0


def test_reaper_no_op_cases():
    from src.core.process_utils import reap_stale_worker_children
    # Either identity empty → no-op (never scans/kills).
    assert reap_stale_worker_children("", "node-a") == []
    assert reap_stale_worker_children("incarn-1", "") == []
    # A live, non-matching incarnation/node finds nothing to reap on this host
    # (current claude procs predate the stamp, so they carry no node/incarnation).
    assert reap_stale_worker_children("incarn-xyz", "no-such-node") == []
