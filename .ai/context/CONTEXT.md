# AI-Team Gateway — Project Context

**Last Updated:** 2026-03-21  **Branch:** `main`  **Status:** Task runner operational — session layer not yet built

---

## What this project is

A **Telegram-controlled remote gateway** for local coding agents (Claude Code, Codex).
Not a general agent framework. Not an autonomous system.

Target: open a session from Telegram → work continues on a local machine → native agent resume on each follow-up → state is file-backed and inspectable.

---

## Current State (verified against code)

### What exists and works

| Component | Location | Notes |
|:----------|:---------|:------|
| Task orchestrator | `src/orchestrator.py` | Queue, workers, retry/backoff, cancellation, event log |
| Claude Code bridge | `src/bridges/claude_bridge.py` | Headless subprocess, cwd routing, git diff detection |
| LLAMA mediator | `src/bridges/llama_mediator.py` | Prompt shaping, summarization, fallback if unavailable |
| File watcher | `src/core/file_watcher.py` | Watches `tasks/`, debounces, archives completed tasks |
| Task parser | `src/core/task_parser.py` | Parses `.task.md` frontmatter + body |
| Telegram interface | `src/telegram/interface.py` | `/task`, `/status`, `/cancel`, `/progress`, agent commands, git commands |
| Validation engine | `src/validation/engine.py` | LLAMA output + result validation |
| Git automation | `src/core/git_automation.py` | Safe commit, branch, push from Telegram |
| Event log | `logs/events.ndjson` | NDJSON, rotated at 1MB |
| Artifact index | `results/index.json` | task_id → latest artifact path |
| State persistence | `logs/state.json` | Pending files survive restart |

### What the system can do today

- Receive a task via Telegram (`/task`, plain message, or agent commands like `/bug_fix`)
- Write a `.task.md` file, pick it up via file watcher, run Claude Code, collect result
- Notify Telegram on completion or failure
- Cancel a running task; show per-task event progress
- Commit results to git from Telegram (`/commit`, `/git-status`)
- Rate-limit requests per user; enforce user allowlist; scope cwd to allowed root

### What is missing (the gap vs. production vision)

The system is **task-centric**, not **session-centric**. Every Telegram message creates a new independent task with no continuity.

Missing:
- No `Session` model — no persistent session record mapping Telegram conversations to backend sessions
- No native Claude Code `--resume` / `--continue` usage — each invocation is stateless
- No Telegram session routing commands (`/session_new`, `/session_list`, `/session_use`, etc.)
- No backend abstraction (`CodingBackend` protocol with `ClaudeCodeBackend` / `CodexBackend`)
- No compact session summaries stored per session (`state/summaries/<session_id>.md`)
- No session lifecycle management (idle / busy / awaiting_input / error / closed)
- No `state/sessions/` or `state/telegram/active_bindings.json` directory structure

---

## What needs to be built (priority order from production_vision.md)

### Phase 1 — Clean interface + Session foundation

**1a. Strip the broken agent/prompt layer (do this first)**

The current Telegram interface has agent-type commands (`/bug_fix`, `/code_review`, `/analyze`, `/documentation`) and a prompt builder that injects role instructions and step-by-step rules on top of Claude Code / Codex. This is wrong — the agents are capable enough to decide on their own; pre-classifying and role-injecting adds noise and unnecessary constraints.

- Remove agent commands from `src/telegram/interface.py`: `/bug_fix`, `/code_review`, `/analyze`, `/documentation`, and the shared `_handle_agent_command` dispatcher
- Strip `_build_prompt()` in `src/bridges/claude_bridge.py` down to minimal context only: user message + cwd/repo, nothing else
- Remove `_get_allowed_tools_for_task()` per-type overrides or collapse to a single safe default set — do not infer tool permissions from task type
- Remove `TaskType`-based branching in the orchestrator/bridge that only existed to serve the agent command pattern
- Plain messages and `/run <instruction>` become the only way to send work to the agent

**1b. Session model**
- `Session` dataclass: `session_id`, `backend`, `backend_session_id`, `machine_id`, `repo_path`, `status`, timestamps, last task/artifact/summary, optional Telegram binding
- Persist sessions to `state/sessions/<session_id>.json`
- Active Telegram binding: `state/telegram/active_bindings.json` (chat_id → session_id)
- Telegram commands: `/session_new <backend> <path>`, `/session_list`, `/session_use <id>`, `/session_status`, `/session_close`

### Phase 2 — Backend session support
- `CodingBackend` protocol: `create_session`, `resume_session`, `run_oneoff`, `cancel`, `summarize`, `close`
- `ClaudeCodeBackend`: invoke `claude --resume <session_id>` for follow-up turns
- `CodexBackend`: equivalent Codex resume flow
- Store native backend session IDs in the session record after each run

### Phase 3 — Session execution flow
- Route plain Telegram messages to the active session (not always a new task)
- Execute backend resume on each follow-up message
- Update session record + compact summary after every turn
- Attach result artifacts under `results/sessions/<session_id>/`

### Phase 4 — Observability
- `/session_status` shows backend, cwd, last run time, last error, files changed, artifact path
- Per-session event log: `logs/session_events/<session_id>.log`
- Compact session summary file: `state/summaries/<session_id>.md`

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | Main orchestrator — task queue, workers, events |
| `src/telegram/interface.py` | All Telegram handlers |
| `src/bridges/claude_bridge.py` | Claude Code subprocess execution |
| `src/bridges/llama_mediator.py` | Local LLAMA for prompt shaping / summarization |
| `src/core/interfaces.py` | Core dataclasses and ABCs (Task, TaskResult, etc.) |
| `src/core/git_automation.py` | Safe git commit/branch/push |
| `config.py` | Runtime config (env vars, paths, rate limits) |
| `tasks/` | Drop `.task.md` here to trigger processing |
| `results/` | JSON artifacts per completed task |
| `logs/events.ndjson` | Structured event stream |
| `.ai/context/production_vision.md` | Full architectural intent and implementation rules |

---

## Architecture rules (from production_vision.md)

- Session continuity via **native backend resume** (e.g. `claude --resume`), not terminal persistence
- State is **file-backed** — no DB required for the session layer
- LLAMA stays narrow: prompt shaping, routing, compact summarization — not the agent brain
- No uncontrolled autonomous behavior; bounded tools, bounded cwd, explicit session ownership
