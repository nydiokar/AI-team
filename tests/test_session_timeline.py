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


def test_context_fill_null_with_reason_when_no_turns_observed() -> None:
    response = build_session_timeline(
        db=_EmptyDB(),
        telemetry_store=None,
        session_id="sess_timeline",
    )

    assert response.context_fill == {
        "context_used_ratio": None,
        "context_window_tokens": None,
        "context_remaining_tokens": None,
        "context_window_source": "unknown",
        "reason": "no_turns_observed",
    }


def test_context_fill_null_with_reason_when_window_unknown() -> None:
    class _TelemetryStore:
        def list_turns(self, session_id=None, limit=100):
            return [
                {
                    "turn_id": "turn_1",
                    "task_id": "task_1",
                    "started_at": "2026-07-01T00:00:00Z",
                    "metrics": {"context_window_tokens": None, "context_used_ratio": None},
                }
            ]

    response = build_session_timeline(
        db=_EmptyDB(),
        telemetry_store=_TelemetryStore(),
        session_id="sess_timeline",
    )

    assert response.context_fill == {
        "context_used_ratio": None,
        "context_window_tokens": None,
        "context_remaining_tokens": None,
        "context_window_source": "unknown",
        "reason": "context_window_unknown_for_backend_model",
    }


def test_context_fill_surfaces_ratio_when_window_known() -> None:
    class _TelemetryStore:
        def list_turns(self, session_id=None, limit=100):
            return [
                {
                    "turn_id": "turn_2",
                    "task_id": "task_2",
                    "started_at": "2026-07-01T00:00:00Z",
                    "metrics": {
                        "context_window_tokens": 200000,
                        "context_used_ratio": 0.42,
                        "context_remaining_tokens": 116000,
                    },
                }
            ]

    response = build_session_timeline(
        db=_EmptyDB(),
        telemetry_store=_TelemetryStore(),
        session_id="sess_timeline",
    )

    assert response.context_fill == {
        "context_used_ratio": 0.42,
        "context_window_tokens": 200000,
        "context_remaining_tokens": 116000,
        "context_window_source": "known",
    }

    turn_item = next(item for item in response.items if item.kind == "turn_event")
    assert turn_item.detail["metrics"]["context_used_ratio"] == 0.42
    assert turn_item.detail["metrics"]["context_window_tokens"] == 200000
    assert turn_item.detail["metrics"]["context_remaining_tokens"] == 116000
