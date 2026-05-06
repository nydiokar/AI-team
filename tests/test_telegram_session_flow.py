import shutil
import uuid
import os
from pathlib import Path
from unittest.mock import patch
import json

import pytest

from config import config
from src.core.session_store import SessionStore
from src.telegram.interface import TelegramInterface
import src.core.session_store as session_store_module


class _DummyMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list[str] = []
        self.reply_kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)
        self.reply_kwargs.append(kwargs)


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


class _DummyCallbackQuery:
    def __init__(self, data: str):
        self.data = data
        self.answers = 0
        self.edits: list[str] = []
        self.edit_kwargs: list[dict] = []

    async def answer(self):
        self.answers += 1

    async def edit_message_text(self, text: str, **kwargs):
        self.edits.append(text)
        self.edit_kwargs.append(kwargs)


class _DummyOrchestrator:
    def __init__(self):
        self.created_tasks = []
        self.cancelled_tasks = []

    async def submit_instruction(self, description, task_type=None, target_files=None, session_id=None, cwd=None, source="runtime"):
        task_id = f"task_{len(self.created_tasks) + 1}"
        self.created_tasks.append(
            {
                "task_id": task_id,
                "description": description,
                "task_type": task_type,
                "target_files": target_files,
                "session_id": session_id,
                "cwd": cwd,
                "source": source,
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
async def test_start_registers_bot_commands(monkeypatch, isolated_session_store):
    class _FakeBot:
        def __init__(self):
            self.commands = None

        async def set_my_commands(self, commands):
            self.commands = commands

    class _FakeUpdater:
        async def start_polling(self):
            return None

    class _FakeApp:
        def __init__(self):
            self.bot = _FakeBot()
            self.updater = _FakeUpdater()

        async def initialize(self):
            return None

        async def start(self):
            return None

    bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])
    bot.app = _FakeApp()
    monkeypatch.setattr(bot, "_acquire_instance_lock", lambda: None)

    await bot.start()

    names = [item.command for item in bot.app.bot.commands]
    assert names[:4] == ["session_new", "session_list", "session_close", "status"]
    assert "git_status" in names


@pytest.mark.asyncio
async def test_session_new_creates_session_and_guides_next_step(monkeypatch, isolated_session_store):
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
        assert "Send a plain message to continue in this session." in update.message.replies[-1]
        assert "/session_dirs" in update.message.replies[-1]
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_new_without_args_shows_backend_picker(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

        update = _DummyUpdate()
        await bot._handle_session_new(update, _DummyContext())

        assert update.message.replies
        assert "Choose the backend" in update.message.replies[-1]
        markup = update.message.reply_kwargs[-1]["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        assert labels == ["Codex", "Claude"]
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_new_backend_callback_shows_recent_repos(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        repo_alpha = workspace / "repo-alpha"
        repo_beta = workspace / "repo-beta"
        (repo_alpha / ".git").mkdir(exist_ok=True)
        (repo_beta / ".git").mkdir(exist_ok=True)
        repo_alpha.touch()
        repo_beta.touch()
        os.utime(repo_alpha, (1_700_000_000, 1_700_000_000))
        os.utime(repo_beta, (1_800_000_000, 1_800_000_000))

        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

        update = _DummyUpdate()
        update.callback_query = _DummyCallbackQuery("session_new_backend:codex")
        await bot._handle_session_new_callback(update, _DummyContext())

        assert update.callback_query.answers == 1
        assert "Choose the repository" in update.callback_query.edits[-1]
        markup = update.callback_query.edit_kwargs[-1]["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        assert labels[:2] == ["repo-beta", "repo-alpha"]
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_new_repo_callback_creates_session(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        repo_alpha = workspace / "repo-alpha"
        (repo_alpha / ".git").mkdir(exist_ok=True)

        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

        update = _DummyUpdate()
        update.callback_query = _DummyCallbackQuery("session_new_repo:codex:0")
        await bot._handle_session_new_callback(update, _DummyContext())

        active = SessionStore().get_active(update.effective_chat.id)
        assert active is not None
        assert active.backend == "codex"
        assert active.repo_path == str(repo_alpha.resolve())
        assert "Session created and set as active" in update.callback_query.edits[-1]
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
        assert "Working" in update.message.replies[-1]
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_help_lists_current_command_set(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

        update = _DummyUpdate()
        await bot._handle_help(update, _DummyContext())

        text = update.message.replies[-1]
        assert "/session_new <backend> <path>" in text
        assert "/session_dirs [path]" in text
        assert "/session_cancel [session_id]" in text
        assert "/git_status [session_id]" in text
        assert "/commit [session_id] [--no-branch] [--push]" in text
        assert "/run <instruction>" not in text
        assert "/say <instruction>" not in text
        assert "/documentation" not in text
        assert "/code_review" not in text
        assert "/bug_fix" not in text
        assert "/analyze" not in text
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_list_hides_closed_by_default(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])
        store = SessionStore()

        open_session = store.create("claude", str((workspace / "repo-alpha").resolve()), telegram_chat_id=100, owner_user_id=1)
        closed_session = store.create("claude", str((workspace / "repo-beta").resolve()), telegram_chat_id=100, owner_user_id=1)
        closed_session.status = session_store_module.SessionStatus.CLOSED
        store.save(closed_session)
        store.bind(100, open_session.session_id)

        update = _DummyUpdate()
        await bot._handle_session_list(update, _DummyContext())
        text = update.message.replies[-1]

        assert len(update.message.replies) == 1
        assert "Open sessions (1) - tap to switch:" in text
        assert "⭐ ACTIVE" in text
        assert "🧠 claude / repo-alpha" in text
        assert "🆔" in text
        assert open_session.session_id in text
        assert closed_session.session_id not in text
        assert str((workspace / "repo-alpha").resolve()) not in text
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_restore_lists_closed_sessions(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])
        store = SessionStore()

        open_session = store.create("claude", str((workspace / "repo-alpha").resolve()), telegram_chat_id=100, owner_user_id=1)
        closed_session = store.create("codex", str((workspace / "repo-beta").resolve()), telegram_chat_id=100, owner_user_id=1)
        closed_session.status = session_store_module.SessionStatus.CLOSED
        closed_session.last_user_message = "Investigate Telegram session picker formatting"
        store.save(closed_session)

        update = _DummyUpdate()
        await bot._handle_session_restore(update, _DummyContext())
        text = update.message.replies[-1]

        assert "Recently closed sessions - tap to restore:" in text
        assert "↩️ 🤖 codex / repo-beta" in text
        assert "📝 Investigate Telegram session picker formatting" in text
        assert closed_session.session_id in text
        assert open_session.session_id not in text
        assert str((workspace / "repo-beta").resolve()) not in text
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_picker_callback_uses_compact_switch_message(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])
        store = SessionStore()
        session = store.create("claude", str((workspace / "repo-alpha").resolve()), telegram_chat_id=100, owner_user_id=1)

        update = _DummyUpdate()
        update.callback_query = _DummyCallbackQuery(f"session_use:{session.session_id}")
        await bot._handle_session_picker_callback(update, _DummyContext())
        text = update.callback_query.edits[-1]

        assert "⭐ Active session switched" in text
        assert "🧠 claude / repo-alpha" in text
        assert session.session_id in text
        assert str((workspace / "repo-alpha").resolve()) not in text
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_git_status_uses_active_session_repo(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        repo_path = str((workspace / "repo-alpha").resolve())
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])
        store = SessionStore()
        session = store.create("claude", repo_path, telegram_chat_id=100, owner_user_id=1)
        store.bind(100, session.session_id)

        captured = {}

        class _FakeGitService:
            def __init__(self, repo_path=None):
                captured["repo_path"] = repo_path

            def get_git_status_summary(self):
                return {
                    "current_branch": "main",
                    "working_directory_clean": False,
                    "changes": {
                        "modified": ["src/app.py"],
                        "created": ["src/new.py"],
                        "deleted": [],
                        "total": 2,
                    },
                    "staged_files": ["src/app.py"],
                    "unstaged_files": ["src/new.py"],
                    "safety": {
                        "safe_files": ["src/app.py", "src/new.py"],
                        "sensitive_files": [],
                        "has_sensitive_files": False,
                    },
                }

        update = _DummyUpdate()
        with patch("src.core.git_automation.GitAutomationService", _FakeGitService):
            await bot._handle_git_status(update, _DummyContext())

        text = update.message.replies[-1]
        assert captured["repo_path"] == repo_path
        assert f"Session: `{session.session_id}`" in text
        assert "• Modified: 1" in text
        assert "• Created: 1" in text
        assert "• Deleted: 0" in text
        assert "• `src/app.py`" in text
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_commit_uses_active_session_context_not_task_id(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        repo_path = str((workspace / "repo-alpha").resolve())
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])
        store = SessionStore()
        session = store.create("claude", repo_path, telegram_chat_id=100, owner_user_id=1)
        session.last_task_id = "task_123"
        session.last_user_message = "Fix the flaky Telegram session recovery path"
        store.save(session)
        store.bind(100, session.session_id)

        captured = {}

        class _FakeGitService:
            def __init__(self, repo_path=None):
                captured["repo_path"] = repo_path

            def safe_commit_task(self, task_id, task_description, create_branch=True, push_branch=False):
                captured["task_id"] = task_id
                captured["task_description"] = task_description
                captured["create_branch"] = create_branch
                captured["push_branch"] = push_branch
                return {
                    "success": True,
                    "branch_name": "feature/task-123-fix-session-recovery",
                    "files_committed": ["src/telegram/interface.py"],
                    "sensitive_files_blocked": [],
                    "errors": [],
                }

        update = _DummyUpdate()
        with patch("src.core.git_automation.GitAutomationService", _FakeGitService):
            await bot._handle_git_commit(update, _DummyContext(["--push"]))

        text = update.message.replies[-1]
        assert captured["repo_path"] == repo_path
        assert captured["task_id"] == "task_123"
        assert captured["task_description"] == "Fix the flaky Telegram session recovery path"
        assert captured["create_branch"] is True
        assert captured["push_branch"] is True
        assert f"session `{session.session_id}`" in text
        assert "Files committed: 1" in text
        assert "Branch pushed to remote." in text
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_cancel_without_args_uses_active_session_last_task(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        orchestrator = _DummyOrchestrator()
        bot = TelegramInterface("", orchestrator, allowed_users=[1])
        store = SessionStore()
        session = store.create("claude", str((workspace / "repo-alpha").resolve()), telegram_chat_id=100, owner_user_id=1)
        session.last_task_id = "task_777"
        store.save(session)
        store.bind(100, session.session_id)

        update = _DummyUpdate()
        await bot._handle_cancel_command(update, _DummyContext())

        assert orchestrator.cancelled_tasks == ["task_777"]
        assert f"session `{session.session_id}` task `task_777`" in update.message.replies[-1]
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_progress_without_args_uses_active_session_last_task(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        logs_dir = Path.cwd() / ".test_telegram_logs" / uuid.uuid4().hex[:8]
        logs_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config.system, "logs_dir", str(logs_dir), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])
        store = SessionStore()
        session = store.create("claude", str((workspace / "repo-alpha").resolve()), telegram_chat_id=100, owner_user_id=1)
        session.last_task_id = "task_888"
        store.save(session)
        store.bind(100, session.session_id)

        events_path = logs_dir / "events.ndjson"
        events = [
            {"timestamp": "2026-03-26T10:00:00", "event": "claude_started", "task_id": "task_888", "worker": "w1"},
            {"timestamp": "2026-03-26T10:00:02", "event": "claude_finished", "task_id": "task_888", "status": "success", "duration_s": 2.0},
        ]
        events_path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")

        update = _DummyUpdate()
        await bot._handle_progress_command(update, _DummyContext())

        text = update.message.replies[-1]
        assert f"session `{session.session_id}` / task `task_888`" in text
        assert "started" in text
        assert "finished" in text
    finally:
        shutil.rmtree(Path.cwd() / ".test_telegram_logs", ignore_errors=True)
        shutil.rmtree(workspace.parent, ignore_errors=True)


def test_format_progress_line_shows_codex_backend():
    bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

    started = bot._format_progress_line(
        {"timestamp": "2026-03-26T10:00:00", "event": "codex_started", "task_id": "task_1", "worker": "w1", "backend": "codex"}
    )
    finished = bot._format_progress_line(
        {"timestamp": "2026-03-26T10:00:02", "event": "codex_finished", "task_id": "task_1", "status": "SUCCESS", "duration_s": 2.0, "backend": "codex"}
    )

    assert "started" in started
    assert "codex" in started
    assert "finished" in finished
    assert "codex" in finished
