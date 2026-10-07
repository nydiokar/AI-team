"""A98 O1 — a restart-lost SDK session forks onto a FRESH create_session.

Root cause (fixed here): `_mesh_dispatch_payload` chose the carrier action purely on
`backend_session_id` presence — a session that HAD one always got `resume_session`,
even when a worker restart had orphaned its in-memory SDK driver
(`driver_status == "lost"`). The worker then refuses that resume ("session was lost
after a worker restart and cannot be resumed by the continuous driver"), and the
Wake-Dispatcher re-injects restart-context into the corpse forever. The restart-
recovery design always intended a fresh `create_session` (role re-boot + A54
boot-reconcile + injected <prior_context>) for a lost session; this routes it there.
"""
import types

import pytest

from src.orchestrator import TaskOrchestrator
import src.orchestrator as orch_mod


HOST = "gateway-host"


def _orch(fork_enabled: bool) -> TaskOrchestrator:
    o = TaskOrchestrator.__new__(TaskOrchestrator)
    # Pin the flag deterministically; the DB read itself is covered by db.py's registry.
    o._restart_lost_fork_enabled = lambda: fork_enabled
    return o


def _session(*, driver_status: str, enrolled: int = 0, backend_session_id: str = "bsid-1"):
    return types.SimpleNamespace(
        session_id="sess-1",
        backend="claude",
        backend_session_id=backend_session_id,
        driver_status=driver_status,
        turn_queue_enrolled=enrolled,
        repo_path="/tmp/x",
        machine_id="",
    )


def _task():
    return types.SimpleNamespace(id="t-1", prompt="continue", metadata={"session_id": "sess-1"})


@pytest.fixture(autouse=True)
def _stub_payload(monkeypatch):
    # Isolate the action decision from the (separately-tested) payload assembly.
    monkeypatch.setattr(orch_mod, "_session_dispatch_payload", lambda s: {}, raising=True)


def _action(orch, session):
    action, _payload = orch._mesh_dispatch_payload(_task(), "sess-1", session, HOST)
    return action


def test_lost_session_forks_fresh_when_enabled():
    orch = _orch(fork_enabled=True)
    assert _action(orch, _session(driver_status="lost")) == "create_session"


def test_lost_session_resumes_when_flag_disabled():
    orch = _orch(fork_enabled=False)
    assert _action(orch, _session(driver_status="lost")) == "resume_session"


def test_live_session_still_resumes():
    orch = _orch(fork_enabled=True)
    assert _action(orch, _session(driver_status="live")) == "resume_session"


def test_lost_but_enrolled_session_untouched():
    # A82 managed sessions keep their own respawn path; O1 is scoped out of them.
    orch = _orch(fork_enabled=True)
    assert _action(orch, _session(driver_status="lost", enrolled=1)) == "resume_session"


def test_lost_session_without_backend_id_still_creates():
    # No backend_session_id → the pre-existing create_session branch already fires.
    orch = _orch(fork_enabled=True)
    assert _action(orch, _session(driver_status="lost", backend_session_id="")) == "create_session"
