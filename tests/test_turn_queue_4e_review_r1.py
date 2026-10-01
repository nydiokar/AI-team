"""A82 Stage 4e review round 1 — regression tests for findings F2-F5.

Real pieces: a file-backed ``MeshDB`` as ``get_db()``, REAL bound orchestrator
methods on a bare instance, the REAL scheduler pass (``_prepare_managed_turn``
→ activation-time revalidation → withdrawal) and the REAL DB seams. No
backend/CLI (autouse spawn guard from the producer-1 suite).
"""
import asyncio
from typing import Any, Dict, List

import pytest

from src.control import turn_admission as ta
from src.core.interfaces import Session, SessionStatus
from tests.test_turn_queue_4b import _pass, _wire
from tests.test_turn_queue_producer1 import (  # noqa: F401
    NOW, _flags, _no_cli_spawn, _setup,
)


@pytest.fixture(autouse=True)
def _fresh_allowance(monkeypatch):
    monkeypatch.setattr(ta, "ALLOWANCE", ta.SharedWaitingAllowance())


def _enrolled(db: Any, sid: str) -> None:
    db.upsert_session(Session(
        session_id=sid, backend="claude", repo_path="/tmp/repo",
        status=SessionStatus.IDLE, created_at=NOW, updated_at=NOW, machine_id="worker-a",
    ))
    db.enroll_session(sid)


def _rebound_case(db: Any, old: str, new: str) -> str:
    """A Case whose Manager binding moved from ``old`` to ``new``."""
    case_id: str = db.open_case("objective", old, role="manager")
    _enrolled(db, new)
    db.create_flow_link(case_id, "session", new, "manager")
    return case_id


def _automation(db: Any, tid: str, sid: str, case_id: str, kind: str,
                payload: Dict[str, Any] | None = None) -> None:
    db.enqueue_turn(tid, sid, body=f"{kind} notification", turn_source="system",
                    turn_kind=kind, idempotency_scope=f"automation:{sid}:{kind}",
                    flow_run_id=case_id, payload=payload, machine_id="worker-a")


def _row(db: Any, tid: str) -> Dict[str, Any]:
    return dict(db.get_task(tid))


# ---------------------------------------------------------------- F2

def test_watched_job_on_rebound_manager_withdrawn_on_first_pass_with_audit(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    case_id = _rebound_case(db, "sess-1", "sess-new")
    _automation(db, "job-turn", "sess-1", case_id, "watched_job",
                payload={"metadata": {"job_id": "job-42"}})

    res = _pass(db, o)

    row = _row(db, "job-turn")
    assert row["status"] == "withdrawn", row
    actor = db._conn().execute(
        "SELECT actor FROM mesh_turn_revisions WHERE task_id = 'job-turn' "
        "AND change_kind = 'withdraw'").fetchone()["actor"]
    assert actor == "scheduler:obsolete:manager_rebound"
    assert res.withdrawn == 1 and res.ineligible == 0
    audit = db.get_task("job-42")
    assert audit is not None and audit["status"] == "completed"
    assert "manager_rebound" in str(audit["reply_text"])
    # Nothing left for head selection to re-select.
    assert db.select_eligible_turn_heads() == []


def test_heartbeat_on_rebound_manager_withdrawn_not_reselected(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    case_id = _rebound_case(db, "sess-1", "sess-new")
    _automation(db, "hb-turn", "sess-1", case_id, "heartbeat")

    async def _otherwise_valid(_db: Any, _row: Dict[str, Any]) -> None:
        return None  # every heartbeat-specific check passes

    o._managed_heartbeat_obsolete = _otherwise_valid
    res = _pass(db, o)
    assert _row(db, "hb-turn")["status"] == "withdrawn"
    assert res.withdrawn == 1 and res.ineligible == 0


def test_stale_automation_cannot_starve_a_human_past_the_head_limit(tmp_path, monkeypatch):
    db, o = _setup(tmp_path, monkeypatch)
    _wire(o)
    stale: List[str] = []
    for i in range(26):
        old, new = f"old-{i}", f"new-{i}"
        _enrolled(db, old)
        case_id = _rebound_case(db, old, new)
        _automation(db, f"job-{i}", old, case_id, "watched_job",
                    payload={"metadata": {"job_id": f"jobid-{i}"}})
        stale.append(f"job-{i}")
    db.enqueue_turn("human", "sess-1", body="do it", turn_source="human",
                    machine_id="worker-a")

    first = _pass(db, o)
    assert first.selected == 25 and first.withdrawn == 25 and first.ineligible == 0
    _pass(db, o)
    assert _row(db, "human")["status"] == "pending"
    assert all(_row(db, t)["status"] == "withdrawn" for t in stale)


def test_withdraw_rebound_automation_covers_every_case_automation_kind(tmp_path, monkeypatch):
    db, _o = _setup(tmp_path, monkeypatch)
    case_id = _rebound_case(db, "sess-1", "sess-new")
    _automation(db, "job-turn", "sess-1", case_id, "watched_job",
                payload={"metadata": {"job_id": "job-7"}})
    db.enqueue_turn("human", "sess-1", body="mine", turn_source="human", flow_run_id=case_id)
    out = db.withdraw_rebound_automation("sess-1", case_id, actor="respawn")
    assert out == ["job-turn"]
    assert _row(db, "human")["status"] == "queued"
    assert db.get_task("job-7")["status"] == "completed"  # 4d audit record
