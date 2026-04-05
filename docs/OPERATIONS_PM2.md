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

## Notes

- Do not run multiple PM2 instances for the same gateway repo.
- Do not set PM2 `instances > 1`.
- The app-level locks are a safety net, not the primary supervision model.
