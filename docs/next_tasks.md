## Next 7 tasks to advance the project - DONE 

- **1) Harden Claude permissions by task type** - DONE 
  - Goal: enforce least-privilege tool use per `TaskType`.
  - Edits: `orchestrator/src/bridges/claude_bridge.py`.
  - Changes:
    - `FIX|ANALYZE` → `Read,Edit,MultiEdit,LS,Grep,Glob,Bash`.
    - `CODE_REVIEW|SUMMARIZE` → `Read,LS,Grep,Glob` (no write).
  - Done when: tool sets match expectations in a smoke run.

- **2) Persist artifacts for every run** - DONE 
  - Goal: save raw CLI JSON/stdout/stderr and human summary.
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: write `results/{task_id}.json` and `summaries/{task_id}_summary.txt` after each run.
  - Done when: files are created with non-empty content.

- **3) Structured logging at key pipeline steps** - DONE 
  - Goal: actionable logs for ops/debugging.
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: log `task_received`, `parsed`, `claude_started`, `claude_finished`, `summarized`, `artifacts_written` with task metadata.
  - Done when: logs show all steps for a run.

- **4) End-to-end watcher smoke test** - DONE 
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


## Next 7 tasks (Refinements for usability, validation, and ops)

- **1) Keep human summaries concise (no raw JSON dump)**
  - Goal: make `summaries/*.txt` readable and stable for users.
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: write only the human-readable summary (top block); never append Claude raw JSON to summaries. Raw stays in `results/*.json -> parsed_output.result`.
  - Done when: new E2E run produces summaries without embedded JSON blocks.

- **2) Basic validation guard (claims vs evidence)**
  - Goal: catch obvious hallucinations (e.g., claims of edits when none happened).
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: add `validation` field to results JSON with `valid: bool`, `reasons: list[str]`; flag when `files_modified` is empty but output claims modifications.
  - Done when: task with such claims is flagged and `validation.valid == false` with a reason.

- **3) Archive processed tasks to avoid reprocessing clutter**
  - Goal: keep `tasks/` tidy and prevent re-queuing on startup.
  - Edits: `orchestrator/src/core/file_watcher.py`, `orchestrator/src/orchestrator.py`, `orchestrator/main.py`.
  - Changes: after completion, move the `.task.md` to `tasks/processed/{id}.{status}.task.md`; on startup, skip files already present in `processed/`.
  - Done when: after a run, the created task is moved and is not reprocessed on next start.

- **4) E2E test hygiene and stronger assertions**
  - Goal: keep tests self-cleaning and assert real content.
  - Edits: `orchestrator/tests/test_watcher_e2e.py`.
  - Changes: delete/archive the temp task after success; assert that summary contains non-trivial content (e.g., keywords from `orchestrator.py`), not just status lines.
  - Done when: test passes and `tasks/` remains uncluttered between runs.


- **6) CLI maintenance commands**
  - Goal: easy housekeeping for artifacts and tasks.
  - Edits: `orchestrator/main.py`.
  - Changes: add `clean` commands: `clean tasks` (move/delete old tasks), `clean artifacts --days N`, `status` already exists.
  - Done when: `python orchestrator/main.py clean tasks` archives old tasks and `clean artifacts` prunes old results/summaries.

- **7) Prompt policy: pass-through with minimal framing**
  - Goal: let Claude use its natural navigation; avoid over-instruction.
  - Edits: `docs/QUICK_START.md`, `docs/README.md`.
  - Changes: document the policy to pass the task prompt verbatim, run from repo root, use read-only tools for summarize/review, and rely on validation + results JSON for guardrails.
  - Done when: docs reflect the simple, reliable prompt strategy and reference validation.