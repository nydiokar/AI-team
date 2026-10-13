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


def test_session_state_head_uses_session_updated_at_not_render_time() -> None:
    # The head session_state row must carry the session's real status-change time
    # (``updated_at``), NOT the response build time. Stamping it with render time
    # made a stale "idle" always look newer than the live worker/turn below it.
    session_updated = "2026-10-14T17:00:00+00:00"
    response = build_session_timeline(
        db=_EmptyDB(),
        telemetry_store=None,
        session_id="sess_ts",
        session_row={"session_id": "sess_ts", "status": "idle",
                     "updated_at": session_updated,
                     "created_at": "2026-10-14T16:00:00+00:00"},
        session_reason={"kind": "idle", "confidence": "high"},
    )
    head = response.items[0]
    assert head.kind == "session_state"
    assert head.timestamp == session_updated
    assert head.timestamp != response.generated_at


def test_session_state_head_sorts_by_time_not_force_pinned_above_live_rows() -> None:
    # Honest ordering: a task observed AFTER the session's last status write must
    # sort ABOVE the session_state head — the head is no longer force-pinned to
    # the top wearing a misleading timestamp (the reported "idle newer than a
    # running worker" inversion).
    class _DB(_EmptyDB):
        def list_tasks(self, session_id=None, limit=50):
            return [{
                "id": "task_live", "session_id": session_id, "status": "running",
                "claimed_by": "node1", "updated_at": "2026-10-14T17:05:00+00:00",
                "started_at": "2026-10-14T17:05:00+00:00",
            }]

        def list_nodes(self):
            return [{"node_id": "node1", "status": "online"}]

    response = build_session_timeline(
        db=_DB(),
        telemetry_store=None,
        session_id="sess_ord",
        session_row={"session_id": "sess_ord", "status": "idle",
                     "updated_at": "2026-10-14T17:00:00+00:00"},
        session_reason={"kind": "idle", "confidence": "high"},
    )
    kinds = [it.kind for it in response.items]
    task_idx = kinds.index("task_state")
    head_idx = kinds.index("session_state")
    assert task_idx < head_idx, "a newer live task row must sort above the session head"


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
