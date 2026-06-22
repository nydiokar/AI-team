"""Transport-neutral session lifecycle service (M1 Move B).

The inbound symmetry to the outbound NotificationService: Telegram and a future
Web UI both call these methods instead of owning create/bind logic. Lifecycle
only — task dispatch stays on orchestrator.submit_instruction(); outbound
notifications stay on NotificationService.

Read-side (``list_views``/``active_view``) return SessionView DTOs (Move C / M2)
so every surface consumes one read shape instead of re-deriving status logic.
"""
from __future__ import annotations
import logging
import socket
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from src.services.session_store import SessionStore
from src.core.interfaces import Session, SessionOrigin, SessionStatus
from src.core.view_models import SessionView
from src.backends.registry import is_valid_backend, DEFAULT_BACKEND

logger = logging.getLogger(__name__)


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

    def close_session(
        self,
        session_id: str,
        *,
        backends: Optional[Dict[str, Any]] = None,
        host: Optional[str] = None,
    ) -> CommandResult:
        """Close a session — transport-neutral extraction of the Telegram close path.

        If the session has a live ``backend_session_id`` and is local (no
        ``machine_id`` or it equals this ``host``), call ``backend.close(session)``;
        a remote session skips the backend close (the owning node will clean up).
        Then clear ``backend_session_id``, set status CLOSED, save. Chat unbinding is
        a transport concern and stays with the caller. ``backends`` is the name→adapter
        map (the orchestrator's ``_backends``); omit it to skip the backend call.
        """
        s = self.store.get(session_id)
        if not s:
            return CommandResult(False, reason="session_not_found")
        host = host or socket.gethostname()
        if s.backend_session_id:
            is_local = (not s.machine_id) or (s.machine_id == host)
            backend = (backends or {}).get(s.backend)
            if backend and is_local:
                try:
                    backend.close(s)
                except Exception as e:
                    logger.warning(
                        "event=session_backend_close_failed session_id=%s backend=%s err=%s",
                        s.session_id, s.backend, e,
                    )
            elif not is_local:
                logger.info(
                    "event=session_backend_close_remote_skipped session_id=%s backend=%s node=%s",
                    s.session_id, s.backend, s.machine_id,
                )
            s.backend_session_id = ""
        s.status = SessionStatus.CLOSED
        self.store.save(s)
        return CommandResult(True, session=s)

    def mark_busy(self, session_id: str, *, last_user_message: Optional[str] = None) -> CommandResult:
        """Set a session BUSY (and optionally record the user's message) before a
        send. The status write lives on the service, not the interface."""
        s = self.store.get(session_id)
        if not s:
            return CommandResult(False, reason="session_not_found")
        if last_user_message is not None:
            s.last_user_message = last_user_message
        s.status = SessionStatus.BUSY
        self.store.save(s)
        return CommandResult(True, session=s)

    def mark_cancelled(self, session_id: str) -> CommandResult:
        """Set a session CANCELLED after its task cancel was requested.

        The transport calls ``orchestrator.cancel_task`` (dispatch concern) and
        then this, so the status write lives on the service, not the interface.
        """
        s = self.store.get(session_id)
        if not s:
            return CommandResult(False, reason="session_not_found")
        s.status = SessionStatus.CANCELLED
        self.store.save(s)
        return CommandResult(True, session=s)

    def restore_session(self, session_id: str) -> CommandResult:
        """Reopen a CLOSED session (→ IDLE). Caller binds it to a chat if needed."""
        s = self.store.get(session_id)
        if not s:
            return CommandResult(False, reason="session_not_found")
        if s.status != SessionStatus.CLOSED:
            return CommandResult(False, reason="not_closed", session=s)
        s.status = SessionStatus.IDLE
        self.store.save(s)
        return CommandResult(True, session=s)

    def set_model(self, session_id: str, model: Optional[str]) -> CommandResult:
        """Pin (or clear) a session's model — extraction of the Telegram /model set path.

        Resolves ``model`` via ``config.models.validate(backend, model)``. For
        non-advisory backends an unresolved name is rejected (``unknown_model``);
        advisory backends pass the value through. A falsy ``model`` clears to default
        (None). Applies on the next turn. Picker UI / labels stay in the transport.
        """
        s = self.store.get(session_id)
        if not s:
            return CommandResult(False, reason="session_not_found")
        if not model:
            s.model = None
            self.store.save(s)
            return CommandResult(True, session=s)
        from config.models import validate, is_advisory
        resolved = validate(s.backend, model)
        if resolved is None and not is_advisory(s.backend):
            return CommandResult(False, reason="unknown_model", session=s)
        s.model = resolved
        self.store.save(s)
        return CommandResult(True, session=s)

    # --- queries (read) — one read shape for every surface (Move C / M2) ---

    def list_views(self, limit: int = 200) -> List[SessionView]:
        """Sessions as operator-facing read models (DB-first, newest first).

        Bounded by ``limit`` so a polling surface doesn't scan the whole table on
        every request. Sessions come back ordered by ``updated_at`` desc from the
        store, so the bound keeps the most recently active ones.
        """
        return [SessionView.from_session(s) for s in self.store.list_all(limit=limit)]

    def active_view(self, chat_id: int) -> Optional[SessionView]:
        """The session currently bound to ``chat_id``, or None.

        Delegates to ``store.get_active`` so the existing stale-binding cleanup
        (a CLOSED session unbinds and reads as None) is preserved.
        """
        s = self.store.get_active(chat_id)
        return SessionView.from_session(s) if s else None
