"""The quota coverage control.

A unified telemetry pipeline is only safe if every configured backend has an
explicit quota decision. These tests fail when a backend is added to the registry
without one, or when a provider declares an observer kind that the supported set
does not implement.
"""

from __future__ import annotations

from src.backends.registry import valid_backend_names
from src.services.quota_provider_registry import (
    CLAUDE_OBSERVER,
    CODEX_OBSERVER,
    QUOTA_PROVIDER_SPECS,
    SUPPORTED_OBSERVERS,
    coverage_gaps,
    observable_providers_for_backends,
    provider_for_backend,
    spec_for_provider,
)


def test_every_registered_backend_has_a_declared_quota_capability():
    gaps = coverage_gaps(valid_backend_names())
    assert gaps == [], (
        "backend(s) with no quota decision: "
        f"{gaps}. Add them to src/services/quota_provider_registry.py as an "
        "observable provider or an explicit unsupported provider with a reason."
    )


def test_every_provider_is_either_observable_or_explicitly_unsupported():
    for provider, spec in QUOTA_PROVIDER_SPECS.items():
        assert spec.provider == provider
        if spec.observable:
            assert spec.observer in SUPPORTED_OBSERVERS
            assert not spec.unsupported_reason
        else:
            assert spec.unsupported_reason, f"{provider} is silently unsupported"


def test_observer_kinds_are_implemented_on_both_ends():
    assert SUPPORTED_OBSERVERS == {CLAUDE_OBSERVER, CODEX_OBSERVER}
    # Provider-specific ingest tests in test_quota_window_coordinator.py and
    # worker-body tests in test_worker_quota_observation.py exercise these kinds.
    assert spec_for_provider("claude").observer == CLAUDE_OBSERVER
    assert spec_for_provider("codex").observer == CODEX_OBSERVER


def test_codex_is_worker_observed_not_locally_spawned():
    spec = spec_for_provider("codex")
    assert spec is not None
    assert spec.observable is True
    assert spec.local_observer is False


def test_backend_provider_mapping_covers_retired_and_server_names():
    assert provider_for_backend("opencode-server") == "opencode"
    assert provider_for_backend("opencode") == "opencode"
    assert observable_providers_for_backends(["claude", "codex"]) == ("claude", "codex")
    assert observable_providers_for_backends(["opencode-server"]) == ()
