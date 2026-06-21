"""M1 Step 3 — SessionService unit tests (no network / no CLI).

Exercises the transport-neutral lifecycle seam independently of Telegram:
create (with backend validation, node/model/origin pinning, bind) and
bind_active. The conftest isolates the DB and forces test mode.
"""
import pytest

from src.core.interfaces import SessionOrigin, SessionStatus
from src.services.session_store import SessionStore
from src.services.session_service import SessionService, CommandResult


@pytest.fixture
def service():
    return SessionService(SessionStore())


def test_create_session_ok_persisted_and_bound(service, tmp_path):
    res = service.create_session(backend="claude", repo_path=str(tmp_path), chat_id=42)
    assert isinstance(res, CommandResult)
    assert res.ok and res.reason == ""
    assert res.session is not None

    # Persisted: reloadable from the store.
    loaded = service.store.get(res.session.session_id)
    assert loaded is not None
    assert loaded.backend == "claude"
    assert loaded.status == SessionStatus.IDLE
    # Bound: chat 42 resolves to this session.
    assert service.store.get_active(42).session_id == res.session.session_id


def test_create_session_pins_node(service, tmp_path):
    res = service.create_session(backend="claude", repo_path=str(tmp_path), node_id="LP-1")
    assert res.ok
    assert res.session.machine_id == "LP-1"
    assert service.store.get(res.session.session_id).machine_id == "LP-1"


def test_create_session_local_node_keeps_default_machine_id(service, tmp_path):
    """node_id="__local__" must NOT overwrite the store's default machine_id."""
    res = service.create_session(backend="claude", repo_path=str(tmp_path), node_id="__local__")
    assert res.ok
    assert res.session.machine_id != "__local__"
    assert res.session.machine_id  # store.create sets it to the hostname


def test_create_session_pins_model(service, tmp_path):
    res = service.create_session(backend="claude", repo_path=str(tmp_path), model="opus")
    assert res.ok
    assert res.session.model == "opus"
    assert service.store.get(res.session.session_id).model == "opus"


def test_create_session_persists_origin(service, tmp_path):
    res = service.create_session(
        backend="claude", repo_path=str(tmp_path),
        origin=SessionOrigin("web", "user"),
    )
    assert res.ok
    assert res.session.origin == SessionOrigin("web", "user")
    assert service.store.get(res.session.session_id).origin == SessionOrigin("web", "user")


def test_create_session_default_origin_is_telegram_user(service, tmp_path):
    res = service.create_session(backend="claude", repo_path=str(tmp_path))
    assert res.session.origin == SessionOrigin("telegram", "user")


def test_create_session_unknown_backend_rejected_nothing_saved(service, tmp_path):
    before = len(service.store.list_all())
    res = service.create_session(backend="nope", repo_path=str(tmp_path))
    assert not res.ok
    assert res.reason == "unknown_backend"
    assert res.session is None
    assert len(service.store.list_all()) == before  # nothing persisted


def test_bind_active_unknown_session(service):
    res = service.bind_active(chat_id=7, session_id="does-not-exist")
    assert not res.ok
    assert res.reason == "session_not_found"


def test_bind_active_existing_session(service, tmp_path):
    created = service.create_session(
        backend="claude", repo_path=str(tmp_path), bind_chat=False,
    )
    res = service.bind_active(chat_id=99, session_id=created.session.session_id)
    assert res.ok
    assert res.session.session_id == created.session.session_id
    assert service.store.get_active(99).session_id == created.session.session_id
