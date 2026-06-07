# OpenCode Server Backend — Implementation Context

Status as of 2026-06-04. Reference for continuing work in future sessions.

---

## What is implemented

`src/backends/opencode.py` contains two backends:

### `OpenCodeBackend` (CLI, backend name: `"opencode"`)
Spawns `opencode run` as a subprocess per turn. Session continuity via `--session <id>`.
- Inactivity timeout (no stdout for N seconds → kill)
- Auto-commit after each run to keep the working tree clean for the next turn
- Session ID recovery fallback via `opencode session list`

### `OpenCodeServerBackend` (HTTP, backend name: `"opencode-server"`)
Manages one persistent `opencode serve` process and talks to it over HTTP.
No subprocess per turn, no cold start, no stdout parsing.

**Lifecycle:**
- `_ensure_server()` starts `opencode serve --hostname 127.0.0.1 --port <N>` on first use
- Port: tries `config.opencode.server_port` (default 4096), falls back to any free port
- Readiness check: polls `GET /session` until 200 (up to 15s)
- stderr captured to `PIPE` — included in error messages if startup fails
- Server proc assigned to `self._proc` immediately after `Popen` (no orphan leaks)
- Kill happens inside `self._lock` in `terminate_active_processes` (no race with restart)

**Per-turn flow:**
1. `create_session` → `POST /session` → `PATCH /session/{id}` (model) → `POST /session/{id}/message`
2. `resume_session` → verify session exists via `GET /session/{id}` → `POST /session/{id}/message`
   - If session missing (server restarted): auto-recreate and replay transparently
   - If `backend_session_id` empty: fall back to `create_session` (no dead end)
3. `cancel` → `POST /session/{id}/abort`
4. `close` → `DELETE /session/{id}`

**HTTP timeout:** uses `config.opencode.timeout_seconds` (default 1800s / 30min) — the wall-clock budget, not the inactivity cap. The server holds the connection open for the entire generation.

**Output extraction:** scans `parts[]` array from message response, collects `type=text` chunks (skips `reasoning`, `step-start`, `step-finish`).

**Finish reasons:**
- `"stop"` / `"tool-calls"` → success
- `"unknown"` → truncated, success=True with appended truncation note
- `""` (no step-finish part) → failure with error message

**Connection errors:** `ConnectionRefusedError` / `OSError` in `_http` clears `_proc` / `_base_url` so the next call to `_ensure_server` restarts cleanly.

**Auto-commit:** reuses `OpenCodeBackend._auto_commit` — stages all changes and commits so the working tree stays clean for the next run (opencode enforces a clean tree).

**Orchestrator wiring:** `src/orchestrator.py` registers `"opencode-server"` backend. `backend_session_id` is now persisted on both success AND failure (previously only on success — caused silent session loss after a failed first turn post-resurrection).

---

## Known bugs fixed in this session

1. `resume_session` dead-end on server restart → now auto-recreates session
2. CLI `resume_session` dead-end on empty session ID → now falls back to `create_session`
3. Orchestrator only persisted `backend_session_id` on success → fixed to always persist
4. `finish="unknown"` treated as failure → now success=True (partial answer shown)
5. HTTP timeout used `inactivity_timeout_sec` (600s) → fixed to `timeout_seconds` (1800s)
6. `_ensure_server` timeout branch discarded stderr → fixed to read and include it
7. Orphan server proc on startup exception → proc registered before readiness loop
8. `terminate_active_processes` race with `_ensure_server` → kill now inside lock
9. `_http` dead import `http.client` → removed
10. `run_oneoff` passed `"directory":""` on empty cwd → guarded
11. `run_oneoff` deleted session on failure → only deletes on success
12. `_find_free_port` double-bind bug → uses two separate socket contexts

---

## What is NOT yet implemented (good next steps)

### High value / low effort

**1. Model override via per-message metadata**
Currently model is set via `PATCH /session/{id}` at session creation time only.
The opencode server API supports `PATCH` at any point — model could be changed per-turn
if `task.metadata` contains `opencode_model`. Already read from `task_history[-1]` but
only applied on `create_session`, not on `resume_session`.

**2. Agent switching mid-session**
Same pattern — `PATCH /session/{id}` with `{"agent": "new-agent"}` is valid.
Could let the user say "switch to plan mode" and have the gateway patch the session.

**3. `opencode export <sessionID>` for session backup**
The CLI has `opencode export <id>` → JSON. If the server is about to restart (e.g.
on gateway shutdown), export all active sessions and store them. On restart, import
them back with `opencode import <file>` so session context survives.
This would make the server backend fully resilient to planned restarts, not just
transparent to the user — actual conversation history preserved.

**4. Token/cost reporting to Telegram**
The message response `info.tokens` and `info.cost` are already captured in
`parsed_output`. Wire them into the Telegram notification so the user sees:
"✓ Done in 12.3s | 8,450 tokens | $0.0012"

**5. `GET /session/{id}/message` for history replay**
After server restart + session recreation, the new session has no history.
Before sending the user's message, we could replay the last N messages from
`session.task_history` by fetching them from the old session (if server still up)
or from stored artifacts (results/*.json raw_stdout fields contain the prior turns).
This would give the model real context instead of a blank slate after restart.

**6. Session `summary` field**
The session list response includes a `summary` object: `{additions, deletions, files}`.
This is a free per-session running diff count. Could surface it in `/status` output.

### Medium effort

**7. SSE event stream for real-time Telegram progress**
`GET /event` is a server-sent events stream that fires `message.part.updated` events
as the model generates text. Could be used to send streaming "still typing..." Telegram
updates, or to detect `session.status = busy/idle` for heartbeat logic.
Implementation: subscribe in a background thread during `_send_message`, accumulate
partial text, send Telegram edit-messages every 2-3 seconds.
Challenge: the current `_send_message` is synchronous blocking — would need a thread
split between the blocking POST and the SSE consumer.

**8. Permission handling**
The session objects from the probe show `permission` arrays:
```json
[{"permission":"question","pattern":"*","action":"deny"},
 {"permission":"plan_enter","pattern":"*","action":"deny"},
 {"permission":"plan_exit","pattern":"*","action":"deny"}]
```
These are already being set (deny questions, deny plan mode) which is correct for
headless operation. But if a future agent needs plan mode, this would need to be
exposed in config. Currently hardcoded by opencode's agent config, not by us.
No action needed unless an agent starts requiring plan confirmation.

**9. `opencode github pr <number>` integration**
The CLI has `opencode pr <number>` which fetches a GitHub PR branch and starts a
session on it. Could be wired as a special Telegram command: `/review PR-123`.
Would spawn a session against the PR branch automatically.

**10. Multi-repo server**
Currently one server serves all sessions but sessions are created with `directory`
pointing to the specific repo. This already works. However, if two repos need
different opencode configs (different models, agents, permissions), they'd need
separate server instances. The current code supports only one server per backend
instance. Could be extended to a `{repo_path: ServerInstance}` registry if needed.

### Lower priority / research needed

**11. `opencode acp` (Agent Client Protocol)**
`opencode acp` starts an ACP server (separate from `opencode serve`). The ACP
protocol is for agent-to-agent communication. Could be relevant for the mesh
architecture (AGENT_MESH_SPEC.md) where worker nodes expose ACP endpoints.
Requires understanding the ACP spec — not documented publicly yet.

**12. mDNS service discovery**
`opencode serve --mdns` broadcasts the server on the local network via mDNS.
Relevant if the gateway runs on a different machine than the opencode server
(mesh node scenario). The `--mdns-domain` flag sets a custom domain.

---

## API reference (verified from live probing)

```
POST   /session                         create session
                                        body: {title, agent, directory?}
GET    /session                         list all sessions
GET    /session/{id}                    get session info
PATCH  /session/{id}                    update session
                                        body: {model?: {providerID, modelID}, agent?}
DELETE /session/{id}                    delete session
GET    /session/{id}/message            list messages
POST   /session/{id}/message            send message (blocking — returns when done)
                                        body: {parts: [{type:"text", text:str}]}
                                        returns: {info: {tokens, cost, finish, ...}, parts: [...]}
POST   /session/{id}/abort              abort running generation (returns: true)
GET    /event                           SSE stream (all events for all sessions)
```

**Message response parts types:**
- `step-start` — generation started (has `snapshot` field = git tree hash)
- `reasoning` — model reasoning text (skip in output)
- `text` — assistant output text (collect these)
- `step-finish` — generation complete, has `reason` field
- `error` — error event, has `message` field

**SSE event types (from /event):**
- `server.connected`
- `session.updated`
- `session.diff` — git diff summary `{diff: [...]}`
- `session.status` — `{status: {type: "busy"|"idle"}}`
- `session.idle` — session returned to idle
- `message.updated`
- `message.part.updated` — streaming partial text
