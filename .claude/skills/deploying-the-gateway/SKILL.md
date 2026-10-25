---
name: deploying-the-gateway
description: Makes merged AI-Team code live on this host by rebuilding the ai-team:local Docker image and recreating the gateway + task-server Compose containers — with preflight, rollback tag, DB backup when migrations apply, health verification, and rollback. Use this after merging anything that runs in the control plane (src/, config/, web/, scripts/ used in-container, Dockerfile, compose.yaml, pyproject/constraints), when asked to "deploy", "restart the gateway", "make it live", or when checking-live-state shows main ahead of the running image.
---

# Deploying the gateway

The control plane runs as two Compose containers (`ai-team-gateway-1` on :9003,
`ai-team-task-server-1` on :9002) from one image, `ai-team:local`, which **bakes in** `src/`,
`config/`, `scripts/` and the built Web UI. A restart alone re-runs the old image; merged code goes
live only via rebuild + recreate. Deploying the gateway is delegated to you — do it when a merge
needs it. Worker restarts require the project's operator authorization; use authorization already
provided in this session instead of asking again. Inspect workload before discussing or executing
any worker restart (see below).

Not needed for: `.ai/**`, `docs/**`, `tests/**`, `.claude/**`, other `*.md`.

## Workflow

```
- [ ] 1. Preflight → GO
- [ ] 2. Backup DB (only if preflight says MIGRATIONS WILL APPLY)
- [ ] 3. Rollback tag
- [ ] 4. Build
- [ ] 5. Recreate
- [ ] 6. Verify (loop until healthy or roll back)
- [ ] 7. Tag prod-<sha> + push deploy/<stamp>-<sha> git tag, report
```

**1. Preflight** (read-only; prints the exact commands for this deploy with SHA/timestamps filled):
```bash
.claude/skills/deploying-the-gateway/scripts/deploy_preflight.sh
```
It requires main == origin/main with a clean tree (the build context is the working tree, so a
dirty file ships). Build from the main checkout, not a fresh worktree: compose resolves
`DOCKER_DATA_ROOT` / `MESH_TAILSCALE_IP` from the host env file that only exists there. If the main
checkout is dirty with someone else's work, stop and surface it — don't stash or discard it. It also shows what goes live, compares live `schema_version` with the highest migration
in `src/control/db.py`, and validates `docker compose config`. Fix every STOP; don't bypass.

**2. DB backup** — migrations run automatically at startup and are forward-only, so the backup is
your only rollback for data. Use SQLite's online backup (safe while the DB is in use; never
`cp` the live `mesh.db`/`-wal`):
`sqlite3 ~/ai-team-data/controller/state/mesh.db ".backup '<path printed by preflight>'"`

**3–5. Tag, build, recreate** — run the preflight's commands in order:
```bash
docker tag ai-team:local ai-team:pre-<sha>
AI_TEAM_GIT_SHA=<sha> docker compose build gateway   # builds the shared image; slow on this Pi — run in background, allow ~20 min
docker compose up -d --no-build gateway task-server
```
Recreate **both**: they share the image and the DB schema, and a task-server left on old code
against a migrated DB is a split-brain. Recreating the task-server briefly interrupts worker
polling (workers retry); it does not restart workers.

**6. Verify** (feedback loop — don't declare done on the first green line):
```bash
docker compose ps                                              # both (healthy); healthcheck takes ~30s
.claude/skills/checking-live-state/scripts/live_status.sh <FLAGS_THIS_CHANGE_DEPENDS_ON>
docker logs --since 5m ai-team-gateway-1 2>&1 | grep -E "embedded_control_server_started|started successfully|db_migration_applied|Traceback|ERROR" | tail -20
```
Expect: both healthy, `/health` ok with `build.git_sha` = the deployed SHA, `schema_version` = HEAD's max migration, `nodes_online`
recovered to its pre-deploy value within ~1 min, no Traceback. Then exercise the change itself
through the surface where its goal is observed (API call, UI path, worker dispatch) — healthy
containers prove the process started, not that your change works.

If a container restart-loops or verification fails → **Rollback** below, then investigate.
A known loop cause is a stale `~/ai-team-data/controller/logs/gateway.lock` (exit 143).

**7. Record identity**: `docker tag ai-team:local ai-team:prod-<sha>` — this tag is how
`checking-live-state` knows which commit is live. Then push the annotated git tag the preflight
printed (`deploy/<UTC stamp>-<sha>`): it is the off-host deploy history (`git tag -l 'deploy/*'`
on GitHub) and survives this host's disk; the Docker tags do not. Only after verification passes —
a rolled-back deploy gets no `deploy/` tag. Report: SHA deployed, migrations applied
(from → to), backup path, verification evidence, rollback tag.

## Worker context before a restart decision

Before asking about or executing a worker restart, inspect what it is doing:

```bash
.venv/bin/python scripts/worker_context.py <exact-node-id>
```

This read-only probe selects fields from authenticated `/api/nodes` and local PM2/process ancestry.
It prints task IDs, backend, action, phase, start time, slots, pooled-session count, heartbeat age,
loaded build/incarnation, exact PM2 name/cwd/script, active login PIDs, and whether the worker hosts
this agent. It never prints prompts, OAuth links, PM2's full environment, or tokens. Run on the worker
host for local process evidence; remote PM2 absence is not evidence that the remote worker is idle.
Missing/stale/malformed state means **unknown**, never idle. `/health` alone proves no workload facts.

- Compare the loaded worker build with the fix's commit. If it already contains the change, exercise
  the behavior instead of requesting a redundant restart. Do not confuse code on disk with loaded code.
- Give a concrete impact statement: exact worker/node, running task IDs/backends and age, pooled
  sessions/login processes affected, and whether this agent would disconnect. Do not dump task prompts.
- Check session authorization first. If the operator already approved this worker update/restart,
  proceed within that scope; do not ask the same question again. Without authorization, ask once with
  the observed workload and recommendation. An idle snapshot does not grant permission.
- Prefer waiting for active turns/logins to finish over interrupting them. Recheck immediately before
  restarting, using a fresh heartbeat and host process check. Do not pause/cancel unrelated queues or
  infer permission to interrupt other nodes. If ongoing work prevents the approved restart, report
  the actual task IDs and bounded waiting/handoff plan, not a generic request to restart.
- If this worker hosts the caller, do not run a restart from its child process and lose the remaining
  work/verification. Finish and commit the changes first. Use a supervised handoff outside the worker
  tree (for example, a transient user systemd unit) which waits for active turns, including this one,
  to finish. Give it a deadline, exact expected node/PM2 PID/source commit, one restart attempt, a
  persistent result/log, and post-restart checks. Do not use an orphaned `nohup` or background shell.
  Report a deferred restart as pending until its result actually confirms success.
- Update the checkout that PM2 actually executes, preserving the operator's env, interpreter, data
  paths and identity. Refuse dirty tracked files or a non-fast-forward update. Avoid `--update-env`
  unless the environment itself is intentionally changing; an agent environment is not a service env.
- Verify a **new incarnation/PID and heartbeat after restart**, expected build, node online, gateway
  coverage, and the changed feature. An old `online` row or a canary startup is not proof of promotion.
  Reap only processes owned by this restart; never kill another session's backend to make a check pass.

## Rollback

```bash
docker tag ai-team:pre-<sha> ai-team:local
docker compose up -d --no-build gateway task-server
```
Code rollback is safe when migrations were additive. Restoring the DB backup discards every write
since the deploy — that is an **operator decision**; propose it, don't do it. Check
`.ai/CONTEXT.md` for standing rollback constraints (e.g. image/protocol pairs that must not be
rolled back across) before rolling back past a feature boundary.

## Do not

- Run a second gateway (`docker compose run gateway`, `python main.py` on the host) — two
  gateways on one Telegram token fight; `python main.py status` takes the lock and kills the live one.
- Use `scripts/auto_deploy.sh` — it is a PM2-era poller and exits fatally against Compose.
- Use `--force` anywhere, or prune `pre-*` images you did not create.

## Old patterns

<details><summary>PM2 gateway (retired)</summary>
`pm2 restart ai-team-gateway` came from the PM2 era (`ecosystem.config.js`). No such PM2 process
exists on this host now; the command fails, or appears to succeed and changes nothing.
`docs/RUNBOOKS/OPERATIONS_DOCKER.md` is the canonical runbook.
</details>
