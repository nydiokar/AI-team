"""A82 Stage 1 — worker bookkeeping RED acceptance tests (WRK01-06).

Exercise `src.worker.agent.WorkerAgent` scheduling/result bookkeeping against
the TARGET contract (design §§5-6, packet §7). Ground truth (Stage 0):

  * `_poll_loop` (agent.py:~1548) creates one `asyncio.create_task(_handle_task)`
    for EVERY fetched row and overwrites `_active[task_id]` — no dedup against a
    row already scheduled/executing/awaiting-result. Only `_semaphore`
    (max_concurrent) bounds concurrent BACKEND calls, not scheduled handlers.
  * There is NO managed result spool in the worker (only a telemetry spool);
    `_post_result_until_accepted` posts in-memory. Design §6 requires a bounded
    carrier-local result spool with boot replay and receipt-cleanup.
  * `submit_result`/claim: the worker executes the earlier POLL snapshot, not
    the authoritative CLAIM response payload (design §5).

Constructed with `WorkerAgent.__new__` to avoid env/config dependence; each test
sets only the attributes it needs and asserts the missing/target behavior.
"""
import asyncio

import pytest

from src.worker.agent import WorkerAgent


def _bare_worker(max_concurrent: int = 2) -> WorkerAgent:
    """A WorkerAgent skeleton with just the scheduling bookkeeping attributes."""
    w = WorkerAgent.__new__(WorkerAgent)
    w._active = {}
    w._active_meta = {}
    w._slots_used = 0
    w._inflight_sessions = set()
    w._shutdown = asyncio.Event()
    w._poll_now = asyncio.Event()
    w._heartbeat_now = asyncio.Event()
    return w


def _resolve(obj, *names):
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            return fn
    return None


# --------------------------------------------------------------------------- #
# WRK01 — dedup before task creation
# --------------------------------------------------------------------------- #
def test_WRK01_already_scheduled_id_is_not_rescheduled():
    """A fetched ID already in scheduled/executing/result-delivery state must
    NOT be scheduled again (design §5, packet §7). Target: a guard consulted
    before `create_task`. RED: no such guard exists; `_poll_loop` overwrites
    `_active[task_id]` unconditionally.
    """
    w = _bare_worker()
    # Pretend t-1 is already scheduled.
    w._active["t-1"] = object()
    guard = _resolve(
        w,
        "_should_schedule",
        "_is_already_scheduled",
        "_dedup_fetched",
        "_already_tracked",
    )
    assert guard is not None, (
        "no dedup guard before create_task; `_poll_loop` reschedules an "
        "already-scheduled id (design §5 / packet §7)"
    )
    # An already-tracked id must be reported as NOT schedulable.
    schedulable = guard({"id": "t-1"}) if _guard_takes_row(guard) else guard("t-1")
    assert schedulable in (False, None), "already-scheduled id was reported schedulable"


def _guard_takes_row(fn) -> bool:
    try:
        import inspect

        params = list(inspect.signature(fn).parameters)
        return bool(params) and params[0] not in ("task_id", "tid", "id")
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# WRK02 — bounded scheduled + executing (<= 2x slots)
# --------------------------------------------------------------------------- #
def test_WRK02_scheduled_plus_executing_capped_at_twice_slots():
    """Total scheduled+executing work must be capped at <= 2x configured slots
    (design §6, packet §7). Target: a bounded scheduling-capacity check.
    RED: no capacity accounting beyond the backend semaphore.
    """
    w = _bare_worker(max_concurrent=2)
    w.cfg = type("C", (), {"max_concurrent": 2})()
    cap = _resolve(w, "_scheduling_capacity_available", "_can_schedule_more", "_has_scheduling_capacity")
    assert cap is not None, (
        "no bounded scheduling-capacity check; scheduled+executing is unbounded "
        "beyond the backend semaphore (design §6)"
    )
    # Fill to 2x slots (=4) and assert further scheduling is refused.
    for i in range(4):
        w._active[f"t-{i}"] = object()
    assert cap() in (False, 0), "scheduling capacity not enforced at 2x configured slots"


# --------------------------------------------------------------------------- #
# WRK03 — claim response payload is authoritative
# --------------------------------------------------------------------------- #
def test_WRK03_worker_executes_claim_response_not_poll_snapshot():
    """The worker must execute the frozen payload returned by CLAIM, not the
    earlier poll snapshot (design §5). Target: `_handle_task` consumes the claim
    response's `task`. RED: current `_handle_task` ignores the claim response
    body and runs the poll row.
    """
    import inspect

    from src.worker import agent

    src = inspect.getsource(agent.WorkerAgent._handle_task)
    # The claim POST currently returns a value the worker discards. The target
    # contract binds execution to that response. Assert the claim response is
    # captured and used (a variable assigned from the claim POST result).
    assert (
        "claim_response" in src
        or "claimed = await" in src
        or "resp = await asyncio.to_thread(self._http.post" in src
    ), (
        "`_handle_task` does not bind execution to the claim response payload; "
        "it executes the poll snapshot (design §5)"
    )


# --------------------------------------------------------------------------- #
# WRK04 — result-spool boot replay / receipt cleanup
# --------------------------------------------------------------------------- #
def test_WRK04_managed_result_spool_replayed_on_boot():
    """Managed results must be spooled to a bounded carrier-local store before
    POST and REPLAYED on boot, removed only on durable receipt (design §6).
    RED: no managed result spool on the worker (only a telemetry spool).
    """
    w = _bare_worker()
    replay = _resolve(w, "_replay_result_spool", "replay_result_spool", "_reconcile_result_spool")
    assert replay is not None, (
        "no managed result-spool boot replay on the worker; a delivered result "
        "obligation is not durable across restart (design §6)"
    )


def test_WRK04b_result_spool_receipt_matches_task_and_token():
    """A spooled result is removed only on a durable accepted/stale receipt that
    matches task AND token — not merely because a 2xx/timeout occurred
    (design §6). RED: no spool + receipt-matching helper.
    """
    w = _bare_worker()
    prune = _resolve(w, "_prune_result_spool_on_receipt", "_ack_result_spool", "_cleanup_result_receipt")
    assert prune is not None, (
        "no receipt-matching spool cleanup; spool must survive an HTTP timeout "
        "and require a task/token-matched receipt (design §6)"
    )


# --------------------------------------------------------------------------- #
# WRK05 — disk-full / oversize result failure is a visible hold
# --------------------------------------------------------------------------- #
def test_WRK05_oversize_result_holds_reconciliation_not_silent_discard():
    """An oversized result (over the 8 MiB envelope) or a disk-write failure must
    leave a VISIBLE recovery obligation and stop new claims — never a silent
    discard nor a false 'accepted' (design §6). RED: no envelope reservation /
    oversize handling on the worker.
    """
    w = _bare_worker()
    reserve = _resolve(w, "_reserve_result_envelope", "_spool_reserve", "_result_envelope_reserve")
    assert reserve is not None, (
        "no result-envelope reservation; an oversize/disk-full result cannot be "
        "held as a visible recovery obligation (design §6)"
    )


# --------------------------------------------------------------------------- #
# WRK06 — shutdown does not release a running backend
# --------------------------------------------------------------------------- #
def test_WRK06_shutdown_does_not_release_a_running_backend():
    """Graceful shutdown must stop NEW claims and await/stop children before ANY
    ownership release; a result pending delivery keeps session ownership even if
    the backend slot is returned (design §6, packet §7). Target: a shutdown that
    does NOT call release_task for a still-running managed turn.

    RED: assert a managed-aware shutdown guard exists that distinguishes a
    running managed turn from a releasable claimed-only one.
    """
    w = _bare_worker()
    guard = _resolve(
        w,
        "_managed_shutdown_release_ok",
        "_can_release_on_shutdown",
        "_release_safe_on_shutdown",
    )
    assert guard is not None, (
        "no managed shutdown-release guard; current shutdown release path does "
        "not distinguish a running managed turn (must NOT release) from a "
        "claimed-only one (design §6)"
    )
    # A running managed turn must NOT be releasable on shutdown.
    running_ok = guard({"id": "t-run", "status": "running", "queue_protocol": 1})
    assert running_ok in (False, None), "shutdown would release a running managed backend"
