---
name: deploying-the-gateway
description: Makes merged AI-Team code live on this host by rebuilding the ai-team:local Docker image and recreating the gateway + task-server Compose containers — with preflight, rollback tag, DB backup when migrations apply, health verification, and rollback. Use this after merging anything that runs in the control plane (src/, config/, web/, scripts/ used in-container, Dockerfile, compose.yaml, pyproject/constraints), when asked to "deploy", "restart the gateway", "make it live", or when checking-live-state shows main ahead of the running image.
---

# Deploying the gateway

The control plane runs as two Compose containers (`ai-team-gateway-1` on :9003,
`ai-team-task-server-1` on :9002) from one image, `ai-team:local`, which **bakes in** `src/`,
`config/`, `scripts/` and the built Web UI. A restart alone re-runs the old image; merged code goes
live only via rebuild + recreate. Deploying the gateway is delegated to you — do it when a merge
needs it. Restarting a **worker** (PM2 `ai-team-worker`, node Horse) is not; surface that to the
operator.

Not needed for: `.ai/**`, `docs/**`, `tests/**`, `.claude/**`, other `*.md`.

## Workflow

```
- [ ] 1. Preflight → GO
- [ ] 2. Backup DB (only if preflight says MIGRATIONS WILL APPLY)
- [ ] 3. Rollback tag
- [ ] 4. Build
- [ ] 5. Recreate
- [ ] 6. Verify (loop until healthy or roll back)
- [ ] 7. Tag prod-<sha>, report
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
docker compose build gateway                       # builds the shared image; slow on this Pi — run in background, allow ~20 min
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
Expect: both healthy, `/health` ok, `schema_version` = HEAD's max migration, `nodes_online`
recovered to its pre-deploy value within ~1 min, no Traceback. Then exercise the change itself
through the surface where its goal is observed (API call, UI path, worker dispatch) — healthy
containers prove the process started, not that your change works.

If a container restart-loops or verification fails → **Rollback** below, then investigate.
A known loop cause is a stale `~/ai-team-data/controller/logs/gateway.lock` (exit 143).

**7. Record identity**: `docker tag ai-team:local ai-team:prod-<sha>` — this tag is how
`checking-live-state` knows which commit is live. Report: SHA deployed, migrations applied
(from → to), backup path, verification evidence, rollback tag.

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
