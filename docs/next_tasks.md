## Next 7 tasks (Validation, Reliability, Telegram, Ops)

- **1) Validation engine MVP** — DONE
  - Goal: basic hallucination guard and result sanity checks.
  - Edits: `orchestrator/src/validation/engine.py`, `orchestrator/src/orchestrator.py`.
  - Changes: implement `ValidationEngine.validate_llama_output` and `validate_task_result` with:
    - similarity (sentence-transformers) and simple entropy checks
    - structure checks for expected fields by `TaskType`
    - wire into pipeline: warn/fail early if invalid; include validation info in results JSON
  - Done when: invalid LLAMA parse or bad Claude result is detected and logged; results JSON includes `validation` details.

- **2) Unit tests for tool permissions** — DONE
  - Goal: prevent regressions in least-privilege tool sets.
  - Edits: `orchestrator/tests/test_permissions.py`.
  - Changes: assert allowed tool sets for `FIX|ANALYZE` and `CODE_REVIEW|SUMMARIZE` match policy.
  - Done when: tests pass on Windows without external dependencies.

- **3) LLAMA JSON-mode + robust parsing** — DONE
  - Goal: make LLAMA parsing stable with JSON responses and graceful fallback.
  - Edits: `orchestrator/src/bridges/llama_mediator.py`.
  - Changes: request JSON output; parse with strict loader; fallback to regex extraction if model not available; include `model` and `ollama_available` in status.
  - Done when: parsing works both with and without Ollama installed.

- **4) Structured events file (NDJSON)** — DONE
  - Goal: durable ops telemetry for every run.
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: append one NDJSON line per event (`task_received`, `parsed`, `claude_started`, `claude_finished`, `summarized`, `artifacts_written`, `retry`) to `logs/events.ndjson` with task metadata.
  - Done when: events file is created and contains entries for a smoke run.

- **5) Telegram minimal interface (optional)**
  - Goal: headless control: create tasks and read status from chat.
  - Edits: `orchestrator/src/telegram/interface.py`, `orchestrator/main.py`, `docs/QUICK_START.md`.
  - Changes: implement `/task <desc>` (creates `.task.md`) and `/status`; enable via env vars; disabled by default.
  - Done when: with valid tokens, `/status` responds and `/task` creates a task file.

- **6) Log rotation** — DONE
  - Goal: prevent unbounded log growth.
  - Edits: `orchestrator/main.py` logging setup.
  - Changes: switch file handler to `RotatingFileHandler` for `logs/orchestrator.log` and rotate `logs/events.ndjson` daily or by size.
  - Done when: rotation occurs after threshold in a simulated run.

- **7) Docs: production and operations** — DONE
  - Goal: clarify setup and ops practices.
  - Edits: `docs/README.md`, `docs/QUICK_START.md`.
  - Changes: add Telegram env/config, describe events file, log rotation, and Windows service/process supervision pointers.
  - Done when: docs clearly describe env vars and operational artifacts.



## Next 7 tasks (Reliability and Ops Enhancements)

- **1) File system robustness (debounce + atomicity + locking)** — DONE
  - Goal: eliminate duplicate/racy processing when tasks are written/edited quickly.
  - Edits: `orchestrator/src/core/file_watcher.py`, `orchestrator/src/orchestrator.py`.
  - Changes: debounce filesystem events, process after short stability window; prefer tmp→rename atomic writes; per-task in-memory lock.
  - Done when: no double-processing observed under rapid edits and startup scans.

- **2) Error recovery tiering (clear taxonomy + backoff jitter)** — DONE
  - Goal: recover gracefully on transient errors and fail fast on fatal ones.
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: explicit transient/fatal markers, exponential backoff with jitter and caps, unit tests.
  - Done when: tests prove retries on transient and immediate stop on fatal.

- **3) LLAMA context/size management** — DONE
  - Goal: keep prompts within reliable size; avoid timeouts on large tasks.
  - Edits: `orchestrator/src/bridges/llama_mediator.py`, `orchestrator/src/bridges/claude_bridge.py`, `orchestrator/config/settings.py`.
  - Changes: cap request sizes, truncation policy in config; log truncation events; tests to verify truncation.
  - Done when: truncation is logged and tests pass; behavior is configurable.

- **4) Lightweight metrics from NDJSON** — DONE
  - Goal: quick visibility without adding infra.
  - Edits: `orchestrator/main.py`.
  - Changes: new command `python main.py stats` to compute counts, success rate, and latency percentiles from `logs/events.ndjson`.
  - Done when: command prints a one-screen summary.

- **5) Queue persistence on restart**
  - Goal: resume cleanly after process restarts.
  - Edits: `orchestrator/src/orchestrator.py`.
  - Changes: persist minimal queue/state to `logs/state.json`; reload on startup; skip `tasks/processed`.
  - Done when: restart mid-run resumes/cleans correctly without reprocessing.

- **6) CLI UX refinements** - cancelled 
  - Goal: faster local operations.
  - Edits: `orchestrator/main.py`.
  - Changes: add `run file <path>` to process one `.task.md` without watcher and `create-task "<desc>"` helper.
  - Done when: both commands work end-to-end.

- **7) Packaging/CI hygiene**
  - Goal: reproducible installs and green checks.
  - Edits: repo root.
  - Changes: add `pyproject.toml`, basic pre-commit config, and GitHub Actions to run pytest on Windows.
  - Done when: one-command setup works and CI is green.

Note: Telegram minimal interface remains optional but high-impact. When enabled via env, implement `/status` and `/task <desc>` (creates `.task.md`).
