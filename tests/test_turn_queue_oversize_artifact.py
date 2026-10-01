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
