# Telegram Coding Gateway

A Telegram-controlled gateway for local coding agents.

Current intended product:
- open a persistent session from Telegram
- continue that session through native Claude Code or Codex resume
- keep state file-backed and inspectable
- constrain execution by explicit workspace scope

This repository is no longer positioned as a generic AI task orchestrator or a local prompt-agent framework.

## What Is Active

- Telegram session control
- native backend resume for Claude Code and Codex
- file-backed session state
- per-session summaries and event logs
- one-off task fallback
- path validation and path suggestions for session creation
- git helper commands from Telegram

## What Is Not The Main Runtime Path

- LLAMA prompt engineering
- local agent-template orchestration
- modular local prompt-agents

Those components may remain in the repo as dormant future-facing code, but they are not the current product path.

## Architecture

```text
Telegram -> active chat binding -> gateway session -> Claude Code / Codex native session
         -> state/sessions/*.json
         -> state/summaries/*.md
         -> logs/session_events/*.log
         -> results/*.json
```

## Key Commands

### Telegram

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
- `pm2 start ecosystem.config.js --only ai-team-gateway --update-env`
- `pm2 restart ai-team-gateway --update-env`

## Configuration

The most important production settings are:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=123456789

CLAUDE_BASE_CWD=C:\Users\you\Projects
CLAUDE_ALLOWED_ROOT=C:\Users\you\Projects
CLAUDE_SKIP_PERMISSIONS=false
CLAUDE_TIMEOUT_SEC=300
CLAUDE_MAX_TURNS=0
```

`CLAUDE_BASE_CWD` and `CLAUDE_ALLOWED_ROOT` should usually point to the parent workspace that contains every repo the gateway is allowed to touch. Do not set them so narrowly that the gateway cannot access intended repos, including itself if self-editing is expected.

## Current Production Status

The session-first architecture is implemented.

The main production gate that still matters is a live end-to-end validation:
1. create a session from Telegram
2. send a first message
3. verify `backend_session_id` is stored
4. send a second message
5. verify native backend resume continues the same conversation

## Canonical Internal Docs

- [.ai/CONTEXT.md](C:/Users/Cicada38/Projects/AI-team/.ai/CONTEXT.md)
- [.ai/context/production_vision.md](C:/Users/Cicada38/Projects/AI-team/.ai/context/production_vision.md)
- [QUICK_START.md](C:/Users/Cicada38/Projects/AI-team/docs/QUICK_START.md)
- [OPERATIONS_PM2.md](C:/Users/Cicada38/Projects/AI-team/docs/OPERATIONS_PM2.md)
- [ROADMAP.md](C:/Users/Cicada38/Projects/AI-team/docs/ROADMAP.md)
- [CLAUDE_HOOK_IDEAS.md](C:/Users/Cicada38/Projects/AI-team/docs/CLAUDE_HOOK_IDEAS.md)
