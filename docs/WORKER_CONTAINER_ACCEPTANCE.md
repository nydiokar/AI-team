# Worker container acceptance baseline (A85)

**Status:** boundary mapped + image/harness/tests landed. The **executable
acceptance gate is DEFERRED to a docker-capable environment** — see
[Deferred gate](#deferred-gate-what-a-docker-host-must-run). Authored
2026-09-25. Branch `feat/worker-container-acceptance`.

> This document is deliberately honest about what was proven where. Everything
> under [Static invariants](#static-invariants-proven-here-no-docker) was run
> in a docker-free environment and passes. Everything under
> [Deferred gate](#deferred-gate-what-a-docker-host-must-run) was **not** run
> here because docker/podman is not installed in the authoring environment; the
> code exists so an operator can run it verbatim.

---

## 1. Current worker launch / deploy boundary (mapped from `main`)

Sources read: `Dockerfile`, `compose.yaml`, `deploy/compose.worker.yaml`,
`deploy/docker-entrypoint.sh`, `deploy/worker.env.example`,
`deploy/worker.compose.env.example`, `deploy/controller.env.example`,
`pyproject.toml`, `constraints.txt`, `src/worker/agent.py`,
`src/worker/config.py`, `src/backends/registry.py`,
`src/backends/codex_native.py`, `src/backends/codex_app_server.py`.

### 1.1 Image topology

The `Dockerfile` is multi-stage:

| Stage | Base | Purpose |
|---|---|---|
| `node-runtime` | `node:22-bookworm-slim` | Node toolchain source |
| `web-build` | `node-runtime` | Builds the web UI with pnpm |
| `runtime` | `python:3.11-slim-bookworm` | Controller/gateway image; installs git+tini+curl, a venv at `/opt/venv`, the Python package via `pip install -c constraints.txt .`, and the built web dist. Creates the non-root `ai-team` user (uid/gid **10001**), `chown -R ai-team /app`. `ENTRYPOINT` is `tini -- docker-entrypoint`; default `CMD` `python main.py`. |
| `worker-agents` | `runtime` | Adds the Node toolchain (`COPY --from=node-runtime /usr/local`) and the coding-agent runtimes (`pnpm`, `@anthropic-ai/claude-code`, `@openai/codex`) via one global `npm install`. `CMD` `python worker_main.py`. |

The worker is the **`worker-agents` target**. `deploy/compose.worker.yaml`
builds `target: worker-agents` and runs `python worker_main.py`.

### 1.2 Runtime install authority

- **Python deps:** single authority = `pyproject.toml` (loose ranges) pinned by
  `constraints.txt` (exact prod stack, e.g. `claude-agent-sdk==0.2.110`,
  `fastapi==0.141.1`). Installed once at build time (`pip install -c
  constraints.txt .`). There is **no** `requirements.txt`.
- **Coding-agent runtimes:** `pnpm`, `@anthropic-ai/claude-code`,
  `@openai/codex`, installed globally via `npm install --global` at build time
  in the `worker-agents` stage. Nothing is installed into a *running*
  container; the entrypoint installs nothing.

### 1.3 Volume / state paths

From `deploy/compose.worker.yaml` (per-worker, keyed by `WORKER_NODE_ID`, under
`${DOCKER_DATA_ROOT}/workers/${WORKER_NODE_ID}/`):

| Container path | Host bind | Role |
|---|---|---|
| `/app/state` | `.../state` | worker runtime state; **also the uid-source** (see 1.5) |
| `/app/logs` | `.../logs` | logs |
| `/app/tasks` | `.../tasks` | task spool |
| `/app/results` | `.../results` | result spool |
| `/app/summaries` | `.../summaries` | summaries |
| **`/app/.codex`** (`CODEX_HOME`) | `.../codex` | **dedicated persistent Codex auth/runtime volume** |
| **`/app/.claude`** | `.../claude` | dedicated persistent Claude auth/runtime volume |
| `${WORKER_PROJECTS_ROOT}` | same host path | **read-write project mount** (the repos the worker edits) |

`HOME=/app` and `CODEX_HOME=/app/.codex` are set both in the compose
`environment:` and exported by `deploy/docker-entrypoint.sh`. Codex auth is
bootstrapped with `codex login --device-auth` **into the persistent
`CODEX_HOME` volume** — never baked into the image, never copied from the host
(`deploy/worker.env.example`).

### 1.4 Project mounts

`WORKER_PROJECTS_ROOT` (application variable; also `WORKSPACE_ROOT` to Compose,
same absolute path) is bind-mounted read-write at the identical path inside the
container. `src/worker/config.py` reads `WORKER_PROJECTS_ROOT`, scans it for
repos (`list_repos()`), and uses it as the default job cwd
(`src/worker/agent.py` job cwd resolution). `CLAUDE_BASE_CWD` /
`CLAUDE_ALLOWED_ROOT` point at the same root.

### 1.5 Identity / permissions (uid-autodetect entrypoint)

`deploy/docker-entrypoint.sh` runs briefly as root (compose grants only
`SETUID`+`SETGID` on top of `cap_drop: ALL`, plus `no-new-privileges:true`,
`read_only: true`) and then `setpriv`-drops permanently to:

- `APP_UID`/`APP_GID` if set, else
- the **owner of `/app/state`** (`stat -c %u /app/state`), else
- the image `ai-team` user (**10001**).

Root is explicitly rejected: `uid=0 → 10001`, `gid=0 → 10001`. Deriving the uid
from the mounted state dir owner is what lets a bind-mounted host repo (owned by
some host uid, e.g. 1000) stay writable by the worker — this is the "uid
autodetect / entrypoint drops to `/app/state` owner" behaviour recorded in
`.ai/CONTEXT.md` (2026-09-25). `GIT_CONFIG_*` sets `safe.directory=*` so git
works across the uid boundary on bind-mounted repos.

### 1.6 Gateway registration (from `src/worker/agent.py`, `src/worker/config.py`)

`WorkerConfig.from_env()` reads `WORKER_NODE_ID`, `WORKER_TOKEN`,
`WORKER_TAILSCALE_IP`, `CONTROLLER_URL`, `WORKER_BACKENDS` (CSV),
`WORKER_API_PORT` (9001), `WORKER_MAX_CONCURRENT` (2), `WORKER_PROJECTS_ROOT`,
`WORKER_ACCEPT_UNPINNED`. The daemon:

- **Registers** once via `POST /nodes/register` (retry with backoff until
  success), advertising backends, max_concurrent, projects_root, repo catalog,
  models.
- **Heartbeats** every 30s via `POST /nodes/heartbeat`; re-registers on a 404.
- **Claim/result loop:** polls `GET /tasks/pending` (adaptive 5→30s backoff),
  claims via `POST /tasks/{id}/claim` (409 = lost the race, skip), posts
  `POST /tasks/{id}/result` with retry until accepted, releases the slot; on
  SIGTERM posts `/tasks/{id}/release`.
- **Backends** built by `src/backends/registry.build_backends()`: `claude` →
  `ClaudeCodeBackend`, `codex` → `CodexBackend` (spawns `codex app-server` via
  `shutil.which("codex")`, JSON-RPC through
  `src/backends/codex_app_server.CodexAppServerClient`), plus opencode variants.
- **Git** is touched only by inspect ops (`git_status`, `commit`, `commit_all`
  via `GitAutomationService`), always scoped to the task `repo_path`.

### 1.7 Smallest compatible container boundary

The production worker needs only:

1. The **`worker-agents` image** (Python venv + Node coding-agent runtimes,
   baked at build time).
2. A **read-write bind mount** of the project root, owned such that the
   uid-autodetect entrypoint can write it.
3. **Persistent volumes** for `/app/.codex` (`CODEX_HOME`) and `/app/.claude`
   so backend auth survives recreation; plus state/logs/tasks/results/summaries.
4. **Egress** to `CONTROLLER_URL` and to the backend providers.
5. **No host docker socket**, **no host-global Codex**, no privileged mode —
   `cap_drop: ALL` + `SETUID/SETGID` + `no-new-privileges` + `read_only` with a
   writable `/tmp` tmpfs is sufficient.

---

## 2. Worker image definition (this change)

Minimal refinement of the existing `worker-agents` stage — **not** a duplicate
Dockerfile (RESERVED R1: reuse the existing deploy convention). Change:

- The three coding-agent runtime versions were **literals inside a `RUN npm
  install` line** (`@openai/codex@0.156.1`, `@anthropic-ai/claude-code@2.1.281`,
  `pnpm@10.30.2`) — not machine-trackable by Renovate and impossible to assert
  requested-vs-actual against.
- They are now **Renovate-readable Docker build ARGs** with
  `# renovate: datasource=npm depName=...` annotations, consumed by the `RUN`
  line as `@openai/codex@${CODEX_VERSION}` etc. Renovate's `regexManagers` /
  built-in ARG support can now bump them via PR.
- The requested versions are recorded into image `ENV`
  (`AI_TEAM_REQUESTED_CODEX_VERSION`, `..._CLAUDE_CODE_VERSION`,
  `..._PNPM_VERSION`) so a *running* container can report requested-vs-actual
  without the build context. An optional `AI_TEAM_GIT_SHA` build-arg records
  image identity.

Unchanged and preserved: non-root `ai-team` (uid 10001); Python deps via
`pyproject.toml`/`constraints.txt` (no second authority); no runtime installs;
the uid-autodetect entrypoint; all volumes and security options.

> Enabling Renovate to actually open these PRs (the Renovate config/rollout) is
> **A86 scope-out** and is deliberately NOT added here. This change only makes
> the versions *readable*; no automation is wired.

---

## 3. Static invariants (proven here, no docker)

`tests/test_container_acceptance.py` parses the Dockerfile / compose / entrypoint
/ harness and asserts, with **no docker and no build**:

- non-root `ai-team` user created; entrypoint drops via `setpriv` and rewrites
  uid/gid 0 → 10001; compose cannot regain privileges (cap_drop ALL, only
  SETUID/SETGID, no-new-privileges);
- Codex **and** Claude Code pinned as **Renovate-annotated build ARGs** and
  consumed by the RUN line; **no** hardcoded `pkg@x.y.z` literal remains;
  requested versions recorded in ENV;
- Python deps come from `pip install -c constraints.txt .`; **no**
  `requirements.txt`; entrypoint installs nothing at container start;
- dedicated persistent volumes back `CODEX_HOME` (`/app/.codex`) and
  `/app/.claude`; project root mounted read-write;
- the harness announces a loud docker-absent skip and **exits non-zero**, and
  asserts requested-vs-actual Codex, non-root exec, and CODEX_HOME persistence.

Run:

```
PYTHONPATH=/tmp/tvenv/lib/python3.11/site-packages /opt/venv/bin/python \
  -m pytest tests/test_container_acceptance.py -q -p no:cacheprovider
```

Result at authoring: **17 passed**. The harness skip-guard was also exercised
here (no docker): it printed `SKIPPED: docker unavailable (deferred gate)` and
exited `2`.

---

## 4. Deferred gate — what a docker host must run

`scripts/container_acceptance.sh` is the **executable** acceptance gate. It was
**NOT run in the authoring environment** because docker/podman is not installed
there; it refuses to fake success (loud skip, exit 2). On a docker-capable host,
run from a clean checkout of this branch:

```
scripts/container_acceptance.sh
```

It proves (fail-loud, real container):

1. **Reproducible build** of `worker-agents` from the checkout, tagged with the
   git SHA (ACCEPTANCE 1).
2. **Runtime inventory:** image git SHA, requested-vs-actual Codex, actual
   Claude Code CLI version, `claude-agent-sdk` version, Python/Node versions —
   and **asserts requested == actual Codex** (ACCEPTANCE 1).
3. **Non-root execution** via the real entrypoint (fails if uid 0)
   (ACCEPTANCE 2).
4. **Project mount writable + git** (`git init/add/commit`) on a bind mount
   (ACCEPTANCE 2).
5. **CODEX_HOME persistence across recreation:** writes a marker in one
   container, destroys it, remounts the same named volume in a fresh container,
   asserts the marker survived (ACCEPTANCE 3).
6. **In-image adapter smoke (deterministic, no paid calls):** `codex app-server
   --help` present; `CodexAppServerClient`, `claude_agent_sdk`, and the worker
   agent module import inside the image (ACCEPTANCE 2 protocol/import subset).

`--inventory-only` runs just the build + inventory + version assertion.

---

## 5. Recreation / auth / resume — operator-gated (RESERVED R2)

What the harness proves without credentials: the **state volume** (`CODEX_HOME`)
survives container recreation (§4.5). This is the structural precondition for
auth persistence — an existing `auth.json` written by `codex login
--device-auth` into `/app/.codex` will be present after recreation because the
same named volume is remounted.

What is **NOT** proven here and must NOT be claimed as working:

- **Authenticated backend calls** (a real Codex `initialize`/`model/list` turn,
  a real Claude turn). These require live provider credentials and would incur
  cost. They are an **operator gate**, never asserted (RESERVED R2). Missing
  credentials are never a reason to assert readiness.
- **Session / thread resume across recreation.** Codex exposes
  `thread/start` / `thread/resume` (`src/backends/codex_app_server.py`) and
  Claude backends support `resume_session`, but whether a thread/session
  started before recreation can be resumed after depends on **provider-side**
  retention, not just the local volume. The intended verification: bootstrap
  auth into `/app/.codex`; start a thread; recreate the container against the
  same volumes; attempt `thread/resume` with the persisted thread id; record the
  **observed** verdict. Until an operator runs this with real credentials, the
  image **does not claim resume works**. The observed limit must be documented
  here after the operator run.

**No live worker was changed.** No image was built or run in the authoring
environment. No production switch, worker restart, registry push, or Renovate
automation was performed (A86 scope-out).
