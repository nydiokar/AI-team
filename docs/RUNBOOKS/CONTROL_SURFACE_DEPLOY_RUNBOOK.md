# Control Surface Deploy Runbook

How to deploy the unified gateway (Web UI + Telegram on one process) from
`docs/CONTROL_SURFACE_UNIFICATION.md` (U1–U6). After this, `python main.py` is the
**only** long-running process: it serves the Web UI + Control API **and**, if
configured, Telegram, on one tailnet-bound port. There is no `dashboard_main.py`
anymore.

Owner: Nyd · Last updated: 2026-06-23

---

## 0. Prerequisites

- The box is on the Tailscale tailnet (`tailscale ip -4` returns an address).
- System Python has `fastapi`, `uvicorn`, `pytest` (these live in the **system**
  Python, not `.venv`).
- Node + npm available on the box (or build `web/dist` elsewhere and copy it).

## 1. Build the Web UI bundle (REQUIRED — `web/dist` is gitignored)

`web/dist` is **not** committed. The gateway serves whatever is in `web/dist`; an
absent or stale bundle is the #1 deploy footgun. The U5 token-injection is
server-side, but the consuming `authStore` code ships **in the bundle** — so a stale
bundle won't read the injected token.

```bash
cd web
npm ci
npm run build          # emits web/dist/{index.html,assets/*}
cd ..
```

Verify the bundle is fresh:

```bash
ls -l web/dist/index.html        # mtime should be "just now"
```

## 2. Configure the bind host + token (`.env`)

```bash
# Bind the Control API + Web UI to the tailnet IP ONLY. Never 0.0.0.0.
CONTROL_API_HOST=<this box's tailscale ip>   # e.g. 100.x.y.z
CONTROL_API_ENABLED=true                      # default; set false to disable the surface
DASHBOARD_PORT=9003                           # default; the UI+API port
DASHBOARD_TOKEN=<a strong secret>             # falls back to WORKER_TOKEN if unset
# CONTROL_API_DOCS=true                        # OPTIONAL, dev only. Re-enables the
#                                              # interactive /docs + /openapi.json,
#                                              # which are OFF in prod (they leak the
#                                              # full API shape). Leave unset in prod.
```

Binding precedence (orchestrator `_start_embedded_control_api`):
`CONTROL_API_HOST` → else `config.mesh.tailscale_ip` → else `127.0.0.1`. The tailnet
(WireGuard private net) is the outer auth layer; the token is defense-in-depth on
`/api/*`. **Do not set `0.0.0.0`** — that exposes the UI + API on every interface.

## 3. Start the gateway (the only process)

```bash
# Local smoke test (no Telegram long-poller, no mesh) to confirm it binds:
GATEWAY_TELEGRAM_BOT_TOKEN="" MESH_ENABLED=false python main.py
```

For production, start it the normal way (PM2 entry / service unit). There is **no**
separate dashboard entry to start — if an old process manager still launches
`dashboard_main.py`, remove that entry (it no longer exists).

> ⚠️ The Telegram bot is a single long-poller. Do not run a second gateway with a
> live `GATEWAY_TELEGRAM_BOT_TOKEN` against the same bot — two pollers fight.

## 4. Point your phone at it

On a tailnet device (phone with Tailscale connected):

```
http://<tailscale-ip>:<DASHBOARD_PORT>/        e.g. http://100.x.y.z:9003/
```

The page loads **token-authenticated with no prompt** — the gateway injects
`window.__DASHBOARD_TOKEN__` into the served `index.html` and the `authStore` reads it
and skips `TokenGate`. The token is readable by anyone who can load the page; that is
acceptable precisely because only tailnet devices can reach the port.

## 5. Verify (smoke checklist)

```bash
TOKEN=<DASHBOARD_TOKEN>
HOST=<tailscale-ip>:9003

curl -s http://$HOST/health                         # {"status":"ok"} — open, no auth
curl -s http://$HOST/api/sessions                    # 401/403 WITHOUT a token
curl -s -H "Authorization: Bearer $TOKEN" http://$HOST/api/sessions   # 200 + sessions
curl -s -o /dev/null -w "%{http_code}\n" "http://$HOST/%2e%2e/%2e%2e/.env"  # NOT the .env (SPA index / not the file)
```

In the browser: open the URL → app loads (no token prompt) → Sessions/System show
live data → the SSE stream (`/api/events/stream?token=…`) pushes events as tasks run.

## 6. Rollback

Each ladder step is one revertible commit. To drop just the security fix or a single
step: `git revert <commit>`. To disable the whole surface without reverting code:
`CONTROL_API_ENABLED=false` and restart (Telegram keeps working).

---

## Appendix — what changed vs the old two-process setup

| Old | New |
|---|---|
| `python dashboard_main.py` (separate, file-side-reading) | folded into the gateway (`src/control/control_api.py`, embedded by `EmbeddedControlServer`) |
| dashboard re-read `state/mesh.db` + re-derived node liveness | reads the live in-process `SessionService` / `NodeRegistry` |
| web served by vite in "prod" | gateway serves `web/dist` at `/` (vite is dev-only) |
| read-only | read **+** write (`/api/instructions|sessions|…`) **+** SSE push |
