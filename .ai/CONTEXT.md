# AI-Team Gateway - Project Context

**Last Updated:** 2026-03-29
**Branch:** `main`
**Status:** Codex backend is validated and corrected against the real CLI contract (v0.115.0); startup no longer blocks on SentenceTransformer eager load

---

## What this project is

A Telegram-controlled remote gateway for local coding agents such as Claude Code and Codex.

It is not a general autonomous agent framework.

Primary runtime flow:
- open a session from Telegram
- route follow-up messages to the active session
- resume the native backend session on demand
- keep gateway state file-backed, inspectable, and bounded

Compatibility flow still supported:
- drop `.task.md` files into `tasks/`
- let the external watcher ingest them
- keep artifacts/summaries for auditability

Canonical intent lives in `.ai/context/production_vision.md`.

---

## Vision vs Current State

### Phase 1 - Session foundation
- Done.
- Session model exists in `state/sessions/<session_id>.json`.
- File-backed session CRUD exists in `src/core/session_store.py`.
- Active Telegram bindings exist in `state/telegram/active_bindings.json`.

### Phase 2 - Backend session support
- Done.
- Claude backend uses native `--resume` in `src/backends/claude_code.py`.
- Codex backend rewritten against real CLI contract (`codex exec` / `codex exec resume`) in `src/backends/codex.py`.
- Session ID is the `thread_id` from the `thread.started` NDJSON event.
- `backend_session_id` is stored in the session record and used for resume.

### Phase 3 - Session execution flow
- Done for the live runtime path.
- Telegram plain text and `/task` queue runtime tasks directly.
- Active sessions no longer create `.task.md` wrappers to execute a turn.
- One-off non-session execution also uses the native backend path directly.
- Artifacts are still written for every completed task/session turn.

### Phase 4 - Observability
- Mostly done.
- Per-session event logs exist in `logs/session_events/<session_id>.log`.
- System-wide event log exists in `logs/events.ndjson`.
- Session summaries exist in `state/summaries/<session_id>.md`.
- Result artifacts exist in `results/<task_id>.json`.
- Path resolver exists in `src/core/path_resolver.py`.

### Phase 5 - Compatibility and cleanup
- Mostly done.
- External `.task.md` watcher ingestion still exists as a compatibility lane.
- `src/bridges/claude_bridge.py` still exists in the repo as legacy code, but it is no longer on the primary Telegram/runtime execution path.
- Old bridge-era tests still need pruning or isolation.
- Telegram no longer advertises task-runner-only commands, and the registered public command set is now session-first.
- Claude dead-session recovery now recreates the backend conversation inside the same gateway session instead of poisoning session state.
- Git commands now target the active session repo by default instead of requiring opaque task IDs.

---

## Production gaps that remain

### 1. Real Codex end-to-end validation

Codex backend is now correct at the code level. Still requires a live two-turn Telegram test:
1. `/session_new codex <repo_path>`
2. send first message — verify `backend_session_id` (thread_id) is captured in `state/sessions/<id>.json`
3. send second message — verify Codex resumes the same thread

### 2. Workspace scope confirmation

`CLAUDE_BASE_CWD` and `CLAUDE_ALLOWED_ROOT` must be explicitly confirmed in `.env`.

### 3. Telegram command polish

- Add prettier, more compact Telegram replies for session status, git status, and errors.
- Decide whether `/commit_all` should remain public.
- Decide whether to keep compatibility-only handler methods for `/run`, `/say`, `/progress`, and `/cancel` in code at all now that they are no longer registered.

### 4. Legacy code removal decision

- Decide whether external `.task.md` ingestion stays supported long-term.
- If not, delete watcher/bridge-era code and associated tests.
- If yes, keep it clearly labeled as compatibility-only, not part of the main runtime path.

### 5. Deployment hardening

- Pin Claude Code and Codex CLI versions or add startup smoke checks so CLI contract shifts are caught immediately (Codex is currently at v0.115.0).
- Add one real backend smoke path per supported backend.
- Confirm operator-facing failure messages stay backend-specific and actionable.

---

## What exists and works

| Component | Location | Notes |
|:----------|:---------|:------|
| Orchestrator | `src/orchestrator.py` | Direct runtime queue, worker pool, retries, artifact persistence |
| Telegram interface | `src/telegram/interface.py` | Session commands and direct runtime submission |
| Session store | `src/core/session_store.py` | File-backed session CRUD and Telegram bindings |
| Path resolver | `src/core/path_resolver.py` | Safe path resolution and suggestions |
| Claude backend | `src/backends/claude_code.py` | Native session create/resume and one-off execution |
| Codex backend | `src/backends/codex.py` | Native session resume and one-off execution |
| Llama helper | `src/bridges/llama_mediator.py` | Optional helper only, not a primary runtime dependency |
| Compatibility watcher | `src/core/file_watcher.py` | Optional external `.task.md` ingestion lane |
| Legacy Claude bridge | `src/bridges/claude_bridge.py` | No longer on the primary runtime path |

---

## Command surface

### Session commands
- `/session_new <backend> <path>`
- `/session_list [all]`
- `/session_use <session_id>`
- `/session_status [session_id]`
- `/session_dirs [path]`
- `/session_cancel [session_id]`
- `/session_close [session_id]`

### Execution commands
- plain text -> continue active session if one exists
- `/task <instruction>` -> one-off task only
- `/status`

Runtime note:
- Telegram/runtime commands now queue tasks directly in memory.
- `.task.md` files are compatibility input, not the primary runtime entrypoint.

---

## Recommended next moves

1. Validate Codex sessions end-to-end with the same rigor already applied to Claude.
2. Polish Telegram replies so the command surface feels intentionally productized rather than debug-oriented.
3. Pin or smoke-test backend CLI versions at startup to catch contract regressions early.
4. Decide whether the compatibility watcher remains a supported feature and prune legacy code accordingly.

---

## Architecture rules

- Session continuity uses native backend resume, not terminal persistence.
- State stays file-backed.
- Artifacts remain mandatory for audit/compliance purposes.
- Ollama remains optional and helper-only.
- Session ownership and path scope must stay explicit.
- No uncontrolled autonomous behavior.

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | Main runtime orchestration and compatibility ingestion |
| `src/telegram/interface.py` | Telegram command surface |
| `src/core/path_resolver.py` | Shared path validation and suggestions |
| `src/core/session_store.py` | File-backed session store |
| `src/core/interfaces.py` | Session/task/backend dataclasses and interfaces |
| `src/backends/claude_code.py` | Claude native session create/resume/one-off |
| `src/backends/codex.py` | Codex native session create/resume/one-off |
| `src/core/file_watcher.py` | Compatibility file watcher |
| `src/bridges/claude_bridge.py` | Legacy compatibility code |
| `main.py` | CLI entrypoints and status/doctor display |
