"""Transport-neutral session lifecycle service (M1 Move B).

The inbound symmetry to the outbound NotificationService: Telegram and a future
Web UI both call these methods instead of owning create/bind logic. Lifecycle
only — task dispatch stays on orchestrator.submit_instruction(); outbound
notifications stay on NotificationService.

The read-side (*_view) methods depend on the deferred SessionView (Move C) and
are intentionally omitted for M1 — the service is fully useful without them.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

from src.services.session_store import SessionStore
from src.core.interfaces import Session, SessionOrigin
from src.backends.registry import is_valid_backend, DEFAULT_BACKEND


@dataclass(frozen=True)
class CommandResult:
    """Accepted/rejected envelope for inbound session commands.

    Carries the structured outcome ONLY — no user-facing text. ``reason`` is a
    stable machine code (e.g. "unknown_backend", "session_not_found"); each
    transport maps it to its own wording. ``session`` is the affected Session so
    a transport can render confirmation without a second lookup.
    """
    ok: bool
    reason: str = ""                    # "" on success; stable code on reject
    session: Optional[Session] = None


class SessionService:
    """Transport-neutral session lifecycle. Owns *session lifecycle only*."""

    def __init__(self, session_store: SessionStore):
        # Reuse the orchestrator's store — never construct a second one.
        self.store = session_store

    def create_session(self, *, backend: str, repo_path: str,
                        chat_id: Optional[int] = None,
                        owner_user_id: Optional[int] = None,
                        node_id: str = "__local__",
                        model: Optional[str] = None,
                        origin: Optional[SessionOrigin] = None,
                        bind_chat: bool = True) -> CommandResult:
        """Faithful extraction of TelegramInterface._create_and_bind_session.

        Preserves node pinning (machine_id), model pinning, and the
        single-save semantics of the original. Adds origin tagging (defaults to
        telegram/user so existing behavior is unchanged).
        """
        backend = (backend or DEFAULT_BACKEND).strip().lower()
        if not is_valid_backend(backend):
            return CommandResult(False, reason="unknown_backend")
        s = self.store.create(backend=backend, repo_path=repo_path,
                              telegram_chat_id=chat_id, owner_user_id=owner_user_id)
        s.origin = origin or SessionOrigin()
        if model:
            s.model = model
        if node_id and node_id != "__local__":
            s.machine_id = node_id
        self.store.save(s)
        if bind_chat and chat_id is not None:
            self.store.bind(chat_id, s.session_id)
        return CommandResult(True, session=s)

    def bind_active(self, chat_id: int, session_id: str) -> CommandResult:
        s = self.store.get(session_id)
        if not s:
            return CommandResult(False, reason="session_not_found")
        self.store.bind(chat_id, session_id)
        return CommandResult(True, session=s)
