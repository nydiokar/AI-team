# PM2 Operations

Use PM2 as the process supervisor for the Telegram Coding Gateway.

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
5. verify Telegram responds

## Auto-Deploy (T1 — gateway host only)

The gateway/server host (the Pi5 `kanebra`) can auto-deploy pushes to `main`
instead of a manual `git pull` + restart. We use a **pull-based** poller that
runs *on the Pi5* rather than GitHub Actions → SSH, because the Pi5 is behind
home NAT and we don't want CI reaching into the tailnet.

**Mechanism:** `scripts/auto_deploy.sh`, driven by the `ai-team-deploy` PM2 entry
as a `cron_restart` job (runs, exits, re-runs every 2 min). Each run:

1. `git fetch`; if `origin/main` == local HEAD → quiet exit (no-op).
2. Fast-forward only (never merge/rewrite); refuses if HEAD isn't `main` or has
   diverged.
3. `pm2 reload ai-team-gateway` (reload, not restart). **Docs-only pushes**
   (`docs/`, `.ai/`, `*.md`) fast-forward but skip the reload.
4. **Health gate:** poll `http://127.0.0.1:9002/health` until `status: ok` or
   60s timeout.
5. **On health failure → roll back** to the previous commit, reload again, and
   exit non-zero (loud). A bad commit never leaves the gateway down silently.

**Enable on the Pi5 (only there):**

```bash
pm2 start ecosystem.config.js --only ai-team-deploy
pm2 save
pm2 logs ai-team-deploy        # watch a deploy happen
```

**Scope — do NOT enable on worker boxes** (e.g. `Horse`). Auto-restarting a
worker mid-task drops its in-flight claim and costs the gateway a full dispatch
timeout (the T4 bug). Worker nodes update on their own cadence. After T4
(reclaim-on-restart) lands, revisit whether workers can auto-deploy safely.

**Tunables** (PM2 `env` block or `.env`): `DEPLOY_PM2_APPS`,
`DEPLOY_HEALTH_URL`, `DEPLOY_HEALTH_TIMEOUT`, `DEPLOY_BRANCH`. Full list in the
script header. The script is Linux/bash only (it runs on the Pi5).

## Notes

- Do not run multiple PM2 instances for the same gateway repo.
- Do not set PM2 `instances > 1`.
- The app-level locks are a safety net, not the primary supervision model.
