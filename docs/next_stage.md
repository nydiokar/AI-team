## AI Task Orchestrator — Next Stage Plan

### Current Status Summary
- ✅ **COMPLETED**: Validation Engine 2.0, Artifact Schema v1, Claude CLI hardening, Telegram interface, Task lifecycle controls, Packaging & CI, Prompt-template agents, Per-task working directory selection
- 🔄 **IN PROGRESS**: Core system stabilization and operational improvements
- 📋 **REMAINING**: 6-7 focused tasks to achieve production-ready v1

### Where we are
- **File-based workflow** is implemented and robust: debounced `FileWatcher`, atomic writes, and processed task archiving.
- **Parsing pipeline** is solid: `TaskParser` + `LlamaMediator` with Ollama support and reliable fallback, prompt capping and JSON-mode where available.
- **Execution bridge** is operational: `ClaudeBridge` with non-interactive mode, structured output, least-privilege `--allowedTools`, retries with backoff, timeout caps.
- **Orchestrator reliability features**: async workers, queue persistence on restart (`logs/state.json`), structured telemetry (`logs/events.ndjson`), artifacts persisted to `results/` and `summaries/`.
- **Validation MVP exists**: similarity/entropy checks and basic structure/coherence validation wired into artifacts and events.
- **CLI and docs**: `python main.py` (plus `status`, `stats`, `clean`, `create-sample`), quick start and architecture docs in place. Unit tests include permission policy and queue persistence.
- **Modular agent system**: `AgentManager` with configurable agents (analyze, bug_fix, code_review, documentation) loaded from `prompts/agents/` with task-specific instructions and validation thresholds.
- **Git-based file detection**: `GitFileDetector` replaces complex session parsing with reliable git status detection, integrated into `ClaudeBridge` for accurate `files_modified` tracking.
- **Enhanced task types**: Extended `TaskType` enum with `DOCUMENTATION` and `BUG_FIX` aliases, integrated with agent system and validation engine.
- **Telegram integration**: Full bot interface with agent commands (`/documentation`, `/code_review`, `/bug_fix`, `/analyze`), task management, and completion notifications.

### Next-stage objective (2–3 weeks)
Strengthen reliability and operability, add essential operational tools, and stabilize artifacts/validation so the system is production-ready and easier to monitor.

### Prioritized next 6-7 tasks


6) **Strengthen test suite (Windows-first)** - PARTIAL
   - Goal: Ensure system stability and catch regressions early.
   - Scope:
     - Golden task tests for agents (deterministic assertions)
     - E2E watcher stability checks
     - Artifact schema validation on CI
     - Windows-specific test coverage
   - Acceptance: All tests pass on Windows; golden tests are deterministic; CI pipeline is green.

### Proposed next 7 production-grade tasks

1) **Metrics and SLOs (operator visibility)** - DONE 
   - Extend `python main.py stats` with per-type success rate, error-class counts, and p50/p95 per phase; emit `logs/metrics.json` snapshots periodically.

2) **Artifact schema v1.1 + validator update** - DONE 
   - Add `security` block (guarded_write, allowlist_root, violations[]); clarify linkage fields; keep `--ignore-legacy` for old artifacts.

3) **Guarded-write staging (optional)** - DONE 
   - Persist diffs to `results/guarded/<task_id>.diff`; add `python main.py apply-guarded <task_id>`; Telegram approval hint when staging occurs.

4) **E2E watcher and restart resilience (Windows-first)** - DONE 
   - Deterministic watcher tests; verify queue persistence on restart; handle Windows file lock edge cases.

5) **Rate limiting and backpressure**
   - Queue caps with `throttled`/`dropped_low_priority` events; Telegram `/task` rate limiting; runtime worker pool via `reload_from_env`.

6) **Secrets and privacy hardening** - DONE 
   - Expand redaction patterns; add `.env.example`; pre-commit secret scan; mask sensitive artifact fields when present.

7) **Operational UX polish**
   - `tail-events` follow mode and colorized output (Windows-safe); `doctor` write probes; `/progress` supports `--since` and clearer status summaries.

8) **Git automation and safe commit workflow** - NEW
   - Goal: Enable safe, automated git operations through Telegram with LLAMA-generated commit messages and intelligent branching.
   - Scope:
     - Telegram commands: `/commit <task_id>` and `/commit-all` for staged changes
     - LLAMA reads git diff and generates contextual commit messages based on task description
     - Safe branching strategy: create feature branch per task (`feature/task-{id}-{description}`)
     - Automatic `git add`, `git commit`, `git push` with safety checks
     - Filter sensitive files (.env, .key, secrets) from commits
     - Optional PR creation for review workflows
   - Safety features:
     - Pre-commit validation of file types and patterns
     - Branch naming conventions and conflict detection
     - Rollback capability if commit fails
     - Integration with existing `files_modified` detection
   - Acceptance: Users can safely commit task changes via Telegram; LLAMA generates meaningful commit messages; sensitive files are protected; branching strategy is clean and conflict-free.

### Future/Optional tasks (post-v1)

8) **LLAMA‑mediated interactive reply flow (turn-based)**
   - Goal: Enable user to continue an already-processed task with follow-up instructions without losing context.
   - Scope:
     - Telegram `/reply <task_id> ...` to post follow-up instructions
     - Persist a `conversation` array in artifacts (turns with role, content, timestamp)
     - LLAMA summarizes prior turns and applies user constraints; Claude runs again headless, using prior context
     - Events: emit `turn_started`/`turn_finished` with linkage to original task
   - Prerequisites: Tasks 1-3 must be completed first (artifact index, context loader, error taxonomy)
   - Status: **DEFERRED** - Building blocks in place, but not required for v1

9) **Real‑time progress monitoring and status**
   - Goal: Provide live visibility into running work: phases, percent complete heuristic, ETA, and recent events.
   - Scope:
     - Extend events with `step_started/step_finished`, include `phase`, `elapsed_ms`, `percent_complete` heuristic
     - Improve `/status` to list active tasks with elapsed/ETA and last 3 events
     - File change feed: show recent `files_modified` deltas as they're detected
   - Status: **DEFERRED** - On-demand progress (Task 5) provides sufficient visibility for v1

### Nice-to-have (after v1)
- Lightweight dashboard: tail `events.ndjson`, show success-rate and p50/p95 durations
- Agentic coordinator mode (optional): drive an LLM loop in `LlamaMediator` to decide when to call Claude, which tools to allow, and when to stop
- Optional guarded-write mode: stage code edits as diffs in results and require manual apply

### Notes
- All proposed work keeps changes minimal and localized, building on the existing architecture and tests.
- Telegram remains strictly optional and is off by default unless env is configured.
- **LLAMA Mediation Vision**: LLAMA acts as intelligent mediator between user intent and Claude execution, handling confusion, suggesting alternatives, and preventing false positive failures from interactive prompts.
- **Interactive Prompt Strategy**: Use CLI flags to prevent prompts, LLAMA to mediate when they occur, and detection only as safety net - never fail a task just because of prompt detection.
- Coordinator role today: `TaskOrchestrator` orchestrates parse → Claude → summarize → persist/telemetry. `LlamaMediator` is not a tool-calling agent; it crafts prompts and summaries.

### Success criteria for v1
- System is production-ready with comprehensive error handling and recovery
- All core functionality is thoroughly tested and stable on Windows
- Operational tools provide clear visibility into system health and progress
- Artifacts are well-structured and machine-readable for external tooling
- Security boundaries are enforced and monitored
- Documentation is complete and accurate
- Git workflow is automated and safe with LLAMA-generated commit messages and intelligent branching