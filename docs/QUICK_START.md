# Quick Start

## 1. Install

```powershell
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install -e ".[dev,test,telegram]"
```

Optional:

```powershell
pip install -e ".[llama]"
```

## 2. Configure

Copy `.env.example` to `.env` and set at least:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=123456789

CLAUDE_BASE_CWD=C:\Users\you\Projects
CLAUDE_ALLOWED_ROOT=C:\Users\you\Projects
```

Recommended rule:
- point both workspace values at the parent directory containing all repos the gateway may edit

## 3. Check Environment

```powershell
python main.py doctor
python main.py status
```

You want `doctor` to show:
- Claude executable found
- `Base CWD` set
- `Allowed root` set

## 4. Start The Gateway

```powershell
python main.py
```

Expected shape:
- Telegram bot initializes
- file watcher starts
- workers start
- the gateway waits for Telegram commands

## 5. First Live Validation

From Telegram:

1. `/session_new claude <repo>`
2. send a plain message like `inspect the repo and summarize current issues`
3. check `state/sessions/<session_id>.json`
4. send a second message
5. verify the backend session id is reused and the conversation continues

## Useful Files

- `state/sessions/`
- `state/summaries/`
- `logs/events.ndjson`
- `logs/session_events/`
- `results/`

## If Something Looks Wrong

- `python main.py doctor`
- `python main.py status`
- `python main.py tail-events`

If path creation fails from Telegram, the gateway now returns close directory matches and available nearby directories.
