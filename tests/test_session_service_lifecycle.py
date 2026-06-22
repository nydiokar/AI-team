"""U3.5 — SessionService lifecycle extraction tests (close/restore/set_model).

These cover the logic lifted off the Telegram class (P1–P3) so both Telegram and
the web API call one transport-neutral service. No paid backend: close uses a fake
backend spy; model resolution uses the real config.models tables.
"""
import socket

import pytest

from src.services.session_store import SessionStore
from src.services.session_service import SessionService
from src.core.interfaces import SessionStatus


@pytest.fixture
def svc():
    return SessionService(SessionStore())


class _FakeBackend:
    def __init__(self):
        self.closed = []

    def close(self, session):
        self.closed.append(session.session_id)


def _make(svc, **kw):
    res = svc.create_session(backend=kw.pop("backend", "claude"),
                             repo_path=kw.pop("repo_path", "/tmp"), **kw)
    assert res.ok
    return res.session


# --- close_session ----------------------------------------------------------

def test_close_local_calls_backend_and_clears(svc):
    s = _make(svc)
    s.backend_session_id = "bsid-123"
    s.machine_id = socket.gethostname()
    svc.store.save(s)
    fake = _FakeBackend()

    res = svc.close_session(s.session_id, backends={"claude": fake}, host=socket.gethostname())
    assert res.ok
    assert fake.closed == [s.session_id]            # backend.close called (local)
    reloaded = svc.store.get(s.session_id)
    assert reloaded.status == SessionStatus.CLOSED
    assert reloaded.backend_session_id == ""        # cleared


def test_close_remote_skips_backend(svc):
    s = _make(svc)
    s.backend_session_id = "bsid-remote"
    s.machine_id = "some-other-node"
    svc.store.save(s)
    fake = _FakeBackend()

    res = svc.close_session(s.session_id, backends={"claude": fake}, host=socket.gethostname())
    assert res.ok
    assert fake.closed == []                         # remote → backend.close NOT called
    assert svc.store.get(s.session_id).status == SessionStatus.CLOSED


def test_close_no_backend_session_just_closes(svc):
    s = _make(svc)  # no backend_session_id
    res = svc.close_session(s.session_id, backends={})
    assert res.ok and svc.store.get(s.session_id).status == SessionStatus.CLOSED


def test_close_unknown_session(svc):
    res = svc.close_session("nope")
    assert not res.ok and res.reason == "session_not_found"


# --- restore_session --------------------------------------------------------

def test_restore_closed_to_idle(svc):
    s = _make(svc)
    svc.close_session(s.session_id, backends={})
    res = svc.restore_session(s.session_id)
    assert res.ok
    assert svc.store.get(s.session_id).status == SessionStatus.IDLE


def test_restore_non_closed_rejected(svc):
    s = _make(svc)  # IDLE
    res = svc.restore_session(s.session_id)
    assert not res.ok and res.reason == "not_closed"


# --- set_model --------------------------------------------------------------

def test_set_model_valid(svc):
    s = _make(svc, backend="claude")
    res = svc.set_model(s.session_id, "opus")
    assert res.ok
    assert svc.store.get(s.session_id).model == "opus"


def test_set_model_unknown_nonadvisory_rejected(svc):
    s = _make(svc, backend="claude")
    res = svc.set_model(s.session_id, "totally-made-up")
    assert not res.ok and res.reason == "unknown_model"


def test_set_model_advisory_passes_through(svc):
    s = _make(svc, backend="opencode")
    res = svc.set_model(s.session_id, "some/custom-model")
    assert res.ok
    assert svc.store.get(s.session_id).model == "some/custom-model"


def test_set_model_clear_to_default(svc):
    s = _make(svc, backend="claude")
    svc.set_model(s.session_id, "opus")
    res = svc.set_model(s.session_id, None)
    assert res.ok and svc.store.get(s.session_id).model is None
