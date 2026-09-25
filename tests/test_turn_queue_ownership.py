"""A82 Stage 1 — carrier/DB ownership RED acceptance tests (OWN01-10).

Real temp file-backed SQLite (`MeshDB`), no network/paid CLI. These assert the
TARGET protocol-1 ownership contract (design §6, A82 §15 decisions 1-2) against
current behavior. Ground truth from Stage 0:

  * `complete_task`/`fail_task` (db.py:~2268/2290) UPDATE `WHERE id=?` only — no
    ownership/claim-token/status predicate — and swallow write errors, returning
    silently as success.
  * `claim_task` (db.py:~2039) guards `status='pending'` and stamps `claimed_by`
    = node_id (a node identity, NOT a fresh per-attempt claim token).
  * `submit_result` (task_server.py:~844) guards `claimed_by != node_id`; a
    reaped/re-offered duplicate from the SAME node passes.
  * `mesh_tasks` has NONE of `claim_token/queue_protocol/started_at/...`.

The strict protocol-1 helpers (compare-and-swap claim token, atomic
terminal+native-id commit, once-only start, recovery hold) do NOT exist yet.
Each test resolves the target helper by name and fails red (assertion) when the
contract is absent; where the symbol exists today it fails by assertion against
the current permissive behavior.
"""
import json
from datetime import datetime, timedelta

import pytest

from src.control.db import MeshDB
from src.core.interfaces import Session, SessionStatus


NOW = datetime(2026, 9, 25, 12, 0, 0)


def _db(tmp_path) -> MeshDB:
    return MeshDB(str(tmp_path / "mesh.db"))


def _session(db: MeshDB, session_id: str = "sess-1", backend: str = "claude") -> None:
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


def _node(db: MeshDB, node_id: str = "worker-a", incarnation_id: str = "inc-a") -> dict:
    db.upsert_node(
        node_id=node_id,
        tailscale_ip="100.64.0.10",
        api_port=9001,
        backends=["claude"],
        max_concurrent=2,
        incarnation_id=incarnation_id,
    )
    return db.get_node(node_id)


def _enqueue(db: MeshDB, task_id: str = "t-1", session_id: str = "sess-1") -> None:
    db.enqueue_task(
        task_id=task_id,
        session_id=session_id,
        machine_id=None,
        backend="claude",
        action="resume_session",
        payload={"task_id": task_id, "prompt": "hi"},
    )


def _managed_claim(db: MeshDB, **kw):
    """Target strict managed claim returning a fresh opaque claim token bound to
    task/carrier-kind/process-incarnation/session. Missing today → red.
    """
    for name in ("claim_turn", "managed_claim_task", "claim_managed_turn", "claim_task_v2"):
        fn = getattr(db, name, None)
        if callable(fn):
            return fn(**kw)
    pytest.fail(
        "no managed claim helper on MeshDB returning a fresh per-attempt claim "
        "token (design §6); contract not implemented"
    )


def _managed_complete(db: MeshDB, **kw):
    """Target atomic managed completion: verify token + allowed state, write
    outcome AND native session id/active identity, transition terminal — all in
    one transaction; throw a typed failure on a losing predicate. Missing → red.
    """
    for name in ("complete_turn", "managed_complete_task", "complete_managed_turn"):
        fn = getattr(db, name, None)
        if callable(fn):
            return fn(**kw)
    pytest.fail(
        "no atomic managed completion helper on MeshDB (native-id + active "
        "identity committed with the terminal transition, ownership predicate); "
        "contract not implemented"
    )


def _managed_start(db: MeshDB, **kw):
    for name in ("start_turn", "authorize_start", "managed_start_task"):
        fn = getattr(db, name, None)
        if callable(fn):
            return fn(**kw)
    pytest.fail(
        "no managed start-authorization helper on MeshDB (claimed->running for a "
        "specific claim token, once-only); contract not implemented"
    )


# --------------------------------------------------------------------------- #
# OWN01 — carrier kind / incarnation / token fencing
# --------------------------------------------------------------------------- #
def test_OWN01_claim_returns_fresh_token_bound_to_carrier_and_incarnation(tmp_path):
    """Claim must return a fresh opaque token bound to task + carrier
    kind/process incarnation + session (design §6). Current `claim_task` returns
    a bool and records only `claimed_by=node_id`. RED: managed claim helper
    missing / no `claim_token` column.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db, incarnation_id="inc-a")
    _enqueue(db)

    token = _managed_claim(
        db, task_id="t-1", node_id="worker-a", carrier_kind="gateway_local", incarnation_id="inc-a"
    )
    assert token, "claim did not return a claim token"
    row = db.get_task("t-1")
    assert row.get("claim_token") == token, "claim_token not persisted on the row"


def test_OWN01b_legacy_claimed_by_is_node_identity_not_attempt_token(tmp_path):
    """Documents the CURRENT gap: `claimed_by` is a node id, not a per-attempt
    token, so two claim attempts by the same node are indistinguishable. Target
    contract requires a distinct token per attempt.

    RED: the row exposes no per-attempt claim_token column.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db)
    _enqueue(db)
    assert db.claim_task("t-1", "worker-a") is True
    row = db.get_task("t-1")
    assert row.get("claim_token"), (
        "current claim records only node identity (claimed_by); a per-attempt "
        "claim_token is required for fencing (design §6)"
    )


# --------------------------------------------------------------------------- #
# OWN02 — lost claim / start response replay (same task+process → same auth)
# --------------------------------------------------------------------------- #
def test_OWN02_repeated_start_same_token_returns_same_authorization(tmp_path):
    """A repeated start with the same live token must return the SAME
    authorization, not create a second executor (design §6). RED: start helper
    missing.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db)
    _enqueue(db)
    token = _managed_claim(
        db, task_id="t-1", node_id="worker-a", carrier_kind="gateway_local", incarnation_id="inc-a"
    )
    auth1 = _managed_start(db, task_id="t-1", claim_token=token)
    auth2 = _managed_start(db, task_id="t-1", claim_token=token)
    assert auth1 == auth2, "repeated start with the same token returned different authorizations"


# --------------------------------------------------------------------------- #
# OWN03 — once-only invocation (start consumes exactly once)
# --------------------------------------------------------------------------- #
def test_OWN03_start_is_once_only_for_a_token(tmp_path):
    """The claim owner consumes start exactly once; a distinct/stale token must
    not authorize a parallel backend call (design §6). RED: start helper missing.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db)
    _enqueue(db)
    token = _managed_claim(
        db, task_id="t-1", node_id="worker-a", carrier_kind="gateway_local", incarnation_id="inc-a"
    )
    _managed_start(db, task_id="t-1", claim_token=token)
    # A different token must be refused (typed failure or falsy authorization).
    refused = None
    try:
        refused = _managed_start(db, task_id="t-1", claim_token="some-other-token")
    except Exception as e:  # noqa: BLE001
        refused = False
    assert not refused, "a foreign token was allowed to start an already-started turn"


# --------------------------------------------------------------------------- #
# OWN04 — post-start restart hold (old authorization cannot start)
# --------------------------------------------------------------------------- #
def test_OWN04_restarted_carrier_old_token_cannot_start(tmp_path):
    """After carrier restart (new incarnation) the OLD authorization cannot
    start anything (design §6). RED: start helper / incarnation binding missing.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db, incarnation_id="inc-a")
    _enqueue(db)
    old_token = _managed_claim(
        db, task_id="t-1", node_id="worker-a", carrier_kind="gateway_local", incarnation_id="inc-a"
    )
    # Carrier restarts in place — new incarnation.
    _node(db, incarnation_id="inc-b")
    started = None
    try:
        started = _managed_start(db, task_id="t-1", claim_token=old_token, incarnation_id="inc-b")
    except Exception:  # noqa: BLE001
        started = False
    assert not started, "old authorization started work after a carrier restart"


# --------------------------------------------------------------------------- #
# OWN05 — old-result rejection (superseded attempt)
# --------------------------------------------------------------------------- #
def test_OWN05_superseded_token_result_is_rejected(tmp_path):
    """A result bearing a superseded claim token must be rejected/ignored, not
    overwrite the current attempt's outcome (design §6). RED: managed complete
    with token predicate missing.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db)
    _enqueue(db)
    token_a = _managed_claim(
        db, task_id="t-1", node_id="worker-a", carrier_kind="gateway_local", incarnation_id="inc-a"
    )
    # Reoffer/reclaim produces a NEW token (superseding token_a).
    token_b = _managed_claim(
        db, task_id="t-1", node_id="worker-a", carrier_kind="gateway_local", incarnation_id="inc-b"
    )
    assert token_a != token_b
    rejected = None
    try:
        rejected = _managed_complete(
            db, task_id="t-1", claim_token=token_a, result={"output": "stale"}
        )
    except Exception:  # noqa: BLE001
        rejected = False
    assert not rejected, "a superseded token committed a result over the current attempt"


# --------------------------------------------------------------------------- #
# OWN06 — same-result idempotency
# --------------------------------------------------------------------------- #
def test_OWN06_identical_result_retry_is_idempotent(tmp_path):
    """An identical repeated result for the current token succeeds without
    duplicate side effects (design §6). RED: managed complete missing.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db)
    _enqueue(db)
    token = _managed_claim(
        db, task_id="t-1", node_id="worker-a", carrier_kind="gateway_local", incarnation_id="inc-a"
    )
    _managed_start(db, task_id="t-1", claim_token=token)
    r1 = _managed_complete(db, task_id="t-1", claim_token=token, result={"output": "done"})
    r2 = _managed_complete(db, task_id="t-1", claim_token=token, result={"output": "done"})
    assert r1 == r2, "identical result retry was not idempotent"


# --------------------------------------------------------------------------- #
# OWN07 — stop/close/compact do not cancel the wrong (newest waiting) turn
# --------------------------------------------------------------------------- #
def test_OWN07_active_row_read_from_ledger_not_last_submitted(tmp_path):
    """Stop must resolve the ACTIVE ledger row, not the most recently submitted
    ID (design §4). Target: an active-turn resolver reading owned state from the
    ledger. RED: resolver missing.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db)
    # Older running turn + newer queued turn.
    _enqueue(db, task_id="t-running", session_id="sess-1")
    _enqueue(db, task_id="t-queued-newer", session_id="sess-1")
    resolver = None
    for name in ("get_active_turn", "active_turn_for_session", "get_running_turn"):
        resolver = getattr(db, name, None)
        if callable(resolver):
            break
        resolver = None
    assert resolver is not None, (
        "no active-turn resolver on MeshDB; stop must target the active ledger "
        "row, not the newest submitted id (design §4)"
    )


# --------------------------------------------------------------------------- #
# OWN08 — native-id atomic commit (terminal + native id together)
# --------------------------------------------------------------------------- #
def test_OWN08_completion_commits_native_id_atomically(tmp_path):
    """Completion must commit the backend/native session id AND active identity
    ATOMICALLY with the terminal transition (design §6, A82 §15 dec.2). Current
    `complete_task` writes only the result and reconciles native id in a
    separate later save. RED: atomic managed complete missing.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db)
    _enqueue(db)
    token = _managed_claim(
        db, task_id="t-1", node_id="worker-a", carrier_kind="gateway_local", incarnation_id="inc-a"
    )
    _managed_start(db, task_id="t-1", claim_token=token)
    _managed_complete(
        db,
        task_id="t-1",
        claim_token=token,
        result={"output": "done"},
        native_session_id="native-XYZ",
    )
    row = db.get_task("t-1")
    assert row["status"] == "completed"
    sess = db.get_session("sess-1")
    assert sess and sess.get("backend_session_id") == "native-XYZ", (
        "native session id was NOT committed atomically with the terminal "
        "outcome (successor could re-create_session)"
    )


def test_OWN08b_legacy_complete_task_has_no_ownership_predicate(tmp_path):
    """Documents the CURRENT gap that dec.2 preserves for legacy but forbids for
    managed: `complete_task` marks ANY task complete with no status/ownership
    predicate. The target managed path must reject a complete on a non-running
    (e.g. already withdrawn) turn.

    RED: no managed guard — assert that completing a QUEUED (never-started)
    managed turn is refused.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db)
    _enqueue(db, task_id="t-queued")
    # The row is 'pending' (queued) — never claimed/started.
    accepted = None
    try:
        accepted = _managed_complete(
            db, task_id="t-queued", claim_token="no-such-token", result={"output": "x"}
        )
    except Exception:  # noqa: BLE001
        accepted = False
    assert not accepted, "managed completion accepted a result for a never-started turn"


# --------------------------------------------------------------------------- #
# OWN09 — stale session-save defense (completion must not be overwritten)
# --------------------------------------------------------------------------- #
def test_OWN09_stale_full_session_save_cannot_revert_canonical_completion(tmp_path):
    """A stale whole-session save must not revert the canonical completion's
    active identity/native id (design §6, P1). Target: field-scoped/versioned
    session update owning only completion fields. RED: versioned session update
    helper missing.
    """
    db = _db(tmp_path)
    _session(db)
    # Target: a config revision on the session row that a stale save cannot
    # clobber. No such helper/field exists today.
    helper = None
    for name in ("update_session_fields", "save_session_fields", "complete_session_identity"):
        helper = getattr(db, name, None)
        if callable(helper):
            break
        helper = None
    assert helper is not None, (
        "no field-scoped session update; whole-session upsert lets a stale save "
        "revert canonical completion (design §6/P1)"
    )


# --------------------------------------------------------------------------- #
# OWN10 — confirmed-quiescence recovery resolution
# --------------------------------------------------------------------------- #
def test_OWN10_recovery_resolution_requires_recorded_quiescence_evidence(tmp_path):
    """Recovery resolution must require the current token PLUS a recorded
    authenticated quiescence observation or a durable result — never a bare
    boolean/offline status (design §6). RED: recovery-resolution helper missing.
    """
    db = _db(tmp_path)
    _session(db)
    _node(db)
    _enqueue(db)
    helper = None
    for name in ("resolve_recovery", "resolve_turn_recovery", "recover_turn"):
        helper = getattr(db, name, None)
        if callable(helper):
            break
        helper = None
    assert helper is not None, (
        "no recovery-resolution helper on MeshDB requiring recorded quiescence "
        "evidence + current token (design §6)"
    )
    # A resolution with NO evidence must be refused.
    resolved = None
    try:
        resolved = helper(task_id="t-1", claim_token="tok", quiescence_evidence=None)
    except Exception:  # noqa: BLE001
        resolved = False
    assert not resolved, "recovery resolved without recorded quiescence evidence"
