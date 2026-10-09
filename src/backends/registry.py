from __future__ import annotations
from typing import Callable, Dict, Tuple

from src.core.interfaces import CodingBackend
from .claude_code import ClaudeCodeBackend
from .codex_native import CodexBackend
from .opencode import OpenCodeServerBackend

DEFAULT_BACKEND = "claude"

# The ONE place the backend set is declared. name -> zero-arg factory.
# [A102 S1-OpenCode, R3] The OpenCode CLI backend (``opencode``) was retired by
# the operator on 2026-10-02 and its class deleted; nodes no longer advertise
# it. OpenCode sessions use ``opencode-server``.
_FACTORIES: Dict[str, Callable[[], CodingBackend]] = {
    "claude":          ClaudeCodeBackend,
    "codex":           CodexBackend,
    "opencode-server": OpenCodeServerBackend,
}


# [A82 Stage 8a / A102 S1-OpenCode R3] Retired backend names: no new session,
# turn or compaction is admitted for them. The OpenCode CLI backend class was
# deleted (R3), so it is no longer in ``_FACTORIES``; keeping the name here lets
# the gateway refuse it with an operator-facing reason instead of a bare
# "unknown backend". Registry-level policy, never carrier/server logic.
RETIRED_BACKENDS: Dict[str, str] = {
    "opencode": "the OpenCode CLI backend is retired; use opencode-server",
}


def is_retired_backend(name: str) -> bool:
    return (name or "").strip().lower() in RETIRED_BACKENDS


def retired_backend_reason(name: str) -> str:
    """Operator-facing reason for a retired backend ("" when not retired)."""
    return RETIRED_BACKENDS.get((name or "").strip().lower(), "")


def active_backend_names() -> Tuple[str, ...]:
    """Backends a NEW session may use (registered and not retired)."""
    return tuple(n for n in _FACTORIES if n not in RETIRED_BACKENDS)


def build_backends() -> Dict[str, CodingBackend]:
    """Instantiate {name: CodingBackend} — replaces the duplicated dict literals."""
    return {name: factory() for name, factory in _FACTORIES.items()}


def valid_backend_names() -> Tuple[str, ...]:
    return tuple(_FACTORIES.keys())


def is_valid_backend(name: str) -> bool:
    return (name or "").strip().lower() in _FACTORIES
