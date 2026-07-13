# Quick Start

## 1. Install

```powershell
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install -e ".[dev,test,telegram]"
```

Optional (Telegram is enabled via the `telegram` extra above regardless — this
one is for the legacy local-LLAMA path, not required for either UI):

```powershell
pip install -e ".[llama]"
```

Build the Web UI (served by the gateway itself — see `docs/frontend/DEV_AND_BUILD.md`):

```powershell
cd web
npm install
npm run build
cd ..
```

## 2. Configure

Copy `.env.example` to `.env` and set at least:

```env
CLAUDE_BASE_CWD=C:\Users\you\Projects
CLAUDE_ALLOWED_ROOT=C:\Users\you\Projects
```

Telegram is optional — only set these if you want the secondary Telegram surface:

```env
GATEWAY_TELEGRAM_BOT_TOKEN=...
GATEWAY_TELEGRAM_ALLOWED_USERS=123456789
```

Recommended rule:
- point both workspace values at the parent directory containing all repos the gateway may edit

## 3. Check Environment

```powershell
python main.py doctor
```

You want `doctor` to show:
- Claude executable found
- `Base CWD` set
- `Allowed root` set

Do **not** run `python main.py status` against a live gateway — it acquires the
gateway lock and kills the running process. Use `curl http://127.0.0.1:9003/health`
instead once the gateway is up.

## 4. Start The Gateway

```powershell
python main.py
```

Expected shape:
- Control API + Web UI start on `:9003` (`CONTROL_API_ENABLED`, default on)
- Telegram bot initializes only if `GATEWAY_TELEGRAM_BOT_TOKEN` is set
- file watcher starts
- workers start

## 5. First Live Validation

From the Web UI (`http://<gateway-host>:9003/`, or `http://127.0.0.1:9003/` locally):

1. create a new session (backend `claude`, pick a repo)
2. send a plain message like `inspect the repo and summarize current issues`
3. check `state/sessions/<session_id>.json`
4. send a second message
5. verify the backend session id is reused and the conversation continues

Same flow works from Telegram (`/session_new claude <repo>` then plain messages)
if that surface is configured.

## Useful Files

- `state/sessions/`
- `state/summaries/`
- `logs/events.ndjson`
- `logs/session_events/`
- `results/`

## If Something Looks Wrong

- `python main.py doctor`
- `curl http://127.0.0.1:9003/health`
- `python main.py tail-events`

If path creation fails, the gateway returns close directory matches and available nearby directories.
