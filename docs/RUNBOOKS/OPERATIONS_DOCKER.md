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
export CONTROL_PUBLISHED_PORT=9003
export MESH_PUBLISHED_PORT=9002
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
install -m 600 deploy/worker.compose.env.example /srv/ai-team-worker/compose.env
```

Set a unique `WORKER_NODE_ID`, the controller's Tailscale URL, and the matching
mesh token in `/srv/ai-team-worker/worker.env`. Set `WORKER_PROJECTS_ROOT`,
`CLAUDE_BASE_CWD`, and `CLAUDE_ALLOWED_ROOT` to the same absolute host projects
path used below. That one declared root is mounted read-write at the same path,
so the worker can use existing repositories or create new ones beneath it but
cannot see files outside its declared mounts.

Use this canonical command for build, login, and lifecycle actions; it loads all
required non-secret Compose inputs from one stable file:

```bash
docker compose --env-file /srv/ai-team-worker/compose.env -f deploy/compose.worker.yaml up -d --build
docker compose --env-file /srv/ai-team-worker/compose.env -f deploy/compose.worker.yaml logs -f worker
```

The image runs as fixed UID/GID `10001` (`ai-team`). Grant that identity access
to the declared projects root with an ACL rather than running the worker as root
or making the tree world-writable. Apply default ACLs before the worker creates
new repositories:

```bash
sudo setfacl -Rm u:10001:rwX /srv/worker-projects
sudo setfacl -Rm d:u:10001:rwX /srv/worker-projects
```

### One-time ChatGPT login for the self-contained Codex worker

The worker image contains the pinned Codex CLI and starts `codex app-server
--stdio` inside the container. Its persistent, worker-owned `CODEX_HOME` is
`$WORKER_DATA_ROOT/codex`; never mount the host user's `~/.codex` or put a
token in `worker.env`.

Bootstrap ChatGPT authentication once, using the device flow displayed by the
container. Complete browser approval as the intended ChatGPT user, then start
the normal worker. The persistent volume survives container replacement.

```bash
docker compose --env-file /srv/ai-team-worker/compose.env -f deploy/compose.worker.yaml run --rm --no-deps worker codex login --device-auth
docker compose --env-file /srv/ai-team-worker/compose.env -f deploy/compose.worker.yaml run --rm --no-deps worker codex login status
docker compose --env-file /srv/ai-team-worker/compose.env -f deploy/compose.worker.yaml up -d worker
```

`localhost` inside this worker is container-local. The normal worker needs only
the controller URL declared in `worker.env`; do not assume host-local MCP
servers or APIs are reachable. Add a specific route only when a named
integration requires one. Host-wide maintenance remains a separate host-native
operation outside this container's filesystem boundary.

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
