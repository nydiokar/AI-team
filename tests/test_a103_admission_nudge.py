"""A103 — managed-admission carrier nudge.

Verifies that ``_admit_managed_session_turn`` (and its producer-turn delegate
``_admit_managed_producer_turn``) fire-and-forget a ``_nudge_worker`` call
after ``notify_turn_queue_changed()`` so the carrier worker is woken without
waiting out its adaptive 30-second poll backoff.

Tests:
  A103-01  nudge is invoked with the carrier node_id and db after admission
  A103-02  nudge failure (create_task raises) does NOT break admission
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import src.control.node_inspector as node_inspector_mod
from tests.test_turn_queue_producer1 import (  # noqa: F401  (autouse fixtures)
    _flags,
    _managed_rows,
    _no_cli_spawn,
    _register_carrier,
    _sess,
    _setup,
    _submit,
)


# A103-01 ------------------------------------------------------------------- #

def test_A103_01_managed_admission_nudges_carrier(tmp_path, monkeypatch):
    """After admission, _nudge_worker is called once with (carrier_node_id, db)."""
    db, o = _setup(tmp_path, monkeypatch)

    nudge_mock = AsyncMock()
    monkeypatch.setattr(node_inspector_mod, "_nudge_worker", nudge_mock)

    _submit(o, operation_id="nudge-a103-01")

    # The mock coroutine was created (and therefore called) exactly once.
    nudge_mock.assert_called_once()
    call_args = nudge_mock.call_args
    assert call_args[0][0] == "worker-a", (
        f"nudge must target the session's carrier, got: {call_args[0][0]!r}"
    )
    assert call_args[0][1] is db, "nudge must receive the same db handle"


# A103-02 ------------------------------------------------------------------- #

def test_A103_02_nudge_failure_does_not_break_admission(tmp_path, monkeypatch):
    """A nudge-path exception (e.g. create_task raises) must not affect admission."""
    db, o = _setup(tmp_path, monkeypatch)

    # A non-async replacement forces asyncio.create_task to raise TypeError,
    # exercising the except-swallow branch in the admission code.
    monkeypatch.setattr(node_inspector_mod, "_nudge_worker", lambda node_id, db: "not-a-coro")

    tid = _submit(o, operation_id="nudge-a103-02")
    assert tid is not None, "admission must succeed even when the nudge path raises"

    rows = _managed_rows(db)
    assert len(rows) == 1, "exactly one managed turn must be queued"
