import shutil
import uuid
import os
from pathlib import Path
from unittest.mock import patch
import json

import pytest

from config import config
from src.services.session_store import SessionStore
from src.telegram import interface as telegram_interface_module
from src.telegram.interface import TelegramInterface
import src.services.session_store as session_store_module


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
        self._backends = {}
        # Mirror the real orchestrator: a transport-neutral SessionService over a
        # SessionStore. The store honors the test-isolated _SESSIONS_DIR/_BINDINGS_FILE
        # monkeypatched by the isolated_session_store fixture.
        from src.services.session_service import SessionService
        self.session_service = SessionService(SessionStore())

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


def test_telegram_contexttypes_default_type_exists():
    assert hasattr(telegram_interface_module.ContextTypes, "DEFAULT_TYPE")


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
    # Most-used commands surface first; session_close stays in the top 3.
    assert names[:4] == ["session_new", "session_list", "session_close", "status"]
    assert "session_closed" in names
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
        reply = update.message.replies[-1]
        assert "Session created" in reply
        assert "Just type your request" in reply
        assert "/session_dirs" in reply
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
        monkeypatch.setattr(bot, "_mesh_online_nodes", lambda: [])

        update = _DummyUpdate()
        # Callback format: session_new_repo:{backend}:{node_id}:{index}
        update.callback_query = _DummyCallbackQuery("session_new_repo:codex:__local__:0")
        await bot._handle_session_new_callback(update, _DummyContext())

        active = SessionStore().get_active(update.effective_chat.id)
        assert active is not None
        assert active.backend == "codex"
        assert active.repo_path == str(repo_alpha.resolve())
        assert "Session created" in update.callback_query.edits[-1]
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
        assert f"#s_{session.session_id}" in update.message.replies[-1]
        assert "#t_task_1" in update.message.replies[-1]
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
        # Core commands are all documented (exact phrasing is friendly now).
        assert "/session_new" in text
        assert "/session_closed" in text
        assert "/session_dirs" in text
        assert "/session_cancel" in text
        assert "/git_status" in text
        assert "/commit" in text
        assert "/nodes" in text
        assert "/run <instruction>" not in text
        assert "/say <instruction>" not in text
        assert "/documentation" not in text
        assert "/code_review" not in text
        assert "/bug_fix" not in text
        assert "/analyze" not in text
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


def test_node_live_state_helpers_format_db_rows():
    row = {
        "max_concurrent": 4,
        "live_state": json.dumps({
            "v": 1,
            "slots_used": 2,
            "slots_total": 4,
            "active_tasks": ["task_a", "task_b"],
        }),
    }

    assert TelegramInterface._node_live_state(row)["slots_used"] == 2
    assert TelegramInterface._node_load_text(row) == "slots 2/4, active 2"


def test_node_live_state_helpers_handle_missing_state():
    assert TelegramInterface._node_live_state({"live_state": ""}) == {}
    assert TelegramInterface._node_load_text({"max_concurrent": 3}) == "slots ?/3"


def test_session_node_picker_filters_backend_and_shows_load(tmp_path, monkeypatch, isolated_session_store):
    from src.control.db import MeshDB

    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_node(
        "claude-full",
        "100.64.0.1",
        9001,
        ["claude"],
        2,
        repos=[{"name": "repo-alpha", "path": "/worker/repo-alpha"}],
    )
    db.heartbeat_node(
        "claude-full",
        live_state=json.dumps({"v": 1, "slots_used": 2, "slots_total": 2, "active_tasks": ["a", "b"]}),
    )
    db.upsert_node(
        "codex-only",
        "100.64.0.2",
        9001,
        ["codex"],
        2,
        repos=[{"name": "repo-beta", "path": "/worker/repo-beta"}],
    )

    monkeypatch.setattr("src.control.db.get_db", lambda: db)
    bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

    rows = bot._mesh_online_node_rows("claude")

    assert [row["node_id"] for row in rows] == ["claude-full"]
    assert TelegramInterface._node_load_text(rows[0]) == "slots 2/2, active 2"
    assert bot._mesh_online_node_rows("opencode") == []


@pytest.mark.asyncio
async def test_session_new_remote_command_uses_db_node_repos(
    tmp_path,
    monkeypatch,
    isolated_session_store,
):
    from src.control.db import MeshDB

    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_node(
        "worker-db",
        "100.64.0.3",
        9001,
        ["claude"],
        2,
        repos=[{"name": "repo-alpha", "path": "/worker/repo-alpha"}],
    )

    monkeypatch.setattr("src.control.db.get_db", lambda: db)
    bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

    update = _DummyUpdate(user_id=1, chat_id=100)
    await bot._handle_session_new(update, _DummyContext(["claude", "worker-db", "repo-alpha"]))

    store = SessionStore()
    session = store.get_active(100)
    assert session is not None
    assert session.machine_id == "worker-db"
    assert session.repo_path == "/worker/repo-alpha"


@pytest.mark.asyncio
async def test_session_new_rejects_node_without_backend(
    tmp_path,
    monkeypatch,
    isolated_session_store,
):
    from src.control.db import MeshDB

    db = MeshDB(str(tmp_path / "mesh.db"))
    db.upsert_node(
        "codex-only",
        "100.64.0.4",
        9001,
        ["codex"],
        2,
        repos=[{"name": "repo-beta", "path": "/worker/repo-beta"}],
    )

    monkeypatch.setattr("src.control.db.get_db", lambda: db)
    bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])

    update = _DummyUpdate(user_id=1, chat_id=100)
    await bot._handle_session_new(update, _DummyContext(["claude", "codex-only", "repo-beta"]))

    assert "does not advertise backend `claude`" in update.message.replies[-1]
    assert SessionStore().get_active(100) is None


@pytest.mark.asyncio
async def test_session_close_closes_local_backend_and_clears_backend_session_id(
    monkeypatch,
    isolated_session_store,
):
    workspace = _make_workspace()
    try:
        store = SessionStore()
        session = store.create(
            "opencode-server",
            str((workspace / "repo-alpha").resolve()),
            telegram_chat_id=100,
            owner_user_id=1,
        )
        session.backend_session_id = "ses_close_me"
        session.machine_id = telegram_interface_module.socket.gethostname()
        store.save(session)
        store.bind(100, session.session_id)

        closed = []

        class _Backend:
            def close(self, session_obj):
                closed.append((session_obj.session_id, session_obj.backend_session_id))

        orchestrator = _DummyOrchestrator()
        orchestrator._backends = {"opencode-server": _Backend()}
        bot = TelegramInterface("", orchestrator, allowed_users=[1])

        update = _DummyUpdate(user_id=1, chat_id=100)
        await bot._handle_session_close(update, _DummyContext())

        saved = store.get(session.session_id)
        assert closed == [(session.session_id, "ses_close_me")]
        assert saved.status == session_store_module.SessionStatus.CLOSED
        assert saved.backend_session_id == ""
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_list_compact_shows_open_and_collapsed_closed(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])
        store = SessionStore()

        open_session = store.create("claude", str((workspace / "repo-alpha").resolve()), telegram_chat_id=100, owner_user_id=1)
        open_session.last_user_message = "Please inspect the repo"
        open_session.last_result_summary = "Added session summaries to the picker"
        open_session.last_task_id = "task_picker"
        store.save(open_session)
        closed_session = store.create("claude", str((workspace / "repo-beta").resolve()), telegram_chat_id=100, owner_user_id=1)
        closed_session.status = session_store_module.SessionStatus.CLOSED
        store.save(closed_session)
        store.bind(100, open_session.session_id)

        update = _DummyUpdate()
        await bot._handle_session_list(update, _DummyContext())
        text = update.message.replies[-1]

        # Open sessions are rich (summary tail shown), the active one starred.
        assert len(update.message.replies) == 1
        assert "Open sessions (1)" in text
        assert "⭐" in text
        assert "repo-alpha" in text
        assert bot._short_id(open_session.session_id) in text
        # The orienting summary is back on each open session.
        assert "Added session summaries to the picker" in text
        # Closed sessions are kept out of the way — just a count + pointer.
        assert "1 closed" in text
        assert "/session_closed" in text
        # The closed session's repo/id should NOT appear inline in this view.
        assert "repo-beta" not in text
        # Full filesystem paths are never leaked — only repo basenames.
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
        assert "Summary: Investigate Telegram session picker formatting" in text
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

        assert "Switched to this session" in text
        assert "🧠 claude" in text
        assert "repo-alpha" in text
        assert bot._short_id(session.session_id) in text
        # Card invites the user to keep typing.
        assert "type to continue" in text.lower()
        assert str((workspace / "repo-alpha").resolve()) not in text
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_session_completion_notification_has_searchable_ref(monkeypatch, isolated_session_store):
    workspace = _make_workspace()
    try:
        monkeypatch.setattr(config.claude, "base_cwd", str(workspace), raising=False)
        monkeypatch.setattr(config.claude, "allowed_root", str(workspace), raising=False)
        bot = TelegramInterface("", _DummyOrchestrator(), allowed_users=[1])
        store = SessionStore()
        session = store.create("claude", str((workspace / "repo-alpha").resolve()), telegram_chat_id=100, owner_user_id=1)
        session.last_task_id = "task_done"
        store.save(session)

        class _FakeBot:
            def __init__(self):
                self.messages = []

            async def send_message(self, chat_id, text):
                self.messages.append({"chat_id": chat_id, "text": text})

        class _FakeApp:
            def __init__(self):
                self.bot = _FakeBot()

        bot.app = _FakeApp()
        bot.is_running = True

        await bot.notify_completion("task_done", "Finished the requested change.", success=True, chat_id=100)

        sent = bot.app.bot.messages[-1]
        assert sent["chat_id"] == 100
        assert sent["text"].startswith(f"#s_{session.session_id} #t_task_done\n")
        assert "Finished the requested change." in sent["text"]
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
        with patch("src.services.git_automation.GitAutomationService", _FakeGitService):
            await bot._handle_git_status(update, _DummyContext())

        text = update.message.replies[-1]
        assert captured["repo_path"] == repo_path
        assert f"#s_{session.session_id}" in text
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
        with patch("src.services.git_automation.GitAutomationService", _FakeGitService):
            await bot._handle_git_commit(update, _DummyContext(["--push"]))

        text = update.message.replies[-1]
        assert captured["repo_path"] == repo_path
        assert captured["task_id"] == "task_123"
        assert captured["task_description"] == "Fix the flaky Telegram session recovery path"
        assert captured["create_branch"] is True
        assert captured["push_branch"] is True
        assert f"#s_{session.session_id}" in text
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
        assert f"#s_{session.session_id}" in update.message.replies[-1]
        assert "task_777" in update.message.replies[-1]
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
        assert f"#s_{session.session_id}" in text
        assert "task_888" in text
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
