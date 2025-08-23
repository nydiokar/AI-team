## AI Task Orchestrator — Next Stage Plan

### Where we are
- File-based workflow is implemented and robust: debounced `FileWatcher`, atomic writes, and processed task archiving.
- Parsing pipeline is solid: `TaskParser` + `LlamaMediator` with Ollama support and reliable fallback, prompt capping and JSON-mode where available.
- Execution bridge is operational: `ClaudeBridge` with non-interactive mode, structured output, least-privilege `--allowedTools`, retries with backoff, timeout caps.
- Orchestrator reliability features: async workers, queue persistence on restart (`logs/state.json`), structured telemetry (`logs/events.ndjson`), artifacts persisted to `results/` and `summaries/`.
- Validation MVP exists: similarity/entropy checks and basic structure/coherence validation wired into artifacts and events.
- CLI and docs: `python main.py` (plus `status`, `stats`, `clean`, `create-sample`), quick start and architecture docs in place. Unit tests include permission policy and queue persistence.

### Next-stage objective (2–3 weeks)
Strengthen reliability and operability, add interactive control, and stabilize artifacts/validation so the system is safer to run unattended and easier to monitor.

### Prioritized next 6 tasks
1) Telegram minimal interface (opt-in)
   - Goal: Chat-based control and notifications.
   - Scope: `/task <desc>` creates `.task.md`; `/status` shows components/workers/queue; optional `/cancel <task_id>`.
   - Acceptance: With valid env vars, commands work end-to-end and notifications on completion/failure are delivered.

2) Validation Engine 2.0 (coherence + structure)
   - Goal: Reduce false positives/negatives and verify outputs align with requested task type and files.
   - Scope: length-aware entropy, optional 3-gram Jaccard fallback, per-`TaskType` structure keywords, cross-check claimed edits vs `files_modified` and target file allowlist.
   - Acceptance: New tests cover summarize/review vs fix/analyze behavior; artifacts include richer `validation` block.

3) Artifact schema v1 (versioned, documented)
   - Goal: Make results machine-stable for analytics and external tooling.
   - Scope: Add `schema_version`, define JSON Schema for `results/*.json`, include `orchestrator`, `bridge`, and `llama` status in artifacts; add `python main.py validate-artifacts` command.
   - Acceptance: Schema file exists in `docs/schema/`, validation command reports OK on fresh runs, CI step passes.

4) Claude CLI hardening and observability
   - Goal: Fewer flaky runs and better diagnostics when CLI behavior changes.
   - Scope: Detect interactive prompts proactively; expand transient error taxonomy; capture first/last 2KB of stdout/stderr in artifacts; optional `--max-turns`/timeout config via env.
   - Acceptance: New unit tests simulate CLI failures; retries behave as expected; artifacts contain concise triage fields.

5) Packaging and CI hygiene (project-wide)
   - Goal: Reproducible installs and green checks on Windows.
   - Scope: Introduce `pyproject.toml` with extras (dev,test); pre-commit hooks (black/ruff/mypy where applicable); GitHub Actions to run pytest on Windows.
   - Acceptance: One-command local setup works; CI is green; `requirements.txt` deprecated in favor of `pyproject.toml`.

6) Task lifecycle controls and SLAs
   - Goal: Operational safety for long-running tasks.
   - Scope: Per-task timeout overrides, graceful cancel, status transitions persisted; surface ETA/elapsed in NDJSON; optional concurrency by `TaskPriority`.
   - Acceptance: Timeouts cancel correctly; `/status` (or CLI) shows lifecycle; events include `cancelled` and timeout markers.


### Nice-to-have (after the six above)
- Optional guarded-write mode: stage code edits as diffs in results and require manual apply; or restrict edits to allowlisted directories.
- Lightweight dashboard: tail `events.ndjson`, show success-rate and p50/p95 durations.
- Agentic coordinator mode (optional): drive an LLM loop in `LlamaMediator` to decide when to call Claude, which tools to allow, and when to stop. Keep current orchestrator path as the default for reliability.

### Add-on task (high utility): Per-task working directory selection - DONE 
- Goal: Spawn Claude in the correct project folder per task/message.
- Scope:
  - Support `cwd` in task YAML frontmatter and detect patterns like “in C:\path\to\project” or “in /path/project” in free-text descriptions when creating tasks.
  - Pass `cwd` through `Task.metadata` and have `ClaudeBridge` run with that as working directory (fallback: project root).
  - Validate path existence and restrict to an allowlist root for safety (configurable).
- Acceptance:
  - Given a description with a path or a `.task.md` with `cwd: ...`, Claude runs with that `cwd`.
  - Artifacts include the effective working directory.
  - Tests cover Windows and POSIX path forms.

### Notes
- All proposed work keeps changes minimal and localized, building on the existing architecture and tests.
- Telegram remains strictly optional and is off by default unless env is configured.
- Coordinator role today: `TaskOrchestrator` orchestrates parse → Claude → summarize → persist/telemetry. `LlamaMediator` is not a tool-calling agent; it crafts prompts and summaries. If you want LLAMA to act as a tool-calling coordinator, track it under the optional “Agentic coordinator mode”.


