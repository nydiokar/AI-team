# AI-Team Gateway - Project Context

**Last Updated:** 2026-05-29
**Branch:** `main`
**Status:** Backend subprocess execution replaced with inactivity-based streaming model; wall-clock task timeout disabled by default; periodic heartbeats added for long-running tasks

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
- Telegram plain-text buffering now merges split messages into a single queued instruction with a short debounce window.
- Session switching is now available through inline Telegram buttons, not just manual ID copy/paste.
- Session lists, switch/restore/close replies, and session status now surface the last material result before the last prompt so closed or inactive sessions are easier to identify.
- Session task acknowledgements and completion replies now include searchable Telegram refs (`#s_<session_id>` and `#t_<task_id>`) so interleaved session messages can be correlated.
- Session task history now stores recent user messages, result summaries, and changed-file lists for richer future summaries while keeping history bounded.
- Gateway/process lifecycle is now guarded by single-instance takeover logic instead of best-effort manual restarts.

### Phase 6 - Operations and persistence
- Done for the current deployment model.
- PM2 supervision config exists in `ecosystem.config.js`.
- Operator runbook exists in `docs/OPERATIONS_PM2.md`.
- `python main.py health [--json]` exists for local/supervisor health checks.
- Gateway startup now safely replaces an older local gateway process instead of spawning duplicate Telegram pollers.
- Backend child process termination is now managed through shared cross-platform process utilities.

---

## Recent changes (2026-05-29)

### Inactivity-based subprocess execution (replaces wall-clock timeout)

Both `ClaudeCodeBackend._run()` and `CodexBackend._run()` now stream stdout/stderr via reader threads feeding `queue.Queue`, instead of blocking on `communicate()`. The main thread drains stdout with `queue.get(timeout=inactivity_sec)` — if no output arrives for that window, the process is considered hung and killed. If Claude is actively working it keeps streaming NDJSON and the timer keeps resetting, so a legitimate 2-hour task will complete naturally.

Config:
- `GATEWAY_INACTIVITY_TIMEOUT_SEC` — how long stdout silence triggers a kill (default 600s / 10 min)
- `GATEWAY_TASK_TIMEOUT_SEC` — wall-clock absolute limit (default 0 = disabled)
- `GATEWAY_HEARTBEAT_INTERVAL_SEC` — Telegram "still working" ping interval (default 300s / 5 min)

Self-review bugs fixed during implementation:
- Used explicit `killed_for_inactivity` flag instead of ambiguous `returncode == -1` check
- `proc.wait()` TimeoutExpired now caught so a resisting process doesn't crash the call
- Stderr is always fully drained after an inactivity kill so diagnostic output is preserved in artifacts

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
- Continue tuning how much summary detail belongs in compact session pickers versus `/session_status`.

### 4. Legacy code removal decision

- Decide whether external `.task.md` ingestion stays supported long-term.
- If not, delete watcher/bridge-era code and associated tests.
- If yes, keep it clearly labeled as compatibility-only, not part of the main runtime path.

### 5. Deployment hardening

- Pin Claude Code and Codex CLI versions or add startup smoke checks so CLI contract shifts are caught immediately (Codex is currently at v0.115.0).
- Add one real backend smoke path per supported backend.
- Confirm operator-facing failure messages stay backend-specific and actionable.
- Validate the full PM2 lifecycle on both Windows and Linux: start, Telegram traffic, restart, boot persistence, and recovery after crash.

---

## What exists and works

| Component | Location | Notes |
|:----------|:---------|:------|
| Orchestrator | `src/orchestrator.py` | Direct runtime queue, worker pool, retries, artifact persistence |
| Telegram interface | `src/telegram/interface.py` | Session commands and direct runtime submission |
| Session store | `src/core/session_store.py` | File-backed session CRUD and Telegram bindings |
| Path resolver | `src/core/path_resolver.py` | Safe path resolution and suggestions |
| Process utilities | `src/core/process_utils.py` | Cross-platform PID checks, takeover matching, and process-tree termination |
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
- `/session_use <session_id>` or `/session_use` with Telegram picker buttons
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
- Plain Telegram messages are buffered briefly so split multi-part thoughts become one task instead of multiple accidental tasks.

### Operations
- `python main.py health [--json]`
- `pm2 start ecosystem.config.js --only ai-team-gateway --update-env`
- `pm2 restart ai-team-gateway --update-env`
- `pm2 logs ai-team-gateway`
- `pm2 save`
- `pm2 startup`

---

## Recommended next moves

1. Validate PM2-managed operation end-to-end on both Windows and Linux, including reboot persistence.
2. Validate Codex sessions end-to-end with the same rigor already applied to Claude.
3. Polish Telegram replies so the command surface feels intentionally productized rather than debug-oriented.
4. Pin or smoke-test backend CLI versions at startup to catch contract regressions early.
5. Decide whether the compatibility watcher remains a supported feature and prune legacy code accordingly.

---

## Architecture rules

- Session continuity uses native backend resume, not terminal persistence.
- External supervision should own restart behavior; the Python app should remain a single foreground worker.
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
| `src/core/process_utils.py` | Cross-platform process lifecycle helpers |
| `src/core/interfaces.py` | Session/task/backend dataclasses and interfaces |
| `src/backends/claude_code.py` | Claude native session create/resume/one-off |
| `src/backends/codex.py` | Codex native session create/resume/one-off |
| `src/core/file_watcher.py` | Compatibility file watcher |
| `src/bridges/claude_bridge.py` | Legacy compatibility code |
| `main.py` | CLI entrypoints and status/doctor display |
| `ecosystem.config.js` | PM2 single-instance supervisor config |
| `docs/OPERATIONS_PM2.md` | PM2 operator runbook |
