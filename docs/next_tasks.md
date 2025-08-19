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


## Next 7 tasks (Validation, Reliability, Telegram, Ops)

- **1) Validation engine MVP**
  - Goal: basic hallucination guard and result sanity checks.
  - Edits: `orchestrator/src/validation/engine.py`, `orchestrator/src/orchestrator.py`.
  - Changes: implement `ValidationEngine.validate_llama_output` and `validate_task_result` with:
    - similarity (sentence-transformers) and simple entropy checks
    - structure checks for expected fields by `TaskType`
    - wire into pipeline: warn/fail early if invalid; include validation info in results JSON
  - Done when: invalid LLAMA parse or bad Claude result is detected and logged; results JSON includes `validation` details.

- **2) Unit tests for tool permissions**
  - Goal: prevent regressions in least-privilege tool sets.
  - Edits: `orchestrator/tests/test_permissions.py`.
  - Changes: assert allowed tool sets for `FIX|ANALYZE` and `CODE_REVIEW|SUMMARIZE` match policy.
  - Done when: tests pass on Windows without external dependencies.

- **3) LLAMA JSON-mode + robust parsing**
  - Goal: make LLAMA parsing stable with JSON responses and graceful fallback.
  - Edits: `orchestrator/src/bridges/llama_mediator.py`.
  - Changes: request JSON output; parse with strict loader; fallback to regex extraction if model not available; include `model` and `ollama_available` in status.
  - Done when: parsing works both with and without Ollama installed.

- **4) Structured events file (NDJSON)**
  - Goal: durable ops telemetry for every run.
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: append one NDJSON line per event (`task_received`, `parsed`, `claude_started`, `claude_finished`, `summarized`, `artifacts_written`, `retry`) to `logs/events.ndjson` with task metadata.
  - Done when: events file is created and contains entries for a smoke run.

- **5) Telegram minimal interface (optional)**
  - Goal: headless control: create tasks and read status from chat.
  - Edits: `orchestrator/src/telegram/interface.py`, `orchestrator/main.py`, `docs/QUICK_START.md`.
  - Changes: implement `/task <desc>` (creates `.task.md`) and `/status`; enable via env vars; disabled by default.
  - Done when: with valid tokens, `/status` responds and `/task` creates a task file.

- **6) Log rotation**
  - Goal: prevent unbounded log growth.
  - Edits: `orchestrator/main.py` logging setup.
  - Changes: switch file handler to `RotatingFileHandler` for `logs/orchestrator.log` and rotate `logs/events.ndjson` daily or by size.
  - Done when: rotation occurs after threshold in a simulated run.

- **7) Docs: production and operations**
  - Goal: clarify setup and ops practices.
  - Edits: `docs/README.md`, `docs/QUICK_START.md`.
  - Changes: add Telegram env/config, describe events file, log rotation, and Windows service/process supervision pointers.
  - Done when: docs clearly describe env vars and operational artifacts.