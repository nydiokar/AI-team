"""[A108] Controller-authoritative dynamic tool profiles — resolver unit tests.

Every path is fail-closed to the static seed map. These cover the gate, a valid
overlay (add a tool / new profile / remap role / extend base), and the full set of
rejection cases (malformed, oversized, bad names, dangling role ref, overflow) plus
the least-privilege invariant and the real worker-snapshot seam.

HERMETIC: no network, no DB — the two registry readers are monkeypatched (and one
test installs a fake A88 controller-state client to exercise the real db seam).
"""
from __future__ import annotations

import json

import pytest

import src.control.db as db
from src.backends import tool_profile_source as tps
from src.backends.claude_role_adapter import static_profile_tools
from src.core.roles import MANAGER_TOOL_PROFILE, WORKER_TOOL_PROFILE


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    # Dedup state must not leak between tests (it only gates logging, but keep clean).
    monkeypatch.setattr(tps, "_last_rejected", None)
    yield


def _set(monkeypatch, *, dynamic: bool, overlay: str = "") -> None:
    monkeypatch.setattr(
        db, "runtime_flag_enabled",
        lambda name: dynamic if name == tps._GATE_FLAG else False,
    )
    monkeypatch.setattr(
        db, "runtime_flag_value",
        lambda name: overlay if name == tps._OVERLAY_FLAG else None,
    )


# --------------------------------------------------------------------------- #
# Gate OFF / absent ⇒ static (byte-identical to pre-A108)
# --------------------------------------------------------------------------- #
def test_gate_off_is_static(monkeypatch):
    _set(monkeypatch, dynamic=False, overlay=json.dumps({"profiles": {"manager_v1": ["mcp__x__y"]}}))
    assert tps.resolved_profile_tools() == static_profile_tools()
    assert tps.base_tool_names() == static_profile_tools()["base_v1"]
    assert tps.tool_profile_for_role("manager") == MANAGER_TOOL_PROFILE


def test_gate_on_empty_overlay_is_static(monkeypatch):
    _set(monkeypatch, dynamic=True, overlay="")
    assert tps.resolved_profile_tools() == static_profile_tools()


# --------------------------------------------------------------------------- #
# Valid overlay applied
# --------------------------------------------------------------------------- #
def test_overlay_adds_tool_to_manager(monkeypatch):
    base = static_profile_tools()["manager_v1"]
    _set(monkeypatch, dynamic=True,
         overlay=json.dumps({"profiles": {"manager_v1": base + ["mcp__manager__new_tool"]}}))
    assert "mcp__manager__new_tool" in tps.profile_tool_names("manager_v1")
    # other profiles untouched
    assert tps.profile_tool_names("worker_v1") == static_profile_tools()["worker_v1"]


def test_overlay_new_profile_and_role_remap(monkeypatch):
    overlay = {
        "profiles": {"advisory_v1": ["mcp__manager__get_case", "Read"]},
        "role_profiles": {"advisory": "advisory_v1"},
    }
    _set(monkeypatch, dynamic=True, overlay=json.dumps(overlay))
    assert tps.tool_profile_for_role("advisory") == "advisory_v1"
    assert tps.profile_tool_names("advisory_v1") == ["mcp__manager__get_case", "Read"]
    # existing roles still resolve to their static profile
    assert tps.tool_profile_for_role("manager") == MANAGER_TOOL_PROFILE


def test_overlay_extends_base(monkeypatch):
    base = static_profile_tools()["base_v1"]
    _set(monkeypatch, dynamic=True,
         overlay=json.dumps({"profiles": {"base_v1": base + ["mcp__jobs__new_universal"]}}))
    assert "mcp__jobs__new_universal" in tps.base_tool_names()


# --------------------------------------------------------------------------- #
# Rejection cases ⇒ fail-closed to static
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "{not json",                                              # invalid JSON
    json.dumps([]),                                           # not an object
    json.dumps({"profiles": {}, "surprise": 1}),              # unknown top key
    json.dumps({"profiles": {"m": "notalist"}}),              # tool list not a list
    json.dumps({"profiles": {"m": ["rm -rf /"]}}),            # bad tool name
    json.dumps({"profiles": {"m": ["mcp__a__b; evil"]}}),     # bad tool name
    json.dumps({"role_profiles": {"advisory": "ghost_v1"}}),  # role → unknown profile
    json.dumps({"profiles": {"x" * 65: ["Read"]}}),           # overlong profile name
])
def test_malformed_overlay_falls_back_to_static(monkeypatch, bad):
    _set(monkeypatch, dynamic=True, overlay=bad)
    assert tps.resolved_profile_tools() == static_profile_tools()
    assert tps.resolved_role_profiles() == tps.static_role_profiles()


def test_oversized_overlay_rejected(monkeypatch):
    # Interior whitespace (valid JSON, survives strip) pushes it past the byte cap.
    huge = '{"profiles":' + " " * (tps._MAX_OVERLAY_BYTES + 1) + '{"m":["mcp__a__b"]}}'
    _set(monkeypatch, dynamic=True, overlay=huge)
    assert tps.resolved_profile_tools() == static_profile_tools()


def test_too_many_profiles_rejected(monkeypatch):
    overlay = {"profiles": {f"p{i}": ["Read"] for i in range(tps._MAX_PROFILES + 1)}}
    _set(monkeypatch, dynamic=True, overlay=json.dumps(overlay))
    assert tps.resolved_profile_tools() == static_profile_tools()


# --------------------------------------------------------------------------- #
# Least-privilege invariant — an overlay can never make an unmapped role privileged
# --------------------------------------------------------------------------- #
def test_unmapped_role_is_least_privilege_even_with_overlay(monkeypatch):
    _set(monkeypatch, dynamic=True,
         overlay=json.dumps({"profiles": {"advisory_v1": ["mcp__manager__dispatch_worker"]},
                             "role_profiles": {"advisory": "advisory_v1"}}))
    # a DIFFERENT, unmapped role gets the least-privilege worker profile, not advisory
    assert tps.tool_profile_for_role("some_other_role") == WORKER_TOOL_PROFILE
    assert tps.profile_tool_names(tps.tool_profile_for_role("some_other_role")) == \
        static_profile_tools()[WORKER_TOOL_PROFILE]


# --------------------------------------------------------------------------- #
# Real db seam — worker path via an installed A88 controller-state snapshot
# --------------------------------------------------------------------------- #
def test_worker_snapshot_seam_end_to_end(monkeypatch):
    """Prove runtime_flag_value/_enabled resolve the overlay from an installed
    controller-state client (the worker path), not just monkeypatched readers."""
    from src.control import controller_state

    overlay = json.dumps({"profiles": {"manager_v1":
                          static_profile_tools()["manager_v1"] + ["mcp__manager__snap_tool"]}})
    rows = {"TOOL_PROFILES_DYNAMIC": {"value": "1"}, "TOOL_PROFILES_OVERLAY": {"value": overlay}}

    class _FakeSnapshot:
        def runtime_flag_row(self, flag_name):
            return rows.get(flag_name)

        def boot_reconcile_case(self, case_id):
            return {}

    controller_state.install(_FakeSnapshot())
    try:
        assert "mcp__manager__snap_tool" in tps.profile_tool_names("manager_v1")
    finally:
        controller_state.uninstall()
    # uninstalled ⇒ back to static (no DB in this process)
    assert "mcp__manager__snap_tool" not in tps.profile_tool_names("manager_v1")
