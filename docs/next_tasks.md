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

- **3) LLAMA JSON-mode + robust parsing**
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