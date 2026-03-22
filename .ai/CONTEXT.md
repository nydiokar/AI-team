# AI-Team Gateway - Project Context

**Last Updated:** 2026-03-22
**Branch:** `main`
**Status:** Session-first gateway implemented; production hardening in progress

---

## What this project is

A Telegram-controlled remote gateway for local coding agents such as Claude Code and Codex.

It is not a general autonomous agent framework.

Target flow:
- open a session from Telegram
- route follow-up messages to the active session
- resume the native backend session on demand
- keep gateway state file-backed, inspectable, and bounded

Canonical intent lives in [.ai/context/production_vision.md](.ai/context/production_vision.md).

---

## Vision vs. Current State

The production_vision.md defined 8 phases. Here is where we stand:

### Phase 1 — Session foundation ✓ DONE
- Session model exists: `state/sessions/<session_id>.json`
- File-backed session CRUD: `src/core/session_store.py`
- Active Telegram session bindings: `state/telegram/active_bindings.json`
- Session CRUD commands in `src/telegram/interface.py`

### Phase 2 — Backend session support ✓ DONE
- Backend interface abstraction in place
- Claude backend with native `--resume`: `src/backends/claude_code.py`
- Codex backend with native resume: `src/backends/codex.py`
- `backend_session_id` stored in session record

### Phase 3 — Session execution flow ✓ DONE (code-level)
- Telegram plain text routes to active session
- `/say`, `/run`, `/task` commands all wired up
- Session state machine: `BUSY`, `AWAITING_INPUT`, `CANCELLED`, `ERROR`
- Results/artifacts attached to sessions
- **GAP: not yet live-validated end-to-end**

### Phase 4 — Observability ✓ MOSTLY DONE
- Per-session event logs: `logs/session_events/<session_id>.log`
- System-wide event log: `logs/events.ndjson`
- Session summaries: `state/summaries/<session_id>.md`
- `/session_status`, `/status` commands in place
- Path resolver with suggestions: `src/core/path_resolver.py`
- **GAP: `CLAUDE_BASE_CWD` / `CLAUDE_ALLOWED_ROOT` not configured**

### Phase 5 — Optional enhancements NOT STARTED
- Live streaming / attached terminal mode
- Web UI
- Machine registry / multi-node awareness
- Better LLAMA-based summarization

---

## Production gaps that remain

These are the only things blocking "call it production".

### 1. Live end-to-end validation (MOST IMPORTANT)

Still required:
1. `/session_new claude <repo_path>`
2. send first message
3. verify `backend_session_id` is captured in `state/sessions/<id>.json`
4. send second message
5. verify backend resumes the existing conversation

This is the primary production gate. Everything else is already in code.

### 2. Workspace scope configuration

`python main.py doctor` currently shows:
- `Base CWD: None`
- `Allowed root: None`

Needs a decision and `.env` entry:
- `CLAUDE_BASE_CWD` — the base working directory
- `CLAUDE_ALLOWED_ROOT` — set to the parent directory covering all intended repos

If left unset, path safety validation has no enforcement boundary.

### 3. Test suite reconciliation

- Several stale tests still target removed legacy paths
- Need focused tests for: session routing, session ownership, path validation, backend behavior
- Remove anything that tests the old agent-template/orchestrator framing

### 4. Remaining Telegram UX cleanup

- Review all Telegram output strings for consistency
- Remove remaining old task-runner wording
- Validate git command responses against real completed tasks

---

## What exists and works

| Component | Location | Notes |
|:----------|:---------|:------|
| Task orchestrator | `src/orchestrator.py` | Queue, workers, retries, cancellation, event log, session updates |
| Telegram interface | `src/telegram/interface.py` | Session commands, one-off task commands, git commands |
| Session store | `src/core/session_store.py` | File-backed CRUD and active Telegram binding |
| Path resolver | `src/core/path_resolver.py` | Safe path resolution and suggestions |
| Claude backend | `src/backends/claude_code.py` | Native `--resume` support |
| Codex backend | `src/backends/codex.py` | Native session resume support |
| Claude bridge | `src/bridges/claude_bridge.py` | Stateless execution for one-off tasks |
| Event log | `logs/events.ndjson` | System-wide NDJSON events |
| Session event log | `logs/session_events/<session_id>.log` | Per-session turn log |
| Session summaries | `state/summaries/<session_id>.md` | Compact readable session summary |
| Session state | `state/sessions/<session_id>.json` | Full session record |
| Telegram bindings | `state/telegram/active_bindings.json` | chat_id -> session_id |

---

## Command surface

### Session commands
- `/session_new <backend> <path>`
- `/session_list`
- `/session_use <session_id>`
- `/session_status [session_id]`
- `/session_dirs [path]`
- `/session_cancel [session_id]`
- `/session_close [session_id]`

### Execution commands
- plain text -> continue active session if one exists
- `/say <instruction>` -> session-only execution
- `/run <instruction>` -> active session if present, otherwise one-off task
- `/task <instruction>` -> one-off task only
- `/progress <task_id>`
- `/cancel <task_id>`
- `/status`

### Git commands
- `/git_status`
- `/commit <task_id> [--no-branch] [--push]`
- `/commit_all <task_id> [--no-branch] [--push]`

---

## Recommended next moves (in order)

1. **Set workspace env vars** — decide `CLAUDE_BASE_CWD` and `CLAUDE_ALLOWED_ROOT` in `.env`, run `doctor` to confirm
2. **Run live E2E test** — create a real Telegram session, send two turns, verify `backend_session_id` and resume
3. **Clean stale tests** — remove/rewrite tests that target removed legacy code
4. **Telegram UX pass** — review and tighten all bot output strings

After those four: the system matches the production_vision.md target.

Phase 5 (live streaming, web UI, multi-node) is explicitly future work.

---

## Architecture rules

- Session continuity uses native backend resume, not terminal persistence
- State stays file-backed
- LLAMA remains narrow and optional (dormant — `src/bridges/llama_mediator.py`)
- Session ownership and path scope must stay explicit
- No uncontrolled autonomous behavior

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | Main orchestrator, task/session execution flow |
| `src/telegram/interface.py` | Telegram command surface |
| `src/core/path_resolver.py` | Shared path validation and suggestions |
| `src/core/session_store.py` | File-backed session store |
| `src/core/interfaces.py` | Session/task/backend dataclasses and enums |
| `src/backends/claude_code.py` | Claude native session create/resume |
| `src/backends/codex.py` | Codex native session create/resume |
| `src/bridges/claude_bridge.py` | Stateless one-off execution |
| `main.py` | CLI entrypoints and status/doctor display |
| `.ai/context/production_vision.md` | Canonical product intent |
