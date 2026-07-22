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

**Source of truth:** `config/settings.py` (`_MANAGED_ENV_KEYS` + `_apply_env_overrides`)
and the ad-hoc `os.getenv` reads listed per row.

**Two classes of key:**
- **Managed** — in `_MANAGED_ENV_KEYS`. When `AI_TEAM_ENV_FILE` is set, a key absent from
  the file is actively *cleared* from the supervisor environment. Safe across PM2 restarts.
- ⚠ **Unmanaged** — read directly in code with `os.getenv`. If you set one in `.env`,
  remove it later, and restart via PM2, the old value may survive in the supervisor
  environment. These are the easiest to forget and the most common source of "why is this
  still on?" bugs.

---

## A. Behaviour gates — default OFF, must be set to take effect

| Flag | Managed? | Default | What it turns on | Code seam |
|------|----------|---------|-----------------|-----------|
| `HARNESS_FLOW_DRIVE` | ⚠ NO | off | M1: authoritative §11 stage writes at each harness transition. **SHADOW-only** — nothing reads the stage to drive execution; OFF ⇒ byte-identical to A19. Turning this ON right now is safe but cosmetic: it writes pretty stage labels to `flow_runs.current_stage`, which nothing reads back. | `orchestrator.py:1742` |
| `HARNESS_LEVEL3_GUARD` | ⚠ NO | off | Level-3 admission gate on `_enqueue_task`: blocks over-scoped submits, returns clean 409. OFF ⇒ no gate, everything passes. | `orchestrator.py:2036` |
| `DURABLE_RELAY_ENABLED` | ⚠ NO | off | **M3.3 A46 — durable worker-wait relay.** `wait_for_worker` is an in-process poll, so a Manager/gateway crash mid-wait loses it. When ON: `dispatch_worker` records a durable append-only `worker.wait_pending` marker on the Case (via `POST /api/cases/{id}/waits`), and `reconcile_waits` (`POST /api/cases/{id}/waits/reconcile`) resolves each outstanding wait against the already-durable `task.finished` event (finished ⇒ append `worker.wait_resolved`; still-open ⇒ re-arm), so a resumed Manager recovers its waits from the ledger. Idempotent. OFF ⇒ no `worker.wait_*` events written, both `/waits` routes return 404 ⇒ byte-identical. Reuses the A25/A26 `flow_events` substrate — no new schema. | `control/db.py:durable_relay_enabled` · `scripts/mcp_manager.py:_reconcile_waits` |
| `MANAGER_TOOLS_ENABLED` | ⚠ NO | off | **M3 A34** — grant a Claude session the Manager MCP tools (`mcp__manager__dispatch_worker` / `wait_for_worker`). **Double-gated:** the tools are added ONLY if this is ON **and** the `manager` server is registered in `~/.claude.json` (`scripts/setup_mcp.py --with-manager`). OFF ⇒ byte-identical even if the server is registered. This is the operator-controlled kill switch for the dispatch primitive; the tools also stay inert until the gateway is restarted to pick up the code. | `backends/claude_driver.py:_manager_tools_enabled` |
| `MANAGER_ROLE_ENABLED` | ⚠ NO | off | **M3 A38 (Phase 3.1)** — the Manager-role path. When ON: (1) a session with `case_role=="manager"` boots via the Claude adapter with the canonical role prompt appended to the Claude Code preset (`system_prompt`); (2) the manager MCP tools are SCOPED per-session (only a manager session gets `manager_v1` = dispatch_worker/wait_for_worker/get_case/close_case), superseding the A34 process-wide `MANAGER_TOOLS_ENABLED` grant; (3) `POST /api/manager` (→ `invoke_manager`) opens one Case + boots the Manager, and `POST /api/cases/{id}/close` (→ `close_case`) is the Decision surface. OFF ⇒ no role prompt, no scoped grant, `/api/manager` refuses (409) — byte-identical to pre-A38. Still requires the `manager` server in `~/.claude.json` for the tool grant. **Depends on `HARNESS_FLOW_DRIVE` ON** for the Case machinery (per-turn attach, worker JOIN, `task.finished` timeline that `wait_for_worker` watches); with FLOW_DRIVE OFF the Manager boots but Case attach/JOIN are inert (a warning is logged). | `backends/claude_driver.py:_manager_role_enabled` · `orchestrator.py:invoke_manager` |
| `MESH_AFFINITY_OFFLINE_GRACE_SEC` | ⚠ NO | 0 (= off) | A18b: when a session's pinned node goes offline, hold it in `PAUSED_PINNED_NODE_OFFLINE` and poll liveness instead of hard-failing. `0` ⇒ byte-identical A11 behavior (hard-fail). | `config/settings.py:635` |
| `MESH_AFFINITY_OFFLINE_POLL_INTERVAL_SEC` | ⚠ NO | (settings default) | Companion to above: how often to probe the offline node. Meaningless unless `MESH_AFFINITY_OFFLINE_GRACE_SEC > 0`. | `config/settings.py:641` |
| `MESH_EMBEDDED_SERVER` | ✅ YES | false | Run the mesh task server embedded in the gateway process vs. standalone. | `config/settings.py:593` |
| `WORKER_CANARY` | ⚠ NO | off | Mark a worker as canary: **disables polling** (poller and job_watcher tasks replaced with `_shutdown.wait()`). Useful only if you're doing staged rollouts with multiple workers. With a single worker deployment, this just makes the worker deaf — effectively a bug trap. | `worker/agent.py:874` |
| `GUARDED_WRITE` | ✅ YES | false | Stored in `config.system.guarded_write`. **Currently only surfaces as metadata** in the result artifact's `security.guarded_write` field (`orchestrator.py:3299`). It does not actually guard or block any file write at the code level today. Vestigial — was presumably planned to enforce something but the enforcement was never wired. | `config/settings.py:445`, `orchestrator.py:3299` |
| `CLAUDE_SKIP_PERMISSIONS` | ✅ YES | false | Appends `--dangerously-skip-permissions` to the Claude CLI. **Security-relevant** — leave OFF unless you know why. | `config/settings.py:307` |
| `CONTROL_API_DOCS` | ⚠ NO | false | Exposes the control-API OpenAPI/Swagger docs route. Keep OFF on tailnet-facing binds. | `control_api.py:203` |

---

## B. Environment-critical toggles — default ON or deployment-specific

| Flag | Managed? | Default | Meaning | Code seam |
|------|----------|---------|---------|-----------|
| `MESH_ENABLED` | ✅ YES | false in code / **true on this box** | Activates worker-dispatch routing through the node registry. The live kanebra+worker mesh requires this **true**. | `config/settings.py:563` |
| `MESH_SHADOW_WRITE` | ✅ YES | true | Mirror session/task writes into the mesh DB. | `config/settings.py:677` |
| `CONTROL_API_ENABLED` | ✅ YES | true | The in-process `/api/*` control surface (Web UI + read APIs, incl. `/api/flows`). | `config/settings.py:611` |
| `TELEMETRY_ENABLED` | ✅ YES | true | LLM-turn telemetry capture. OFF ⇒ `NullTelemetrySink`. | `config/settings.py:695` |
| `WORKER_ACCEPT_UNPINNED` | ⚠ NO | true | Whether a worker accepts tasks not pinned to it (machine_id=NULL tasks). | `worker/config.py:35` |
| `OPENCODE_SERVER_ENABLED` | ✅ YES | false | OpenCode-server backend path. | `config/settings.py:550` |

---

## C. Tuning knobs — have working defaults, rarely need changing

| Flag | Managed? | Default | Purpose | Code seam |
|------|----------|---------|---------|-----------|
| `CLAUDE_MAX_TURNS` | ✅ YES | (settings default) | Max turns per Claude task. | `config/settings.py:312` |
| `CLAUDE_TIMEOUT_SEC` | ✅ YES | (settings default) | Per-task wall-clock timeout. | `config/settings.py:318` |
| `CLAUDE_DEFAULT_MODEL` | ✅ YES | (settings default) | Default Claude model string. | `config/settings.py:532` |
| `CLAUDE_DRIVER_TYPE` | ✅ YES | (settings default) | `cli` or `sdk` driver. | `config/settings.py:324` |
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

---

## D. Telemetry tuning

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
| `HARNESS_FLOW_DRIVE` | Enables SHADOW writes to `current_stage`. Nothing reads that column to drive execution (by design — M1 safety). Setting it ON writes prettier labels to a DB column. Zero operational effect on task outcomes. **Will matter when M3 wires the Manager role.** |
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
| `HARNESS_FLOW_DRIVE` | Could leave stage writes ON after you remove it from `.env` |
| `HARNESS_LEVEL3_GUARD` | Could leave the admission gate ON/OFF unexpectedly |
| `MANAGER_TOOLS_ENABLED` | Could leave the Manager dispatch tools granted to sessions after removal from `.env` (still also requires the `~/.claude.json` `manager` server) |
| `CONTROL_API_DOCS` | Could expose OpenAPI docs unintentionally |
| `TG_REPLY_MAX_CHARS` | Message truncation stuck at old value |
| `MESH_AFFINITY_OFFLINE_GRACE_SEC` | A18b grace mode stuck on |
| `MESH_AFFINITY_OFFLINE_POLL_INTERVAL_SEC` | Companion to above |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` | Web push secrets surviving inadvertent rotation |

---

**Maintenance rule:** when a dispatch adds a new `os.getenv`/`os.environ.get` call, add a
row here in the same change, and decide whether it belongs in `_MANAGED_ENV_KEYS`. A
default-OFF flag with no row here is a future "why isn't it working" ticket.
