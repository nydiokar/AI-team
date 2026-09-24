# Docker Operations

This is the supported Linux production deployment for the AI-Team gateway. It
replaces PM2 with Docker Compose. Read [`DEPLOYMENT_DOCKER_DESIGN.md`](../DEPLOYMENT_DOCKER_DESIGN.md)
for scope and non-goals before deploying.

## Controller machine

Prerequisites: Docker Engine with Compose v2, a local filesystem for persistent
data, and Tailscale if workers or remote browsers will connect. Do not place the
data directory on a network share or synced folder.

```bash
git clone <release-repository-url> ai-team
cd ai-team
sudo ./scripts/prepare_docker_data_dirs.sh /srv/ai-team
install -m 600 deploy/controller.env.example /srv/ai-team/controller.env
```

Edit `/srv/ai-team/controller.env`: generate distinct secrets with `openssl rand
-hex 32`, set any optional Telegram/VAPID settings, and retain
`GATEWAY_LOCAL_EXECUTION_ENABLED=false` unless this host intentionally executes
agent work.

Publish only trusted addresses. With Tailscale, use the controller's Tailscale IPv4
for both addresses; use `127.0.0.1` for an SSH tunnel-only deployment.

```bash
export DEPLOY_DATA_ROOT=/srv/ai-team
export CONTROLLER_ENV_FILE=/srv/ai-team/controller.env
export CONTROL_BIND_IP=<controller-tailscale-ip>
export MESH_BIND_IP=<controller-tailscale-ip>
docker compose up -d --build
docker compose ps
curl http://$CONTROL_BIND_IP:9003/health
curl http://$MESH_BIND_IP:9002/health
```

Open `http://<controller-tailscale-ip>:9003/`. The Web UI is in the gateway
image—there is no separate frontend container or PM2 process.

## Worker machine (Codex target)

Clone the same release, create an independent data root and secret file, and mount
only the projects this worker may modify.

```bash
cd ai-team
sudo ./scripts/prepare_docker_data_dirs.sh /srv/ai-team-worker
install -m 600 deploy/worker.env.example /srv/ai-team-worker/worker.env
```

Set a unique `WORKER_NODE_ID`, the controller's Tailscale URL, the matching mesh
token, and `/workspaces` paths in `/srv/ai-team-worker/worker.env`.

```bash
export WORKER_DATA_ROOT=/srv/ai-team-worker
export WORKER_ENV_FILE=/srv/ai-team-worker/worker.env
export WORKSPACE_ROOT=/srv/worker-projects
export WORKER_BIND_IP=<worker-tailscale-ip>
docker compose -f deploy/compose.worker.yaml up -d --build
docker compose -f deploy/compose.worker.yaml logs -f worker
```

Do not mount the controller `state` directory on a worker. A worker may be updated
only after its active claims are drained or intentionally abandoned according to
the mesh lease policy.

## Validate, upgrade, and recover

Use `docker compose ps` and `docker compose logs --tail=200 gateway task-server`
for liveness. A healthy process is not a full readiness guarantee: verify an
authenticated API call and a worker registration before use.

Before upgrading, stop new work and make a quiesced SQLite backup. Retain the
previous immutable image digest. Recreate only controller services, verify both
health endpoints and worker registration, then resume work. Rollback recreates the
prior image digest against the unchanged state directory; never copy SQLite WAL
files while services are running.

Automatic controller failover is unsupported. Docker restarts a failed controller
on its original host; promoting another host requires future leader fencing and a
replicated canonical store.
