"""Actual Linux process exits around durable effects; fake external transport only."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import config
from src.control import db as db_module
from src.control.db import MeshDB, producer_turn_id
from src.core.session_task_queue import SessionTaskQueue
from src.orchestrator import TaskOrchestrator
from src.services import session_store as store_module
from src.services.notification_service import NotificationService
from src.telegram.interface import TelegramInterface
from tests.test_turn_queue_4d import _env, _kind, _restart
from tests.test_turn_queue_producer1 import _flags, _no_cli_spawn  # noqa: F401


def _child(directory: str, boundary: str) -> None:
    """Fresh interpreter, real persisted session/receipts, no inherited DB handles."""
    root = Path(directory)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config.mesh, "jobs_read_mode", "controller")
        patch.setattr(config.mesh, "enabled", False)
        # Same isolated registered-carrier policy as tests/conftest.py; never
        # start a gateway or enable networking in this fresh interpreter.
        patch.setattr(TaskOrchestrator, "_REFUSE_SESSION_TURNS_WITHOUT_MESH", False)
        patch.setenv("HARNESS_LEVEL3_GUARD", "0")
        patch.setattr(store_module, "_SESSIONS_DIR", root / "sessions")
        patch.setattr(store_module, "_BINDINGS_FILE", root / "bindings.json")
        _no_cli_spawn.__wrapped__(patch)
        db = MeshDB(str(root / "mesh.db"))
        patch.setattr(db_module, "get_db", lambda: db)
        orch = _restart(SimpleNamespace(task_queue=SessionTaskQueue(50, lambda _t: "")))
        tg = TelegramInterface.__new__(TelegramInterface)
        tg.app = object()
        tg.is_running = True
        tg._find_session_for_task = lambda *args, **kwargs: None

        async def send(*args, **kwargs) -> None:
            # A fsynced local sink stands for an externally accepted message.
            # Deliberately no network/API credential is used.
            with (root / "accepted-notices").open("a") as sink:
                sink.write("accepted\n")
                sink.flush()
                os.fsync(sink.fileno())
            if boundary == "after_notice":
                os._exit(73)

        tg._send_long_message = send
        orch.telegram_interface = tg
        orch.notifier = NotificationService(orch)
        patch.setattr(orch.notifier, "_maybe_push_outcome", lambda *args, **kwargs: None)
        patch.setattr(orch.notifier, "_maybe_push_cost_alerts", lambda: None)
        if boundary == "before_discovery":
            patch.setattr(db, "stage_local_job_completions", lambda: os._exit(73))
        elif boundary == "after_claim":
            original = db.claim_job_completion
            def claim(job_id: str):
                original(job_id)
                os._exit(73)
            patch.setattr(db, "claim_job_completion", claim)
        elif boundary == "after_admission":
            original_finish = db.finish_job_effect
            def finish(job_id, token, effect, state, error="") -> bool:
                if effect == "action":
                    os._exit(73)
                return original_finish(job_id, token, effect, state, error)
            patch.setattr(db, "finish_job_effect", finish)
        try:
            asyncio.run(orch._poll_job_completions_once())
            if boundary == "execute":
                from tests.test_turn_queue_4b import _pass, _run
                turn = _kind(db, "watched_job")[0]
                _pass(db, orch)
                token = _run(db, turn["id"])
                db.complete_turn(turn["id"], token, {"success": True})
        finally:
            db.close()


def _run_child(root: Path, boundary: str) -> subprocess.CompletedProcess[str]:
    # subprocess.run owns/reaps each interpreter; timeout also reaps a stuck child.
    return subprocess.run(
        [sys.executable, "-c", "from tests.test_job_completion_process import _child; import sys; _child(*sys.argv[1:])", str(root), boundary],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=30, check=False,
        env={**os.environ, "AI_TEAM_TEST_MODE": "1", "MESH_ENABLED": "false"},
    )


@pytest.mark.parametrize("boundary,notices_before,total_notices", [
    ("before_discovery", 0, 1), ("after_claim", 0, 1),
    ("after_notice", 1, 2), ("after_admission", 1, 1),
])
def test_process_death_recovers_receipt_and_executes_one_turn(tmp_path, monkeypatch, boundary, notices_before, total_notices) -> None:
    monkeypatch.setattr(store_module, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store_module, "_BINDINGS_FILE", tmp_path / "bindings.json")
    db, orch = _env(tmp_path, monkeypatch)
    session = orch.session_store.get("sess-1")
    session.telegram_chat_id = 42
    orch.session_store.save(session)
    db.register_job(job_id="process-job", session_id="sess-1", node_id="worker-a",
                    label="isolated process acceptance", notify=True, notify_agent=True)
    db.complete_job("process-job", 0)
    db.close()
    crashed = _run_child(tmp_path, boundary)
    assert crashed.returncode == 73, crashed.stderr
    sink = tmp_path / "accepted-notices"
    assert (len(sink.read_text().splitlines()) if sink.exists() else 0) == notices_before
    db = MeshDB(str(tmp_path / "mesh.db"))
    receipt = db.get_job_completion("process-job")
    assert len(_kind(db, "watched_job")) == (1 if boundary == "after_admission" else 0)
    if boundary == "before_discovery":
        assert receipt is None
    else:
        assert receipt["lease_token"] and receipt["lease_until"] > 0
        assert receipt["action_state"] == "pending"
        assert receipt["notification_attempts"] == (0 if boundary == "after_claim" else 1)
        assert receipt["notification_state"] == ("done" if boundary == "after_admission" else "pending")
        assert receipt["action_attempts"] == (1 if boundary == "after_admission" else 0)
    # Advance ONLY the isolated test lease instead of sleeping 120 seconds.
    with db._write() as conn:
        conn.execute("UPDATE job_completion_effects SET lease_until=0")
    db.close()
    recovered = _run_child(tmp_path, "execute")
    assert recovered.returncode == 0, recovered.stderr
    replayed = _run_child(tmp_path, "replay")
    assert replayed.returncode == 0, replayed.stderr
    db = MeshDB(str(tmp_path / "mesh.db"))
    try:
        receipt = db.get_job_completion("process-job")
        assert receipt["notification_state"] == receipt["action_state"] == "done"
        turns = _kind(db, "watched_job")
        assert [t["id"] for t in turns] == [producer_turn_id("watched:process-job", "sess-1", 1, prefix="jturn")]
        assert turns[0]["status"] == "completed"
        assert len(sink.read_text().splitlines()) == total_notices
    finally:
        db.close()
