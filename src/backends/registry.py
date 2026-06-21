from __future__ import annotations
from typing import Callable, Dict, Tuple

from src.core.interfaces import CodingBackend
from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend
from .opencode import OpenCodeBackend, OpenCodeServerBackend

DEFAULT_BACKEND = "claude"

# The ONE place the backend set is declared. name -> zero-arg factory.
_FACTORIES: Dict[str, Callable[[], CodingBackend]] = {
    "claude":          ClaudeCodeBackend,
    "codex":           CodexBackend,
    "opencode":        OpenCodeBackend,
    "opencode-server": OpenCodeServerBackend,
}


def build_backends() -> Dict[str, CodingBackend]:
    """Instantiate {name: CodingBackend} — replaces the duplicated dict literals."""
    return {name: factory() for name, factory in _FACTORIES.items()}


def valid_backend_names() -> Tuple[str, ...]:
    return tuple(_FACTORIES.keys())


def is_valid_backend(name: str) -> bool:
    return (name or "").strip().lower() in _FACTORIES
