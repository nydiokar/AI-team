## Next 7 tasks to advance the project

- **1) Harden Claude permissions by task type**
  - Goal: enforce least-privilege tool use per `TaskType`.
  - Edits: `orchestrator/src/bridges/claude_bridge.py`.
  - Changes:
    - `FIX|ANALYZE` → `Read,Edit,MultiEdit,LS,Grep,Glob,Bash`.
    - `CODE_REVIEW|SUMMARIZE` → `Read,LS,Grep,Glob` (no write).
  - Done when: tool sets match expectations in a smoke run.

- **2) Persist artifacts for every run**
  - Goal: save raw CLI JSON/stdout/stderr and human summary.
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: write `results/{task_id}.json` and `summaries/{task_id}_summary.txt` after each run.
  - Done when: files are created with non-empty content.

- **3) Structured logging at key pipeline steps**
  - Goal: actionable logs for ops/debugging.
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: log `task_received`, `parsed`, `claude_started`, `claude_finished`, `summarized`, `artifacts_written` with task metadata.
  - Done when: logs show all steps for a run.

- **4) End-to-end watcher smoke test**
  - Goal: verify watcher→parse→Claude→artifacts works in real mode.
  - Edits: add a test (e.g., `orchestrator/tests/test_watcher_e2e.py`).
  - Changes: create a temp `.task.md` (read-only), start the orchestrator, wait for result, assert artifacts exist, stop.
  - Done when: test passes on Windows without manual input.

- **5) Config hardening + docs**
  - Goal: make headless behavior explicit.
  - Edits: `orchestrator/config/settings.py`, `docs/QUICK_START.md`.
  - Changes: add `config.claude.max_turns` (default 10); default `--permission-mode bypassPermissions`; allow `--dangerously-skip-permissions` via `CLAUDE_SKIP_PERMISSIONS=true`.
  - Done when: settings present and Quick Start documents exact env/flags.

- **6) Error classification and limited retry**
  - Goal: recover from transient failures (timeout/rate-limit).
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: classify failure, retry up to 2x with backoff for transient classes; include retry info in logs and `results` JSON.
  - Done when: simulated transient error triggers a retry.

- **7) Read-only smoke task profile**
  - Goal: safe health check task that never writes.
  - Edits: add `orchestrator/tasks/read_only_smoke.task.md`.
  - Done when: running the smoke task succeeds with no writes and only read tools allowed.

### Notes
- Keep `CLAUDE_SKIP_PERMISSIONS=false` by default; enable only for unattended CI runs.
- Do not use unsupported flags (`--cwd`, `--permission-prompt-tool auto`). Current flags are cross-platform.

