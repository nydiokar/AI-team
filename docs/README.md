# AI-Team Gateway

A gateway for local coding agents, controlled from its own Web UI (and, as a
secondary surface, Telegram).

Current intended product:
- open a persistent session from the Web UI (or Telegram)
- continue that session through native Claude Code or Codex resume
- keep state file-backed and inspectable
- constrain execution by explicit workspace scope

This repository is no longer positioned as a generic AI task orchestrator or a local prompt-agent framework.

## What Is Active

- Web UI session control (`web/` — React, served by the gateway itself)
- Telegram session control (secondary surface, same backend)
- native backend resume for Claude Code and Codex
- file-backed session state
- per-session summaries and event logs
- one-off task fallback
- path validation and path suggestions for session creation
- git helper commands

## What Is Not The Main Runtime Path

- LLAMA prompt engineering
- local agent-template orchestration
- modular local prompt-agents

Those components may remain in the repo as dormant future-facing code, but they are not the current product path.

## Architecture

```text
Web UI / Telegram -> gateway session -> Claude Code / Codex native session
         -> state/sessions/*.json
         -> state/summaries/*.md
         -> logs/session_events/*.log
         -> results/*.json
```

See [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) for the full process/HTTP map.

## Key Commands

### Web UI

Open `http://<gateway-host>:9003/` (Tailscale-bound; see `docs/frontend/`).

### Telegram (secondary surface)

- `/session_new <backend> <path>`
- `/session_list`
- `/session_use <session_id>`
- `/session_status [session_id]`
- `/session_dirs [path]`
- `/session_cancel [session_id]`
- `/session_close [session_id]`
- `/run <instruction>`
- `/say <instruction>`
- `/task <instruction>`
- `/progress <task_id>`
- `/cancel <task_id>`
- `/git_status`
- `/commit <task_id> [--no-branch] [--push]`
- `/commit_all <task_id> [--no-branch] [--push]`

### CLI

- `python main.py`
- `python main.py status`
- `python main.py doctor`
- `python main.py health`
- `python main.py tail-events`
- `python main.py stats`
- `python main.py telemetry-reconcile [--turn-id ID] [--since HOURS]`
- `python main.py telemetry-cleanup [--event-days N] [--summary-days N]`
- `pm2 start ecosystem.config.js --only ai-team-gateway --update-env`
- `pm2 restart ai-team-gateway --update-env`

## Configuration

The most important production settings are:

```env
GATEWAY_TELEGRAM_BOT_TOKEN=...
GATEWAY_TELEGRAM_ALLOWED_USERS=123456789

CLAUDE_BASE_CWD=C:\Users\you\Projects
CLAUDE_ALLOWED_ROOT=C:\Users\you\Projects
CLAUDE_SKIP_PERMISSIONS=false
CLAUDE_TIMEOUT_SEC=300
CLAUDE_MAX_TURNS=0
```

`CLAUDE_BASE_CWD` and `CLAUDE_ALLOWED_ROOT` should usually point to the parent workspace that contains every repo the gateway is allowed to touch. Do not set them so narrowly that the gateway cannot access intended repos, including itself if self-editing is expected.

Turn observability is enabled by default. Its primary settings are:

```env
TELEMETRY_ENABLED=true
TELEMETRY_DETAILED_EVENTS=true
TELEMETRY_UPLOAD_BATCH_SIZE=50
TELEMETRY_UPLOAD_INTERVAL_MS=1000
TELEMETRY_UPLOAD_MAX_BYTES=524288
TELEMETRY_SPOOL_MAX_BYTES=268435456
TELEMETRY_EVENT_RETENTION_DAYS=30
TELEMETRY_SUMMARY_RETENTION_DAYS=180
TELEMETRY_TASK_SERVER_URL=
```

Durable accounting lives in `state/mesh.db`; `logs/events.ndjson` remains the
operational event tail. Failed remote uploads spool under
`logs/telemetry_spool/`.

## Current Production Status

The session-first architecture is implemented.

The main production gate that still matters is a live end-to-end validation:
1. create a session from the Web UI (or Telegram)
2. send a first message
3. verify `backend_session_id` is stored
4. send a second message
5. verify native backend resume continues the same conversation

## Canonical Internal Docs

**Source of truth (read these first):**
- [.ai/CONTEXT.md](../.ai/CONTEXT.md) — current priorities, wiring, constraints, shipped ledger
- [.ai/dispatch/DISPATCH_LOG.md](../.ai/dispatch/DISPATCH_LOG.md) — state of every dispatched job
- [.ai/context/production_vision.md](../.ai/context/production_vision.md) — strategic intent + anti-goals

**Everything else in `docs/`:**
- [OVERVIEW.md](OVERVIEW.md) — newcomer front door, routes by topic
- [INDEX.md](INDEX.md) — full categorized catalog of every doc in `docs/`, with current/superseded/archived status
