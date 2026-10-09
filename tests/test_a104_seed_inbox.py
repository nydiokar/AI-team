"""A104 Gate 4 — the one-time inbox migration (scripts/a104_seed_inbox.py).

SM01 dry-run writes nothing, yet reports the real after-state (lost = 0)
SM02 apply: a finished, unreviewed, unconsumed legacy child is seeded pending to the
     Case member that was executing when it was dispatched; a reviewed child whose
     A84 row never cleared is acked; Manager-own / operator / wake junk is
     dead(superseded); stuck cont: tokens discharged; never-run telemetry relabelled
SM03 apply is idempotent (a second run changes nothing) and never loses a genuine one
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

import src.control.db as db_mod
from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus

_SPEC = importlib.util.spec_from_file_location(
    "a104_seed_inbox", Path(__file__).resolve().parent.parent / "scripts" / "a104_seed_inbox.py")
mig = importlib.util.module_from_spec(_SPEC)
sys.modules["a104_seed_inbox"] = mig
_SPEC.loader.exec_module(mig)  # type: ignore[union-attr]


def _task(conn, tid, *, session=None, status="completed", created="2026-10-07T10:00:00+00:00",
          claimed=None, completed=None, source="web_session", case=None, protocol=1, action="resume_session",
          payload=None, result=None):
    conn.execute(
        "INSERT INTO mesh_tasks (id, session_id, backend, action, payload, status, queue_protocol, flow_run_id, "
        "created_at, updated_at, claimed_at, completed_at, result) VALUES (?, ?, 'claude', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, session, action, payload or json.dumps({"metadata": {"source": source}}), status, protocol, case,
         created, created, claimed, completed, result))


@pytest.fixture()
def seeded(tmp_path):
    db = MeshDB(str(tmp_path / "mesh.db"))
    for sid in ("mgr", "wkr"):
        db.upsert_session(Session(session_id=sid, backend="claude", repo_path="/r",
                                         status=SessionStatus.IDLE, created_at="2026-10-07T09:00:00+00:00",
                                         updated_at="2026-10-07T09:00:00+00:00", machine_id="m"))
    legacy = db.open_case("legacy", "mgr", role="manager")
    outbox = db.open_case("outbox", "mgr", role="manager")
    db.create_flow_link(legacy, "session", "wkr", "worker", created_by="manager")
    with db._write() as conn:
        # The Manager's turn that dispatched the legacy child (running 10:00–10:05).
        _task(conn, "mgr-turn", session="mgr", created="2026-10-07T09:59:00+00:00",
              claimed="2026-10-07T10:00:00+00:00", completed="2026-10-07T10:05:00+00:00")
        # Genuine: finished, unreviewed, unconsumed (no requester recorded — pre-A104).
        _task(conn, "child-ok", session="wkr", created="2026-10-07T10:01:00+00:00",
              completed="2026-10-07T10:30:00+00:00", source="automation_session", case=legacy)
        # Reviewed child whose A84 row never cleared.
        _task(conn, "child-rev", session="wkr", created="2026-10-07T11:00:00+00:00",
              completed="2026-10-07T11:30:00+00:00", source="automation_session", case=outbox)
        # Junk: a Manager-own turn with an A84 row.
        _task(conn, "mgr-own", session="mgr", created="2026-10-07T12:00:00+00:00", case=outbox)
        # A stuck continuation token + a withdrawn wake with 'cancelled' telemetry.
        _task(conn, f"cont:{outbox}:1", status="pending", protocol=0, action="manager_continuation",
              payload=json.dumps({"case_id": outbox, "attempt": 600}))
        _task(conn, "cturn_x", session="mgr", status="withdrawn")
        conn.execute("INSERT INTO llm_turns (turn_id, task_id, session_id, final_status, created_at, updated_at) "
                     "VALUES ('cturn_x', 'cturn_x', 'mgr', 'cancelled', '2026-10-07T12:00:00+00:00', '2026-10-07T12:00:00+00:00')")
        for tid, delivered in (("child-rev", None), ("mgr-own", None)):
            conn.execute("INSERT INTO agent_inbox (message_id, recipient_session_id, about_task_id, case_id, kind, "
                         "outcome, state, attempts, last_error, created_at, updated_at, alerted_at) "
                         "VALUES (?, '', ?, ?, 'completion', 'success', 'dead', 0, 'unaddressed_pre_inbox', "
                         "'2026-10-07T12:00:00+00:00', '2026-10-07T12:00:00+00:00', '2026-10-07T12:00:00+00:00')",
                         (f"completion:{tid}", tid, outbox))
    for t, c in (("child-ok", legacy), ("child-rev", outbox)):
        db.create_flow_link(c, "task", t, "task", created_by="manager")
    db.create_flow_link(outbox, "task", "mgr-own", "task", created_by="system")
    db.append_flow_event(outbox, "review.accepted", "manager", entity_type="task", entity_id="child-rev",
                         payload={"verdict": "accepted"})
    return db, legacy, outbox


def _inbox(db):
    return {r["about_task_id"]: dict(r) for r in db._conn().execute("SELECT * FROM agent_inbox")}


def test_SM01_dry_run_writes_nothing_and_reports_the_outcome(seeded):
    db, legacy, outbox = seeded
    before = _inbox(db)
    plan = mig.build_plan(db, "x")
    lost = mig._simulate(db, plan)
    assert lost == [] and plan.genuine == ["child-ok"]
    assert _inbox(db) == before
    assert db.get_task(f"cont:{outbox}:1")["status"] == "pending"
    by_case = {c.case_id: c for c in plan.cases}
    assert by_case[legacy].pending_after == ["child-ok"]


def test_SM02_apply_seeds_acks_retires_discharges_relabels(seeded):
    db, legacy, outbox = seeded
    plan = mig.build_plan(db, "x")
    mig.apply_plan(db, plan)
    rows = _inbox(db)
    assert (rows["child-ok"]["state"], rows["child-ok"]["recipient_session_id"]) == ("pending", "mgr")
    assert rows["child-ok"]["resolution"] == "seeded_a104:executing_member_at_dispatch"
    assert (rows["child-rev"]["state"], rows["child-rev"]["resolution"]) == ("acked", "reviewed")
    assert (rows["mgr-own"]["state"], rows["mgr-own"]["last_error"]) == ("dead", "superseded")
    tok = db.get_task(f"cont:{outbox}:1")
    assert (tok["status"], tok["error"]) == ("cancelled", "superseded_by_agent_inbox")
    assert db._conn().execute("SELECT final_status FROM llm_turns WHERE turn_id = 'cturn_x'").fetchone()[0] == "withdrawn"
    assert [m.about_task_id for m in db.pending_for("mgr").messages] == ["child-ok"]


def test_SM03_apply_is_idempotent_and_lossless(seeded):
    db, *_ = seeded
    mig.apply_plan(db, mig.build_plan(db, "x"))
    first = _inbox(db)
    plan2 = mig.build_plan(db, "x")
    mig.apply_plan(db, plan2)
    assert {k: (v["state"], v["recipient_session_id"]) for k, v in _inbox(db).items()} == \
        {k: (v["state"], v["recipient_session_id"]) for k, v in first.items()}
    assert plan2.tokens_discharged == [] and plan2.telemetry_relabelled == 0
    assert mig.verify(db._conn(), plan2) == []
