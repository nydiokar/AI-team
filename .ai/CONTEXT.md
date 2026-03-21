# AI-Team Gateway — Project Context

**Last Updated:** 2026-03-22  **Branch:** `main`  **Status:** Session layer complete and verified running

---

## What this project is

A **Telegram-controlled remote gateway** for local coding agents (Claude Code, Codex).
Not a general agent framework. Not an autonomous system.

Target: open a session from Telegram → work continues on a local machine → native agent resume on each follow-up → state is file-backed and inspectable.

---

## Current State (verified running 2026-03-22)

All components confirmed green on startup:
- Claude Code CLI: **OK**
- LLAMA/Ollama (llama3.2:latest): **OK**
- File Watcher: **OK**
- Telegram Bot: **OK** (initialized and polling)
- 3 workers active

### What exists and works

| Component | Location | Notes |
|:----------|:---------|:------|
| Task orchestrator | `src/orchestrator.py` | Queue, workers, retry/backoff, cancellation, event log |
| Claude Code bridge | `src/bridges/claude_bridge.py` | Headless subprocess, cwd routing, git diff detection |
| LLAMA mediator | `src/bridges/llama_mediator.py` | Prompt shaping/summarization, skipped for session tasks |
| File watcher | `src/core/file_watcher.py` | Watches `tasks/`, debounces, archives completed tasks |
| Task parser | `src/core/task_parser.py` | Parses `.task.md` frontmatter + body |
| Telegram interface | `src/telegram/interface.py` | `/task`, `/status`, `/cancel`, `/progress`, session commands, git commands |
| Session store | `src/core/session_store.py` | File-backed CRUD for sessions + Telegram bindings |
| Claude Code backend | `src/backends/claude_code.py` | Sync subprocess, `--resume` for continuations, parses `session_id` from output |
| Codex backend | `src/backends/codex.py` | Equivalent resume flow for Codex CLI |
| Validation engine | `src/validation/engine.py` | LLAMA output + result validation |
| Git automation | `src/core/git_automation.py` | Safe commit, branch, push from Telegram |
| Event log | `logs/events.ndjson` | NDJSON, rotated at 1MB |
| Session event log | `logs/session_events/<session_id>.log` | Per-session turn log |
| Session summaries | `state/summaries/<session_id>.md` | Compact human-readable summary, updated each turn |
| Session state | `state/sessions/<session_id>.json` | Full session record |
| Telegram bindings | `state/telegram/active_bindings.json` | chat_id → session_id mapping |
| Artifact index | `results/index.json` | task_id → latest artifact path |
| State persistence | `logs/state.json` | Pending files survive restart |

### What the system can do today

- Create a session from Telegram (`/session_new claude <path>`) and bind it to the chat
- Route plain messages to the active session — or create a standalone task if no session active
- Resume native Claude Code session via `--resume <session_id>` on each follow-up
- Store and update session record, compact summary, and per-session event log after every turn
- List, switch, inspect, and close sessions (`/session_list`, `/session_use`, `/session_status`, `/session_close`)
- Run standalone tasks via `/task` or plain message when no session is active
- Notify Telegram on completion or failure
- Cancel a running task; show per-task event progress (`/cancel`, `/progress`)
- Commit results to git from Telegram (`/commit`, `/commit_all`, `/git_status`)
- Rate-limit requests per user; enforce user allowlist; scope cwd to allowed root

### Known minor issues (non-blocking)

- Console summary in `main.py` always prints `Telegram Bot: [--] Not configured` — display bug, logs confirm bot is running. Low priority.
- `create_task_from_expanded` still has a dead `template_id` field in frontmatter — inert, not called from anywhere active.

---

## Task List (all complete)

### Phase 1a — Strip agent/prompt layer

| # | Status | Task |
|:-:|:------:|:-----|
| 1 | [x] | Remove agent commands (`/bug_fix`, `/code_review`, `/analyze`, `/documentation`) from Telegram |
| 2 | [x] | Strip `_build_prompt()` to user message + file hints only |
| 3 | [x] | Collapse tool allowlist to single safe default; remove TaskType-based branching |
| 4 | [x] | Remove `DOCUMENTATION`, `BUG_FIX` from `TaskType`; update help text |

### Phase 1b — Session model

| # | Status | Task |
|:-:|:------:|:-----|
| 5 | [x] | `Session` dataclass + `SessionStatus` enum |
| 6 | [x] | `SessionStore` — file-backed CRUD, Telegram binding, closed-session guard |
| 7 | [x] | Session commands in Telegram (`/session_new`, `/session_list`, `/session_use`, `/session_status`, `/session_close`) |
| 8 | [x] | Plain messages route to active session; fallback to standalone task |

### Phase 2 — Backend abstraction + native resume

| # | Status | Task |
|:-:|:------:|:-----|
| 9 | [x] | `CodingBackend` protocol + `ExecutionResult` dataclass |
| 10 | [x] | `ClaudeCodeBackend` — sync subprocess, `--resume` for continuations |
| 11 | [x] | `CodexBackend` — equivalent |
| 12 | [x] | `backend_session_id` stored after first turn, used on resume |

### Phase 3 — Session execution flow

| # | Status | Task |
|:-:|:------:|:-----|
| 13 | [x] | Session tasks use `resume_session`/`create_session` instead of raw bridge |
| 14 | [x] | Session record + compact summary updated after every turn |
| 15 | [x] | Artifacts written; session record holds artifact path |

### Phase 4 — Observability

| # | Status | Task |
|:-:|:------:|:-----|
| 16 | [x] | `/session_status` shows backend, cwd, last run, last result, artifact path |
| 17 | [x] | Per-session event log at `logs/session_events/<session_id>.log` |
| 18 | [x] | Compact session summary at `state/summaries/<session_id>.md` |

---

## What's next

The implementation is complete and running. The next validation step is an end-to-end live test:

1. `/session_new claude <repo_path>` from Telegram
2. Send a plain message — verify it creates a task with `session_id` in frontmatter
3. Check that Claude returns a `session_id` in output and it gets stored in `state/sessions/<id>.json`
4. Send a second message — verify it calls `--resume <session_id>` and Claude continues the conversation

After that passes, the remaining minor items are:
- Fix the `main.py` console Telegram status display bug
- Decide whether to keep `create_task_from_expanded` or remove it (dead code from old agent pattern)

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | Main orchestrator — task queue, workers, session routing, events |
| `src/telegram/interface.py` | All Telegram handlers including session commands |
| `src/core/session_store.py` | File-backed session CRUD and Telegram binding |
| `src/core/interfaces.py` | All dataclasses and ABCs (Task, Session, CodingBackend, etc.) |
| `src/backends/claude_code.py` | Claude Code CLI — first turn + resume |
| `src/backends/codex.py` | Codex CLI — first turn + resume |
| `src/bridges/claude_bridge.py` | Legacy stateless Claude execution (non-session tasks) |
| `src/bridges/llama_mediator.py` | LLAMA prompt shaping / summarization |
| `config/settings.py` | Runtime config (env vars, paths, rate limits) |
| `state/sessions/` | One JSON file per session |
| `state/telegram/active_bindings.json` | chat_id → session_id |
| `state/summaries/` | Per-session compact summaries |
| `logs/session_events/` | Per-session turn logs |
| `.ai/context/production_vision.md` | Full architectural intent and implementation rules |

---

## Architecture rules (from production_vision.md)

- Session continuity via **native backend resume** (`claude --resume <id>`), not terminal persistence
- State is **file-backed** — no DB required
- LLAMA stays narrow: prompt shaping for standalone tasks only, not session turns
- No uncontrolled autonomous behavior; bounded tools, bounded cwd, explicit session ownership
