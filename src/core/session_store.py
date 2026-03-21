"""
File-backed session store.

Layout:
  state/sessions/<session_id>.json       — one file per session
  state/telegram/active_bindings.json    — chat_id (str) -> session_id
"""
import json
import logging
import socket
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .interfaces import Session, SessionStatus

logger = logging.getLogger(__name__)

_SESSIONS_DIR = Path("state/sessions")
_BINDINGS_FILE = Path("state/telegram/active_bindings.json")


class SessionStore:
    def __init__(self):
        _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        _BINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def create(self, backend: str, repo_path: str,
                telegram_chat_id: Optional[int] = None,
                owner_user_id: Optional[int] = None) -> Session:
        session = Session(
            session_id=uuid.uuid4().hex[:12],
            backend=backend,
            repo_path=repo_path,
            status=SessionStatus.IDLE,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            machine_id=socket.gethostname(),
            telegram_chat_id=telegram_chat_id,
            owner_user_id=owner_user_id,
        )
        self._write(session)
        logger.info(f"session_created id={session.session_id} backend={backend} path={repo_path}")
        return session

    def get(self, session_id: str) -> Optional[Session]:
        path = _SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            return self._from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"session_load_failed id={session_id} error={e}")
            return None

    def save(self, session: Session) -> None:
        session.updated_at = datetime.now().isoformat()
        self._write(session)

    def list_all(self) -> List[Session]:
        sessions = []
        for p in sorted(_SESSIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                sessions.append(self._from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                pass
        return sessions

    def delete(self, session_id: str) -> None:
        path = _SESSIONS_DIR / f"{session_id}.json"
        if path.exists():
            path.unlink()

    # ------------------------------------------------------------------
    # Telegram active binding  (chat_id -> session_id)
    # ------------------------------------------------------------------

    def bind(self, chat_id: int, session_id: str) -> None:
        bindings = self._load_bindings()
        bindings[str(chat_id)] = session_id
        self._save_bindings(bindings)

    def unbind(self, chat_id: int) -> None:
        bindings = self._load_bindings()
        bindings.pop(str(chat_id), None)
        self._save_bindings(bindings)

    def get_active(self, chat_id: int) -> Optional[Session]:
        bindings = self._load_bindings()
        session_id = bindings.get(str(chat_id))
        if not session_id:
            return None
        return self.get(session_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, session: Session) -> None:
        path = _SESSIONS_DIR / f"{session.session_id}.json"
        path.write_text(json.dumps(self._to_dict(session), indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_bindings(self) -> Dict[str, str]:
        if not _BINDINGS_FILE.exists():
            return {}
        try:
            return json.loads(_BINDINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_bindings(self, bindings: Dict[str, str]) -> None:
        _BINDINGS_FILE.write_text(json.dumps(bindings, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _to_dict(s: Session) -> dict:
        return {
            "session_id": s.session_id,
            "backend": s.backend,
            "repo_path": s.repo_path,
            "status": s.status.value,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
            "machine_id": s.machine_id,
            "backend_session_id": s.backend_session_id,
            "last_task_id": s.last_task_id,
            "last_artifact_path": s.last_artifact_path,
            "last_summary": s.last_summary,
            "last_user_message": s.last_user_message,
            "last_result_summary": s.last_result_summary,
            "telegram_chat_id": s.telegram_chat_id,
            "telegram_thread_id": s.telegram_thread_id,
            "owner_user_id": s.owner_user_id,
        }

    @staticmethod
    def _from_dict(d: dict) -> Session:
        return Session(
            session_id=d["session_id"],
            backend=d["backend"],
            repo_path=d["repo_path"],
            status=SessionStatus(d["status"]),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            machine_id=d.get("machine_id", ""),
            backend_session_id=d.get("backend_session_id", ""),
            last_task_id=d.get("last_task_id", ""),
            last_artifact_path=d.get("last_artifact_path", ""),
            last_summary=d.get("last_summary", ""),
            last_user_message=d.get("last_user_message", ""),
            last_result_summary=d.get("last_result_summary", ""),
            telegram_chat_id=d.get("telegram_chat_id"),
            telegram_thread_id=d.get("telegram_thread_id"),
            owner_user_id=d.get("owner_user_id"),
        )
