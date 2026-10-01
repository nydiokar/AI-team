"""The recovery artifact survives an oversize result and a carrier restart."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.worker.managed_result_spool import ManagedResultSpool, OversizeResultError


def test_oversize_result_is_preserved_outside_replay_spool(tmp_path: Path) -> None:
    spool = ManagedResultSpool(tmp_path, max_envelope_bytes=32)
    envelope: dict[str, str] = {"output": "complete result " * 100}
    with pytest.raises(OversizeResultError):
        spool.commit("turn-1", "private-token", envelope)

    artifact = spool.preserve_oversize("turn-1", "private-token", envelope)
    assert artifact.is_file()
    assert "private-token" not in artifact.name
    assert json.loads(artifact.read_text()) == envelope
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert not list(spool.dir.glob("*.json"))
    assert ManagedResultSpool(tmp_path).has_oversize_artifacts()


def test_oversize_artifact_budget_is_finite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.worker.managed_result_spool as spool_module

    monkeypatch.setattr(spool_module, "MAX_OVERSIZE_ARTIFACT_BYTES", 32)
    spool = ManagedResultSpool(tmp_path)
    with pytest.raises(spool_module.ResultSpoolError):
        spool.preserve_oversize("turn-2", "token", {"output": "x" * 100})
    assert not list((spool.dir / "oversize").glob("*.json"))


# --------------------------------------------------------------------------- #
# [A82 Stage 4e] §7 deferral closed end to end: the genuine carrier path
# (`_handle_task` → `_deliver_managed_result`) against the real task server and
# a real MeshDB. The full result lands in carrier artifact storage, the
# controller row points at it, the hold is NOT auto-failed by the reconciler,
# it survives a carrier restart, and an operator resolution releases it.
# --------------------------------------------------------------------------- #
def test_oversize_artifact_held_through_restart_until_operator_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from fastapi.testclient import TestClient

    import src.control.task_server as ts
    import src.worker.agent as agent_mod
    from src.control.db import MeshDB
    from tests.test_turn_queue_carrier_integration import (
        NODE, _ClientHTTP, _FakeBackend, _row, _run_one, _seed_turn, _worker,
    )
    import src.control.db as db_mod
    import src.control.node_registry as nr_mod
    from tests.test_turn_queue_carrier_integration import TOKEN

    db = MeshDB(str(tmp_path / "mesh.db"))
    monkeypatch.setattr(ts, "get_db", lambda: db)
    monkeypatch.setattr(db_mod, "get_db", lambda: db)
    monkeypatch.setattr(nr_mod, "_registry", nr_mod.NodeRegistry())
    monkeypatch.setattr(ts, "_worker_token", lambda: TOKEN)

    full_output: str = "y" * 4096

    class _Huge(_FakeBackend):
        async def __call__(self, *a, **k):
            out = await super().__call__(*a, **k)
            out["output"] = full_output
            return out

    monkeypatch.setattr(agent_mod, "_execute_task", _Huge())
    http = _ClientHTTP(TestClient(ts.app))
    w = _worker(tmp_path, http)
    w._result_spool.max_envelope_bytes = 1024
    _seed_turn(db, "t-ov", "sess-ov")
    _run_one(w, "t-ov")

    # Durable carrier artifact with the COMPLETE result; the row points at it.
    artifacts: list[Path] = list((w._result_spool.dir / "oversize").glob("*.json"))
    assert len(artifacts) == 1
    assert json.loads(artifacts[0].read_text())["output"] == full_output
    row = _row(db, "t-ov")
    assert row["status"] == "recovery_required"
    assert row["blocked_reason"].startswith("managed_result_oversize")
    assert f"artifact={artifacts[0]}" in row["blocked_reason"]
    assert len(row["blocked_reason"]) <= 500
    # Surfaced through the operator recovery read model (turn-request route).
    view = db.get_turn_request("t-ov")
    assert view["status"] == "recovery_required"
    assert f"artifact={artifacts[0]}" in view["blocked_reason"]

    # A quiescent backend must NOT auto-resolve the held artifact attempt to
    # `failed` (that would erase the pointer and orphan the result).
    asyncio.run(w._reconcile_managed_claims())
    row = _row(db, "t-ov")
    assert row["status"] == "recovery_required", "reconciler auto-failed an oversize hold"
    assert f"artifact={artifacts[0]}" in row["blocked_reason"]

    # Carrier restart: the obligation and the claim block survive.
    w2 = _worker(tmp_path, http, incarnation="inc-2")
    w2._restore_managed_claims_block()
    assert w2._managed_claims_blocked
    assert asyncio.run(w2._fetch_pending_managed({"node_id": NODE})) == []
    asyncio.run(w2._reconcile_managed_claims())
    assert _row(db, "t-ov")["status"] == "recovery_required"
    assert artifacts[0].is_file()

    # Operator resolution (the exact evidence the resolve-recovery route records).
    db.resolve_recovery(
        "t-ov", row["claim_token"],
        {"source": "operator", "task_id": "t-ov", "quiescent": True, "terminal": True,
         "terminal_status": "failed", "acknowledged_uncertain": True, "note": "read artifact"},
        resolved_status="failed",
    )
    asyncio.run(w2._reconcile_managed_claims())
    assert w2._claim_store.get("t-ov") is None
    assert w2._managed_claims_blocked is None, "resolved oversize hold still blocks claims"
    assert not w2._result_spool.has_oversize_artifacts()
    kept: list[Path] = list((w2._result_spool.dir / "oversize").glob("*.resolved"))
    assert len(kept) == 1 and json.loads(kept[0].read_text())["output"] == full_output


def test_resolved_oversize_artifacts_are_evicted_oldest_first_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.worker.managed_result_spool as spool_module

    monkeypatch.setattr(spool_module, "MAX_OVERSIZE_ARTIFACT_BYTES", 250)
    spool = ManagedResultSpool(tmp_path)
    first = spool.preserve_oversize("turn-a", "tok-a", {"output": "a" * 100})
    spool.resolve_oversize(first)
    second = spool.preserve_oversize("turn-b", "tok-b", {"output": "b" * 100})
    spool.resolve_oversize(second)
    third = spool.preserve_oversize("turn-c", "tok-c", {"output": "c" * 100})
    oversize_dir: Path = spool.dir / "oversize"
    total: int = sum(p.stat().st_size for p in oversize_dir.iterdir() if p.is_file())
    assert total <= 250
    assert third.is_file() and spool.has_oversize_artifacts()
    assert not first.with_name(first.name + ".resolved").exists(), "oldest resolved not evicted"
