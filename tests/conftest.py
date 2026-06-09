"""
Global pytest configuration — cost & safety guards.

Several tests construct a real ``TaskOrchestrator`` (which starts a file
watcher) or call backends directly. Historically that could dispatch task files
to the **live, paid Claude CLI** during an ordinary ``pytest`` run, silently
burning tokens. This conftest makes that impossible:

  1. ``AI_TEAM_TEST_MODE=1`` is forced for the whole session, so the cost guard
     in ``src/core/test_guard.py`` blocks every paid backend spawn.
  2. ``MESH_ENABLED`` is forced off so no test starts the embedded task server
     or routes anything to a worker.
  3. The file watcher is neutralised so a stray ``*.task.md`` can't be picked up
     and executed.
  4. ``e2e`` tests are deselected by default; run them explicitly with
     ``pytest --run-e2e`` (and, for a real OpenCode run, also set
     ``AI_TEAM_ALLOW_OPENCODE_E2E=1``). Claude/Codex are never reachable.

These guards are set BEFORE any test imports config, so they win over .env.
"""

import os
import tempfile
from pathlib import Path

import pytest

# --- 1–2. Force safe env as early as possible (import time) -----------------
os.environ["AI_TEAM_TEST_MODE"] = "1"
os.environ["MESH_ENABLED"] = "false"


@pytest.fixture(autouse=True, scope="session")
def _enforce_test_mode():
    """Belt-and-suspenders: re-assert the guards for the whole session."""
    os.environ["AI_TEAM_TEST_MODE"] = "1"
    os.environ["MESH_ENABLED"] = "false"
    # If config was already imported, make sure its mesh flag reflects test mode.
    try:
        from config import config
        config.mesh.enabled = False
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _isolate_db():
    """Redirect the mesh DB to a unique temporary file per test so tests never
    touch the live ``state/mesh.db`` and see a clean, empty database.

    Phase 1 made ``SessionStore`` read from DB first. Without isolation,
    ``list_all()`` / ``get()`` return live production sessions, breaking
    tests that assert specific session counts or states.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    test_db = Path(tmp.name)
    try:
        from config import config
        config.mesh.shadow_write = True
        config.mesh.db_path = str(test_db)
        import src.control.db as db_mod
        old = db_mod._db_instance
        db_mod._db_instance = None
        if old is not None:
            old.close()
        yield
    finally:
        for ext in ("", "-wal", "-shm"):
            p = Path(str(test_db) + ext)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass


@pytest.fixture(autouse=True)
def _disable_file_watcher(monkeypatch):
    """Neutralise the orchestrator file watcher so no .task.md auto-dispatches.

    Tests that need watcher behaviour explicitly drive it; the default-on
    background watcher is what historically caused live executions.
    """
    try:
        from src.core.file_watcher import AsyncFileWatcher

        async def _noop_start_async(self, *a, **k):
            return None

        async def _noop_stop_async(self, *a, **k):
            return None

        monkeypatch.setattr(AsyncFileWatcher, "start_async", _noop_start_async, raising=False)
        monkeypatch.setattr(AsyncFileWatcher, "stop_async", _noop_stop_async, raising=False)
    except Exception:
        pass
    yield


# --- 4. e2e marker + opt-in gate --------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.e2e (may invoke real backends — OpenCode only).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: end-to-end test that may invoke a real backend; deselected unless --run-e2e.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="e2e test skipped (pass --run-e2e to run)")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
