"""Unit tests for `resolve_control_api_hosts` — the Control API bind-host policy.

HERMETIC: pure function, no network, no gateway, no paid CLI.

Guards the security-critical rule: the UI serves the dashboard token in-page, so
the API must never bind an untrusted interface by default. The default binds only
loopback + the node's Tailscale IP (both trusted zones); an explicit
CONTROL_API_HOST is honored verbatim as a deliberate operator override.

Run: `pytest tests/test_control_api_bind_hosts.py` (plain pytest — cost guard).
"""
from __future__ import annotations

from src.orchestrator import resolve_control_api_hosts


def test_mesh_node_binds_loopback_and_tailscale() -> None:
    # No explicit host + a Tailscale IP → both trusted zones, LAN never bound.
    assert resolve_control_api_hosts("", "100.1.2.3") == ["127.0.0.1", "100.1.2.3"]


def test_no_tailscale_binds_loopback_only() -> None:
    assert resolve_control_api_hosts("", "") == ["127.0.0.1"]


def test_explicit_host_is_honored_verbatim_single_bind() -> None:
    # Operator override wins outright — including the deliberate 0.0.0.0 choice.
    assert resolve_control_api_hosts("0.0.0.0", "100.1.2.3") == ["0.0.0.0"]
    assert resolve_control_api_hosts("127.0.0.1", "100.1.2.3") == ["127.0.0.1"]


def test_whitespace_is_treated_as_unset() -> None:
    assert resolve_control_api_hosts("  ", "  ") == ["127.0.0.1"]


def test_tailscale_equal_to_loopback_is_deduped() -> None:
    assert resolve_control_api_hosts("", "127.0.0.1") == ["127.0.0.1"]


def test_default_never_binds_all_interfaces() -> None:
    # The whole point: without an explicit override, 0.0.0.0 must never appear.
    assert "0.0.0.0" not in resolve_control_api_hosts("", "100.9.9.9")
