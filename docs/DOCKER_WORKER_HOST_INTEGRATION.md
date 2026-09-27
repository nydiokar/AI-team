# Docker Worker/Host Integration — Owner Report & Canonical Architecture

**Owner:** Docker-migration integration owner
**Date:** 2026-09-25
**Status:** Architecture LOCKED. One code change implemented + unit-tested (MCP path-neutral
launcher). Compose/entrypoint/projection redesign specified as review-ready diffs; **live
acceptance is operator-gated** (see §13 & §18 — the sandbox this was authored in has no Docker
daemon and paid Claude/Codex calls are barred by the project TEST COST GUARD).

This document is the single coherent worker/host integration design that all Docker-related point
fixes and branches must reconcile to. It supersedes ad-hoc mount lists in `deploy/` and the
scattered assumptions in `docs/DEPLOYMENT_DOCKER_DESIGN.md`.

---

## 0. TL;DR for the operator

The Docker migration silently changed a **worker** from *"run tools on this machine as this user"*
into *"run tools inside a synthetic machine with its own HOME, its own Claude/Codex login, and its
own config."* That second identity is the root of the regressions.

- **Already fixed and aligned** (commit `11c8e18`, branch `feat/finish-docker-controller-worker-boundary`):
  quota is now observed on the worker and POSTed to the controller; activity forwarding is an
  explicit transport with no network→filesystem inference. **PRESERVE.**
- **Still broken — the core of this job:** the worker runs with `HOME=/app`, `CODEX_HOME=/app/.codex`,
  and mounts **private** `workers/<id>/claude` + `workers/<id>/codex` profiles that are *not* the
  host's. That is a second Claude/Codex identity created by accident. MCP is registered with a
  host-absolute `<python> <abs script>` command that cannot resolve identically on host and in the
  container.
- **This report** locks the fix: a **Host User Environment Projection** (bind-mount the host's
  authoritative Claude/Codex/Git state into a stable container HOME; isolate only the
  concurrency-unsafe runtime files), and a **PATH-resolvable MCP launcher** so one platform-neutral
  config works everywhere.
- **Implemented now (safe, unit-tested):** the MCP launcher (`ai-team-mcp-jobs` /
  `ai-team-mcp-manager`) on branch `feat/docker-mcp-path-neutral`.
- **Operator-gated:** the compose/entrypoint/projection cutover and the entire §18 runtime
  acceptance matrix (destroy/recreate worker, concurrent live Claude, paid quota) — because they
  need a Docker host + paid backends, and the project rules forbid me from recreating a worker or
  spending on paid CLIs unilaterally.

---

## 1. Previous architecture and why it failed

The migration split the monolith (`ai-team-gateway` PM2 process, backends co-located) into three
container roles built from one image:

| Role | Dockerfile stage | Command | Owns |
|---|---|---|---|
| gateway | `runtime` | `python main.py` | Control API :9003, orchestration |
| task-server | `runtime` | `python server_main.py` | Mesh :9002, `mesh.db` |
| worker | `worker-agents` | `python worker_main.py` | Claude/Codex CLIs, task execution |

**Why it broke:** the split was done as *"make a stateless container run the process"*, so the
worker container was given `HOME=/app` and **its own** persistent `.claude`/`.codex` volumes under
`DOCKER_DATA_ROOT/workers/<id>/`. Nothing projected the host's real identity in, so the container
became an independent synthetic machine. Concretely:

1. **Activity streaming** stuck on "Working…": the worker inferred *"controller URL looks local ⇒
   we share `events.ndjson`"* and silently disabled HTTP forwarding — but two containers never share
   a filesystem. (Fixed in `11c8e18`.)
2. **Quota empty**: the quota observer ran controller-side, but the controller image (`runtime`
   stage) has no Claude binary, no `~/.claude`, no credentials — by design. (Fixed in `11c8e18`:
   observe on the worker, POST to controller.)
3. **Manager MCP missing**: MCP is registered in `~/.claude.json` as `command=<host python>`,
   `args=[<host repo>/scripts/mcp_manager.py]`. Those absolute paths don't exist in the container,
   and the container's private `~/.claude.json` (never the host's) had no registration at all.
4. **UID/GID**: containers dropped to uid 10001 while host repos are uid 1000 → read-only repos.
   (Fixed in `d043013`: derive uid/gid from the mounted state-dir owner.)

The common cause of (1)-(3) is the **synthetic-identity semantics**, not four unrelated bugs.

---

## 2. Final worker/host/container ownership model (canonical)

```
HOST (physical machine)            CONTAINER (managed execution env)      CONTROLLER (gateway+task-server)
─────────────────────────          ─────────────────────────────         ────────────────────────────────
• human/backend identity           • pinned Claude CLI  (2.1.281)         • orchestration / scheduling
• Claude auth  (~/.claude          • pinned Codex CLI   (0.156.1)         • canonical app state (mesh.db)
   /.credentials.json, .claude.json)• pinned pnpm / node / python deps    • quota_windows.db (ingest)
• Codex auth  (~/.codex/auth.json) • worker daemon (worker_main.py)       • telemetry after ingestion
• user Claude/Codex settings       • worker-private runtime state:        • control-only persistence
• user MCP registrations              queues, tmp, caches, logs           • NO Claude/Codex binary
• Git identity (~/.gitconfig)      • ephemeral per-turn state             • NO Claude/Codex credentials
• repositories / workspaces        • (executes as the HOST uid/gid)       • NO worker HOME
• machine-specific secrets
```

**Rule:** *Executables are container-owned; identity/config/state are host-authoritative where
appropriate.* The container supplies **how to run**; the host supplies **who runs and on what**.

The worker container must **never** create a second Claude/Codex/user environment. A different
container pathname (e.g. `/home/worker/.claude`) is acceptable **only** when it is a bind mount of
the same host data — not an independent copy.

---

## 3. HOME / user-profile audit (current vs desired)

Runtime facts established by direct inspection of a live `/app`-HOME worker environment and a
read-only code sweep (`src/`, `scripts/`, `config/`).

**Current identity/HOME semantics:**
- Worker `HOME=/app`, `CODEX_HOME=/app/.codex` — hardcoded in `deploy/docker-entrypoint.sh:4-5` and
  `deploy/compose.worker.yaml:17-18`.
- Controller `HOME=/app`, no `.claude`/`.codex` mounts (correct — must stay so).
- Runtime user: derived from the owner of the mounted `/app/state` (entrypoint `stat -c %u`),
  overridable via `APP_UID`/`APP_GID`; never root; fallback 10001. (Good — keep.)
- Backend subprocess inherits the worker env (`src/backends/claude_code.py:443` `_build_proc_env`
  starts from `os.environ`), so it reads whatever `HOME`/`CODEX_HOME` the worker has.

**Resource table** — `resource | host source | current container path | authoritative owner |
shared/copied/private | mutable? | concurrency risk | desired model`:

| resource | host source | current container path | authoritative owner | shared/copied/private | mutable | concurrency risk | desired model |
|---|---|---|---|---|---|---|---|
| HOME | `/home/<user>` | `/app` | host (identity) | private | — | — | stable `/home/worker`, populated by projection |
| Claude auth | `~/.claude/.credentials.json` | `/app/.claude/.credentials.json` (private vol) | **host** | **private (FORK)** | refreshed on token rotation | low (rarely written) | **bind-mount host** `.credentials.json` |
| Claude config/settings | `~/.claude/settings.json`, `~/.claude.json`* | private vol | **host** | **private (FORK)** | yes | med (single-file rewrite) | bind-mount host; see §6 |
| Claude MCP registry | `~/.claude.json` `mcpServers` | private vol | **host** | **private (FORK)** | yes | med | host-authoritative + PATH-neutral cmd (§7) |
| Claude project/session history | `~/.claude/projects`,`sessions` | private vol | host | private | yes | low (per-project files) | bind-mount host `.claude` dir |
| Claude churny runtime | `~/.claude/shell-snapshots`,`todos`,`statsig` | private vol | container | private | yes | **high if shared** | isolate per-worker (tmpfs/overlay) |
| Codex auth | `~/.codex/auth.json` | `/app/.codex/auth.json` (private vol) | **host** | **private (FORK)** | refreshed | med (rewrite on refresh) | bind-mount host `auth.json` |
| Codex config | `~/.codex/config.toml` | private vol | **host** | **private (FORK)** | yes | low | bind-mount host `config.toml` |
| Codex SQLite runtime | `~/.codex/{logs_2,queue_1,memories_1,goals_1}.sqlite(+wal/shm)`, `gateway-ownership.sqlite3` | private vol | container | private | yes | **CRITICAL if shared** (WAL cross-process corruption) | **isolate per-worker** — do NOT share |
| Codex sessions/cache | `~/.codex/sessions`, `cache` | private vol | container | private | yes | med | isolate per-worker |
| Git identity | `~/.gitconfig` | (none; `safe.directory=*` only) | **host** | absent | yes | low | bind-mount host `.gitconfig` (read-only) |
| SSH | `~/.ssh` | (none) | host | absent | — | — | opt-in read-only bind if repos use SSH remotes |
| Workspaces/repos | `$WORKER_PROJECTS_ROOT` | same path (rw bind) | host | shared bind | yes | n/a (host FS) | keep; central resolver (§8) |
| tmp | — | `/tmp` tmpfs 1g | container | private | yes | none | keep |
| logs/state/tasks/results/summaries | `DOCKER_DATA_ROOT/workers/<id>/*` | `/app/*` | container (worker-private) | private bind | yes | none | keep |

\* This Claude build (2.1.281) keeps config under `~/.claude/`; `~/.claude.json` may be absent.
The projection must handle both: mount `.claude/` and, if present, `.claude.json`.

**Every place host state has already forked into container-private state:** the four **FORK** rows
above — Claude auth, Claude config+MCP, Codex auth, Codex config — all mounted from
`workers/<id>/{claude,codex}` instead of the host. This is the migration's core defect.

**Was `/app` chosen because it correctly models the worker?** No. `/app` is the WORKDIR of a
stateless image (Dockerfile `WORKDIR /app`, user home-dir `/app`). Making it HOME was convenient for
a stateless container, not a decision that the worker *is* the host user. It should be a stable
`/home/worker` whose identity files are projections of the host.

---

## 4. Host User Environment Projection (the one mechanism)

One explicit, declarative mechanism replaces the ad-hoc private-profile mounts. The container uses a
stable internal HOME `/home/worker`; its **authoritative** user files are bind mounts of host files.

**Logical variables (identical names on every platform; values differ per host `.env`):**

| variable | Linux example | Windows (Docker Desktop) example | projected to |
|---|---|---|---|
| `HOST_CLAUDE_DIR` | `/home/cifran/.claude` | `C:\Users\me\.claude` | `/home/worker/.claude` |
| `HOST_CLAUDE_JSON` | `/home/cifran/.claude.json` | `C:\Users\me\.claude.json` | `/home/worker/.claude.json` (if present) |
| `HOST_CODEX_DIR` | `/home/cifran/.codex` | `C:\Users\me\.codex` | `/home/worker/.codex` (see §6 isolation) |
| `HOST_GITCONFIG` | `/home/cifran/.gitconfig` | `C:\Users\me\.gitconfig` | `/home/worker/.gitconfig` (ro) |
| `HOST_WORKSPACE_ROOT` | `/srv/worker-projects` | `C:\dev` | `CONTAINER_WORKSPACE_ROOT` |
| `HOST_UID` / `HOST_GID` | `1000`/`1000` | (Docker Desktop maps) | entrypoint drop target |

**Invariants of the projection:** one authoritative source; no sync jobs; no copy-on-start /
copy-back; no parallel editable configs. A bind mount means the container edits the *same bytes* the
host reads — not a second config.

**Cross-platform:** works Linux→Linux and Windows/Docker-Desktop→Linux because Docker Desktop
bind-mounts Windows paths into the Linux VM. The **logical** variable names are identical; only the
host-side path syntax differs, carried in platform-specific `.env`/override files (§16).

---

## 5. Backend integration contract (matrix)

| field | Claude Code | Codex | MCP (jobs+manager) | Git |
|---|---|---|---|---|
| container executable | `@anthropic-ai/claude-code@2.1.281` (Dockerfile `worker-agents`) | `@openai/codex@0.156.1` | n/a (stdio server via launcher) | `git` (apt, Dockerfile `runtime`) |
| version policy | pinned in image; `CLAUDE_SDK_CLI_PATH` can override | pinned in image | ships with repo | distro pin |
| host-authoritative auth path | `~/.claude/.credentials.json` | `~/.codex/auth.json` | (uses gateway tokens via .env) | n/a |
| host-authoritative config path | `~/.claude/settings.json`, `~/.claude.json` | `~/.codex/config.toml` | `~/.claude.json` `mcpServers`, `~/.codex/config.toml` `[mcp_servers.*]` | `~/.gitconfig` |
| mutable persistent state | projects/, sessions/ | (none host-shared) | registration entries | n/a |
| runtime-only state | shell-snapshots, todos, statsig | *.sqlite(+wal), sessions/, cache/ | process stdio | n/a |
| env vars | `HOME` | `HOME`, `CODEX_HOME` | `AI_TEAM_SCRIPTS_DIR` (launcher), gateway `.env` | `GIT_CONFIG_*`/`safe.directory=*` |
| workspace requirements | cwd within `CLAUDE_ALLOWED_ROOT` | cwd within workspace | reaches controller over HTTP | operates in repo |
| MCP/config interaction | reads `mcpServers` from `~/.claude.json` | reads `[mcp_servers.*]` from config.toml | **must be PATH-neutral cmd** | n/a |
| concurrency characteristics | single-file config rewrite (med) | **SQLite WAL cross-process (critical)** | stateless per call | append (low) |
| host bridge requirements | none (runs in container) | none | none | none |
| Windows differences | host path `C:\Users\...`; creds may be in Credential Manager, not a file | same | launcher is `.exe` shim on PATH | CRLF/autocrlf |
| Linux differences | `.credentials.json` is a file | `auth.json` is a file | launcher is venv console script | standard |

> **Concurrency call-out (Codex):** `~/.codex/*.sqlite` use WAL and are **not** safe for two OS
> processes (host Codex + worker Codex) to open for write simultaneously — cross-process WAL is a
> corruption risk. These runtime DBs are **isolated per worker** (§6). `auth.json` + `config.toml`
> remain host-authoritative (shared), because they are the identity/config, not runtime scratch.

---

## 6. Claude & Codex: one logical user environment

**Decision (grounded in the live `~/.codex` / `~/.claude` layout inspected for this report):**

Share the **authoritative identity/config**; isolate **only** the concurrency-unsafe runtime
components — never fork the whole profile.

**Claude** — bind-mount `HOST_CLAUDE_DIR` → `/home/worker/.claude` and `HOST_CLAUDE_JSON` (if
present). Authoritative: `.credentials.json`, `settings.json`, `.claude.json` (MCP + config).
Isolate the churny runtime dirs (`shell-snapshots/`, `todos/`, `statsig/`) per worker via a tmpfs or
per-worker overlay so interactive-host and worker Claude don't fight over scratch files. If a future
Claude build proves that a *single shared file* is rewritten destructively under concurrency, treat
**that one file** as a deterministic ephemeral projection (§7), not the whole dir.

**Codex** — bind-mount only `auth.json` and `config.toml` from `HOST_CODEX_DIR` (host-authoritative).
Give the container a **per-worker** `CODEX_HOME` for everything else so the SQLite/WAL runtime
(`logs_2`, `queue_1`, `memories_1`, `goals_1`, `gateway-ownership.sqlite3`, `sessions/`, `cache/`) is
private. This satisfies "isolate ONLY the specific unsafe runtime component" without forking auth.

**Interactive host Claude/Codex and worker Claude/Codex may run simultaneously** — this is the
explicit falsifier in the §18 "Concurrent use" acceptance test (operator-gated: needs paid backends
+ a Docker host). The design above is built to pass it; only that live run can confirm it.

---

## 7. Cross-platform config & absolute paths — MCP (IMPLEMENTED)

**Problem:** MCP was registered as three incompatible configs for one logical tool:
```
Linux : command=/home/cifran/.venv/bin/python  args=[/home/cifran/dev/AI-team/scripts/mcp_manager.py]
Docker: (host paths above — do not exist in the container)
Windows: command=C:\...\python.exe  args=[C:\...\scripts\mcp_manager.py]
```

**Fix (shipped on `feat/docker-mcp-path-neutral`):** register a **stable, PATH-resolvable command
name** — `ai-team-mcp-jobs` / `ai-team-mcp-manager` — with empty args. Each environment provides that
command on its own PATH:

- `pyproject.toml` `[project.scripts]` maps the names to `src/mcp_launchers.py:{jobs,manager}_main`.
  `pip install .` (already run in the Dockerfile `runtime` stage) installs them into the venv `bin`,
  which is first on the container PATH (`/opt/venv/bin`). On a host that `pip install`s the package
  they land on the host PATH; on Windows they become `.exe` shims.
- `src/mcp_launchers.py` locates `scripts/mcp_{jobs,manager}.py` platform-neutrally: `AI_TEAM_SCRIPTS_DIR`
  override → repo walk-up (`pyproject.toml` marker) → `<cwd>/scripts` → `/app/scripts`. The stdio
  server logic is unchanged and remains the single source of truth.
- `scripts/setup_mcp.py` now writes the bare launcher name for Claude, Codex, and OpenCode instead of
  `sys.executable` + absolute path.

Result: the **same** `~/.claude.json` / `config.toml` works whether used on the host or bind-mounted
into the container — one authoritative registration, no path rewrite layer, no Docker-only registry.

For any third-party backend that *forces* an absolute path, the escape hatch (§16) is ONE canonical
declarative source generating a **deterministic, non-authoritative, non-persisted** runtime
projection — used only where PATH-neutral config is impossible. Not needed for jobs/manager.

**Unit-tested:** `tests/test_mcp_path_neutral.py` (5 tests, green) asserts each backend registration
is a bare launcher name (not `sys.executable`, not absolute) and that the launcher resolves the
scripts dir. **Not yet live-validated:** that the console script is actually on PATH inside a built
container and that the manager MCP hands tools to a real session — that is the §18 "MCP" acceptance
item (operator-gated).

---

## 8. Workspace model

Repositories stay host-authoritative; workers operate on bind-mounted host workspaces. Do **not**
assume `host absolute path == container absolute path` except where deliberately guaranteed.

- Keep the existing app variable `WORKER_PROJECTS_ROOT` as the single source of truth, and mount it
  at the **same path** on Linux (`${WORKER_PROJECTS_ROOT}:${WORKER_PROJECTS_ROOT}:rw`) so
  `CLAUDE_BASE_CWD`/`CLAUDE_ALLOWED_ROOT` (which reference it) line up. This equality is *deliberate
  and platform-specific* — it holds on Linux only.
- On Windows, host paths (`C:\dev`) cannot equal the container path. Use explicit
  `HOST_WORKSPACE_ROOT` → `CONTAINER_WORKSPACE_ROOT` (e.g. `/workspaces`) and set
  `CLAUDE_BASE_CWD`/`CLAUDE_ALLOWED_ROOT` to the **container** root. `src/services/path_resolver.py`
  is the central resolver; no subsystem should reinvent translation.
- Because MCP config is PATH-neutral (§7), repository mount paths never leak into global user config.

---

## 9. UID/GID and ownership

- **Linux (keep the merged fix `d043013`):** the entrypoint drops to the owner of the mounted state
  dir (defaults to host repo owner, e.g. uid 1000), overridable with `APP_UID`/`APP_GID`, never root.
  This makes bind-mounted repos writable without leaving root-owned files and without `chmod 777`.
  Projected identity files must be owned by / readable by that uid on the host.
- **Windows/Docker Desktop:** the Linux-uid model does not apply; Docker Desktop handles the
  host↔VM permission mapping and bind mounts are effectively world-accessible inside the VM. Document
  this difference explicitly rather than assuming Linux semantics.

---

## 10. Secrets

- Claude/Codex credentials are **per physical worker** and stay on the host, projected at runtime by
  bind mount. They must never reach the controller, image layers, another worker, or another machine.
- **Never** `COPY` credential files during build; **never** bake credentials into the image. (The
  current Dockerfile does not — confirmed. Keep it that way.)
- `WORKER_TOKEN`/`DASHBOARD_TOKEN` come from the runtime `.env` (role env files, chmod 600), not the
  image. Reconcile with `feat/security-per-node-credentials` (§11) which replaces the shared token
  with per-node credentials.

---

## 11. Existing branches / parallel work — reconciliation

| branch / commit | what it does | verdict | reason |
|---|---|---|---|
| `11c8e18` (finish-docker-controller-worker-boundary) | quota observed on worker→POST controller; explicit activity transport; controller harness-availability from mesh, not local CLI | **PRESERVE** | Exactly the target model; removes locality inference; no forked identity introduced. |
| `feat/docker-production-bundle-main` (merged #162) | run as host uid; drop phantom self-node; nested `DOCKER_DATA_ROOT` | **PARTIAL — REWRITE the profile part** | uid/self-node parts are correct; the `workers/<id>/{claude,codex}` **private profile** mounts are the fork to remove (replace with projection §4). |
| `feat/docker-uid-autodetect` (merged #163) | derive uid/gid from state-dir owner | **PRESERVE** | Correct permission model; orthogonal to identity projection. |
| `feat/security-per-node-credentials` | per-node task-server credentials (A71) | **PRESERVE** | No MCP/identity impact; composes with §10. Land on its own track. |
| `feat/smarter-quota-resume` / `-clean` | quota-resume cost estimate + operator choice UI | **PRESERVE** | Orthogonal; controller-side; no Docker identity assumptions. |
| current `setup_mcp.py` host-absolute MCP registration | `<python> <abs script>` | **REWRITE (done)** | Replaced by PATH-neutral launcher (§7). |
| `deploy/compose.worker.yaml` `HOME=/app` + private `.claude`/`.codex` + "codex login inside Docker" ceremony | synthetic identity | **REJECT / REWRITE** | The forbidden second identity; replace with projection (§4/§6). |

**Rejected patterns confirmed present and to be removed:** separate persistent Docker Claude profile;
separate persistent Docker Codex profile; a Docker-only `codex login --device-auth` setup ceremony
(`deploy/worker.env.example:18-21`); container-private MCP.

---

## 12. Network / control-plane architecture

- **Explicit endpoints only** (already the shape after `11c8e18`): `CONTROLLER_URL` (worker→task
  server :9002, `WORKER_TOKEN`), `TELEMETRY_TASK_SERVER_URL` (→ :9002), MCP manager → Control API
  :9003 (`DASHBOARD_TOKEN`). Worker nudge listener :9001.
- **9002 vs 9003 ARE distinct trust boundaries** (confirmed in code): 9002 is the write-heavy
  mesh/data plane authenticated by `WORKER_TOKEN`; 9003 is the read-mostly control/UI plane
  authenticated by `DASHBOARD_TOKEN` with tailnet + DNS-rebind protection. **Do not expose 9003**
  publicly just because manager MCP needs it — the manager reaches it over the existing authenticated
  path. Keep 9003 published on `127.0.0.1` (Tailscale Serve proxies it).
- **Locality inference — remaining items to clean (audit §14):** `socket.gethostname()` is used for
  `claimed_by`/telemetry `node_id`/session `machine_id` defaults (`src/orchestrator.py` claim sites,
  `src/core/telemetry.py:80`, `src/services/session_store.py:51`). For a worker these are already
  overridden by explicit `WORKER_NODE_ID`; the residual risk is a container hostname (random id)
  leaking as identity when an explicit id is absent. **Recommendation:** require explicit
  `WORKER_NODE_ID`/gateway node id everywhere; treat a gethostname fallback as a logged degraded
  state, not silent. The gateway self-node phantom is already gated by
  `GATEWAY_LOCAL_EXECUTION_ENABLED=false`.

---

## 13. Reconcile the known failures under this architecture

- **Activity streaming — RESOLVED (preserve `11c8e18`).** Worker always forwards over the explicit
  HTTP transport; the only skip is the explicit opt-in `WORKER_SHARES_CONTROLLER_FS=1`; failures are
  counted + logged, not swallowed. No `events.ndjson` sharing.
- **Quota telemetry — RESOLVED (preserve `11c8e18`).** Observed worker-side with the projected
  host-authoritative Claude identity; POSTed to `/telemetry/quota-observation`; controller ingests
  via the existing coordinator with an injected `read_usage`. Controller needs no Claude
  binary/creds/HOME. Prewarm stays separate and OFF.
- **Manager MCP — DESIGN COMPLETE, launcher IMPLEMENTED (§7).** With the projection (§4) the worker
  Claude sees the host's authoritative `~/.claude.json` `mcpServers`, and the PATH-neutral launcher
  makes that registration resolve inside the container. No Docker-only registry. Role gating is
  unchanged (`src/backends/claude_driver.py` — manager tools require `case_role==manager` +
  `MANAGER_TOOLS_ENABLED`). Manager RPC reaches the controller via the Control API path (§12).
  *Live confirmation is the §18 "MCP" / "MCP manager" acceptance run — operator-gated.*

---

## 14. Locality-dependency audit (classified)

Full sweep performed over `src/`, `scripts/`, `config/`. Summary of real hits and classification:

**Identity/HOME/config:** `HOME`/`Path.home()`/`expanduser` reads in `claude_driver.py`,
`codex_native.py:156` (`CODEX_HOME`), `codex_ownership.py:20-22` (SQLite lock — **isolate**),
`opencode.py`, `scripts/setup_mcp.py`, `scripts/*_doctor.py`, `path_resolver.py` (pure expansion,
safe). All backend auth/config reads are **host-authoritative**; the SQLite ownership/runtime DBs are
**container-isolate**. No code silently *creates* a profile — the fork is entirely in the compose
mounts, which is the good news (fix is config-layer).

**Networking/IPC:** `socket.gethostname()` claim/telemetry/session sites (see §12); OpenCode
`server_host=127.0.0.1` default; Control API bind fallback chain; `telemetry_sink._http_target_is_colocated()`
loopback/own-IP inference (borderline — best-effort shadow-DB skip); `events.ndjson` is a
**single-process audit/SSE sink**, not cross-container IPC (confirmed: gateway writes, UI reads on the
same host; worker never ships its file). Canonical endpoint vars enumerated in §12.

**Verdict:** the remaining locality items are either already gated (`GATEWAY_LOCAL_EXECUTION_ENABLED`,
`WORKER_SHARES_CONTROLLER_FS`) or low-risk fallbacks. The one worth hardening is the
`gethostname`-as-identity fallback (§12). No *new* silent locality violation is introduced by this
design; none is fixed by "drive-by" — each is listed for a deliberate follow-up.

---

## 15. Source-of-truth proof (§19)

After this design there is exactly **one** authoritative source per logical setting:

| logical setting | single authoritative source | container access |
|---|---|---|
| Claude auth | host `~/.claude/.credentials.json` | bind mount (same bytes) |
| Claude config + MCP registry | host `~/.claude.json` / `~/.claude/settings.json` | bind mount |
| Codex auth | host `~/.codex/auth.json` | bind mount |
| Codex config + MCP | host `~/.codex/config.toml` | bind mount |
| Git identity | host `~/.gitconfig` | bind mount (ro) |
| MCP launch command | one PATH-neutral name resolved locally per platform | console script on PATH |
| workspaces | host FS via `WORKER_PROJECTS_ROOT` / `HOST_WORKSPACE_ROOT` | bind mount |
| Codex runtime SQLite | per-worker private (NOT a config) | container-owned volume |

No "host config + container config both independently editable for the same logical setting" remains
once the private `workers/<id>/{claude,codex}` mounts are replaced by the projection. The only
container-private state is genuine runtime scratch (queues, caches, SQLite runtime, logs, tmp) — which
is *not* configuration.

---

## 16. Compose/config redesign (review-ready; apply + validate on a Docker host)

Replace `deploy/compose.worker.yaml`'s identity section. Grouped, documented by purpose:

```yaml
# deploy/compose.worker.yaml — worker service (identity section)
services:
  worker:
    environment:
      # (A) container-owned runtime
      HOME: /home/worker
      # (D) worker-private Codex runtime (SQLite/WAL isolated from host)
      CODEX_HOME: /home/worker/.codex-runtime
      # (F) git works on foreign-uid bind mounts
      GIT_CONFIG_COUNT: "1"
      GIT_CONFIG_KEY_0: safe.directory
      GIT_CONFIG_VALUE_0: "*"
    volumes:
      # (B) PROJECTED host-authoritative user state — one source of truth, no copies
      - ${HOST_CLAUDE_DIR:?}:/home/worker/.claude
      - ${HOST_CODEX_AUTH:?}:/home/worker/.codex-runtime/auth.json:ro   # identity only
      - ${HOST_CODEX_CONFIG:?}:/home/worker/.codex-runtime/config.toml:ro
      - ${HOST_GITCONFIG:?}:/home/worker/.gitconfig:ro
      # (optional) - ${HOST_CLAUDE_JSON}:/home/worker/.claude.json
      # (C) workspaces
      - ${WORKER_PROJECTS_ROOT:?}:${WORKER_PROJECTS_ROOT}:rw
      # (D) worker-private state
      - ${DOCKER_DATA_ROOT:?}/workers/${WORKER_NODE_ID}/state:/app/state
      - ${DOCKER_DATA_ROOT:?}/workers/${WORKER_NODE_ID}/logs:/app/logs
      - ${DOCKER_DATA_ROOT:?}/workers/${WORKER_NODE_ID}/tasks:/app/tasks
      - ${DOCKER_DATA_ROOT:?}/workers/${WORKER_NODE_ID}/results:/app/results
      - ${DOCKER_DATA_ROOT:?}/workers/${WORKER_NODE_ID}/summaries:/app/summaries
    tmpfs:
      - /tmp:rw,nosuid,size=1g
      # isolate Claude churny scratch so host+worker don't fight (see §6)
      - /home/worker/.claude/shell-snapshots
      - /home/worker/.claude/statsig
```
Entrypoint change: set `HOME=/home/worker` and `CODEX_HOME=/home/worker/.codex-runtime` (not `/app`),
`mkdir -p` the private codex-runtime, keep the existing uid/gid drop. Note the projected `.claude`
must be owned by the drop uid on the host.

Platform overrides — identical logical vars, different host syntax:
- `deploy/worker.env.linux.example`: `HOST_CLAUDE_DIR=/home/<user>/.claude`, …
- `deploy/worker.env.windows.example`: `HOST_CLAUDE_DIR=C:\Users\<user>\.claude`, …

**No developer username is hardcoded** in the compose file — every host path is a variable.

> These files are delivered as a design here rather than committed live because I cannot build/run
> them in this sandbox (no Docker daemon); committing unvalidated compose that alters the live
> worker's identity mounts would violate "don't patch first and rationalize afterward." Apply on the
> operator's Docker host together with the §18 acceptance run.

---

## 17. Portability definition (target)

On a new worker machine: install Docker; configure Claude/Codex normally for the human **on the
host**; have repos on the host; set the `HOST_*` projection vars; `docker compose up`. The worker then
operates with that host's identity/config/workspaces — **no second Claude/Codex/MCP setup inside
Docker**, and recreation/upgrade never destroys user state (it lives on the host). The image supplies
execution dependencies; the host supplies identity and user-owned state.

---

## 18. Acceptance tests — status

**Automated / unit (runnable without paid backends) — DONE where marked:**
- ✅ MCP path-neutral registration + launcher resolution: `tests/test_mcp_path_neutral.py` (5 green).
- ⛔ (recommend) a compose-render lint (`docker compose config`) — needs Docker.

**Runtime acceptance matrix — OPERATOR-GATED (needs a Docker host + paid Claude/Codex; the project
TEST COST GUARD + "never recreate a worker unilaterally" rule and the absence of a Docker daemon in
this environment make these impossible for me to run):**

| test | how | gate |
|---|---|---|
| Recreate | destroy/recreate worker; Claude+Codex auth still work, MCP still registered, no manual init | operator |
| Shared config | change a harmless setting via host Claude; worker Claude sees it with no copy/sync/rebuild; inverse where safe | operator |
| MCP | register manager MCP via normal mechanism; interactive + worker Claude both see it; one registration; command resolves on both platforms | operator (launcher unit-verified) |
| Concurrent use | interactive host Claude while worker Claude runs; auth/settings/mutable-state integrity; Codex SQLite not corrupted | operator (§6 is built to pass) |
| Controller isolation | controller has no Claude/Codex profile/creds and needs no Claude/Codex binary | **partially provable now** — image `runtime` stage installs no CLI; `11c8e18` removed the controller-side observer/CLI probe |
| Activity | run task; multiple live transitions reach UI; no shared event file; forwarding failure observable | operator (design resolved in `11c8e18`) |
| Quota | worker obtains quota via machine identity; controller receives; UI shows; controller never authenticates Claude | operator (design resolved in `11c8e18`) |
| MCP manager | manager role gets tools; dispatch_worker/arm_wait_group work; non-manager session does not | operator (role gating unchanged) |
| Network | worker reaches controller services; no unnecessary public exposure; same-host + remote-worker | operator |
| Linux / Windows | full run on Linux worker; Windows host + Linux container projection | operator |

---

## 19. Remaining platform limitations (genuine technical constraints)

1. **No Docker daemon / no paid backends in the authoring sandbox** → all live acceptance is
   operator-gated. Not a design gap.
2. **Windows Claude credential storage** may use the Windows Credential Manager rather than a
   file-in-`~/.claude`; if so, file projection cannot carry auth and a Windows worker would need a
   host-bridge or a file-based credential export. To confirm on a Windows host during acceptance.
3. **Codex concurrent write semantics** across host+worker are mitigated by isolating the SQLite
   runtime (§6); the residual question is whether `auth.json` token refresh can race — low
   probability (rare writes) but must be observed in the "Concurrent use" test.

---

## 20. Failure visibility (§20)

Expose distinct capability/health states rather than silent degradation. `11c8e18` already made
activity-forward failures counted/logged and quota records adapter-UNAVAILABLE (with reason) distinct
from an empty window; controller `claude_available` derives from online mesh workers. **Follow-up**
(not yet coded): surface explicit per-integration states — `backend_executable_missing`,
`host_profile_unavailable`, `auth_unavailable`, `mcp_unavailable`, `controller_unreachable`,
`permission_failure`, `incompatible_profile`, `ready` — on the existing health/status surface. Listed
as a deliberate next task, not silently skipped.

---

## 21. Final cleanup (once acceptance passes)

Remove: private `workers/<id>/{claude,codex}` profile mounts; the "codex login inside Docker"
ceremony text; the `HOME=/app`/`CODEX_HOME=/app/.codex` worker settings; any Docker-only MCP
registration; obsolete locality heuristics superseded by explicit transports. Do not keep two
architectures "for safety" — the projection model becomes canonical.

---

## 22. Invariant PASS/FAIL

| # | invariant | status | note |
|---|---|---|---|
| A | one human/backend identity per physical worker | **DESIGN PASS / live-gated** | achieved by projection (§4/§6); fork removed in §16 |
| B | no independent container Claude/Codex/MCP identity except a forced isolated runtime component | **DESIGN PASS** | only Codex SQLite runtime isolated, with justification (§6) |
| C | container owns executable/runtime deps by default | **PASS** | Dockerfile pins CLIs/deps; confirmed |
| D | host owns identity/config/secrets/workspaces | **DESIGN PASS / live-gated** | §4/§10; needs the compose cutover |
| E | controller does not inherit worker backend identities | **PASS** | `runtime` image has no CLI; `11c8e18` removed controller observer/probe |
| F | no network identity used to infer FS/process locality | **PASS (core) / residual noted** | `11c8e18` removed the activity heuristic; `gethostname`-as-identity fallback flagged (§12/§14) |
| G | no copying/syncing user config as the normal architecture | **PASS (by design)** | projection is bind-mount only; no sync jobs |
| H | cross-platform behavior explicit | **PASS (design)** | logical vars + per-platform `.env` (§4/§8/§16) |
| I | no host-specific absolute-path proliferation in shared config | **PASS (implemented + unit-tested)** | MCP launcher §7 |
| J | container recreation needs no Claude/Codex/MCP reconfig | **DESIGN PASS / live-gated** | state lives on host (§17); §18 "Recreate" confirms |
| K | point fixes not DONE until they fit this architecture | **PASS (governance)** | §11 reconciliation table; forks marked REJECT/REWRITE |

**Overall:** architecture is locked and internally consistent; one invariant (I) is implemented and
unit-tested; the rest are design-complete and blocked only on a Docker host + paid-backend acceptance
run that this environment cannot perform.
