from src.core.interfaces import SessionStatus
from src.services.session_service import SessionService
from src.services.session_store import SessionStore


def _service() -> SessionService:
    return SessionService(SessionStore(), repo_path_validator=lambda _p: None)


def _closed_empty_session(svc: SessionService) -> str:
    result = svc.create_session(backend="codex", repo_path="/tmp")
    assert result.ok and result.session is not None
    sid = result.session.session_id
    close = svc.close_session(sid, backends={})
    assert close.ok
    return sid


def test_prune_empty_closed_sessions_dry_run_keeps_rows():
    svc = _service()
    sid = _closed_empty_session(svc)

    result = svc.store.prune_empty_closed_sessions(limit=50, dry_run=True)

    assert result["matched"] == 1
    assert result["deleted"] == 0
    assert result["session_ids"] == [sid]
    assert svc.store.get(sid) is not None


def test_prune_empty_closed_sessions_deletes_db_and_json():
    svc = _service()
    sid = _closed_empty_session(svc)

    result = svc.store.prune_empty_closed_sessions(limit=50, dry_run=False)

    assert result["matched"] == 1
    assert result["deleted"] == 1
    assert result["session_ids"] == [sid]
    assert svc.store.get(sid) is None


def test_prune_empty_closed_sessions_keeps_sessions_with_content_or_refs():
    svc = _service()
    empty_sid = _closed_empty_session(svc)

    with_summary = _closed_empty_session(svc)
    session = svc.store.get(with_summary)
    assert session is not None
    session.last_summary = "real result"
    svc.store.save(session)

    with_task = _closed_empty_session(svc)
    session = svc.store.get(with_task)
    assert session is not None
    session.last_task_id = "task_has_history"
    session.status = SessionStatus.CLOSED
    svc.store.save(session)

    from src.control.db import get_db
    db = get_db()
    db.enqueue_task(
        "task_has_history",
        with_task,
        None,
        "codex",
        "create_session",
        {"prompt": "hello", "task_id": "task_has_history"},
    )

    result = svc.store.prune_empty_closed_sessions(limit=50, dry_run=False)

    assert result["deleted"] == 1
    assert result["session_ids"] == [empty_sid]
    assert svc.store.get(empty_sid) is None
    assert svc.store.get(with_summary) is not None
    assert svc.store.get(with_task) is not None
