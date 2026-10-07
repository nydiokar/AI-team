"""A98 O2/O5/O7 — graceful worker-restart reconcile beyond the O1 fresh-fork.

O2: a Manager left ERROR+driver_lost by a restart is crash-respawn-eligible.
O5: the orchestrator notifies the operator once per detected node incarnation flip.
O7: the worker memory watchdog samples RSS and flags pressure past a threshold.
"""
import types

import pytest

from src.orchestrator import TaskOrchestrator
from src.core import SessionStatus


# ----------------------------- O2 --------------------------------------------
def _sess(status, driver_status="lost"):
    return types.SimpleNamespace(status=status, driver_status=driver_status)


def test_o2_error_plus_lost_is_restart_dead():
    assert TaskOrchestrator._is_restart_dead_session(_sess(SessionStatus.ERROR), True) is True


def test_o2_error_without_lost_is_not_dead():
    # A genuine non-restart ERROR must NOT be auto-respawned.
    assert TaskOrchestrator._is_restart_dead_session(_sess(SessionStatus.ERROR, ""), True) is False


def test_o2_respects_opt_out_flag():
    assert TaskOrchestrator._is_restart_dead_session(_sess(SessionStatus.ERROR), False) is False


def test_o2_live_session_not_dead():
    assert TaskOrchestrator._is_restart_dead_session(_sess(SessionStatus.AWAITING_INPUT), True) is False


def test_o2_none_session_not_dead():
    assert TaskOrchestrator._is_restart_dead_session(None, True) is False


# ----------------------------- O5 --------------------------------------------
class _Notifier:
    def __init__(self):
        self.calls = []

    async def notify_restart(self, **kw):
        self.calls.append(kw)


def _orch(notifier):
    o = TaskOrchestrator.__new__(TaskOrchestrator)
    o.notifier = notifier
    return o


def _patch_nodes(monkeypatch, nodes, disabled=False, lost=2):
    import src.orchestrator as orch_mod
    import src.control.db as dbmod
    db = types.SimpleNamespace(
        list_nodes=lambda status=None: nodes,
        count_lost_sessions_for_node=lambda nid: lost,
    )
    monkeypatch.setattr(orch_mod, "get_db", lambda: db, raising=False)
    monkeypatch.setattr(dbmod, "restart_notify_disabled", lambda: disabled, raising=False)


@pytest.mark.asyncio
async def test_o5_first_sight_does_not_notify(monkeypatch):
    n = _Notifier()
    o = _orch(n)
    _patch_nodes(monkeypatch, [{"node_id": "Horse", "incarnation_id": "inc1"}])
    await o._detect_node_restarts_once()
    assert n.calls == []  # baseline only


@pytest.mark.asyncio
async def test_o5_incarnation_flip_notifies_once(monkeypatch):
    n = _Notifier()
    o = _orch(n)
    _patch_nodes(monkeypatch, [{"node_id": "Horse", "incarnation_id": "inc1"}])
    await o._detect_node_restarts_once()                      # baseline
    _patch_nodes(monkeypatch, [{"node_id": "Horse", "incarnation_id": "inc2"}])
    await o._detect_node_restarts_once()                      # flip -> notify
    await o._detect_node_restarts_once()                      # unchanged -> no 2nd notify
    assert len(n.calls) == 1
    assert n.calls[0]["node_id"] == "Horse"
    assert n.calls[0]["lost_sessions"] == 2
    assert n.calls[0]["old_incarnation"] == "inc1"
    assert n.calls[0]["new_incarnation"] == "inc2"


@pytest.mark.asyncio
async def test_o5_opt_out_disables_notify(monkeypatch):
    n = _Notifier()
    o = _orch(n)
    _patch_nodes(monkeypatch, [{"node_id": "Horse", "incarnation_id": "inc1"}], disabled=True)
    await o._detect_node_restarts_once()
    _patch_nodes(monkeypatch, [{"node_id": "Horse", "incarnation_id": "inc2"}], disabled=True)
    await o._detect_node_restarts_once()
    assert n.calls == []


# ----------------------------- O7 --------------------------------------------
def _worker_stub():
    from src.worker.agent import WorkerAgent  # noqa: F401 — ensure import path valid
    w = object.__new__(WorkerAgent)
    w.cfg = types.SimpleNamespace(node_id="drill", max_concurrent=2)
    w._backends = {}
    return w


def test_o7_watchdog_flags_pressure(monkeypatch):
    w = _worker_stub()
    import src.control.db as dbmod
    monkeypatch.setattr(dbmod, "worker_memory_watchdog_disabled", lambda: False)
    monkeypatch.setenv("WORKER_MEMORY_WATCHDOG_MB", "1")  # 1 MB ceiling => always over
    monkeypatch.setenv("WORKER_MEMORY_WATCHDOG_PCT", "100")
    sample = w._memory_watchdog_sample()
    assert sample is not None
    assert sample["over_threshold"] is True
    assert sample["rss_mb"] >= 1


def test_o7_watchdog_disabled_returns_none(monkeypatch):
    w = _worker_stub()
    import src.control.db as dbmod
    monkeypatch.setattr(dbmod, "worker_memory_watchdog_disabled", lambda: True)
    assert w._memory_watchdog_sample() is None
