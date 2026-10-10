---
name: checking-live-state
description: Read-only probes that establish what is actually running for AI-Team on this host — gateway and task-server containers, which commit the live image was built from, schema version, mesh nodes, PM2 worker, and whether a feature flag is ON in the running process. Use this before claiming anything is "live", "deployed", "enabled" or "fixed in prod", when debugging the running gateway, after a deploy, or whenever a merged change might not be running yet.
---

# Checking live state

Merged is not live, and the repo is not the process. The gateway and task-server run as Docker
Compose containers built from an image (`ai-team:local`), so code reaches production only through
an image rebuild — and flags can be set by DB registry, env, or default. Prose, PR titles and
`.env` all describe *intent*; only the probes below describe *reality*.

## Snapshot (run first)

```bash
.claude/skills/checking-live-state/scripts/live_status.sh [FLAG_NAME ...]
```
Read-only. Prints: compose containers + health, the live image's build time and `prod-<sha>` tag
(and how many `origin/main` commits are not live), gateway `/health`, task-server `/health`
(`schema_version`, `nodes_online/total`, pending/claimed tasks, mesh degraded), the PM2 worker,
and any flags you name.

The gateway `/health` probe targets the **MCP-resolved controller URL** (DASHBOARD_URL, else
CONTROLLER_URL host + DASHBOARD_PORT, else 127.0.0.1:DASHBOARD_PORT — the same resolution
`mcp_manager._base_url()` uses). When the gateway runs REMOTELY (CONTROLLER_URL on the tailnet)
the probe reports the real remote address and its true up/down — it does **not** hardcode
127.0.0.1 (which would falsely read "down" for a healthy remote gateway and wrongly imply the
harness is dead). The printed `(URL)` is the address actually probed.

## Reading the answers

- **Is commit X live?** Only if the image was built after it. With a `prod-<sha>` tag:
  `git merge-base --is-ancestor X <sha>`. Without one, compare `built=` to
  `git log --merges --since=<built> origin/main` — everything listed is *not* live.
- **Is flag F on?** `scripts/ops_flag.sh get F` (wraps authenticated `GET /api/flags`). Trust its
  `value` + `source` (registry/env/default). `effect_scope=startup` means a changed value needs a
  gateway restart before it bites. Do not infer flags from `/proc/<pid>/environ` — dotenv sets
  them after exec, so `/proc` lies.
- **Are workers alive?** task-server `nodes_online`; per-node detail via authenticated
  `GET /api/nodes` on the gateway. A nonzero PM2 restart count is normal under autorestart — only
  `claimed` tasks stuck for hours against an online node is an incident.
- **What did it log?** `docker logs --tail 200 ai-team-gateway-1`; files under
  `~/ai-team-data/controller/logs/` (`orchestrator.log`, `events.ndjson`). Startup markers:
  `embedded_control_server_started`, `Telegram Coding Gateway started successfully!`,
  `db_migration_applied version=N`.

## Never, while "just checking"

- `python main.py status` — takes the gateway lock and kills the live gateway.
- Restarting the PM2 `ai-team-worker` (or anything on node Horse) — interrupts live sessions; that
  is an operator decision.
- Writing flags (`ops_flag.sh on|off|unset`) — that is a change, not a check; say so and confirm.

## Report

State each claim with the probe that backs it ("`ops_flag.sh get X` → value=true source=registry"),
and name what you could not probe.
