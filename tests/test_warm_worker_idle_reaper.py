"""[A60] Warm-worker idle-reaper — DB query + orchestrator sweep coverage."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from config import config
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from src.orchestrator import TaskOrchestrator


def _iso(delta_sec: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_sec)).isoformat()


def _worker_session(
    session_id: str,
    *,
    status: SessionStatus = SessionStatus.IDLE,
    idle_sec: float = 7200,
    current_case_id: str = None,
) -> Session:
    ts = _iso(idle_sec)
    return Session(
        session_id=session_id,
        backend="claude",
        repo_path="C:/repo",
        status=status,
        created_at=ts,
        updated_at=ts,
        machine_id="Horse",
        case_role="worker",
        current_case_id=current_case_id,
    )


def test_list_idle_warm_workers_includes_unaffiliated_idle(tmp_path: Path) -> None:
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_worker_session("s_idle_unaffiliated", idle_sec=7200))

    cutoff = _iso(3600)
    rows = db.list_idle_warm_workers(cutoff)

    assert [r["session_id"] for r in rows] == ["s_idle_unaffiliated"]


def test_list_idle_warm_workers_excludes_open_case(tmp_path: Path) -> None:
    db = MeshDB(str(tmp_path / "mesh.db"))
    flow_run_id = db.create_flow_run("task_1", "in_progress")
    db.upsert_session(
        _worker_session("s_open_case", idle_sec=7200, current_case_id=flow_run_id)
    )

    cutoff = _iso(3600)
    rows = db.list_idle_warm_workers(cutoff)

    assert rows == []


def test_list_idle_warm_workers_includes_closed_case(tmp_path: Path) -> None:
    db = MeshDB(str(tmp_path / "mesh.db"))
    flow_run_id = db.create_flow_run("task_1", "in_progress")
    with db._write() as conn:
        conn.execute(
            "UPDATE flow_runs SET status = 'closed' WHERE flow_run_id = ?",
            (flow_run_id,),
        )
    db.upsert_session(
        _worker_session("s_closed_case", idle_sec=7200, current_case_id=flow_run_id)
    )

    cutoff = _iso(3600)
    rows = db.list_idle_warm_workers(cutoff)

    assert [r["session_id"] for r in rows] == ["s_closed_case"]


def test_list_idle_warm_workers_excludes_busy(tmp_path: Path) -> None:
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(
        _worker_session("s_busy", status=SessionStatus.BUSY, idle_sec=7200)
    )

    cutoff = _iso(3600)
    rows = db.list_idle_warm_workers(cutoff)

    assert rows == []


def test_list_idle_warm_workers_excludes_within_ttl(tmp_path: Path) -> None:
    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_session(_worker_session("s_fresh", idle_sec=30))

    cutoff = _iso(3600)
    rows = db.list_idle_warm_workers(cutoff)

    assert rows == []


def test_reap_idle_warm_workers_ttl_zero_disabled(tmp_path: Path) -> None:
    old_ttl = config.mesh.warm_worker_idle_ttl_sec
    config.mesh.warm_worker_idle_ttl_sec = 0
    try:
        orch = TaskOrchestrator()
        import asyncio

        reaped = asyncio.run(orch._reap_idle_warm_workers_once())
        assert reaped == 0
    finally:
        config.mesh.warm_worker_idle_ttl_sec = old_ttl


def test_reap_idle_warm_workers_closes_and_clears_affiliation(tmp_path: Path) -> None:
    old_ttl = config.mesh.warm_worker_idle_ttl_sec
    config.mesh.warm_worker_idle_ttl_sec = 3600
    try:
        orch = TaskOrchestrator()
        db = MeshDB(str(tmp_path / "mesh.db"))
        flow_run_id = db.create_flow_run("task_1", "in_progress")
        with db._write() as conn:
            conn.execute(
                "UPDATE flow_runs SET status = 'closed' WHERE flow_run_id = ?",
                (flow_run_id,),
            )
        session = _worker_session(
            "s_reap_me", idle_sec=7200, current_case_id=flow_run_id
        )
        db.upsert_session(session)
        orch.session_store.save(session, touch=False)

        with patch("src.control.db.get_db", return_value=db), \
             patch.object(orch.session_service, "close_session") as mock_close:
            from src.services.session_service import CommandResult
            mock_close.return_value = CommandResult(True)
            import asyncio

            reaped = asyncio.run(orch._reap_idle_warm_workers_once())

        assert reaped == 1
        mock_close.assert_called_once()
        assert mock_close.call_args.args[0] == "s_reap_me"
    finally:
        config.mesh.warm_worker_idle_ttl_sec = old_ttl


def test_reap_idle_warm_workers_never_reaps_open_case(tmp_path: Path) -> None:
    old_ttl = config.mesh.warm_worker_idle_ttl_sec
    config.mesh.warm_worker_idle_ttl_sec = 3600
    try:
        orch = TaskOrchestrator()
        db = MeshDB(str(tmp_path / "mesh.db"))
        flow_run_id = db.create_flow_run("task_1", "in_progress")  # stays open
        session = _worker_session(
            "s_open_case_live", idle_sec=7200, current_case_id=flow_run_id
        )
        db.upsert_session(session)
        orch.session_store.save(session, touch=False)

        with patch("src.control.db.get_db", return_value=db), \
             patch.object(orch.session_service, "close_session") as mock_close:
            import asyncio

            reaped = asyncio.run(orch._reap_idle_warm_workers_once())

        assert reaped == 0
        mock_close.assert_not_called()
    finally:
        config.mesh.warm_worker_idle_ttl_sec = old_ttl
