# Environment Feature Flags — complete knob inventory

**Why this file exists.** The runtime is configured from a real `.env` (loaded by
`config/settings.py`; the file itself is git-ignored and never committed). Several
behaviours are **gated behind flags that default to OFF** — if you don't set them, the
feature silently does nothing. This is the "why isn't X working — oh, it's behind a flag"
reference so nobody has to rediscover that later.

> There is **no `.env.example`** in this repo by design: `.env*` writes are denied by the
> agent guardrails (secret-safety). This doc is the tracked, secret-free substitute — copy
> the keys you need into your real `.env`. Values shown are the **code defaults**, not the
> live box's settings.

**Source of truth:** `GET /api/flags` renders the live effective flag inventory from
`src/control/db.py:RUNTIME_FLAG_DEFINITIONS`. Registry-writable flags can be changed with
`PUT /api/flags/{FLAG}` and fall back to `.env`/defaults when no DB override exists. Bootstrap,
worker-local, and settings-only booleans are listed there as `registry_writable=false` so the
operator can see their effective env/default value without the API pretending it can safely flip
them. **Typed values (A62):** each entry declares a `value_type` (`bool` | `int` | `float` |
`str`; absent ⇒ `bool`), shown in `GET /api/flags`. A PUT is validated against it — a wrong JSON
type (e.g. `"40"` for an int flag) is a `422 invalid_flag_value`, never coerced. Bool flags keep
their `"1"`/`"0"` rows and lax inputs unchanged. Numeric/str flags: `scripts/ops_flag.sh set
<FLAG> <VALUE>` (`on`/`off` stay bool-only).

**Two classes of key:**
- **Managed** — in `_MANAGED_ENV_KEYS`. When `AI_TEAM_ENV_FILE` is set, a key absent from
  the file is actively *cleared* from the supervisor environment. Safe across PM2 restarts.
- ⚠ **Unmanaged** — read directly in code with `os.getenv`. If you set one in `.env`,
  remove it later, and restart via PM2, the old value may survive in the supervisor
  environment. These are the easiest to forget and the most common source of "why is this
  still on?" bugs.

---

## Rollout-flag retirement burndown (started 2026-11-04)

Goal: delete every **temporary rollout flag** whose rollout is finished, so the code has one
path. Live values were read with `scripts/ops_flag.sh list` against the running gateway on
2026-11-04. A flag is removed by **keeping its live path** and deleting the other branch, its
wrapper in `src/control/db.py`, its `RUNTIME_FLAG_DEFINITIONS` entry and the tests that only
covered the dead branch. Branches: `feat/retire-rollout-flags` (batch 1, PR #275),
`feat/retire-rollout-flags-2` (batch 2).

**Removal rules (read before taking a row):**
1. **Callers first, definition last.** `runtime_flag_enabled()` raises `ValueError` on an
   unregistered name (deliberate: fail-fast on typos). `tests/test_runtime_flag_names_registered.py`
   statically fails if any `runtime_flag_enabled/value("X")` literal is left without a definition.
2. **Worker-read flags wait for a worker restart.** `/control/runtime-flags` only serves rows whose
   name is still in the controller's definitions (`src/control/task_server.py`). Once a definition is
   deleted, a not-yet-restarted worker (Horse) stops receiving the row and falls back to its own
   `.env`/default — `0` for most rollout flags. Only retire a worker-read flag early if its default
   equals its live value.
3. **Delete the OFF-path tests, don't adapt them.** Drop now-no-op `setenv("FLAG", "1")` lines.
4. **DB rows and `.env` keys are cleaned last.** Leftover `runtime_flags` rows for retired names are
   inert (the snapshot filters them; the admin routes 404 unknown names). Leftover `.env` keys are
   inert too (`_MANAGED_ENV_KEYS` only *pops* managed keys that are absent from the file).

| Flag | Group | Live 2026-11-04 | Action | Why | Status |
|---|---|---|---|---|---|
| `RESTART_CONTEXT_RESTORE_DISABLED` | kill switch | false (registry) | removed, restore-context always ON | A98-era opt-out, never used | ✅ `065040c` |
| `RESTART_LOST_SESSION_FORK_DISABLED` | kill switch | false (registry) | removed, lost session always forks fresh | A98 O1 opt-out | ✅ `065040c` |
| `RESPAWN_ON_RESTART_ERROR_DISABLED` | kill switch | false (registry) | removed, ERROR+lost Manager always respawn-eligible | A98 O2 opt-out | ✅ `065040c` |
| `RESTART_NOTIFY_DISABLED` | kill switch | false (registry) | removed, restart notify always ON | A98 O5 opt-out | ✅ `065040c` |
| `WORKER_MEMORY_WATCHDOG_DISABLED` | kill switch, worker-read | false (registry) | removed, watchdog always ON | A98 O7 opt-out; old worker falls back to default `0` = same behaviour | ✅ `065040c` |
| `OPENCODE_SERVER_ENABLED` | dead | false (env) | removed from registry + settings | never read by any logic | ✅ `065040c` |
| `REVIEW_EMITTER_ENABLED` | rollout | true (registry) | removed; review route + rework close gate unconditional | M3.2 proven live 2026-07-13 | ✅ `dcb69d4` |
| `CASE_QUOTA_RESUME_ENABLED` | rollout | true (registry) | removed; quota pause always recorded (still needs `HARNESS_FLOW_DRIVE`) | default was already ON | ✅ `dcb69d4` |
| `TRANSIENT_PROVIDER_RESUME_ENABLED` | rollout | true (registry) | removed; 529 turn leaves session AWAITING_INPUT, Case self-heals | ON live | ✅ `dcb69d4` |
| `CACHE_HEARTBEAT_OBSERVE` | rollout | true (default) | removed; observe-only always ON | default ON | ✅ `dcb69d4` |
| `CACHE_HEARTBEAT_CHECKPOINT_ENABLED` | rollout | true (default) | removed; checkpoint beats always ON | default ON | ✅ `dcb69d4` |
| `SPEC_AUTHORING_ENABLED` | rollout | true (registry) | removed; spec/review/decompose routes + db writes unconditional | ON live; Manager spec→score→DAG workflow | ✅ `92cc1bf` |
| `CASE_CONTINUATION_ENABLED` | rollout | true (registry) | removed; Wake-Dispatcher starts whenever mesh is on, wait-group route + resume always available | ON live; web `continuation_disabled` copy dropped | ✅ `f89dccc` |
| `HARNESS_FLOW_DRIVE` | rollout | true (registry) | removed; Case admission, lineage, stage + event writes, approval links, quota/transient pause recording unconditional | ON live; legacy `dispatch_start`/`queued` path deleted | ✅ `1853edc` |
| `APP_METRICS_ENABLED` | env kill switch | unset ⇒ true | removed; sampler always starts | settings-only, not in container env | ✅ `e37eeaa` |
| `MESH_PENDING_REAPER_ENABLED` | env kill switch | unset ⇒ true | removed; reaper always runs | settings-only, not in container env | ✅ `e37eeaa` |
| `QUOTA_ARCHIVE_ENABLED` | env kill switch | unset ⇒ true | removed; pruned quota history always archived to `quota_archive.db` | settings-only, not in container env | ✅ `abeb3e4` |
| `QUOTA_COORDINATOR_ENABLED` | rollout, startup + worker env | true (registry + `.env`) | **deferred** | two blockers: (1) `src/worker/config.py` reads it from the WORKER's `.env` as the default for quota observation — only `kanebra` has it (19,756 ingests, Horse 0), so dropping the read would silently start observation on Horse; set `WORKER_QUOTA_OBSERVE=1` on kanebra first. (2) 15 test modules build `TaskOrchestrator()`; with no gate each would create `state/quota_windows.db` under the cwd — isolate `config.quota.db_path` in `tests/conftest.py` first | ⏭ deferred |
| `MANAGER_ROLE_ENABLED` | rollout, worker-read | true (registry + `.env`) | removed; role boot, scoped grants, `/api/manager`, `POST /api/cases` and Manager respawn unconditional | ON live; needs both workers restarted onto this code (an old worker falls back to its `.env`) | ✅ `1ad136c` |
| `MANAGER_TOOLS_ENABLED` | legacy, worker-read | true (registry + `.env`) | removed with the legacy process-wide 2-tool grant | the grant was only reachable with `MANAGER_ROLE_ENABLED` OFF; role-less sessions now never get manager tools | ✅ `1ad136c` |
| `DURABLE_RELAY_ENABLED` | rollout, worker-read | **false** (registry, set 2026-08-03) | removed with its A104 shims: `POST /api/cases/{id}/waits`, `/api/cases/{id}/boot-reconcile`, `/control/cases/{id}/boot-reconcile`, driver boot hook | dead since A104 (agent inbox owns waits); removal = the live OFF behaviour (routes 404, hook no-op) | ✅ `a2e70db` |
| `HARNESS_LEVEL3_GUARD` | rollout | **false** (registry) | removed with the Level-3 predicate + `harness_level3_needs_approval` reason | nothing in `src`/`scripts` sets `harness_level`; `HarnessAdmissionBlocked` stays for `local_execution_disabled` | ✅ `3bf0317` |
| `MANAGER_ADVANCEMENT_GATE` | rollout | **false** (default) | kept OFF, operator decision | the only OFF flag with real behaviour: refuses a Manager `close_case` without ≥2 dispatches / a rework verdict / a ≥40-char `exhaustion_attestation` | 🔍 operator |
| `COST_ALERT_ENFORCE_ENABLED` | rollout | **false** (default) | removed with the `enforcement` field of `/api/cost/alerts` + the banner line | placebo: no enforcement existed; the real cap is `CLAUDE_SDK_MAX_BUDGET_USD` | ✅ `c4ca62b` |
| `TOOL_PROFILES_DYNAMIC` + `TOOL_PROFILES_OVERLAY` | feature, worker-read | **false** / empty | kept as is | a real feature (edit role tool lists without a redeploy); no-op until an overlay is written. Candidate later: drop the DYNAMIC switch, keep OVERLAY | ⏸ kept |
| `QUOTA_DIGEST_TELEGRAM_ENABLED` | temporary | **false** (registry + `.env`) | feature deleted (`src/services/quota_digest.py`, `notify_quota_digest`, `QUOTA_DIGEST_INTERVAL_SEC`) | self-labelled temporary, never ON | ✅ `abeb3e4` |
| `GATEWAY_LOCAL_EXECUTION_ENABLED` | architectural | false (compose) | belongs to A102 | **code default is `True`**: never drop the compose line before the code | 🧱 A102 |
| `MESH_ENABLED` / `MESH_EMBEDDED_SERVER` / `MESH_SHADOW_WRITE` | architectural | true / false / true (env) | separate job | the pre-split single-process fallback; 22+ refs | 🧱 separate job |
| `JOBS_READ_MODE` / `OPENCODE_MODE` / `CLAUDE_DRIVER_TYPE` | config/migration | controller / server / sdk | check other values still needed | mode selectors, not booleans | 🧱 separate job |

**Kept on purpose (not rollout flags):** `CACHE_HEARTBEAT_ACTIVE` (paid-turn switch, ON via `.env`),
`QUOTA_PREWARM_ENABLED` (live cost switch by design), `CASE_QUOTA_RESUME_AUTO`,
`CASE_RESPAWN_REQUIRES_APPROVAL`, `CLAUDE_SKIP_PERMISSIONS`, `GUARDED_WRITE`, `CONTROL_API_ENABLED`,
`CONTROL_API_DOCS`, `TELEMETRY_ENABLED`, `TELEMETRY_DETAILED_EVENTS`, `WORKER_ACCEPT_UNPINNED`,
`WORKER_CANARY`, `WORKER_REAP_STALE_SESSIONS`, `CLAUDE_SDK_MAX_TURNS`, `CLAUDE_SDK_MAX_BUDGET_USD`.

**Operator `.env` leftovers to delete after the deploy (inert, cosmetic):** `OPENCODE_SERVER_ENABLED`,
`REVIEW_EMITTER_ENABLED`, `WORKER_MANAGED_TURNS` (retired by A102 `f567c1a`); after batch 2:
`CASE_CONTINUATION_ENABLED`, both `HARNESS_FLOW_DRIVE` lines, `QUOTA_DIGEST_TELEGRAM_ENABLED`,
`QUOTA_DIGEST_INTERVAL_SEC`. `MANAGER_ROLE_ENABLED` / `MANAGER_TOOLS_ENABLED` only **after both workers
run batch-2 code** (an older worker still falls back to them). **Keep** `QUOTA_COORDINATOR_ENABLED` (still
read by the worker) and `CACHE_HEARTBEAT_ACTIVE` (the only thing turning paid heartbeats ON).

---

## Jobs read authority (startup setting)

`JOBS_READ_MODE=controller` (default, managed) reads watched jobs exclusively from
the controller MeshDB. Compose explicitly selects this mode because gateway and
task-server share the state volume. `CONTROLLER_URL` alone never enables remote
jobs reads. Healthy-empty returns empty running/recent arrays; unavailable DB
returns HTTP 503 `jobs_unavailable`. Ownership means a matching SQLite session,
including closed sessions. JSON-only sessions remain orphaned in jobs while the
existing session fallback remains available. Empty/whitespace IDs use the existing
SQL predicate (non-NULL without an exact matching session is orphaned).

For an older local Web UI gateway that intentionally merges jobs from a separate
task controller, explicitly set `JOBS_READ_MODE=federated`, `CONTROLLER_URL` and
`WORKER_TOKEN` before restarting that gateway. Invalid mode/missing credentials
fail configuration loading. Compatibility mode retains the legacy local-first
union, a two-second remote cache and gateway/JSON ownership reconciliation.
Failed local reads, failed/malformed remote reads and expired-cache contention
return 503 rather than claiming complete/empty coverage. The existing jobs UI
marks cached rows stale on error and offers Retry. Explicitly disabling local
shadow storage (or installing the existing remote controller-state client) still permits the remote-only UI topology; a configured
local DB whose initialization failed returns 503 instead. Completion receipts still
require local DB storage. Per-job compatibility reconciliation is outside the
controller performance guarantee. Missing union facade returns 503.

Upgrade the authoritative task-server before a federated gateway: the gateway now
requests `GET /jobs?format=verified-v1`, whose envelope confirms the availability
contract. Older servers' arrays are rejected explicitly. Default `/jobs` successful
arrays remain unchanged; modern servers return 503 for failed/absent DB and 429
when four reads through serialization are occupied. Limit is 1–1000, node/session
IDs at most 128, status is running/done/failed/lost; gateway callers use at most 50.
Legacy TaskServerClient callers retain best-effort defaults; only this UI path opts
into strict reads. This setting is not writable through `/api/flags`.

`GET /api/jobs` admits at most four reads through database materialization and
JSON serialization; excess requests get 429 `jobs_busy`. Limit stays 1–50 per
array; recent still includes running jobs. Completion fetches use the same mode
and run serially off the event loop, with thread-local DB connections. Cursor,
retry and restart reliability are tracked separately as J2 in
[`READ_PATH_BURNDOWN.md`](READ_PATH_BURNDOWN.md); no exactly-once claim is made.

J2 (merged and deployed at schema 48) adds migration 47 and durable completion-effect
receipts. Terminal discovery no longer uses timestamps. Existing running and future
jobs are eligible; pre-migration terminals remain historical unless an operator
explicitly authorizes backfill. Each job's notification and audit/continuation
receipts retry independently, at most five attempts with a 120 s claim lease.
Unknown external sends may duplicate; continuation replay retains the existing
permanent `watched:<job_id>` managed-admission key. Web Push remains best effort.

In federation mode, the authority must support authenticated `GET /jobs/completions`
before upgrading the gateway: 25 compact rows/page, four pages/pass, rotating keyset
cursor, no start-time/newest-50 filter. An old server fails explicitly. A local
controller DB is mandatory for completion receipts; its absence pauses effects.
The UI's successful federation union remains compatible; strict availability needs
the versioned jobs envelope above. Details, service bounds,
rollout order and operator recovery are in burndown J2. No worker restart is needed.

## A. Behaviour gates — default OFF, must be set to take effect

| Flag | Managed? | Default | What it turns on | Code seam |
|------|----------|---------|-----------------|-----------|
| `MESH_AFFINITY_OFFLINE_GRACE_SEC` | ⚠ NO | 0 (= off) | A18b: when a session's pinned node goes offline, hold it in `PAUSED_PINNED_NODE_OFFLINE` and poll liveness instead of hard-failing. `0` ⇒ byte-identical A11 behavior (hard-fail). | `config/settings.py:635` |
| `MESH_AFFINITY_OFFLINE_POLL_INTERVAL_SEC` | ⚠ NO | (settings default) | Companion to above: how often to probe the offline node. Meaningless unless `MESH_AFFINITY_OFFLINE_GRACE_SEC > 0`. | `config/settings.py:641` |
| `MESH_EMBEDDED_SERVER` | ✅ YES | false | Run the mesh task server embedded in the gateway process vs. standalone. | `config/settings.py:593` |
| `WORKER_CANARY` | ⚠ NO | off | Mark a worker as canary: **disables polling** (poller and job_watcher tasks replaced with `_shutdown.wait()`). Useful only if you're doing staged rollouts with multiple workers. With a single worker deployment, this just makes the worker deaf — effectively a bug trap. | `worker/agent.py:874` |
| `GUARDED_WRITE` | ✅ YES | false | Stored in `config.system.guarded_write`. **Currently only surfaces as metadata** in the result artifact's `security.guarded_write` field (`orchestrator.py:3299`). It does not actually guard or block any file write at the code level today. Vestigial — was presumably planned to enforce something but the enforcement was never wired. | `config/settings.py:445`, `orchestrator.py:3299` |
| `CLAUDE_SKIP_PERMISSIONS` | ✅ YES | false | Appends `--dangerously-skip-permissions` to the Claude CLI. **Security-relevant** — leave OFF unless you know why. | `config/settings.py:307` |
| `CONTROL_API_DOCS` | ⚠ NO | false | Exposes the control-API OpenAPI/Swagger docs route. Keep OFF on tailnet-facing binds. | `control_api.py:203` |
| `TOOL_PROFILES_DYNAMIC` | ✅ YES (registry-writable) | off | **A108 — controller-authoritative dynamic tool profiles.** When ON, a Claude session's tool grant resolves through the `TOOL_PROFILES_OVERLAY` value layered over the static seed map, instead of the static map alone. Read at SESSION BOOT via the A88 controller snapshot (memory-only, no boot I/O), so a change reaches the next session with **no worker restart** (and, riding the registry, no redeploy). OFF ⇒ the static map is used, byte-identical to A107. Claude-only: Codex/OpenCode gate per server and already pick up a new tool on a configured server with no change. Activate: `scripts/ops_flag.sh on TOOL_PROFILES_DYNAMIC`, then set `TOOL_PROFILES_OVERLAY`. | `backends/tool_profile_source.py` · `backends/claude_driver.py:_session_allowed_tools` |
| `TOOL_PROFILES_OVERLAY` | ✅ YES (registry-writable) | "" (empty) | **A108 — the overlay JSON** honored only when `TOOL_PROFILES_DYNAMIC` is ON. Shape: `{"profiles": {"<profile>": ["mcp__srv__tool", …]}, "role_profiles": {"<role>": "<profile>"}}`. Per-profile replacement layered over the static seed; validated + size-bounded on read. ANY malformation (bad JSON, unknown top key, bad tool name, role → unknown profile, oversized) is rejected whole → static map (fail-closed, least-privilege). An unmapped role always resolves to `worker_v1` regardless of the overlay. Set: `PUT /api/flags/TOOL_PROFILES_OVERLAY {"value":"<json>"}`. | `backends/tool_profile_source.py:_parse_overlay` |

---

## B. Environment-critical toggles — default ON or deployment-specific

| Flag | Managed? | Default | Meaning | Code seam |
|------|----------|---------|---------|-----------|
| `MESH_ENABLED` | ✅ YES | false | Activates worker-dispatch routing through the node registry. Set to **true** when running a multi-node mesh (gateway + worker). | `config/settings.py:563` |
| `MESH_SHADOW_WRITE` | ✅ YES | true | Mirror session/task writes into the mesh DB. | `config/settings.py:677` |
| `CONTROL_API_ENABLED` | ✅ YES | true | The in-process `/api/*` control surface (Web UI + read APIs, incl. `/api/flows`). | `config/settings.py:611` |
| `TELEMETRY_ENABLED` | ✅ YES | true | LLM-turn telemetry capture. OFF ⇒ `NullTelemetrySink`. | `config/settings.py:695` |
| `WORKER_ACCEPT_UNPINNED` | ⚠ NO | true | Whether a worker accepts tasks not pinned to it (machine_id=NULL tasks). | `worker/config.py:35` |

---

## C. Tuning knobs — have working defaults, rarely need changing

| Flag | Managed? | Default | Purpose | Code seam |
|------|----------|---------|---------|-----------|
| `CLAUDE_MAX_TURNS` | ✅ YES | (settings default) | Max turns per Claude task. | `config/settings.py:312` |
| `CLAUDE_TIMEOUT_SEC` | ✅ YES | (settings default) | Per-task wall-clock timeout. | `config/settings.py:318` |
| `CLAUDE_DEFAULT_MODEL` | ✅ YES | (settings default) | Default Claude model string. | `config/settings.py:532` |
| `CLAUDE_DRIVER_TYPE` | ✅ YES | (settings default) | `cli` or `sdk` driver. | `config/settings.py:324` |
| `CLAUDE_SDK_MAX_TURNS` | ✅ YES · ✅ registry-writable (`int`, A62) | unset (no cap) | **M3.3/A53 SDK-driver turn ceiling** (`max_turns` on new SDK session processes). Resolves registry → env → no cap (`db.sdk_governor_ceilings`). Registry values must be > 0 (a PUT of 0 or less is a 422); registry `null` (stored as an empty row) = **no cap, overriding env** — the only way to store "no cap"; `unset`/DELETE drops the row ⇒ env. (The env var keeps its A53 convention: 0 or less = no cap.) `ops_flag.sh migrate` never pins a null knob. Scope `session_boot`: read when the SDK driver creates a session process — on a worker from the A88 controller snapshot (refreshed ~30 s), so no restart is needed once the worker runs A62 code; a live process keeps the ceiling it booted with. A worker on pre-A62 code, or one without a controller snapshot, uses its own env. Set: `scripts/ops_flag.sh set CLAUDE_SDK_MAX_TURNS 40` / `... null`. Effective value: `GET /health` → `governor`. | `control/db.py:sdk_governor_ceilings` · `backends/claude_driver.py:_get_or_create` · `config/settings.py:_apply_sdk_governor_env` |
| `CLAUDE_SDK_MAX_BUDGET_USD` | ✅ YES · ✅ registry-writable (`float`, A62) | unset (no cap) | **M3.3/A53 SDK-driver USD budget ceiling** (`max_budget_usd`); the A65 cost-alert enforcement lever. Same resolution, > 0 / null convention and `session_boot` scope as `CLAUDE_SDK_MAX_TURNS`. Set: `scripts/ops_flag.sh set CLAUDE_SDK_MAX_BUDGET_USD 2.5`. | same as above |
| `CLAUDE_SDK_CLI_PATH` | ✅ YES | "" | Optional Claude CLI binary used by SDK sessions instead of the SDK-bundled CLI. Use this to keep gateway sessions on the installed Claude Code version. | `config/settings.py` |
| `CLAUDE_ALLOWED_ROOT` | ✅ YES | (settings default) | Filesystem root Claude is permitted to write. | `config/settings.py:438` |
| `CLAUDE_BASE_CWD` | ✅ YES | (settings default) | Default working directory for Claude. | `config/settings.py:433` |
| `MAX_CONCURRENT_TASKS` | ✅ YES | (settings default) | Gateway worker-slot count. | `config/settings.py:452` |
| `MAX_QUEUE_SIZE` | ✅ YES | (settings default) | Max depth of the task queue before throttle. | `config/settings.py:458` |
| `GATEWAY_TASK_TIMEOUT_SEC` | ✅ YES | (settings default) | Hard wall-clock timeout the gateway applies to tasks. | `config/settings.py:494` |
| `GATEWAY_HEARTBEAT_INTERVAL_SEC` | ✅ YES | (settings default) | Gateway → worker heartbeat cadence. | `config/settings.py:500` |
| `GATEWAY_INACTIVITY_TIMEOUT_SEC` | ✅ YES | (settings default) | Session inactivity reaper. | `config/settings.py:506` |
| `GATEWAY_SDK_TURN_TIMEOUT_SEC` | ✅ YES | (settings default) | SDK-driver per-turn timeout. | `config/settings.py:512` |
| `GATEWAY_UPLOAD_MAX_MB` | ✅ YES | (settings default) | Max file upload size. | `config/settings.py:488` |
| `TELEGRAM_RATE_LIMIT_REQUESTS` | ✅ YES | (settings default) | Telegram rate limit window requests. | `config/settings.py:464` |
| `TELEGRAM_RATE_LIMIT_WINDOW_SEC` | ✅ YES | (settings default) | Telegram rate limit window seconds. | `config/settings.py:470` |
| `TELEGRAM_MESSAGE_BUFFER_SEC` | ✅ YES | (settings default) | Message buffer delay. | `config/settings.py:476` |
| `TG_REPLY_MAX_CHARS` | ⚠ NO | 0 (= unlimited) | Truncates long Telegram reply messages. Active (`notification_service.py:255`), but not in `_MANAGED_ENV_KEYS`. | `config/settings.py:482` |
| `MESH_CLAIM_LEASE_SEC` | ✅ YES | (settings default) | How long before a stale claim is reaped. | `config/settings.py:647` |
| `MESH_CLAIM_MAX_RUNTIME_SEC` | ✅ YES | (settings default) | Hard cap on task runtime before forcibly reaped. | `config/settings.py:653` |
| `MESH_HEARTBEAT_TIMEOUT_SEC` | ✅ YES | (settings default) | How long before a node goes offline. | `config/settings.py:623` |
| `MESH_ONEOFF_QUEUE_TIMEOUT_SEC` | ✅ YES | (settings default) | Timeout waiting for a one-off task result. | `config/settings.py:629` |
| `MESH_ROUTING_FRESHNESS_WAIT_SEC` | ✅ YES | (settings default) | Routing freshness window. | `config/settings.py:665` |
| `MESH_ROUTING_LIVE_STATE_MAX_AGE_SEC` | ✅ YES | (settings default) | How stale a node's live-state can be before skipping. | `config/settings.py:671` |
| `MESH_SESSION_RECONCILE_INTERVAL_SEC` | ✅ YES | (settings default) | Stale-busy session reconciliation cadence. | `config/settings.py:659` |
| `MESH_HEALTH_WINDOW_SIZE` | ✅ YES | (settings default) | Mesh health rolling window. | `config/settings.py:683` |
| `MESH_HEALTH_FAILURE_THRESHOLD` | ✅ YES | (settings default) | Failure count before marking node degraded. | `config/settings.py:689` |
| `MESH_DB_PATH` | ✅ YES | (settings default) | Path to the SQLite mesh DB. | `config/settings.py:569` |
| `MESH_TAILSCALE_IP` | ✅ YES | (settings default) | This gateway's Tailscale IP. | `config/settings.py:575` |
| `MESH_TASK_SERVER_PORT` | ✅ YES | (settings default) | Port the task server listens on. | `config/settings.py:581` |
| `WORKER_MAX_OUTPUT_CHARS` | ⚠ NO | 500 000 | Truncation limit on worker agent output. Active (`worker/agent.py:51`), not managed. | `worker/agent.py:51` |
| `WORKER_MAX_CONCURRENT` | ⚠ NO | 2 | Worker slot count. Worker-side; separate `.env`. | `worker/config.py:32` |
| `WORKER_API_PORT` | ⚠ NO | 9001 | Worker-side API port. | `worker/config.py:31` |
| `WORKER_PROJECTS_ROOT` | ⚠ NO | "" | Worker project root hint. | `worker/config.py:33` |
| `DASHBOARD_PORT` | ✅ YES | (settings default) | Port the control API listens on. | `config/settings.py:599` |
| `CONTROL_API_HOST` | ✅ YES | (settings default) | Bind address for control API. | `config/settings.py:617` |
| `OPENCODE_TIMEOUT_SEC` | ✅ YES | (settings default) | Per-task timeout for OpenCode backend. | `config/settings.py:519` |
| `OPENCODE_DEFAULT_MODEL` | ✅ YES | (settings default) | Default model for OpenCode. | `config/settings.py:525` |
| `OPENCODE_DEFAULT_AGENT` | ✅ YES | (settings default) | Default agent for OpenCode. | `config/settings.py:544` |
| `OPENCODE_MODE` | ✅ YES | (settings default) | OpenCode execution mode. | `config/settings.py:556` |
| `OPENCODE_ALLOWED_ROOT` | ⚠ NO | (falls back to CLAUDE_ALLOWED_ROOT) | OpenCode filesystem root. Not managed; falls back to `CLAUDE_ALLOWED_ROOT`. | `backends/opencode.py:821` |
| `CODEX_DEFAULT_MODEL` | ✅ YES | (settings default) | Default model for Codex backend. | `config/settings.py:538` |
| `CODEX_NODE_PATH` / `NODE_EXE` | ⚠ NO | (PATH lookup) | Node.js binary path for Codex/OpenCode on Windows. Not managed. | `backends/codex.py:123` |
| `COST_ALERT_DAILY_BUDGET_USD` | ⚠ NO | 0 (= off) | Current-UTC-day known billable USD threshold across attributed and unattributed usage. | `services/cost_alerts.py` |
| `COST_ALERT_SESSION_BURN_USD` | ⚠ NO | 0 (= off) | Current-UTC-day known billable USD threshold for the highest-spend session. | `services/cost_alerts.py` |
| `COST_ALERT_CASE_TOTAL_USD` | ⚠ NO | 0 (= off) | Current-UTC-day known billable USD threshold for each Case. | `services/cost_alerts.py` |

---

## D. Quota coordinator

| Flag | Managed? | Default | Purpose | Code seam |
|------|----------|---------|---------|-----------|
| `QUOTA_COORDINATOR_ENABLED` | ✅ YES | false | Construct and start the observe-only quota coordinator at gateway startup. When off, no quota DB is created. | `orchestrator.py`, `quota_window_coordinator.py` |
| `QUOTA_DB_PATH` | ✅ YES | `state/quota_windows.db` | Dedicated SQLite store for quota observations. | `config/settings.py`, `quota_window_coordinator.py` |
| `QUOTA_OBSERVE_INTERVAL_SEC` | ✅ YES | 300 | Tight polling interval for unknown/unavailable/limit-reached quota telemetry. | `config/settings.py`, `quota_window_coordinator.py` |
| `QUOTA_OBSERVE_MAX_INTERVAL_SEC` | ✅ YES | 21600 | Maximum adaptive sleep once reset telemetry is known. | `config/settings.py`, `quota_window_coordinator.py` |
| `QUOTA_RESET_PROBE_LEAD_SEC` | ✅ YES | 900 | Resume probing this many seconds before a known reset boundary. | `config/settings.py`, `quota_window_coordinator.py` |
| `CLAUDE_GET_USAGE_TIMEOUT_SEC` | ✅ YES | 60.0 | Timeout for the canonical Claude quota read: a Python Agent SDK control request with subtype `get_usage`. This is server-backed subscription quota telemetry, not token estimation or status-line parsing. | `config/settings.py`, `quota_window_coordinator.py`, `claude_usage_control.py` |
| `CLAUDE_STATUS_LINE_JSON_PATH` | ⚠ NO | `state/claude_statusline_latest.json` | **Not a quota knob.** Output path for the terminal status-line capture script, read via `os.getenv` by the script itself (it is a Claude Code `statusLine` command, not gateway code). The quota coordinator does not read this file — `get_usage` is the only quota source. | `scripts/claude_statusline_capture.py` |
| `CLAUDE_QUOTA_PRINCIPAL_KEY` | ✅ YES | "" | Operator-provided stable account label used only to derive `principal_hash`. | `config/settings.py`, `quota_window_coordinator.py` |
| `QUOTA_PREWARM_ENABLED` | ✅ YES | false | **The only path in the gateway that spends without a user asking.** When telemetry shows NO open 5-hour window, spend one minimal `haiku` turn to start one, then re-observe to VERIFY a window actually opened. Requires `QUOTA_COORDINATOR_ENABLED` (reads/activates through its adapter). Deliberately has **no quiet hours** — see the deviation note below. | `orchestrator.py`, `quota_window_prewarmer.py` |
| `QUOTA_PREWARM_MODEL` | ✅ YES | `haiku` | Model for the activation turn. Cheapest tier on purpose, and it keeps the opus/fable weekly-scoped buckets out of it. | `config/settings.py`, `quota_window_coordinator.py` |
| `QUOTA_PREWARM_MIN_INTERVAL_SEC` | ✅ YES | 3600 | Floor between two activations, independent of what telemetry says. | `config/settings.py`, `quota_window_prewarmer.py` |
| `QUOTA_PREWARM_MAX_PER_DAY` | ✅ YES | 8 | Hard daily activation budget (a 5-hour rhythm needs ~5). | `config/settings.py`, `quota_window_prewarmer.py` |
| `QUOTA_PREWARM_DELAY_AFTER_RESET_SEC` | ✅ YES | 120 | How long after a window's `reset_at` to re-check and open the next one. | `config/settings.py`, `quota_window_prewarmer.py` |
| `QUOTA_PREWARM_MAX_ACTIVATION_PERCENT` | ✅ YES | 2.0 | Spec §13 cost gate: max `used_percent` the activation itself may consume, measured as the provider's own delta across it. Two breaches open the circuit. | `config/settings.py`, `quota_window_prewarmer.py` |
| `QUOTA_SNAPSHOT_RETENTION_DAYS` | ✅ YES | 14 | Bounded history (A78 §4): redundant confirmation snapshots older than this are pruned daily; the FIRST sighting of each window boundary is always kept (it is the anchored-window evidence). `0` disables pruning (keep everything forever). | `config/settings.py`, `quota_window_coordinator.py` |
| `QUOTA_EVENT_RETENTION_DAYS` | ✅ YES | 14 | Bounded `coordinator_events` (A93): diagnostic events older than this are pruned in the same daily pass as snapshots, on the controller and the worker. Nothing in code reads the table back. `0` disables event pruning. | `config/settings.py`, `quota_window_coordinator.py` |

**Spec conformance.** The full record — what was built, what diverges, and why — lives in
`docs/SESSION_WINDOW_WARMING_SPEC.md` §19 (that document owns this design). The headline divergence:
**no work horizon / quiet hours** (§10), a deliberate operator decision, because the value only exists
before work starts. Every other activation gate is kept and several are stronger than proposed:
verification after every activation, permanent anchor-drift detection (drift ⇒ circuit open), a cost
gate measured from the provider's own utilization delta, and a principal-identity gate.
`automation_ready` is NOT consulted — the `active_session_state` plumbing exists (A78 §1), but the
production `get_usage` adapter has no session sensor, so it stays `unknown` and `automation_ready` stays
`False` in production.

---

## E. Telemetry tuning

| Flag | Managed? | Default | Purpose | Code seam |
|------|----------|---------|---------|-----------|
| `TELEMETRY_DETAILED_EVENTS` | ✅ YES | (settings default) | Whether to capture fine-grained events. | `config/settings.py:701` |
| `TELEMETRY_UPLOAD_BATCH_SIZE` | ✅ YES | (settings default) | HTTP batch size for worker→gateway shipping. | `config/settings.py:707` |
| `TELEMETRY_UPLOAD_INTERVAL_MS` | ✅ YES | (settings default) | Flush interval for worker HTTP sink. | `config/settings.py:713` |
| `TELEMETRY_UPLOAD_MAX_BYTES` | ✅ YES | (settings default) | Max per-upload payload. | `config/settings.py:719` |
| `TELEMETRY_SPOOL_MAX_BYTES` | ✅ YES | (settings default) | Spool disk limit (on-disk buffer when gateway unreachable). | `config/settings.py:725` |
| `TELEMETRY_EVENT_RETENTION_DAYS` | ✅ YES | (settings default) | DB retention for raw events. | `config/settings.py:731` |
| `TELEMETRY_SUMMARY_RETENTION_DAYS` | ✅ YES | (settings default) | DB retention for summaries. | `config/settings.py:737` |
| `TELEMETRY_TASK_SERVER_URL` | ✅ YES | (auto-derived) | Worker→gateway upload URL. If blank, auto-derived from `MESH_TAILSCALE_IP`+`MESH_TASK_SERVER_PORT`. Rarely needs explicit setting. | `config/settings.py:743`, `telemetry_sink.py:416` |
| `TELEMETRY_OTLP_ENDPOINT` | ✅ YES | "" | **Currently dead config.** Stored in `config.telemetry.otlp_endpoint` but no code reads it to actually export OTLP. The comment says "intentionally OTLP-shippable later" — this is a reserved slot for a future export path that hasn't been built. Setting it does nothing today. | `config/settings.py:749` |

---

## E. Required secrets / identity (no default — must be set)

Not flags, but listed so a fresh deploy knows what's mandatory. **Never commit real values.**

**Gateway:**
- `GATEWAY_TELEGRAM_BOT_TOKEN` ✅ managed
- `GATEWAY_TELEGRAM_ALLOWED_USERS` ✅ managed
- `GATEWAY_TELEGRAM_CHAT_ID` ✅ managed
- `DASHBOARD_TOKEN` ✅ managed
- `WORKER_TOKEN` ✅ managed

**Web push:**
- `VAPID_PUBLIC_KEY` ⚠ NOT managed
- `VAPID_PRIVATE_KEY` ⚠ NOT managed
- `VAPID_SUBJECT` ⚠ NOT managed

**Worker identity (worker `.env` — separate from gateway):**
- `WORKER_NODE_ID` ⚠ NOT managed (required — crashes if absent)
- `WORKER_TAILSCALE_IP` ⚠ NOT managed (required — crashes if absent)
- `WORKER_BACKENDS` ⚠ NOT managed (required — crashes if absent)
- `CONTROLLER_URL` ⚠ NOT managed (required — crashes if absent)

---

## F. Test / bootstrap meta-keys (not for production .env)

| Flag | Purpose |
|------|---------|
| `AI_TEAM_ENV_FILE` | Override which `.env` file is loaded. Bootstrap meta-key — read before managed env is set up. |
| `AI_TEAM_ALLOW_OPENCODE_E2E` | Opt-in for OpenCode end-to-end tests. Test-only — never set in production. |

---

## Flags that are currently pointless to set

Honest assessment — these either do nothing yet, or are dead in the current deployment:

| Flag | Why it's pointless right now |
|------|------------------------------|
| `GUARDED_WRITE` | Does not guard any file write at the code level. Only sets `security.guarded_write: true` in the result artifact JSON metadata. The enforcement was never wired. |
| `TELEMETRY_OTLP_ENDPOINT` | Stored in config, never consumed. No OTLP exporter exists in the codebase yet. Reserved for a future path. |
| `WORKER_CANARY` | Disables the worker's polling loop. With one worker and no staged-rollout infrastructure, this just creates a worker that connects and then does nothing. |
| `MESH_AFFINITY_OFFLINE_POLL_INTERVAL_SEC` | Only meaningful if `MESH_AFFINITY_OFFLINE_GRACE_SEC > 0`. If that's 0 (the default), this value is never consulted. |
| `OPENCODE_*` knobs (model/agent/mode/timeout) | OpenCode backend is wired and callable, but if you're not using it, all these knobs are dead config. |
| `CODEX_DEFAULT_MODEL` / `CODEX_NODE_PATH` / `NODE_EXE` | Same — only relevant if running the Codex backend. |
| `TELEMETRY_TASK_SERVER_URL` | Auto-derived from `MESH_TAILSCALE_IP`+port if blank. Almost never needs explicit setting unless the worker is on a different network segment. |

---

## ⚠ Keys missing from `_MANAGED_ENV_KEYS` that probably should be there

These are gateway-side, actively read, and silently survive PM2 restarts if you set
then remove them from `.env`:

| Key | Impact if stale in supervisor env |
|-----|----------------------------------|
| `CONTROL_API_DOCS` | Could expose OpenAPI docs unintentionally |
| `TG_REPLY_MAX_CHARS` | Message truncation stuck at old value |
| `MESH_AFFINITY_OFFLINE_GRACE_SEC` | A18b grace mode stuck on |
| `MESH_AFFINITY_OFFLINE_POLL_INTERVAL_SEC` | Companion to above |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` | Web push secrets surviving inadvertent rotation |

---

**Maintenance rule:** when a dispatch adds a new boolean feature/config gate, add it to
`RUNTIME_FLAG_DEFINITIONS` in the same change. If it is safe for DB overrides, mark it
`registry_writable=true` and route code through a canonical reader; otherwise mark it read-only
with an honest `effect_scope`.

### Cross-process turn signal (A105)

`GATEWAY_TURN_SIGNAL_URL` is task-server topology configuration, set by Compose to
`http://gateway:${DASHBOARD_PORT:-9003}/internal/turn-signal`. It is not a runtime
flag. Outside split deployments it defaults empty; embedded producers use the
same in-process signal directly. The bodyless POST uses the existing `WORKER_TOKEN`
(the control API already accepts it), carries no state, and never waits on the
carrier request path. One lifespan-owned sender coalesces bursts, keeps at most
one pending hint and sends at most ten requests/second. Async network IO and
connection cleanup share a two-second budget; there are no redirects, proxies
or response-body reads. Unavailable transport falls back to existing DB polls.
The endpoint admits four empty-body readers with a one-second read deadline;
nonempty/chunked garbage is rejected. Subscribers independently re-read the DB.
No worker restart, migration or new credential is needed.
