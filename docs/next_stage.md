## AI Task Orchestrator — Next Stage Plan

### Current Status Summary
- ✅ **COMPLETED (7/9)**: Validation Engine 2.0, Artifact Schema v1, Claude CLI hardening, Telegram interface, Task lifecycle controls, Packaging & CI, Prompt-template agents
- 🔄 **IN PROGRESS**: LLAMA-mediated reply flow (groundwork complete)
- 📋 **REMAINING**: Real-time progress monitoring, implement /reply flow, broaden CLI error taxonomy

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
1) Telegram minimal interface (opt-in) - DONE
   - Goal: Chat-based control and notifications.
   - Scope: `/task <desc>` creates `.task.md`; `/status` shows components/workers/queue; `/cancel <task_id>` for task cancellation.
   - Acceptance: With valid env vars, commands work end-to-end and notifications on completion/failure are delivered.
   - ✅ **COMPLETED**: All commands implemented, cancel integration wired to orchestrator, notifications working.

2) Validation Engine 2.0 (coherence + structure) - DONE
   - Goal: Reduce false positives/negatives and verify outputs align with requested task type and files.
   - Scope: length-aware entropy, optional 3-gram Jaccard fallback, per-`TaskType` structure keywords, cross-check claimed edits vs `files_modified` and target file allowlist.
   - Acceptance: New tests cover summarize/review vs fix/analyze behavior; artifacts include richer `validation` block.

3) Artifact schema v1 (versioned, documented) - DONE
   - Goal: Make results machine-stable for analytics and external tooling.
   - Scope: Add `schema_version`, define JSON Schema for `results/*.json`, include `orchestrator`, `bridge`, and `llama` status in artifacts; add `python main.py validate-artifacts` command.
   - Acceptance: Schema file exists in `docs/schema/`, validation command reports OK on fresh runs, CI step passes.

4) Claude CLI hardening and observability - DONE
   - Goal: Fewer flaky runs and better diagnostics when CLI behavior changes.
   - Scope: Detect interactive prompts proactively; expand transient error taxonomy; capture first/last 2KB of stdout/stderr in artifacts; optional `--max-turns`/timeout config via env.
   - Acceptance: New unit tests simulate CLI failures; retries behave as expected; artifacts contain concise triage fields.

**Updated Scope with LLAMA Mediation:**
- **Primary**: Use proper CLI flags (`--dangerously-skip-permissions`, `-p`) to prevent interactive prompts
- **Secondary**: LLAMA mediates when Claude gets confused or hits prompts (suggests alternatives, rephrases requests)
- **Safety Net**: Interactive detection as fallback, but LLAMA determines if task is actually incomplete
- **False Positive Protection**: Only fail if task is genuinely incomplete, not just on prompt detection
- **Environment Config**: `CLAUDE_TIMEOUT_SEC` and `CLAUDE_MAX_TURNS` for operational control

5) Packaging and CI hygiene (project-wide)
   - Goal: Reproducible installs and green checks on Windows.
   - Scope: Introduce `pyproject.toml` with extras (dev,test); pre-commit hooks (black/ruff/mypy where applicable); GitHub Actions to run pytest on Windows.
   - Acceptance: One-command local setup works; CI is green; `requirements.txt` deprecated in favor of `pyproject.toml`.

6) Task lifecycle controls and SLAs - DONE
   - Goal: Operational safety for long-running tasks.
   - Scope: Per-task timeout overrides, graceful cancel, status transitions persisted; surface ETA/elapsed in NDJSON; optional concurrency by `TaskPriority`.
   - Acceptance: Timeouts cancel correctly; `/status` (or CLI) shows lifecycle; events include `cancelled` and timeout markers.
   - ✅ **COMPLETED**: Per-task timeout, cooperative cancellation, cancel events, Telegram /cancel integration.

7) LLAMA‑mediated interactive reply flow (turn-based) - PARTIAL (groundwork)
   - Goal: Enable user to continue an already-processed task with follow-up instructions without losing context; LLAMA mediates constraints and re-invokes Claude in a new turn.
   - Scope:
     - Telegram `/reply <task_id> ...` to post follow-up instructions (no live sessions required)
     - Persist a `conversation` array in artifacts (turns with role, content, timestamp)
     - LLAMA summarizes prior turns and applies user constraints; Claude runs again headless, using prior context
     - Events: emit `turn_started`/`turn_finished` with linkage to original task
   - Acceptance:
     - Given an existing artifact, a reply creates a new turn artifact linked to the original
     - Constraints like "yes, but skip A/B; focus on X" are respected in resulting changes
     - Strict validation passes for new artifacts; summaries show conversation context
   - Prerequisites (activation with current system):
     - Add optional `conversation` field to results schema and validator (no breaking change)
     - Add `parent_task_id` or `turn_of` linkage field in new-turn artifacts for traceability
     - Implement a lightweight artifact index/lookup (map `task_id` → latest `results/*.json`) to avoid relying on original `.task.md` (which is archived under `tasks/processed/`)
     - Orchestrator context loader that pulls latest artifact for a task and passes condensed context to LLAMA
     - Telegram `/reply` endpoint wiring (authz via allowlist), rate limiting, and basic input validation
     - Eventing: add `turn_started/turn_finished` with `turn_index`, `parent_task_id`
     - Tests: conversation persistence, constraint application, schema validation for new-turn artifacts


8) Prompt‑template agents and Telegram commands (LLAMA‑expanded) - DONE
   - Goal: Enable concise user intents like `/documentation <file>` or `/code-review <path>` to expand into high‑quality, best‑practice tasks via LLAMA, then execute with Claude headlessly.
   - Scope:
     - Define agent templates (documentation, code_review, bug_fix, analyze) with structured JSON outputs and embedded best‑practice hints
     - Implement `LlamaMediator.expand_agent_intent(agent, args, attachments)` → enriched task (type, prompt, targets, success criteria)
     - Add Telegram commands: `/documentation`, `/code_review`, `/bug_fix`, `/analyze` with input validation and allowlist
     - Attachment/path ingestion from messages; safe path resolution under allowed root
     - Tests: golden prompts for determinism, safety checks, schema validation of expanded tasks
   - Acceptance:
     - Given a blueprint file and `/documentation`, the system creates an enriched task with clear sections and best practices, then runs successfully under constraints
     - Expanded tasks remain within token caps, produce valid artifacts, and pass validation
   - ✅ **COMPLETED**: Agent templates with few-shots, Telegram commands, LLAMA expansion, file attachment handling, directory creation, file copying to working directory
   - Pitfalls → Mitigations:
     - Prompt drift/hallucination → Use JSON schema + few‑shot examples; validate required fields; fallback to minimal template
     - Context bloat (token overrun) → Summarize input files; cap sizes via config; include only salient snippets
     - Unsafe paths/overreach → Enforce allowed root; sanitize user paths; deny absolute paths unless configured
     - Inconsistent outputs → Pin template versions; snapshot template id in artifact; add golden tests

9) Real‑time progress monitoring and status - NEW
   - Goal: Provide live visibility into running work: phases, percent complete heuristic, ETA, and recent events via CLI/Telegram.
   - Scope:
     - Extend events with `step_started/step_finished`, include `phase`, `elapsed_ms`, `percent_complete` heuristic
     - Improve `/status` to list active tasks with elapsed/ETA and last 3 events; add CLI `python main.py tail-events`
     - File change feed: show recent `files_modified` deltas as they’re detected
     - Lightweight log tailing with rotation; Windows‑friendly
   - Acceptance:
     - `/status` shows active/queued tasks with live elapsed/ETA and recent steps; `events.ndjson` contains step events; `tail-events` streams without blocking the orchestrator
   - Pitfalls → Mitigations:
     - Event volume/noise → Sample non‑critical events; size‑cap and rotate logs
     - ETA accuracy → Display ranges/confidence; degrade gracefully when cues are missing
     - Overhead in hot loop → Batch writes, async buffers; avoid synchronous fs ops on critical path

### Nice-to-have (after the six above)
- Optional guarded-write mode: stage code edits as diffs in results and require manual apply; or restrict edits to allowlisted directories.
- Lightweight dashboard: tail `events.ndjson`, show success-rate and p50/p95 durations.
- Agentic coordinator mode (optional): drive an LLM loop in `LlamaMediator` to decide when to call Claude, which tools to allow, and when to stop. Keep current orchestrator path as the default for reliability.

### Add-on task (high utility): Per-task working directory selection - DONE 
- Goal: Spawn Claude in the correct project folder per task/message.
- Scope:
  - Support `cwd` in task YAML frontmatter and detect patterns like "in C:\path\to\project" or "in /path/project" in free-text descriptions when creating tasks.
  - Pass `cwd` through `Task.metadata` and have `ClaudeBridge` run with that as working directory (fallback: project root).
  - Validate path existence and restrict to an allowlist root for safety (configurable).
- Acceptance:
  - Given a description with a path or a `.task.md` with `cwd: ...`, Claude runs with that `cwd`.
  - Artifacts include the effective working directory.
  - Tests cover Windows and POSIX path forms.

### Notes
- All proposed work keeps changes minimal and localized, building on the existing architecture and tests.
- Telegram remains strictly optional and is off by default unless env is configured.
- **LLAMA Mediation Vision**: LLAMA acts as intelligent mediator between user intent and Claude execution, handling confusion, suggesting alternatives, and preventing false positive failures from interactive prompts.
- **Interactive Prompt Strategy**: Use CLI flags to prevent prompts, LLAMA to mediate when they occur, and detection only as safety net - never fail a task just because of prompt detection.
- Coordinator role today: `TaskOrchestrator` orchestrates parse → Claude → summarize → persist/telemetry. `LlamaMediator` is not a tool-calling agent; it crafts prompts and summaries. If you want LLAMA to act as a tool-calling coordinator, track it under the optional "Agentic coordinator mode".


### Cross-cutting prerequisites and compatibility (for interactive reply enablement)
- **Config reload semantics**: Add `config.reload_from_env()` and a `python main.py doctor` command to verify env and CLI flags; ensure timeout/max_turns can be changed without process restart or document that env must be set before run.
- **Artifact linkage/index**: Create a lightweight index to resolve latest artifact by `task_id`; implement an orchestrator context loader that compacts previous artifact into a prompt-ready summary.
- **Events model**: Add `turn_started`/`turn_finished` with `parent_task_id`, `turn_index` and surface in NDJSON for observability.
- **Schema evolution (backward-compatible)**: Add optional `conversation` and `parent_task_id`/`turn_of` fields; keep validator strict by default with `--ignore-legacy` for older runs.
- **Security/limits**: Enforce `allowed_root` and least-privilege tool set on reply turns; validate constraints (e.g., "don't touch A/B").
- **Performance**: Compact context passed to LLAMA/Claude (avoid dumping whole artifacts); cap sizes using existing config soft caps.
- **Tests**: Add unit tests for config overrides, artifact index/context loader, turn events, schema validation for new fields, and end-to-end reply flow (Telegram optional).