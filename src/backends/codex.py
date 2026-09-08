"""Codex backend public entry point.

The semantic adapter lives in ``codex_native`` and exclusively uses the
persistent app-server client. Keeping this import boundary preserves the
registry's established ``CodexBackend`` contract while keeping protocol and
runtime machinery out of gateway callers.
"""

from src.backends.codex_native import CodexBackend

__all__ = ["CodexBackend"]
