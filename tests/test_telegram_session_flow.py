import shutil
import uuid
from pathlib import Path

import pytest

from config import config
from src.core.session_store import SessionStore
from src.telegram.interface import TelegramInterface
import src.core.session_store as session_store_module


class _DummyMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str):
        self.replies.append(text)


class _DummyUser:
    def __init__(self, user_id: int):
        self.id = user_id


class _DummyChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class _DummyUpdate:
    def __init__(self, user_id: int = 1, chat_id: int = 100, text: str = ""):
        self.effective_user = _DummyUser(user_id)
        self.effective_chat = _DummyChat(chat_id)
        self.message = _DummyMessage(text)


class _DummyContext:
    def __init__(self, args=None):
        self.args = args or []


class _DummyOrchestrator:
    def __init__(self):
        self.created_tasks = []
        self.cancelled_tasks = []

    def create_task_from_description(self, description, task_type=None, target_files=None, session_id=None, cwd=None):
        task_id = f"task_{len(self.created_tasks) + 1}"
        self.created_tasks.append(
            {
                "task_id": task_id,
                "description": description,
                "task_type": task_type,
                "target_files": target_files,
                "session_id": session_id,
                "cwd": cwd,
            }
        )
        return task_id

    def cancel_task(self, task_id):
        self.cancelled_tasks.append(task_id)
        return True

    def get_status(self):
        return {
            "components": {
                "claude_available": True,
                "llama_available": False,
                "file_watcher_running": True,
            },
            "tasks": {"active": 0, "queued": 0, "completed": 0, "workers": 1},
            "telegram": {"configured": True, "running": False},
            "scope": {
                "base_cwd": config.claude.base_cwd,
                "allowed_root": config.claude.allowed_root,
                "root_dirs": [],
            },
        }


def _make_workspace() -> Path:
    root = Path.cwd() / ".test_telegram_session" / uuid.uuid4().hex[:8]
    (root / "repo-alpha" / "src").mkdir(parents=True, exist_ok=True)
    (root / "repo-beta").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def isolated_session_store(monkeypatch):
    base = Path.cwd() / ".test_session_store" / uuid.uuid4().hex[:8]
    sessions_dir = base / "state" / "sessions"
    bindings_file = base / "state" / "telegram" / "active_bindings.json"
    monkeypatch.setattr(session_store_module, "_SESSIONS_DIR", sessions_dir, raising=False)
    monkeypatch.setattr(session_store_module, "_BINDINGS_FILE", bindings_file, raising=False)
    yield
    shutil.rmtree(base, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_new_rejects_bad_path_with_suggestion(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

        update = _DummyUpdate()
        context = _DummyContext(["claude", "repo-alph"])
        await bot._handle_session_new(update, context)

        assert update.message.replies
        assert "Path does not exist." in update.message.replies[-1]
        assert "repo-alpha" in update.message.replies[-1]
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_new_creates_session_and_lists_dirs(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

        update = _DummyUpdate()
        context = _DummyContext(["claude", "repo-alpha"])
        await bot._handle_session_new(update, context)

        active = SessionStore().get_active(update.effective_chat.id)
        assert active is not None
        assert active.repo_path == str((workspace / "repo-alpha").resolve())
        assert "Top directories:" in update.message.replies[-1]
        assert "src" in update.message.replies[-1]
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_run_routes_to_active_session_with_bound_cwd(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        orchestrator = _DummyOrchestrator()
        bot = TelegramInterface("", orchestrator, allowed_users=[1])

        store = SessionStore()
        session = store.create("claude", str((workspace / "repo-alpha").resolve()), telegram_chat_id=100, owner_user_id=1)
        store.bind(100, session.session_id)

        update = _DummyUpdate(text="")
        context = _DummyContext(["inspect", "the", "repo"])
        await bot._handle_run_command(update, context)

        assert orchestrator.created_tasks
        created = orchestrator.created_tasks[-1]
        assert created["session_id"] == session.session_id
        assert created["cwd"] == str((workspace / "repo-alpha").resolve())
        assert "Running in session" in update.message.replies[-1]
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)
