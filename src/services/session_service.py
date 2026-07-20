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
    detail: str = ""                    # optional human-facing context (e.g. why a
                                        # repo_path was rejected); transports may show it


class SessionService:
    """Transport-neutral session lifecycle. Owns *session lifecycle only*."""

    def __init__(self, session_store: SessionStore,
                 repo_path_validator: Optional[Callable[[str], Optional["CommandResult"]]] = None,
                 remote_close_dispatcher: Optional[Callable[[Session], None]] = None):
        # Reuse the orchestrator's store — never construct a second one.
        self.store = session_store
        # Injectable so tests can supply a permissive validator and the real path
        # policy (PathResolver against the configured allowed_root) isn't reached
        # implicitly. Defaults to the real local-repo validator (Feature #38).
        self._repo_path_validator = repo_path_validator or self._validate_local_repo_path
        # Injected by the orchestrator: given a remote (mesh) session, enqueue a
        # close_session task pinned to its owning node so the worker tears down
        # the live backend process. Absent ⇒ legacy no-op (remote close skipped).
        self._remote_close_dispatcher = remote_close_dispatcher

    def create_session(self, *, backend: str, repo_path: str,
                        chat_id: Optional[int] = None,
                        owner_user_id: Optional[int] = None,
                        node_id: str = "__local__",
                        model: Optional[str] = None,
                        origin: Optional[SessionOrigin] = None,
                        role_boot: Optional[str] = None,
                        continued_from: Optional[str] = None,
                        bind_chat: bool = True) -> CommandResult:
        """Faithful extraction of TelegramInterface._create_and_bind_session.

        Preserves node pinning (machine_id), model pinning, and the
        single-save semantics of the original. Adds origin tagging (defaults to
        telegram/user so existing behavior is unchanged).
        """
        backend = (backend or DEFAULT_BACKEND).strip().lower()
        if not is_valid_backend(backend):
            return CommandResult(False, reason="unknown_backend")
        # Fail early on a bad working directory (Move #38). For LOCAL sessions the
        # gateway host can stat the path, so validate it up front — a nonexistent /
        # not-a-dir / outside-allowed-root repo is rejected at create time instead
        # of silently surfacing only when the first instruction tries to cd into it.
        # Remote (mesh) sessions are skipped: their path lives on the owning node and
        # cannot be stat'd here. Telegram already passes a pre-resolved path, which
        # simply re-validates as ok — so this is transparent to the existing wizard.
        is_local = (not node_id) or node_id == "__local__"
        if is_local:
            invalid = self._repo_path_validator(repo_path)
            if invalid is not None:
                return invalid
        # A11: pass the pin into create() so the very first written row already
        # names the target node — no transient window where it says the local host.
        pin = node_id if (node_id and node_id != "__local__") else None
        s = self.store.create(backend=backend, repo_path=repo_path,
                              telegram_chat_id=chat_id, owner_user_id=owner_user_id,
                              machine_id=pin)
        s.origin = origin or SessionOrigin()
        if model:
            s.model = model
        if pin:
            s.machine_id = pin
        # [Worker role] Stamp the explicit opt-in role-boot signal at create time.
        # Absent ⇒ None ⇒ tier-0 default (byte-identical). Distinct from case_role.
        if role_boot:
            s.role_boot = role_boot
        # [Session-fork] Stamp session→session lineage at create time (a fork). Pure
        # session-axis pointer; independent of Case/role. Absent ⇒ None.
        if continued_from:
            s.continued_from = continued_from
        self.store.save(s)
        if bind_chat and chat_id is not None:
            self.store.bind(chat_id, s.session_id)
        return CommandResult(True, session=s)

    def _validate_local_repo_path(self, repo_path: str) -> Optional[CommandResult]:
        """Reject a bad LOCAL repo_path up front. Returns a rejecting CommandResult
        (reason="invalid_repo_path", detail=<why>) or None when the path is fine.

        Uses the same PathResolver the Telegram wizard uses, so the rules (must
        exist, be a directory, live inside the allowed root) are identical across
        surfaces. If a resolver cannot be constructed (no workspace configured), we
        do NOT block — validation is best-effort and must not regress create().
        """
        try:
            from src.services.path_resolver import PathResolver
            resolver = PathResolver.from_config()
        except Exception as e:
            logger.warning("event=repo_path_validation_skipped err=%s", e)
            return None
        resolution = resolver.resolve_session_path(repo_path)
        if resolution.ok:
            return None
        return CommandResult(
            False,
            reason="invalid_repo_path",
            detail=resolution.error or "Invalid working directory.",
        )

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
                if self._remote_close_dispatcher is not None:
                    try:
                        self._remote_close_dispatcher(s)
                        logger.info(
                            "event=session_backend_close_remote_dispatched session_id=%s backend=%s node=%s",
                            s.session_id, s.backend, s.machine_id,
                        )
                    except Exception as e:
                        logger.warning(
                            "event=session_remote_close_dispatch_failed session_id=%s backend=%s node=%s err=%s",
                            s.session_id, s.backend, s.machine_id, e,
                        )
                else:
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

    def mark_idle(self, session_id: str) -> CommandResult:
        """Return a session to IDLE — the inverse of ``mark_busy``.

        Used when a send was optimistically marked BUSY but never actually
        dispatched (e.g. the task-harness Level-3 admission gate refuses the task
        at the queue choke point *after* ``mark_busy`` already ran). Without this
        the session would be stranded BUSY with no in-flight task. Idempotent: a
        session that is already IDLE stays IDLE. Does not touch CLOSED/CANCELLED
        sessions — only an active BUSY/IDLE session is reset.
        """
        s = self.store.get(session_id)
        if not s:
            return CommandResult(False, reason="session_not_found")
        if s.status in (SessionStatus.CLOSED, SessionStatus.CANCELLED):
            return CommandResult(False, reason="not_active", session=s)
        s.status = SessionStatus.IDLE
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
        Claude rejects unresolved names (``unknown_model``); Codex and OpenCode
        pass the value through. A falsy ``model`` clears to default
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

    def set_effort(self, session_id: str, effort: Optional[str]) -> CommandResult:
        """Pin or clear the backend thinking/reasoning effort for a session."""
        s = self.store.get(session_id)
        if not s:
            return CommandResult(False, reason="session_not_found")
        from config.models import validate_effort
        resolved = validate_effort(s.backend, effort)
        if effort and resolved is None:
            return CommandResult(False, reason="unknown_effort", session=s)
        s.effort = resolved
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
