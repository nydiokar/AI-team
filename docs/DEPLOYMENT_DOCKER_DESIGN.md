# Production Docker Deployment Design

**Status:** proposed implementation design. This document is the build contract for
the Docker deployment work; it is not a claim that the artifacts exist yet.

## 1. Objective and scope

Ship a Linux-only deployment that starts the AI-Team control plane with Docker
Compose, persists durable state across restarts, includes the production Web UI,
and supports a controller on one machine with workers on the same or other
machines. Docker replaces PM2 as the supported production supervisor.

The first release provides high availability *recovery*, not controller high
availability: a stopped controller can be restarted automatically by Docker, but
it cannot safely be promoted on another machine. The canonical state is one local
SQLite database and the mesh protocol has no leader-election, replicated-log, or
fencing contract. Pretending otherwise could create two controllers accepting the
same claim. Controller failover is deliberately a later, separately designed
database-and-protocol project.

Non-goals for this release:

- Kubernetes, Swarm, automatic multi-host failover, or a distributed filesystem.
- Bundling user secrets or user repositories into an image.
- Replacing Tailscale. Tailscale remains the private network boundary.
- Automatically updating workers while they may have an in-flight claim.

## 2. Observed application topology

| Runtime | Entry point | Responsibility | Durable data |
|---|---|---|---|
| Gateway | `main.py` | orchestrator, Control API, Web UI, optional Telegram | `state/`, `logs/` |
| Task server | `server_main.py` | mesh registration, claim, heartbeat and telemetry API | gateway's local `state/mesh.db` |
| Worker | `worker_main.py` | executes backend CLIs and reports to controller | worker-local state/spool and backend auth |
| Web UI | `web/dist` | static React SPA served by gateway | none |

The gateway must serve `web/dist` itself. It decides whether a browser gets the
trusted-device token-injection response; an independent Nginx static container
would either duplicate or bypass that behavior. A gateway image therefore builds
the SPA and copies only its `dist` output into the Python runtime image.

## 3. Deployment topology

### One machine

`gateway` and `task-server` run as separate Compose services, share a single
*local* `state` bind mount, and run with `MESH_EMBEDDED_SERVER=false`. A local
`worker` is optional and enabled by a Compose profile. The services use Docker
restart policies and health checks, not PM2.

### Two or more machines

The controller machine runs `gateway` and `task-server`. Every worker machine
runs only a `worker` service with a unique `WORKER_NODE_ID`; it contacts the
controller over the tailnet URL in `CONTROLLER_URL`. Workers never mount or write
the controller database. They own only a bounded local spool/state directory and
their permitted project trees.

```
browser -- Tailscale --> gateway :9003 --> local state/mesh.db <-- task-server :9002
                                           ^
worker A ---- authenticated mesh HTTP ------|
worker B ---- authenticated mesh HTTP ------|
```

The controller state directory must be on a local Linux filesystem. Network
shares, cloud-sync folders, and distributed Docker volumes are prohibited for
SQLite WAL state.

## 4. Image and Compose contract

One multi-stage `Dockerfile` will provide named build targets:

1. `web-build`: Node LTS + pinned pnpm, `pnpm install --frozen-lockfile`, then
   `pnpm build`.
2. `python-base`: pinned Python slim base, production Python dependencies from
   `constraints.txt`, the application source, and the built UI.
3. `runtime`: non-root application user, `tini`, Git and only runtime libraries;
   default command is `python main.py`.
4. Backend targets extend `runtime` only when needed. `worker-codex` adds the
   pinned Node/Codex runtime; other backend targets will be introduced only after
   their exact install/auth contract is verified.

The first implemented worker target will be `worker-codex`, because the project
already invokes `codex app-server` by executable name. Gateway/task-server do not
need a Node runtime unless configured to execute Codex locally. The deployment
will offer a `local-worker` profile explicitly for that case; controller-only is
the safer default. It must be enforced by configuration, not inferred from an
absent executable: today the gateway starts a local worker pool and otherwise
would accept unpinned work locally.

Compose rules:

- code is baked into images; production never bind-mounts the repository;
- every mutable application directory (`state`, `logs`, `tasks`, `results`, and
  `summaries`) is an explicit host path under `DEPLOY_DATA_ROOT`;
- Compose secrets mount one complete per-role environment file at
  `/run/secrets/role.env` and set `AI_TEAM_ENV_FILE`. It must contain every
  configuration value the process needs, including non-secret values: the existing
  explicit-file loader correctly clears managed variables missing from that file;
- `restart: unless-stopped`, bounded JSON log rotation, and HTTP health checks;
- `init: true`, `read_only: true`, `tmpfs: /tmp` where compatible, dropped Linux
  capabilities, `no-new-privileges`, and resource limits;
- no privileged mode, host networking, Docker socket, or broad home-directory
  mount.

The image references will be parameterised (`AI_TEAM_IMAGE`, default local build)
so operators can either build from a checked-out release or pull an immutable
registry tag/digest. A release must publish an SBOM, image digest, and supported
backend targets.

## 5. Configuration changes required before Compose

Current settings conflate an address to bind inside a process with the host's
Tailscale identity. Bridged Docker containers cannot bind the host's Tailscale IP.
Implement these backward-compatible settings:

| New variable | Consumer | Default / compatibility |
|---|---|---|
| `CONTROL_API_BIND_HOST` | gateway | unset means existing `CONTROL_API_HOST` behavior |
| `MESH_BIND_HOST` | task server / embedded mesh | unset means existing `MESH_TAILSCALE_IP` behavior |
| `GATEWAY_LOCAL_EXECUTION_ENABLED` | gateway local worker pool | `true`; Compose controller sets `false` |

Compose sets both bind variables to `0.0.0.0` *inside the private container
network*. Docker port publication is then the external boundary: UI is published
only at the controller Tailscale IP or loopback; mesh is published at the
controller Tailscale IP. Existing non-Docker installations retain their exact
behavior because the variables are optional.

When `GATEWAY_LOCAL_EXECUTION_ENABLED=false`, the gateway keeps its queue workers
because they are also the dispatchers for remote-pinned tasks, but rejects an
unpinned/local task with a structured reason before it queues or attempts to use
an absent CLI. Existing installations retain `true`; the Compose controller
explicitly sets `false` and the local-worker profile explicitly sets `true` with a
backend image and project mount. This is a security boundary, not a convenience
toggle.

Do not set `CONTROL_API_HOST=0.0.0.0` as a Docker workaround. It has a distinct
existing security meaning and must remain an explicit operator choice.

## 6. Secrets, identity, and filesystem boundaries

Each machine has a role-specific secret file, mode `0600`, never committed:

- controller: `DASHBOARD_TOKEN`, mesh credentials, optional Telegram/VAPID and
  backend credentials only if a local worker profile is enabled;
- worker: mesh credential, backend credentials and backend-specific config;
- no shared `.env` copied between controller and workers.

The current shared `WORKER_TOKEN` remains temporarily supported for compatibility,
but deployment docs will warn that it is an interim limitation and point to the
already-planned per-node credential rollout. Docker must not claim to solve mesh
identity by itself.

Worker project directories are bind-mounted at `/workspaces`, the only writable
project root. `CLAUDE_BASE_CWD`, `CLAUDE_ALLOWED_ROOT`, and
`WORKER_PROJECTS_ROOT` must resolve below that mount. Backend homes are separate
named/host volumes, never the host user's whole home directory. If a backend needs
to refresh its login credentials, its auth volume is writable; otherwise mount it
read-only. This must be selected per backend and documented.

## 7. Operations contract

The deliverable will add:

- root README quick path: configure, build/pull, `docker compose up -d`, health;
- `docs/RUNBOOKS/OPERATIONS_DOCKER.md`: one-machine, two-machine, local worker,
  remote worker, tailnet exposure, logs, upgrade, rollback, backup and restore;
- `deploy/` templates for Compose and role-specific environment files;
- an explicit statement that PM2 remains legacy until the Docker release is
  accepted, then Docker becomes primary production guidance.

Upgrade uses a stopped-or-healthy replacement sequence: backup state, pull/build
the exact release image, run the configuration preflight, recreate gateway and
task-server, then pass health and mesh checks. Rollback uses the prior image digest
and retained state; it never rewrites the database. Worker upgrades are manual and
only after draining/observing that machine's active claims.

Backups are a quiesced SQLite backup using the project-supported SQLite method (not
a blind copy of WAL files), plus session artifacts and role secret files stored
separately. Restore is tested into a new data directory before it is relied on.

## 8. Staged implementation and acceptance gates

### Stage 0 — design and adversarial review

Write this design, challenge every trust/durability/topology assumption, record
accepted changes, and only then implement. Gate: review findings are resolved in
this document.

### Stage 1 — container-safe configuration and tests

Add bind-host settings and the local-execution gate, keep legacy defaults
unchanged, and add focused tests for gateway/task-server host resolution and
disabled-local-task rejection. Gate: existing bind-host tests plus new tests pass.

### Stage 2 — reproducible images

Add `Dockerfile`, `.dockerignore`, pinned build inputs, OCI labels and backend
targets. Gate: fresh `docker build`, image has built `web/dist`, runs non-root,
and does not contain `.env`, `state`, `.git`, or host credentials.

### Stage 3 — Compose bundle and templates

Add controller/task-server services, optional worker profiles, health checks,
volumes, secrets, port bindings and security options. Gate: `docker compose
config` succeeds for controller and worker configurations.

### Stage 4 — documentation and release ergonomics

Write the short README path and complete Docker runbook. Gate: a clean-machine
reader can perform every command without PM2 or undocumented files.

### Stage 5 — automated verification

Add a Linux Compose smoke script that builds an isolated project name and data
directory; proves gateway health, task-server health, UI asset serving, gateway
restart persistence, and no cross-container database access from a worker. Gate:
script is repeatable and cleans its named test resources on success/failure.

Container health checks are deliberately liveness checks only: gateway `GET
/health` and task-server `GET /health` prove their HTTP processes respond. The
smoke script is the readiness gate: it also verifies the authenticated control API,
the shared database path, and the static UI. This avoids falsely declaring the
stateful gateway ready based solely on a process response.

### Stage 6 — operator integration validation

Run the bundle locally without touching existing PM2 services or live state; then
run a remote-worker validation on a second machine supplied by the operator. Gate:
worker registers, claims a deliberately safe test workload, survives controller
restart according to existing lease semantics, and reports an honest terminal
result. Paid provider calls require explicit operator-provided credentials and are
never run automatically.

## 9. Release criteria

The Docker bundle is production-ready only when all stages pass, image digests are
published, rollback and restore have been exercised, and the two-machine validation
has been recorded. Automatic controller failover remains excluded until a separate
design establishes a replicated canonical store, single-writer leader election,
lease fencing, and failure-mode tests.

## 10. Adversarial review (Stage 0, completed before implementation)

| Finding | Risk if ignored | Resolution incorporated above |
|---|---|---|
| Gateway currently starts a local worker pool and can run unpinned tasks locally. | A supposedly controller-only image either fails late due to absent CLIs or executes work in an unintended container. | Add `GATEWAY_LOCAL_EXECUTION_ENABLED`, default true for compatibility; Compose controller uses false and rejects unpinned work. |
| Explicit `AI_TEAM_ENV_FILE` is authoritative for managed keys. | Splitting managed non-secrets into Compose `env_file` silently drops them at startup. | Use one complete role environment file mounted as a Compose secret; never rely on a second source for managed values. |
| The app creates `tasks`, `results`, `summaries`, and `logs` relative to its working directory. | `read_only` runtime either crashes or leaks writes into an image layer. | Mount all five mutable directories from the host and set the remaining filesystem read-only. |
| The task-server upload staging root is hard-coded below repository `state/uploads`. | A separate state mount or a worker-accessible controller DB would break staging or weaken isolation. | Gateway and task-server share only the controller-local `/app/state` mount; workers never receive it. |
| Existing health endpoints are liveness-oriented. | Compose can mark a broken deployment healthy before API/database integration works. | Keep health checks narrow; make authenticated API and persistence checks mandatory in smoke and release gates. |
| A static shared mesh token remains a shell-equivalent secret. | Containerisation could be mistaken for node identity or authorization hardening. | Docker docs state the limitation and require per-role 0600 secret files; per-node credentials remain a prerequisite for a stronger mesh trust model. |
| SQLite has one canonical writer domain. | Automatic promotion after a network partition can create dual active controllers and corrupt task truth. | Exclude failover; require a future leader/fencing/replicated-store design. |
