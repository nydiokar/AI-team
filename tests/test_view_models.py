"""M2 (Move C) — SessionView read-model tests (no network / no CLI).

Covers the derived booleans for every SessionStatus, JSON-serializability of
to_dict(), origin propagation, and the SessionService read methods
(list_views / active_view).
"""
import json

import pytest

from src.core.interfaces import Session, SessionStatus, SessionOrigin
from src.core.view_models import SessionView
from src.services.session_store import SessionStore
from src.services.session_service import SessionService


def _make_session(status: SessionStatus, **kw) -> Session:
    base = dict(
        session_id="s1",
        backend="claude",
        repo_path="/repo",
        status=status,
        created_at="2026-06-21T00:00:00",
        updated_at="2026-06-21T00:00:01",
    )
    base.update(kw)
    return Session(**base)


# --- derived booleans for every status -------------------------------------

_ACTIVE_STATUSES = {
    SessionStatus.IDLE, SessionStatus.BUSY, SessionStatus.AWAITING_INPUT,
    SessionStatus.CANCELLED,
    # A18: an in-flight hold behaves like BUSY.
    SessionStatus.PAUSED_PINNED_NODE_OFFLINE,
}
_INACTIVE_STATUSES = {
    SessionStatus.CLOSED, SessionStatus.ERROR,
    # A18: a needs-attention terminal behaves like ERROR (resumable, but inactive).
    SessionStatus.PINNED_NODE_OFFLINE,
}


@pytest.mark.parametrize("status", list(SessionStatus))
def test_is_active_matches_status(status):
    view = SessionView.from_session(_make_session(status))
    assert view.is_active == (status in _ACTIVE_STATUSES)
    assert view.is_active != (status in _INACTIVE_STATUSES)


@pytest.mark.parametrize("status", list(SessionStatus))
def test_needs_input_only_for_awaiting(status):
    view = SessionView.from_session(_make_session(status))
    assert view.needs_input == (status == SessionStatus.AWAITING_INPUT)


def test_status_string_is_enum_value():
    view = SessionView.from_session(_make_session(SessionStatus.BUSY))
    assert view.status == "busy"


# --- field mapping ----------------------------------------------------------

def test_last_summary_prefers_result_summary():
    s = _make_session(SessionStatus.IDLE, last_summary="old", last_result_summary="new")
    assert SessionView.from_session(s).last_summary == "new"


def test_last_summary_falls_back_to_summary():
    s = _make_session(SessionStatus.IDLE, last_summary="old", last_result_summary="")
    assert SessionView.from_session(s).last_summary == "old"


def test_files_modified_copied_not_aliased():
    files = ["a.py"]
    s = _make_session(SessionStatus.IDLE, last_files_modified=files)
    view = SessionView.from_session(s)
    files.append("b.py")
    assert view.last_files_modified == ["a.py"]  # snapshot, not a live alias


def test_origin_propagated():
    s = _make_session(SessionStatus.IDLE, origin=SessionOrigin("web", "cron"))
    view = SessionView.from_session(s)
    assert view.origin_channel == "web"
    assert view.origin_kind == "cron"


def test_origin_defaults_when_absent():
    # __post_init__ defaults origin to SessionOrigin(); verify the view reflects it.
    view = SessionView.from_session(_make_session(SessionStatus.IDLE))
    assert view.origin_channel == "telegram"
    assert view.origin_kind == "user"


# --- serialization ----------------------------------------------------------

def test_to_dict_is_json_serializable():
    view = SessionView.from_session(_make_session(SessionStatus.AWAITING_INPUT))
    blob = json.dumps(view.to_dict())  # must not raise
    restored = json.loads(blob)
    assert restored["session_id"] == "s1"
    assert restored["needs_input"] is True
    assert restored["status"] == "awaiting_input"


def test_to_dict_has_no_session_object_leak():
    view = SessionView.from_session(_make_session(SessionStatus.IDLE))
    d = view.to_dict()
    # Every value must be a JSON primitive / container — no dataclass leaks.
    for v in d.values():
        assert isinstance(v, (str, int, float, bool, list, type(None)))


# --- SessionService read methods -------------------------------------------

@pytest.fixture
def service():
    return SessionService(SessionStore(), repo_path_validator=lambda _p: None)


def test_list_views_returns_session_views(service, tmp_path):
    service.create_session(backend="claude", repo_path=str(tmp_path), chat_id=1)
    views = service.list_views()
    assert views and all(isinstance(v, SessionView) for v in views)


def test_active_view_returns_bound_session(service, tmp_path):
    res = service.create_session(backend="claude", repo_path=str(tmp_path), chat_id=55)
    view = service.active_view(55)
    assert view is not None
    assert view.session_id == res.session.session_id
    assert isinstance(view, SessionView)


def test_active_view_none_when_unbound(service):
    assert service.active_view(999999) is None
