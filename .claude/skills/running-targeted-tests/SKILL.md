---
name: running-targeted-tests
description: Picks and runs the exact pytest/vitest targets that cover a change in the AI-Team repo, without ever touching the paid e2e path or the full suite. Use this whenever you are about to run tests, verify a fix, prove a change green before commit/PR/hand-back, or a hook blocked a pytest command — even if the user just says "run the tests" or "make sure it passes".
---

# Running targeted tests

Tests in this repo can reach the **paid** Claude CLI and have burned millions of tokens before, so
the rule is: plain pytest on the modules you touched, never the whole suite, never e2e. A
`PreToolUse` hook (`.claude/hooks/pytest_guard.py`) blocks bare `pytest`, `pytest tests/`,
`--run-e2e` and `AI_TEAM_ALLOW_OPENCODE_E2E=`; this skill is how you pick targets it will accept.

## Workflow

1. **Select targets** (prints matches by reason and a ready command; runs nothing):
   ```bash
   python3 .claude/skills/running-targeted-tests/scripts/select_tests.py           # diff vs origin/main + worktree
   python3 .claude/skills/running-targeted-tests/scripts/select_tests.py src/x.py  # explicit files
   ```
   Matching is `src/<pkg>/<name>.py` → `tests/test_<name>.py`, `tests/test_<name>_*.py`, plus any
   test that references `src.<pkg>.<name>`. Name matching alone misses ~60% of modules (e.g.
   `turn_queue` is covered by ~45 `test_turn_queue_*` files), which is why the script also greps.
2. **Run the direct matches first**: `.venv/bin/pytest <files>` (add `-k <expr>` to narrow).
   Widen to the "references" set only when the change crosses a seam those tests exercise.
   `pyproject.toml` already sets `addopts = "-q"`; adding another `-q` makes it `-qq`, which
   suppresses the `N passed` summary you need as evidence.
   **Speed:** for more than ~10 files add `-n 4` (pytest-xdist; on this 4-core Pi the A104 wide
   set dropped 8m03s → 2m58s). A single file is faster without `-n` (worker start-up). Every
   test's DB is seeded from a once-per-session migrated template (`tests/conftest.py`), so a
   new DB costs ~3 ms instead of replaying every migration (~150 ms). A test that sleeps a
   fixed time for a background thread will flake under `-n` — poll with a deadline instead.
3. **Web UI** (`web/src/**`): from `web/`, `pnpm exec vitest related --run <files>` then
   `pnpm typecheck`. CI runs neither, so this is the only check they get.
4. **Read failures before rerunning.** A failure in a file you didn't touch is evidence about your
   change until shown otherwise. Before calling it pre-existing, show it also fails on
   `origin/main` in a scratch tree (`git worktree add <scratch-dir> origin/main`, run the same file
   there, `git worktree remove` it).

## What protects you (so you know what *not* to work around)

- `tests/conftest.py` forces `AI_TEAM_TEST_MODE=1` and `MESH_ENABLED=false`, gives each test a
  temp SQLite DB, and no-ops the file watcher.
- `src/core/test_guard.py::assert_live_calls_allowed` raises in every backend spawn path under
  test mode. A test that needs a real backend is wrong — fake the driver instead.
- e2e files (`test_full_pipeline.py`, `test_opencode_server_integration.py`, `test_watcher_e2e.py`)
  only run with `--run-e2e`; the selector excludes them. Real e2e is operator-only.
- There is **no** network block in conftest. If a new test opens sockets or spawns `claude`,
  `codex` or `opencode`, stop and fake it.

## Reporting

Hand back the exact command and the result line (`N passed, M skipped in Xs`). State what the
targets do *not* cover (the selector's "No test found for" list, cross-layer seams) — a green run
proves only the layer it exercised.
