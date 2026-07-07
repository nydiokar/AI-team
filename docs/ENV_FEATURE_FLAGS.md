# Environment Feature Flags — what must be enabled separately

**Why this file exists.** The runtime is configured from a real `.env` (loaded by
`config/settings.py`; the file itself is git-ignored and never committed). Several
behaviours are **gated behind flags that default to OFF** — if you don't set them, the
feature silently does nothing. This is the "why isn't X working — oh, it's behind a flag"
reference so nobody has to rediscover that later.

> There is **no `.env.example`** in this repo by design: `.env*` writes are denied by the
> agent guardrails (secret-safety). This doc is the tracked, secret-free substitute — copy
> the keys you need into your real `.env`. Values shown are the **code defaults**, not the
> live box's settings.

Source of truth: `config/settings.py` (`_MANAGED_ENV_KEYS` + `_apply_env_overrides`) and the
ad-hoc `os.getenv` reads listed per row. Flags marked **⚠ not in `_MANAGED_ENV_KEYS`** are
read directly in code and are the easiest to forget.

---

## A. Behaviour gates — default OFF, must be enabled to take effect

| Flag | Default | What it turns on | Read at |
|------|---------|------------------|---------|
| `HARNESS_FLOW_DRIVE` | **off** (`""`) | **M1.** Authoritative flow-run *stage writes* at each harness transition. SHADOW-only — nothing reads the stage to drive execution; OFF ⇒ byte-identical to A19. ⚠ not in `_MANAGED_ENV_KEYS`. | `src/orchestrator.py:1742` |
| `HARNESS_LEVEL3_GUARD` | **off** (`""`) | Level-3 admission gate on `_enqueue_task` (blocks over-scoped submits → clean 409). OFF ⇒ no gate. ⚠ not in `_MANAGED_ENV_KEYS`. | `src/orchestrator.py:2036` |
| `MESH_AFFINITY_OFFLINE_GRACE_SEC` | **0 = off** | A18b: hold a pinned session in `PAUSED_PINNED_NODE_OFFLINE` and poll liveness instead of hard-failing when the pinned node is offline. `0` ⇒ byte-identical A11. | `config/settings.py:635` |
| `MESH_EMBEDDED_SERVER` | **false** | Run the mesh task server embedded in the gateway process (vs standalone). | `config/settings.py:593` |
| `WORKER_CANARY` | **off** | Marks a worker as a canary (staged-deploy validation). ⚠ not in `_MANAGED_ENV_KEYS`. | `src/worker/agent.py:874` |
| `GUARDED_WRITE` | **false** | Write-guard mode for agent file writes. | `config/settings.py:445` |
| `CONTROL_API_DOCS` | **false** | Exposes the control-API OpenAPI docs route. Keep OFF on tailnet-facing binds. ⚠ not in `_MANAGED_ENV_KEYS`. | `src/control/control_api.py:203` |
| `CLAUDE_SKIP_PERMISSIONS` | **false** | Appends `--dangerously-skip-permissions` to the Claude CLI. **Security-relevant** — leave OFF unless you know why. | `config/settings.py:307` |

## B. Environment-critical toggles — default ON or must match deployment

| Flag | Default | Meaning | Read at |
|------|---------|---------|---------|
| `MESH_ENABLED` | **false** in code / **`true` on this box** | Activates worker-dispatch routing through the node registry. The live kanebra+Horse mesh runs with this **true** — set in the real `.env`, not the default. | `config/settings.py:563` |
| `MESH_SHADOW_WRITE` | see settings | Mirror session/task writes into the mesh DB. | `config/settings.py:677` |
| `CONTROL_API_ENABLED` | **true** | The in-process `/api/*` control surface (Web UI + read APIs, incl. M1 `/api/flows`). | `config/settings.py:611` |
| `TELEMETRY_ENABLED` | **true** | LLM-turn telemetry capture/upload. | `config/settings.py` (TelemetryConfig) |
| `WORKER_ACCEPT_UNPINNED` | **true** | Whether a worker accepts tasks not pinned to it. | `src/worker/config.py:35` |
| `OPENCODE_SERVER_ENABLED` | false | OpenCode-server backend path. | `config/settings.py:550` |

## C. Required secrets / identity (no default — must be set)

Not flags, but listed so a fresh deploy knows what's mandatory. **Never commit real values.**

- Gateway: `GATEWAY_TELEGRAM_BOT_TOKEN`, `GATEWAY_TELEGRAM_ALLOWED_USERS`, `GATEWAY_TELEGRAM_CHAT_ID`
- Auth: `WORKER_TOKEN`, `DASHBOARD_TOKEN`
- Web Push: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`
- Worker identity: `WORKER_NODE_ID`, `WORKER_TAILSCALE_IP`, `CONTROLLER_URL`, `WORKER_BACKENDS`

---

**Maintenance rule:** when a dispatch adds a new `os.getenv`/`os.environ.get` flag, add a row
here in the same change — especially if it is *not* added to `_MANAGED_ENV_KEYS`. A default-OFF
flag with no row here is a future "why isn't it working" ticket.
