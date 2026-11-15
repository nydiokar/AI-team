"""Single source of truth for quota-provider coverage.

Every backend that can own a provider plan quota MUST be declared here. Adding a
backend to ``src.backends.registry`` without a quota decision fails the
coverage test in ``tests/test_quota_provider_coverage.py`` instead of silently
disappearing behind a hardcoded provider check.

An unsupported provider is a first-class declaration with a reason. It is never
silently skipped: the worker does not observe it, ingest rejects it explicitly,
and the coverage test keeps the declaration exhaustive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

CLAUDE_OBSERVER = "claude_get_usage"
CODEX_OBSERVER = "codex_account_rate_limits"

#: Observer kinds implemented by both the worker dispatch and controller ingest.
#: A spec may only name one of these; adding a new kind fails coverage until both
#: sides implement it.
SUPPORTED_OBSERVERS = frozenset({CLAUDE_OBSERVER, CODEX_OBSERVER})


@dataclass(frozen=True)
class QuotaProviderSpec:
    provider: str
    observer: str = ""
    unsupported_reason: str = ""
    #: True only for observers this process may run without a remote worker.
    #: Codex quota is observed harness-side so the controller never spawns it.
    local_observer: bool = False

    @property
    def observable(self) -> bool:
        return bool(self.observer)


QUOTA_PROVIDER_SPECS: dict[str, QuotaProviderSpec] = {
    "claude": QuotaProviderSpec(
        provider="claude",
        observer=CLAUDE_OBSERVER,
        local_observer=True,
    ),
    "codex": QuotaProviderSpec(
        provider="codex",
        observer=CODEX_OBSERVER,
    ),
    "opencode": QuotaProviderSpec(
        provider="opencode",
        unsupported_reason="opencode_is_provider_router_no_quota_owner",
    ),
}

#: Backend name -> quota owner. The OpenCode CLI is retired but kept mapped so a
#: stale carrier config still resolves to the explicit unsupported declaration.
BACKEND_TO_QUOTA_PROVIDER: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "opencode-server": "opencode",
}


def provider_for_backend(backend: str) -> str:
    name = (backend or "").strip().lower()
    return BACKEND_TO_QUOTA_PROVIDER.get(name, name)


def spec_for_provider(provider: str) -> Optional[QuotaProviderSpec]:
    return QUOTA_PROVIDER_SPECS.get((provider or "").strip().lower())


def coverage_gaps(backends: Iterable[str]) -> list[str]:
    """Backends with no declared quota capability (must stay empty)."""
    return [b for b in backends if provider_for_backend(b) not in QUOTA_PROVIDER_SPECS]


def observable_providers_for_backends(backends: Iterable[str]) -> tuple[str, ...]:
    """Providers this worker can observe, sorted and de-duplicated.

    A backend with no declaration is omitted here; ``coverage_gaps`` is the
    loud control that surfaces it.
    """
    providers = {provider_for_backend(b) for b in backends}
    return tuple(
        provider
        for provider in sorted(providers)
        if (spec := spec_for_provider(provider)) is not None and spec.observable
    )


def local_observable_providers() -> tuple[str, ...]:
    return tuple(
        sorted(
            provider
            for provider, spec in QUOTA_PROVIDER_SPECS.items()
            if spec.observable and spec.local_observer
        )
    )
