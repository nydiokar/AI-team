# Architecture — One Controller, Many Interfaces

A visual + tabular map of the AI-team gateway so the process topology and the HTTP
surfaces are reviewable in one place. Keep this current when you add/remove a route or
a process. The "many equal interfaces" end state it describes was the goal of
[`CONTROL_SURFACE_UNIFICATION.md`](../archive/control-surface-unification/CONTROL_SURFACE_UNIFICATION.md)
(U1–U6, done).

This file describes what the system **is**. Which deployment mode and which flags are
live right now is `.ai/CONTEXT.md`'s job, not this file's.

Last updated: 2026-10-05 (Control API split into per-area routers in `src/control/routes/`;
re-grounded against `main` @ `5282fca`)

---

## 1. Process & network topology

The **controller** is the gateway (`main.py`) plus the mesh **task server**
(`server_main.py`). Both share the controller's `state/mesh.db` — the controller is the
sole DB authority ([`DATABASE_AUTHORITY.md`](DATABASE_AUTHORITY.md)). **Workers** are
separate processes (`worker_main.py` → `src/worker/agent.py`) that dial the task server,
on this host or on other machines. Workers never open `mesh.db`; they read controller
state over the task-server API.

```
   ┌──────────────────────────── CONTROLLER HOST ─────────────────────────────────┐
   │                                                                              │
   │  main.py  ── the gateway process ─────────────────────────────────────────── │
   │  ├─ TaskOrchestrator     sessions · dispatch · notifier · backends · recovery│
   │  │    • session_service (lifecycle)   • get_registry() (mesh nodes)          │
   │  │    • submit_instruction (dispatch) • turn queue / scheduler (A82)         │
   │  │                                                                           │
   │  │   ── interfaces, all holding the SAME orchestrator references ──          │
   │  ├─ Control API   :9003 (in-process)  ── unless CONTROL_API_ENABLED=false     │
   │  │    • /api/*  read · write · Manager/Case · cost · flags                   │
   │  │    • /api/events/stream (SSE push)                                        │
   │  │    • serves web/dist (the React UI — our own primary UI) at /             │
   │  ├─ Task server   (embedded) ── only if MESH_EMBEDDED_SERVER=true (default off)│
   │  └─ TelegramInterface (secondary) ── only if GATEWAY_TELEGRAM_BOT_TOKEN set   │
   │                                                                              │
   │  server_main.py ── standalone mesh task server  :9002  (default mode)        │
   │    • node registry · task claim/result · managed-turn carrier protocol       │
   │    • telemetry ingest · watched jobs · staged files                          │
   │                    (both processes share state/mesh.db)                      │
   └──────┬───────────────────────────────┬───────────────────────────────────────┘
          │ HTTP :9003                    │ HTTP :9002 (mesh protocol, WORKER_TOKEN)
   ┌──────┴────────────┐          ┌───────┴──────────────────────────┐
   │ web/ (React) in a │          │ WORKER NODES  worker_main.py     │
   │ phone/laptop      │          │ (this host + other machines)     │
   │ browser           │          │ run claude / codex / opencode    │
   └───────────────────┘          │ sessions; the managed-turn       │
                                  │ carrier for enrolled sessions    │
   Manager sessions call the      └──────────────────────────────────┘
   Control API (:9003) through scripts/mcp_manager.py (MCP stdio server)
```

### Deployment modes

The same code runs under either supervisor. Process-for-process:

| Process | PM2 app (`ecosystem.config.js`) | Docker service (`compose.yaml`) |
|---|---|---|
| Gateway (`main.py`) | `ai-team-gateway` | `gateway` — publishes `:9003`; `CONTROL_API_BIND_HOST=0.0.0.0` inside the container |
| Task server (`server_main.py`) | `ai-team-server` | `task-server` — publishes `:9002` on `MESH_TAILSCALE_IP` |
| Worker (`worker_main.py`) | `ai-team-worker` | — **always native under PM2**; Docker does not supervise the worker |
| Auto-deploy (`scripts/auto_deploy.sh`) | `ai-team-deploy` (cron, restarts gateway + server) | — |

A dedicated Docker controller sets `GATEWAY_LOCAL_EXECUTION_ENABLED=false`: the gateway
executes nothing itself, does not register a local self-node, and delegates all work to
workers. With the default (`true`) the gateway can also run turns in-process.
Operating either mode: [`RUNBOOKS/OPERATIONS_PM2.md`](../RUNBOOKS/OPERATIONS_PM2.md),
[`RUNBOOKS/OPERATIONS_DOCKER.md`](../RUNBOOKS/OPERATIONS_DOCKER.md).

### Who talks to whom

| Component | Is a… | Talks to | On |
|---|---|---|---|
| Gateway (`main.py`) | controller process — hosts the Control API + Telegram | — | `9003` |
| Task server (`server_main.py`) | controller process — the mesh endpoint | — | `9002` |
| Web UI (`web/dist`) | static files in your **browser** — our own primary UI | the Control API | `9003` |
| Telegram | in-process interface, secondary/optional | Telegram servers (long-poll) | — |
| Worker | separate process, this host or another machine | the task server | `9002` (`CONTROLLER_URL`) |
| Manager MCP (`scripts/mcp_manager.py`) | stdio MCP server inside a Manager's Claude session | the Control API (`/api/sessions`, `/api/instructions`, `/api/cases/*`, `/api/work`) | `9003` |

The Web UI and a worker sit at opposite ends: the Web UI is a **client** that controls
the gateway; a worker is a **compute node** the controller hands tasks to. They never
talk to each other.

### Host/bind vars (not redundant)

| Var | Set on | Means |
|---|---|---|
| `CONTROLLER_URL` | **worker** boxes | where a worker dials out to — the task server, `:9002` |
| `CONTROL_API_BIND_HOST` | gateway (Docker) | bind this host verbatim — for bridged containers that can't bind the Tailscale IP |
| `CONTROL_API_HOST` | gateway | operator override, bound verbatim (`0.0.0.0` = deliberate LAN exposure) |
| `MESH_TAILSCALE_IP` | controller | tailnet address; default Control API bind + task-server bind |
| `MESH_BIND_HOST` | task server | task-server bind override (else `MESH_TAILSCALE_IP`, else `127.0.0.1`) |
| `MESH_LOCAL_CARRIER_NODE_ID` | controller | node id of this host's worker — the managed-turn carrier for unpinned enrolled sessions |

Control API bind order (`src/orchestrator.py::resolve_control_api_hosts`):
`CONTROL_API_BIND_HOST` → `CONTROL_API_HOST` → otherwise bind **both** `127.0.0.1` and
`MESH_TAILSCALE_IP`. The LAN interface stays unbound by default. That's the outer auth
layer: only local clients and tailnet devices can reach the port.

### Turning interfaces on/off

| Want | Set |
|---|---|
| Web UI only (no Telegram), the default posture | `GATEWAY_TELEGRAM_BOT_TOKEN=""` |
| Telegram only (no web) | `CONTROL_API_ENABLED=false` |
| Both surfaces at once | bot token set + `CONTROL_API_ENABLED=true` |
| Remote/local workers | `MESH_ENABLED=true`, run `server_main.py`, workers point `CONTROLLER_URL` at it |
| Task server inside the gateway (legacy) | `MESH_EMBEDDED_SERVER=true` |

---

## 2. HTTP surface — the Control API (`:9003`)

All `/api/*` require `Authorization: Bearer <token>`, where the token is
`DASHBOARD_TOKEN` (falls back to `WORKER_TOKEN` when unset; `WORKER_TOKEN` is also
accepted alongside it). Exceptions are noted per row. Lifecycle ops return the
`CommandResult` envelope (`{ok, reason, session}`) with **no prose**: the client maps
`reason` codes to wording.

**Code layout.** `src/control/control_api.py::build_control_api()` builds the app: middleware,
auth (`_require_auth`), the idempotency cache, the Web UI mount, and the shared helpers and
request/response models. Each area below is one `APIRouter` in `src/control/routes/`, included
in this order:

| Area (section below) | Module | Auth |
|---|---|---|
| Monitoring | `routes/monitoring.py` | per route (`/health` and the SSE stream are special) |
| Turn requests | `routes/turn_requests.py` | per route (admission also accepts `AITeamSender`) |
| Approvals, push, runtime flags, git | `routes/admin.py` | router-level Bearer |
| Sessions | `routes/sessions.py` | router-level Bearer |
| Work / flows (§2b) | `routes/work.py` | router-level Bearer |
| Manager / Case (§2b) | `routes/cases.py` | router-level Bearer |
| Cost, metrics, quota | `routes/cost.py` | router-level Bearer |

Routes marked `# REVISIT` in code have no caller yet. Each needs to be either wired up or
removed (git ×3, session `bind`, `/api/metrics/system`, `/api/turns/{id}` + `/diagnostics`).
`POST /api/instructions` and `POST /api/sessions/{id}/turn-requests` carry a `# REVISIT` for
a different reason: both admit an enrolled session's turn; fold them at A82 Stage 8b (plan in
the comment in `routes/sessions.py`). The public surface (no Bearer) is pinned by
`tests/test_control_api_auth_coverage.py`.

### Sessions

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/sessions` | list sessions (SessionView) |
| POST | `/api/sessions` | new session (`origin.channel="web"`); `Idempotency-Key` |
| POST | `/api/instructions` | send a message/task (session or one-off); routed to the managed queue if enrolled; `Idempotency-Key` |
| GET | `/api/sessions/{id}/messages` | conversation transcript |
| GET | `/api/sessions/{id}/timeline` | durable, bounded activity timeline |
| GET | `/api/sessions/{id}/usage` | token totals + estimated USD |
| POST | `/api/sessions/{id}/bind` | bind session to a chat |
| POST | `/api/sessions/{id}/stop` | cancel the active turn (enrolled: pauses the queue first) |
| POST | `/api/sessions/{id}/compact` | compact context |
| POST | `/api/sessions/{id}/close` | close session |
| POST | `/api/sessions/{id}/restore` | reopen a closed session |
| POST | `/api/sessions/{id}/keep` | set/clear the "keep" mark + note |
| POST | `/api/sessions/{id}/model` | pin / clear model |
| POST | `/api/sessions/{id}/effort` | set reasoning effort |
| POST | `/api/sessions/{id}/upload` | upload a file into the session's `uploads/` |
| POST | `/api/sessions/{id}/inspect` | read-only repo/dir/git inspect on the owning node |
| GET | `/api/cache-heartbeats` | list prompt-cache heartbeat controllers (A80) |
| POST | `/api/sessions/{id}/cache-heartbeat` | enable a heartbeat. 409 when `CACHE_HEARTBEAT_OBSERVE` and `CACHE_HEARTBEAT_ACTIVE` are both off |
| DELETE | `/api/sessions/{id}/cache-heartbeat` | stop the session's heartbeats |

### Turn requests (A82 managed turn queue)

Only meaningful for sessions **enrolled** on the managed queue. Enrollment is gated by
`TURN_QUEUE_ENROLLMENT_ENABLED` (default off).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/sessions/{id}/turn-requests` | admit a managed turn → 202 receipt. Auth: operator Bearer **or** `Authorization: AITeamSender <capability>` (scoped agent sender, `src/control/agent_sender.py`). 409 `session_not_enrolled` |
| GET | `/api/sessions/{id}/turn-requests` | cursor page of open managed turns |
| GET | `/api/turn-requests/{task_id}` | turn-request detail |
| PATCH | `/api/turn-requests/{task_id}` | edit a queued human turn (`If-Match` revision) |
| POST | `/api/turn-requests/{task_id}/withdraw` | withdraw a queued human turn (`If-Match`) |
| POST | `/api/turn-requests/{task_id}/resolve-recovery` | operator exit for a turn held in recovery |
| POST | `/api/sessions/{id}/turn-requests/pause` | persistent operator queue pause |
| POST | `/api/sessions/{id}/turn-requests/resume` | clear the operator pause |
| POST | `/api/sessions/{id}/turn-requests/enroll` | enroll the session (flag-gated, above) |
| POST | `/api/sessions/{id}/turn-requests/unenroll` | unenroll when no obligations remain |

### Monitoring — tasks, turns, events, mesh

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + SDK governor caps. **No auth** |
| GET | `/api/tasks` | task history (optional `sectioned` lifecycle view) |
| GET | `/api/artifacts`, `/api/artifacts/{task_id}` | artifact summaries / one artifact + changed files (DB-first) |
| GET | `/api/turns`, `/api/turns/{turn_id}` | telemetry turn list / detail |
| GET | `/api/turns/{turn_id}/diagnostics` · `/graph` · `/events` | per-turn diagnostics, call graph, telemetry events |
| GET | `/api/events` | poll event deltas (`?since=offset`) |
| GET | `/api/events/stream` | **SSE** live push (EventSource). Auth: `?token=` or Bearer, checked inline |
| GET | `/api/nodes` | worker nodes + liveness |
| GET | `/api/mesh/health` | mesh health trend + reconcile backlog |
| GET | `/api/jobs` | watched jobs (running + recent) |
| GET | `/api/projects` | repos a node can use (repo picker) |
| GET | `/api/models` | model catalog per backend/node |
| GET | `/api/backends/usage` | account/usage facts per backend |

### Cost, metrics, quota

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/cost/explorer` | spend breakdown by a chosen dimension |
| GET | `/api/cost/top` | top-spending sessions |
| GET | `/api/cost/projects` | projects with usage |
| GET | `/api/cost/alerts` | budget / burn-rate alerts (reports `COST_ALERT_ENFORCE_ENABLED` state) |
| GET | `/api/metrics/system` | per-minute loop / latency / resource rollups |
| GET | `/api/metrics/health` | ok/warn/bad verdict for the UI banner |
| GET | `/api/system-alerts` | outages recorded by the healthcheck script |
| GET | `/api/quota-windows` | quota coordinator + prewarmer status (`enabled:false` without a coordinator) |

### Approvals, push, runtime flags, git

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/approvals` | pending approvals queue |
| POST | `/api/approvals` | record a pending approval (503 if the service is unavailable) |
| POST | `/api/approvals/{id}/resolve` | approve / reject (double-resolve → 409) |
| GET | `/api/push/status` | whether browser push is available (VAPID configured) + public key |
| POST | `/api/push/subscribe`, `/api/push/unsubscribe` | register / disable a browser push subscription |
| GET | `/api/flags` | effective runtime flags (registry → env → default) |
| PUT | `/api/flags/{flag_name}` | set a registry override (404 unknown, 409 not registry-writable) |
| DELETE | `/api/flags/{flag_name}` | delete a registry override |
| POST | `/api/git/status` | git status summary |
| POST | `/api/git/commit` | commit one task's changes |
| POST | `/api/git/commit_all` | commit all staged |

### Static (the Web UI)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | none\* | `web/dist/index.html`, token injected for trusted requests |
| GET | `/assets/*` | none\* | JS/CSS/img (StaticFiles; traversal-safe) |
| GET | `/{path}` | none\* | SPA fallback → index (confined to `web/dist`; unknown `/api/*` → 404) |

\* The UI files are unauthenticated by design. The token is injected into the page
(`window.__DASHBOARD_TOKEN__`) **only** when the request is trusted: the Host is the bind
host, a `*.ts.net` name or a tailnet IP, and the peer is a tailnet address. The response
is `no-store` + `X-Frame-Options: DENY`; `/api/*` still enforce the token. Removing the
token from served HTML entirely is open work (A75). The interactive docs (`/docs`, `/redoc`,
`/openapi.json`) are **disabled by default** because they would leak the API shape. Set the
runtime flag `CONTROL_API_DOCS=true` to re-enable them for local development.

---

## 2b. Manager / Case surface (M2/M3, flag-gated)

On top of the plain task/session surface above, the gateway can run a **Manager**:
a Claude session bound to one durable **Case**, which can dispatch **worker**
sessions into the same Case and authoritatively close it. This is invoked, not
autonomous-by-default: nothing here runs unless something calls `/api/manager`.
Managers drive these routes through `scripts/mcp_manager.py` tools (`dispatch_worker`,
`open_case`, `get_case_brief`, `arm_wait_group`, `close_case`, `record_review`, …).

Each route group is gated by its own flag (see [`ENV_FEATURE_FLAGS.md`](ENV_FEATURE_FLAGS.md)).
Flag OFF returns a structured refusal (409 or 404 as noted), never a bare error.

| Method | Path | Gate | Purpose |
|---|---|---|---|
| POST | `/api/manager` | `MANAGER_ROLE_ENABLED` (409) | boot a Manager: session + one Case + objective as the first assignment turn |
| POST | `/api/cases` | `MANAGER_ROLE_ENABLED` (409) | open a new Case on an existing Manager session |
| POST | `/api/cases/{id}/close` | — | authoritative close. Refuses on unmet criteria / open child work / pending approval (`{ok:false, reason}` with 200) |
| POST | `/api/cases/{id}/operator-close` | — | manual operator close; may waive criteria |
| POST | `/api/cases/{id}/review` | `REVIEW_EMITTER_ENABLED` (404) | record a review verdict (`accepted`\|`rework_requested`\|`waived`) |
| POST | `/api/cases/{id}/waits` | `DURABLE_RELAY_ENABLED` (404) | record a pending worker-wait marker |
| POST | `/api/cases/{id}/waits/reconcile` | `DURABLE_RELAY_ENABLED` (404) | reconcile waits against `task.finished` |
| POST | `/api/cases/{id}/boot-reconcile` | `DURABLE_RELAY_ENABLED` (404) | reconcile waits + re-arm wait-groups at Manager boot |
| POST | `/api/cases/{id}/wait-group` | `CASE_CONTINUATION_ENABLED` (404) | arm a wake-dispatcher wait-group (M3.4) |
| POST | `/api/cases/{id}/artifacts` | `SPEC_AUTHORING_ENABLED` (404) | publish a durable artifact to the Case |
| POST | `/api/cases/{id}/spec` · `/spec-review` · `/decompose` | `SPEC_AUTHORING_ENABLED` (404) | M4: author a spec, score it from a separate seat, expand it into a task-DAG |
| GET | `/api/cases/{id}/brief` | — | full Case working state from the DB (for role-boot / respawn) |
| POST | `/api/cases/{id}/interrupt` | — (safety valve) | kill in-flight workers; Case → blocked |
| POST | `/api/cases/orphans/sweep` | — | block stale Cases that have no live Manager |
| POST | `/api/cases/{id}/state` | — | operator state change (non-terminal Cases) |
| GET | `/api/cases/{id}/resume-state` | — | quota-pause + resume-cost status |
| POST | `/api/cases/{id}/resume` | continuation (409 `continuation_disabled`) | resume a quota-paused Case now |
| GET | `/api/cases/{id}/usage` | — | Case cost split, Manager vs workers |
| GET | `/api/flows`, `/api/flows/{id}` | — | read-only `flow_runs` records (the low-level per-turn ledger) |
| GET | `/api/work`, `/api/work/{id}` | — | Case summaries (attention buckets) / Case detail + ledger + parent/children |
| GET | `/api/work/{id}/timeline` · `/graph` · `/roster` | — | Case audit trail (`flow_events`), lineage graph, live sessions + jobs |
| GET | `/api/work/affiliations/sessions` | — | which session belongs to which Case |

The Manager's stable identity (role prompt, allowed decisions, tool profile
`manager_v1`) is [`docs/harness/roles/manager.md`](../harness/roles/manager.md), loaded via
`src/core/roles.py::load_manager_role()`. The per-invocation objective/Case/branch is
delivered as a first user turn and never folded into the system prompt. That keeps it
provider-neutral: `src/core/roles.py` imports no Claude SDK types, and the Claude adapter
lives in `src/backends/claude_role_adapter.py`. A worker dispatched by a Manager **joins** the
Manager's Case (`membership:worker`) rather than opening a child Case. See
[`docs/dictionary/words_&_relations.md`](../dictionary/words_&_relations.md) for the
Case/Task/Session vocabulary.

For the loop this surface drives (dispatch → worker joins → review → close) see
[`docs/harness/dispatch_pipeline.md`](../harness/dispatch_pipeline.md) and
[`docs/Task_Harness_v0.7_AUTOMATION.md`](../Task_Harness_v0.7_AUTOMATION.md).

---

## 3. HTTP surface — the mesh task server (`:9002`)

The worker-facing protocol. Every route except `/health` requires
`Authorization: Bearer <WORKER_TOKEN>`, a single shared token. Per-node credentials
are designed (A71, [`MESH_SECURITY.md`](MESH_SECURITY.md)) but **not built**. The
interim guard (A74) refuses a proactive turn for a pinned session from a different
`node_id`. Code: `src/control/task_server.py`.

| Area | Method | Path | Purpose |
|---|---|---|---|
| health | GET | `/health` | DB stats + mesh health. **No auth** |
| health | GET | `/metrics` | task counts, node liveness, success rate |
| nodes | POST | `/nodes/register` · `/nodes/heartbeat` · `/nodes/deregister` | node lifecycle (heartbeat 404 → re-register) |
| nodes | GET | `/nodes` | registry nodes |
| nodes | POST | `/nodes/{node_id}/nudge` | tell a worker to poll now (409 if offline) |
| control | GET | `/control/runtime-flags` | registry flag rows + revision, for workers |
| control | POST | `/control/cases/{case_id}/boot-reconcile` | Manager boot reconcile (self-gated on `DURABLE_RELAY_ENABLED`) |
| legacy tasks | GET | `/tasks/pending` | pending tasks for a node/backends |
| legacy tasks | POST | `/tasks/{id}/claim` · `/release` · `/result` | claim, release (graceful shutdown), submit result |
| managed turns (A82) | GET | `/tasks/pending-managed` | pending managed turns (carrier negotiates the protocol) |
| managed turns | POST | `/tasks/{id}/claim-managed` | claim; mints a token + frozen payload |
| managed turns | POST | `/tasks/{id}/start-managed` · `/release-managed` | claimed → running / release an unstarted claim |
| managed turns | POST | `/tasks/{id}/enter-recovery` | uncertain outcome → `recovery_required` |
| managed turns | POST | `/tasks/{id}/result-managed` · `/quiescence` | atomic result commit / quiescence evidence + auto-reconcile |
| telemetry | POST | `/telemetry/batches` | idempotent telemetry batch store |
| telemetry | POST | `/telemetry/quota-observation` | worker quota observation |
| telemetry | POST | `/events/activity` | re-emit a remote worker's activity into the gateway SSE |
| sessions | POST | `/sessions/{id}/proactive-turn` | persist an autonomous SDK turn + notify |
| files | POST · GET · DELETE | `/files`, `/files/{file_id}` | stage / fetch / delete an upload for a remote worker |
| jobs | POST | `/jobs` · `/jobs/{id}/start` · `/probe` · `/done` | watched-job lifecycle (register, PID, liveness, terminal) |
| jobs | GET | `/jobs`, `/jobs/{id}` | list / detail |

Backends a worker can run (`src/backends/registry.py`): `claude` (default), `codex`
(app-server, see [`CODEX_APP_SERVER_ADAPTER_CONVERGENCE.md`](CODEX_APP_SERVER_ADAPTER_CONVERGENCE.md)),
`opencode`, `opencode-server`.

---

## 4. Keeping this map honest

- The **always-current detail view** is FastAPI's auto-generated schema. Run with
  `CONTROL_API_DOCS=true` and open `/docs` (Swagger) or `/openapi.json`.
- The **enforcement gate** `tests/test_u6_interface_enforcement.py` proves no interface
  mutates session lifecycle state directly (everything goes through `SessionService`).
  That is the machine-checkable form of "many *equal* interfaces."
- When you add or remove a route, update the tables above. If it changes the topology,
  update the diagram too. Deploy steps live in
  [`RUNBOOKS/CONTROL_SURFACE_DEPLOY_RUNBOOK.md`](../RUNBOOKS/CONTROL_SURFACE_DEPLOY_RUNBOOK.md).
