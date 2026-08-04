"""Regression coverage for the standalone dispatch state manager."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "dispatch" / "dispatch_state.py"
    spec = importlib.util.spec_from_file_location("dispatch_state_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_naive_timestamp_is_safe() -> None:
    dispatch = _module()

    assert dispatch._age_days("2026-01-01T00:00:00") is not None


def test_done_packet_requires_evidence() -> None:
    dispatch = _module()
    job = dispatch.JobState("DONE", "DONE.md", "done", "", "", evidence=[])

    assert "CLAIMED_DONE_NO_PROOF" in dispatch._derive_flags(job)


def test_dependency_never_unblocks_without_explicit_opt_in() -> None:
    dispatch = _module()
    prerequisite = dispatch.JobState(
        "PRE", "PRE.md", "done", "", "", evidence=["pyproject.toml"]
    )
    gated = dispatch.JobState("GATED", "GATED.md", "blocked", "", "", depends_on=["PRE"])
    opted_in = dispatch.JobState(
        "OPTED", "OPTED.md", "blocked", "", "", depends_on=["PRE"], auto_unblock=True
    )
    unproven = dispatch.JobState("UNPROVEN", "UNPROVEN.md", "done", "", "", evidence=[])
    unsafe = dispatch.JobState(
        "UNSAFE", "UNSAFE.md", "blocked", "", "", depends_on=["UNPROVEN"], auto_unblock=True
    )
    jobs = [prerequisite, gated, opted_in, unproven, unsafe]
    unproven.flags = dispatch._derive_flags(unproven, jobs)

    assert "READY_TO_UNBLOCK" not in dispatch._derive_flags(gated, jobs)
    assert "READY_TO_UNBLOCK" in dispatch._derive_flags(opted_in, jobs)
    assert "READY_TO_UNBLOCK" not in dispatch._derive_flags(unsafe, jobs)
