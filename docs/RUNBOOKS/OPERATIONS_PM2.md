# PM2 Operations

Use PM2 as the process supervisor for the AI-Team Gateway (Web UI + Telegram).

This is the supported way to keep the gateway alive across crashes, machine reboots,
and code updates without asking the Python app to restart itself.

## Why PM2

- one stable process owner
- cross-platform enough for Windows and Linux
- simple `start`, `restart`, `stop`, `logs`
- boot persistence via `pm2 save` and `pm2 startup`

The gateway itself already guards against duplicate local instances, but PM2 should
still be configured with exactly one process instance.

## Files

- `ecosystem.config.js`

## Start

From the repo root:

```bash
pm2 start ecosystem.config.js --only ai-team-gateway --update-env
```

## Restart After Code Changes

```bash
pm2 restart ai-team-gateway --update-env
```

This is the normal operator path after `git pull` or local edits.

For mesh-worker Codex package upgrades, do not use gateway auto-deploy or assume
the install replaces a running app-server. Follow the explicit validation and
carrier-recycle boundary in [Codex app-server adapter convergence](../CODEX_APP_SERVER_ADAPTER_CONVERGENCE.md#codex-upgrades-and-runtime-recycle).

## Stop / Remove

```bash
pm2 stop ai-team-gateway
pm2 delete ai-team-gateway
```

## Logs

```bash
pm2 logs ai-team-gateway
```

PM2 also writes process logs into:

- `logs/pm2-out.log`
- `logs/pm2-error.log`

## Log Rotation

Install the PM2 logrotate module once:

```bash
pm2 install pm2-logrotate
pm2 set pm2-logrotate:max_size 10M
pm2 set pm2-logrotate:retain 7
pm2 set pm2-logrotate:compress true
pm2 set pm2-logrotate:rotateInterval '0 0 * * *'
```

That keeps the PM2-managed logs from growing unbounded.

## Persist Across Reboots

After you have a healthy running process:

```bash
pm2 save
pm2 startup
```

Run the command printed by `pm2 startup` for your platform, then run `pm2 save` again
if needed.

## Recommended Operator Flow

1. `git pull`
2. `pm2 restart ai-team-gateway --update-env`
3. `python main.py health`
4. `pm2 logs ai-team-gateway`
5. verify the Web UI loads (`curl http://127.0.0.1:9003/health`) and, if configured, Telegram responds

## Auto-Deploy (T1 — gateway host only)

The gateway/server host (the gateway-host `gateway-host`) can auto-deploy pushes to `main`
instead of a manual `git pull` + restart. We use a **pull-based** poller that
runs *on the gateway-host* rather than GitHub Actions → SSH, because the gateway-host is behind
home NAT and we don't want CI reaching into the tailnet.

**Mechanism:** `scripts/auto_deploy.sh`, driven by the `ai-team-deploy` PM2 entry
as a `cron_restart` job (runs, exits, re-runs every 2 min). Each run:

1. `git fetch`; if `origin/main` == local HEAD → quiet exit (no-op).
2. **Poison guard:** refuse to redeploy a commit that already failed the health
   gate (recorded in `.deploy.poison`) — prevents an every-2-min redeploy loop on
   a bad commit. Push a fix to clear it.
3. Fast-forward only (never merge/rewrite); refuses if HEAD isn't `main` or has
   diverged.
4. Restart the target apps — **`ai-team-gateway` + `ai-team-server`** by default
   (the live split runs the task server standalone on :9002). Apps not present on
   this host are skipped. **Docs-only pushes** (`docs/`, `.ai/`, `*.md`)
   fast-forward but skip the restart. (These apps run in PM2 *fork* mode, where
   `reload` is not zero-downtime, so we `restart` plainly — expect a brief blip.)
5. **Health gate (authoritative = PM2 process status):** every restarted app must
   reach `online` and *stay* online for a stability window without its PM2 restart
   counter climbing (catches a crash-loop on bad code). Additionally, if
   `ai-team-server` is running, the `:9002/health` HTTP endpoint must report
   `status: ok`. The HTTP check alone is **not** trusted — it only proves the task
   server is up, not that the gateway came back.
6. **On health failure → roll back** to the previous commit, restart again, record
   the bad SHA in `.deploy.poison`, and exit non-zero (loud). A bad commit never
   leaves the gateway down silently or loops.

**Enable on the gateway-host (only there):**

```bash
pm2 start ecosystem.config.js --only ai-team-deploy
pm2 save
pm2 logs ai-team-deploy        # watch a deploy happen
```

**Scope — do NOT enable on worker boxes** (e.g. `worker-node`). Auto-restarting a
worker mid-task drops its in-flight claim and costs the gateway a full dispatch
timeout (the T4 bug). Worker nodes update on their own cadence. After T4
(reclaim-on-restart) lands, revisit whether workers can auto-deploy safely.

**Tunables** (PM2 `env` block or `.env`): `DEPLOY_PM2_APPS`,
`DEPLOY_HEALTH_URL`, `DEPLOY_HEALTH_TIMEOUT`, `DEPLOY_BRANCH`. Full list in the
script header. The script is Linux/bash only (it runs on the gateway-host).

## Native Worker (canonical execution node)

**Canonical architecture (as of 2026-09-25).** The execution worker runs as a
**native host process supervised by PM2**, as the ordinary host user — *not* in a
container. The control plane (gateway + task-server) stays containerized (Docker)
and is independent of the worker. A containerized worker (`deploy/compose.worker.yaml`)
is **non-canonical / experimental** and must not be run alongside the native worker
for the same node.

| Concern | Canonical value |
| --- | --- |
| Control plane (gateway / task-server) | Docker containers |
| Execution worker | Native host process |
| Supervisor | PM2 (`ecosystem.config.js`, app `ai-team-worker`) |
| Host environment | The normal host user's `HOME`, `PATH`, Claude/Codex/Git/gh/Docker/Python/Node, MCP config, repos — authoritative |
| Worker ↔ controller | Explicit HTTP over the tailnet (Bearer `WORKER_TOKEN`); no shared filesystem/credentials |

The worker runs as the host user (`whoami` = the login user, `HOME=/home/<user>`),
resolving `claude`, `codex`, `git`, `gh`, `docker`, `python`, `node`, `npm`, `pnpm`,
and MCP servers from the normal host `PATH` and `~/.claude.json` / `~/.codex` — never
synthetic container paths like `/app/.claude` or `/app/.codex`.

### Required env (in `.env`, loaded by `worker_main.py`)

- `WORKER_NODE_ID` — stable node identity (e.g. `kanebra-worker`)
- `WORKER_TOKEN` — controller worker token (Bearer auth to the task-server)
- `CONTROLLER_URL` — task-server URL, e.g. `http://<controller-tailscale-ip>:9002`
- `WORKER_TAILSCALE_IP`, `WORKER_API_PORT` (default `9001`)
- `WORKER_BACKENDS` (e.g. `claude,codex,opencode,opencode-server`), `WORKER_MAX_CONCURRENT`
- `WORKER_PROJECTS_ROOT` — repository/workspace root for discovery

Template: [`deploy/worker.env.example`](../../deploy/worker.env.example). Secrets are
**never** captured into PM2's dump — `ecosystem.config.js` uses `filter_env`
(prefixes `WORKER_`, `CONTROLLER_`, `CLAUDE_`, …) so `pm2 save` cannot leak them; each
app re-reads `.env` on start.

### Endpoints

- Task-server (controller API the worker uses): `CONTROLLER_URL` → `:9002`
- Gateway / Web UI + SSE + read models: `:9003`
- Worker API (node-local): `:9001`

### Start / Stop / Restart / Logs

```bash
# Start (worker node only)
pm2 start ecosystem.config.js --only ai-team-worker --update-env

# Restart (rare — disrupts live sessions; prefer letting autorestart handle crashes)
pm2 restart ai-team-worker --update-env

# Stop / remove
pm2 stop ai-team-worker
pm2 delete ai-team-worker

# Logs
pm2 logs ai-team-worker
# files: logs/pm2-worker-out.log , logs/pm2-worker-error.log
```

PM2 config for the worker: `autorestart: true`, `max_restarts: 10`,
`kill_timeout: 35000` (longer than the ~30s in-process drain window in
`src/worker/agent.py`, so an in-flight task drains before the process dies).

### Health verification (do NOT trust `online` alone)

```bash
pm2 describe ai-team-worker                       # status online, sane restart count
curl -s http://127.0.0.1:9003/health              # gateway ok
curl -s http://<controller-ip>:9002/health        # task-server ok (nodes_online)
# Confirm THIS node heartbeats fresh in the controller read model:
curl -s -H "Authorization: Bearer $DASHBOARD_TOKEN" \
  http://127.0.0.1:9003/api/nodes | jq '.[] | {node_id,status,last_heartbeat}'
```

A healthy worker: exactly one **online** entry for this node with a fresh
`last_heartbeat`, activity `task_activity` events flowing to `/api/events` and the
SSE stream `/api/events/stream`, and (if the quota repair is deployed)
`quota.worker_observation_ingested` events from this node.

### Node identity

`WORKER_NODE_ID` is the stable identity registered with the controller. Only one
**online** worker per physical node is expected; stale offline registrations
(old test/canary node ids) are harmless historical rows.

### Recovery after reboot

```bash
pm2 save        # after a healthy start, persist the process list
pm2 startup     # run the printed command once to install the boot unit
```

On reboot PM2 resurrects `ai-team-worker`; it re-reads `.env`, re-registers with the
controller, and resumes claiming — **no re-authentication or manual env
reconstruction** (Claude/Codex identity lives in the host user's `~/.claude.json` /
`~/.codex`, untouched by restarts).

### Basic smoke test

1. `pm2 restart ai-team-worker --update-env` (or rely on autorestart).
2. `curl .../api/nodes` → this node `online`, fresh heartbeat.
3. Dispatch/allow a small task; watch `task_activity` on `/api/events/stream`.
4. Confirm a telemetry turn row appears (`/api/turns`) and the task completes
   (`/api/tasks`).

## Notes

- Do not run multiple PM2 instances for the same gateway repo.
- Do not set PM2 `instances > 1`.
- The app-level locks are a safety net, not the primary supervision model.
- Do **not** run the containerized worker (`deploy/compose.worker.yaml`) and the
  native `ai-team-worker` for the same node at once — that produces two workers for
  one physical node.
