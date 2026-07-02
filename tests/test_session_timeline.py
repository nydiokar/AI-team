from src.control.session_timeline import build_session_timeline


class _EmptyDB:
    def list_tasks(self, session_id=None, limit=50):
        return []

    def list_nodes(self):
        return []

    def list_jobs(self, session_id=None, ownership=None, limit=50):
        return []

    def list_approvals(self, session_id=None, limit=50):
        return []


def test_session_timeline_reports_unavailable_source_instead_of_complete() -> None:
    class _FailingDB(_EmptyDB):
        def list_tasks(self, session_id=None, limit=50):
            raise RuntimeError("db is down")

    response = build_session_timeline(
        db=_FailingDB(),
        telemetry_store=None,
        session_id="sess_timeline",
    )

    assert response.coverage["tasks"] == "unavailable"
    assert response.coverage["artifacts"] == "unavailable"
    assert response.items == []


def test_session_timeline_pages_newest_first_from_stable_bounded_window() -> None:
    class _ManyTasksDB(_EmptyDB):
        def list_tasks(self, session_id=None, limit=50):
            rows = [
                {
                    "id": f"task_{i:03d}",
                    "session_id": session_id,
                    "status": "pending",
                    "created_at": f"2026-07-01T00:00:{i:03d}Z",
                    "updated_at": f"2026-07-01T00:00:{i:03d}Z",
                }
                for i in range(600)
            ]
            return list(reversed(rows))[:limit]

    db = _ManyTasksDB()
    first = build_session_timeline(
        db=db,
        telemetry_store=None,
        session_id="sess_timeline",
        limit=2,
    )
    second = build_session_timeline(
        db=db,
        telemetry_store=None,
        session_id="sess_timeline",
        limit=2,
        cursor=first.next_cursor,
    )

    assert [item.task_id for item in first.items] == ["task_599", "task_598"]
    assert [item.task_id for item in second.items] == ["task_597", "task_596"]
