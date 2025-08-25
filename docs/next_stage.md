## AI Task Orchestrator — Next Stage Plan

### Current Status Summary
- ✅ **COMPLETED**: Validation Engine 2.0, Artifact Schema v1, Claude CLI hardening, Telegram interface, Task lifecycle controls, Packaging & CI, Prompt-template agents, Per-task working directory selection
- 🔄 **IN PROGRESS**: Core system stabilization and operational improvements
- 📋 **REMAINING**: 6-7 focused tasks to achieve production-ready v1

### Where we are
- File-based workflow is implemented and robust: debounced `FileWatcher`, atomic writes, and processed task archiving.
- Parsing pipeline is solid: `TaskParser` + `LlamaMediator` with Ollama support and reliable fallback, prompt capping and JSON-mode where available.
- Execution bridge is operational: `ClaudeBridge` with non-interactive mode, structured output, least-privilege `--allowedTools`, retries with backoff, timeout caps.
- Orchestrator reliability features: async workers, queue persistence on restart (`logs/state.json`), structured telemetry (`logs/events.ndjson`), artifacts persisted to `results/` and `summaries/`.
- Validation MVP exists: similarity/entropy checks and basic structure/coherence validation wired into artifacts and events.
- CLI and docs: `python main.py` (plus `status`, `stats`, `clean`, `create-sample`), quick start and architecture docs in place. Unit tests include permission policy and queue persistence.

### Next-stage objective (2–3 weeks)
Strengthen reliability and operability, add essential operational tools, and stabilize artifacts/validation so the system is production-ready and easier to monitor.

### Prioritized next 6-7 tasks

1) **Implement results index + compact context loader**
   - Goal: Create a lightweight artifact index for efficient task context lookup and provide compact, prompt-ready context summaries.
   - Scope: 
     - Maintain `results/index.json` mapping `task_id -> latest artifact path`
     - Implement `ContextLoader` class that returns summarized, prompt-ready context from latest artifacts
     - Add artifact linkage fields (`parent_task_id`, `turn_of`) for future multi-turn support
   - Acceptance: Index stays current; context loader produces concise summaries under token caps; artifacts include linkage metadata.

2) **Add `doctor` command and env reload pipeline**
   - Goal: Provide system health diagnostics and runtime configuration management.
   - Scope:
     - Implement `python main.py doctor` to validate env vars, Claude availability, working directory access
     - Add `config.reload_from_env()` for runtime tunables (timeouts, max_turns, working directories)
     - Document supported environment variables and their effects
   - Acceptance: Doctor reports clear status; env changes take effect without restart; documentation covers all configurable options.

3) **Extend error taxonomy and retry logic**
   - Goal: Improve error classification and provide actionable error information in artifacts.
   - Scope:
     - Extend transient/fatal classifier with specific interactive and network markers
     - Produce clearer error messages in artifacts with suggested actions
     - Implement smarter retry backoff for different error types
   - Acceptance: Errors are clearly categorized; artifacts contain actionable error information; retry behavior is predictable and appropriate.

4) **Implement guarded-write mode and enforce allowlist boundaries**
   - Goal: Add optional safety controls and improve security monitoring.
   - Scope:
     - Optional "guarded-write mode" that stages edits for review before applying
     - Enhanced allowlist enforcement with telemetry for attempted out-of-root access
     - Logging and alerting for security boundary violations
   - Acceptance: Guarded mode works when enabled; all file operations respect allowlist; security events are logged and surfaced.

5) **Add on-demand progress monitoring**
   - Goal: Provide progress visibility without real-time spam.
   - Scope:
     - Telegram `/progress <task_id>` command showing current status and recent events
     - CLI `python main.py tail-events` for non-blocking event inspection
     - Windows-friendly log tailing with rotation
   - Acceptance: Progress commands work end-to-end; tail-events doesn't block orchestrator; events are properly formatted and accessible.

6) **Strengthen test suite (Windows-first)**
   - Goal: Ensure system stability and catch regressions early.
   - Scope:
     - Golden task tests for agents (deterministic assertions)
     - E2E watcher stability checks
     - Artifact schema validation on CI
     - Windows-specific test coverage
   - Acceptance: All tests pass on Windows; golden tests are deterministic; CI pipeline is green.

7) **Project cleanup and consistency**
   - Goal: Improve code quality and maintainability.
   - Scope:
     - Standardize imports (`from src.core ...` project-wide)
     - Remove sys.path hacks and clean up import resolution
     - Light linting pass for consistency
     - Consolidate any remaining duplicate code
   - Acceptance: Clean import structure; no sys.path modifications; consistent code style; no duplicate functionality.

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