from __future__ import annotations
from typing import Callable, Dict, Tuple

from src.core.interfaces import CodingBackend
from .claude_code import ClaudeCodeBackend
from .codex_native import CodexBackend
from .opencode import OpenCodeBackend, OpenCodeServerBackend

DEFAULT_BACKEND = "claude"

# The ONE place the backend set is declared. name -> zero-arg factory.
_FACTORIES: Dict[str, Callable[[], CodingBackend]] = {
    "claude":          ClaudeCodeBackend,
    "codex":           CodexBackend,
    "opencode":        OpenCodeBackend,
    "opencode-server": OpenCodeServerBackend,
}


# [A82 Stage 8a] Backends kept ONLY so existing sessions stay readable and
# closable: no new session, turn or compaction is admitted for them (operator
# decision 2026-10-02: the OpenCode CLI backend is retired; OpenCode sessions
# use ``opencode-server``). Registry-level policy, never carrier/server logic.
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
