"""A82 Stage 1 — internal producer RED acceptance tests (SYS01-08).

Assert the TARGET managed-producer admission contract (design §7, packet §8)
against current behavior, using a real temp file-backed `MeshDB`. Ground truth
(Stage 0): managed turns must set Case membership + durable
producer-token→turn linkage explicitly; `enqueue_task` does NOT populate
`flow_run_id`; there is no `coalesce_key`, no durable finalizer reconciliation,
and no managed retry (A/B/R) matrix. Each producer maps:
producer → durable trigger identity → turn id → completion effect.

These fail red by asserting the missing managed helpers / columns; where a
symbol exists today (continuation watermark) they assert the missing durable
linkage contract.
"""
from datetime import datetime

import pytest

from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus
from tests.stage8a_legacy import enqueue_pre_cutover


NOW = datetime(2026, 9, 25, 12, 0, 0)


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _session(db: MeshDB, session_id: str = "mgr-1", backend: str = "claude") -> None:
    db.upsert_session(
        Session(
            session_id=session_id,
            backend=backend,
            repo_path="/tmp/repo",
            status=SessionStatus.BUSY,
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
            machine_id="worker-a",
        )
    )


def _enqueue(db: MeshDB, task_id: str, session_id: str = "mgr-1", **payload) -> None:
    enqueue_pre_cutover(
        db,
        task_id=task_id,
        session_id=session_id,
        machine_id=None,
        backend="claude",
        action="resume_session",
        payload={"task_id": task_id, "prompt": "p", **payload},
    )


def _enqueue_turn(db: MeshDB, **kw):
    """The target transport-neutral managed admission service (design §4). It
    does not exist yet; resolve dynamically and fail red when absent.
    """
    for name in ("enqueue_turn", "admit_turn", "enqueue_managed_turn"):
        fn = getattr(db, name, None)
        if callable(fn):
            return fn(**kw)
    pytest.fail(
        "no managed admission helper (enqueue_turn) exposing FIFO sequence, "
        "coalesce/idempotency and durable producer-token linkage (design §4/§7); "
        "contract not implemented"
    )


def _resolve(obj, *names):
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            return fn
    return None


# --------------------------------------------------------------------------- #
# SYS01 — busy-Manager wake accepted while busy, single durable continuation
# --------------------------------------------------------------------------- #
def test_SYS01_busy_manager_continuation_accepted_while_busy_single_turn(tmp_path):
    """A Case continuation must be admissible to a BUSY Manager as ONE durable
    managed turn (design §7). Target: enqueue_turn admits a continuation with a
    durable Case+generation trigger identity. RED: managed admission missing.
    """
    db = _db(tmp_path)
    _session(db, "mgr-1")
    case_id = db.open_case(objective="obj", session_id="mgr-1", role="manager")
    tid = _enqueue_turn(
        db,
        session_id="mgr-1",
        body="continue the case",
        turn_kind="continuation",
        flow_run_id=case_id,
        coalesce_key=f"case:{case_id}:gen:1",
    )
    assert tid, "continuation was not admitted to a busy Manager"
    row = db.get_task(tid)
    assert row.get("flow_run_id") == case_id, "managed continuation did not record Case membership"


# --------------------------------------------------------------------------- #
# SYS02 — obsolete continuation is withdrawn at activation
# --------------------------------------------------------------------------- #
def test_SYS02_obsolete_continuation_withdrawn(tmp_path):
    """A continuation made obsolete by an intervening human review must be
    withdrawn with a reason at activation, not run (design §7, §3.10). Target: a
    revalidation/withdraw seam. RED: helper missing.
    """
    db = _db(tmp_path)
    _session(db, "mgr-1")
    withdraw = _resolve(db, "withdraw_turn", "revalidate_and_withdraw", "withdraw_obsolete_continuation")
    assert withdraw is not None, (
        "no withdraw seam for obsolete system continuations at activation "
        "(design §7)"
    )


# --------------------------------------------------------------------------- #
# SYS03 — durable token → turn crash handoff (deterministic id)
# --------------------------------------------------------------------------- #
def test_SYS03_producer_token_maps_to_deterministic_turn_id(tmp_path):
    """A producer that claims a scheduling token must map it durably to ONE
    deterministic turn id; a crash retry after the token claim must discover the
    SAME id, not mint a new random one (design §7, packet §8). RED: no durable
    token→turn linkage for managed turns.
    """
    db = _db(tmp_path)
    _session(db, "mgr-1")
    link = _resolve(db, "link_producer_token", "claim_producer_token_turn", "token_to_turn")
    assert link is not None, (
        "no durable producer-token→turn linkage; a crash after token claim would "
        "mint a new random id on retry (design §7)"
    )
    id1 = link(coalesce_key="case:c1:gen:2", session_id="mgr-1")
    id2 = link(coalesce_key="case:c1:gen:2", session_id="mgr-1")
    assert id1 == id2, "token→turn linkage was not deterministic across retries"


# --------------------------------------------------------------------------- #
# SYS04 — restart finalizer reconcilable from durable links/results
# --------------------------------------------------------------------------- #
def test_SYS04_finalizer_is_restart_reconcilable(tmp_path):
    """Finalization must be restart-safe from durable state; an in-memory
    `asyncio.create_task(_finalize_...)` cannot be the sole completion mechanism
    (design §7). A104: there is no finalizer at all — the wake's inbox messages
    are acked INSIDE its own terminal txn, so a fresh process over the same DB
    finds the round counted and nothing pending or ready to re-admit.
    """
    from tests.inbox_seed import seed_finished_child

    db = _db(tmp_path)
    _session(db, "mgr-1")
    case_id = db.open_case(objective="obj", session_id="mgr-1", role="manager")
    seed_finished_child(db, case_id, "w1", requester="mgr-1")
    (msg,) = db.pending_for("mgr-1").messages
    wake = str(db.enqueue_turn(
        session_id="mgr-1", body="wake", turn_kind="continuation", operation_id="wake-1",
        flow_run_id=case_id, machine_id="worker-a", inbox_message_ids=[msg.message_id],
    ))
    with db._write() as conn:  # the scheduler's activation (not under test here)
        conn.execute("UPDATE mesh_tasks SET status = 'pending' WHERE id = ?", (wake,))
    tok = db.claim_turn(wake, "worker-a", "worker_daemon", "inc-1")
    db.start_turn(wake, tok, incarnation_id="inc-1")
    assert db.complete_turn(wake, tok, {"success": True, "output": "ok"}, status="completed")

    fresh = MeshDB(str(tmp_path / "mesh.db"))  # a restarted process: no in-memory state
    state = fresh._conn().execute(
        "SELECT state, delivery_turn_id FROM agent_inbox WHERE message_id = ?", (msg.message_id,),
    ).fetchone()
    assert (state["state"], state["delivery_turn_id"]) == ("acked", wake)
    assert fresh.pending_for("mgr-1").messages == []
    assert fresh.inbox_ready_recipients() == []
    assert fresh.inbox_rounds_used(case_id) == 1


# --------------------------------------------------------------------------- #
# SYS05 — A/B/R retry matrix
# --------------------------------------------------------------------------- #
def test_SYS05_retry_ABR_matrix_supersede_and_head_rules(tmp_path):
    """The fixed retry rule (design §7, §3.9): failed A + earlier waiting B +
    eligible pause → supersede A's retry, release only that pause, run B; with no
    B, admit head retry R through its own pause only; later B stays after R.
    RED: no managed retry-decision helper.
    """
    db = _db(tmp_path)
    decide = _resolve(db, "decide_retry", "resolve_retry_decision", "managed_retry_decision")
    assert decide is not None, (
        "no managed A/B/R retry-decision helper enforcing supersede/head rules "
        "(design §7 / §3.9)"
    )
    # With an earlier waiting B, A's automatic retry is superseded and B is head.
    decision = decide(
        failed_task_id="A",
        earlier_waiting=["B"],
        pause_eligible=True,
    )
    assert getattr(decision, "supersede_retry", None) is True or (
        isinstance(decision, dict) and decision.get("supersede_retry") is True
    ), "retry decision did not supersede A's retry in favour of earlier waiting B"


# --------------------------------------------------------------------------- #
# SYS06 — heartbeat expiry (idle-only, expires behind useful work)
# --------------------------------------------------------------------------- #
def test_SYS06_cache_heartbeat_is_idle_only_and_expires(tmp_path):
    """Cache-heartbeat turns are idle-only with a deadline and must be
    skipped/expired when useful work exists (design §7, §3.10). Target: an
    eligibility check that returns False when real work is queued. RED: helper
    missing.
    """
    db = _db(tmp_path)
    eligible = _resolve(db, "heartbeat_eligible", "is_heartbeat_eligible", "heartbeat_admission_eligible")
    assert eligible is not None, (
        "no idle-only heartbeat eligibility check (design §7); heartbeat could "
        "queue behind real work"
    )
    _session(db, "mgr-1")
    _enqueue(db, "real-work", session_id="mgr-1")  # real queued work exists
    assert eligible(session_id="mgr-1") in (False, None), (
        "heartbeat reported eligible while real queued work exists"
    )


# --------------------------------------------------------------------------- #
# SYS07 — respawn linkage (durable new-session/turn link)
# --------------------------------------------------------------------------- #
def test_SYS07_respawn_links_new_session_turn_durably(tmp_path):
    """Manager respawn must persist a durable new-session/turn linkage and NOT
    treat the respawn as sending to the closed old session (design §7). RED: no
    respawn-linkage helper.
    """
    db = _db(tmp_path)
    link = _resolve(db, "link_respawn_turn", "record_respawn_link", "respawn_new_session_turn")
    assert link is not None, (
        "no durable respawn new-session/turn linkage (design §7)"
    )


# --------------------------------------------------------------------------- #
# SYS08 — watched-job single notification / Case membership
# --------------------------------------------------------------------------- #
def test_SYS08_watched_job_single_notification_one_case(tmp_path):
    """A watched-job completion produces ONE notification identity attached to
    the right Case — no second wake for the same obligation (design §7). Target:
    idempotent coalesce on the watched-job identity. RED: no coalesce_key on
    managed rows to enforce single-notification.
    """
    db = _db(tmp_path)
    _session(db, "mgr-1")
    case_id = db.open_case(objective="obj", session_id="mgr-1", role="manager")
    # Two admissions with the SAME watched-job coalesce identity must collapse
    # to one accepted turn.
    tid1 = _enqueue_turn(
        db,
        session_id="mgr-1",
        body="job X finished",
        turn_kind="continuation",
        flow_run_id=case_id,
        coalesce_key=f"watched:jobX",
    )
    tid2 = _enqueue_turn(
        db,
        session_id="mgr-1",
        body="job X finished",
        turn_kind="continuation",
        flow_run_id=case_id,
        coalesce_key=f"watched:jobX",
    )
    assert tid1 == tid2, "watched-job notification produced a duplicate wake (coalesce not enforced)"
