# Architecture — One Gateway, Many Interfaces

A visual + tabular map of the AI-team gateway so the process topology and the HTTP
surface are reviewable in one place. Keep this current when you add/remove a route or
a process. The end state this describes is the goal of
`docs/CONTROL_SURFACE_UNIFICATION.md` (U1–U6, done).

Last updated: 2026-07-13 (added §2b Manager/Case surface — M2/M3, flag-gated)

---

## 1. Process & network topology

There is **one** long-running process on the gateway box: `python main.py`. The
Control API (which serves the Web UI — our own primary UI), the mesh task server,
and Telegram (a secondary, optional interface) are all coroutines **inside** it,
sharing the same live `TaskOrchestrator` — so every interface sees the same
sessions, the same registry, the same event stream. Workers are separate
processes on **other** machines that dial in over the mesh.

```
   ┌─────────────────────────── GATEWAY BOX (one machine) ──────────────────────────┐
   │                                                                                 │
   │   python main.py   ──►  ONE process (the "gateway" / "server" / "controller")   │
   │   │                                                                             │
   │   ├─ TaskOrchestrator        sessions · dispatch · notifier · backends          │
   │   │     • session_service (lifecycle)   • get_registry() (mesh nodes)           │
   │   │     • submit_instruction (dispatch) • notifier (outbound fan-out)           │
   │   │                                                                             │
   │   │   ── interfaces, all holding the SAME orchestrator references ──            │
   │   ├─ Control API        (in-process, U1)  ── only if CONTROL_API_ENABLED=true    │
   │   │     • read:  /api/sessions|tasks|nodes|jobs|events                          │
   │   │     • write: /api/instructions|sessions/*|git/*                             │
   │   │     • push:  /api/events/stream (SSE)                                       │
   │   │     • serves web/dist (the React UI — our own UI) at /                      │
   │   │     • binds CONTROL_API_HOST → tailscale_ip → 127.0.0.1 · port 9003         │
   │   ├─ Mesh Task Server   (in-process)   ── only if MESH_ENABLED=true              │
   │   │     • workers claim/run tasks here · port 9002                              │
   │   └─ TelegramInterface  (in-process, secondary) ── only if GATEWAY_TELEGRAM_BOT_TOKEN set │
   └───────────────────┬───────────────────────────────────┬─────────────────────────┘
                       │ HTTP (browser can't import Python) │ HTTP (mesh protocol)
              ┌────────┴─────────┐                 ┌────────┴──────────────┐
              │  web/  (React)   │                 │  WORKER NODES          │ (Pi5, Horse…)
              │  your phone /    │                 │  src/worker/agent.py   │ separate machines,
              │  laptop browser  │                 │  separate processes    │ separate processes
              │  → port 9003     │                 │  → CONTROLLER_URL:9002  │
              └──────────────────┘                 └────────────────────────┘
```

### Who talks to whom

| Component | Is a… | Talks to | On |
|---|---|---|---|
| Gateway (`main.py`) | single process | — (hosts everything) | — |
| Web UI (`web/dist`) | static files in your **browser** — our own primary UI | the gateway's Control API | `9003` |
| Telegram | in-process interface, secondary/optional | Telegram servers (long-poll) | — |
| Worker | separate process, other machine | the gateway's **mesh** server | `9002` (`CONTROLLER_URL`) |

The Web UI and a worker sit at opposite ends: the Web UI is a **client** that controls
the gateway; a worker is a **compute node** the gateway hands tasks to. They never talk
to each other.

### The two IP/host vars (not redundant)

| Var | Set on | Means | Points at |
|---|---|---|---|
| `CONTROLLER_URL` | **worker** boxes | "where I, a worker, dial out to" | gateway **mesh** port 9002 |
| `CONTROL_API_HOST` | **gateway** box | "which interface I bind my UI/API on" | gateway's own UI port 9003 |

`CONTROL_API_HOST` defaults to `tailscale_ip` then `127.0.0.1`. **Never `0.0.0.0`** —
that exposes the UI+API on every interface. Binding to the Tailscale IP is the outer
auth layer (only tailnet devices can reach the port; internet bots cannot).

### Turning interfaces on/off

Each interface is independently gated — all four combinations are valid:

| Want | Set |
|---|---|
| Web UI only (no Telegram) — the default posture | `GATEWAY_TELEGRAM_BOT_TOKEN=""` |
| Telegram only (no web)    | `CONTROL_API_ENABLED=false` |
| Both surfaces at once     | bot token set + `CONTROL_API_ENABLED=true` |
| Mesh / remote workers     | `MESH_ENABLED=true` (+ workers point `CONTROLLER_URL` here) |

---

## 2. HTTP surface (the Control API)

All `/api/*` require `Authorization: Bearer <DASHBOARD_TOKEN>` (falls back to
`WORKER_TOKEN`). Responses for lifecycle ops use the `CommandResult` envelope
(`{ok, reason, session}`) — **no prose**; the client maps `reason` codes to wording.
Served on `CONTROL_API_HOST:DASHBOARD_PORT` (default `…:9003`).

### Read

| Method | Path | Auth | Backing call | Purpose |
|---|---|---|---|---|
| GET | `/health` | none | — | liveness probe |
| GET | `/api/sessions` | Bearer | `session_service.list_views` | list sessions (SessionView) |
| GET | `/api/tasks` | Bearer | `db.list_tasks` | task history |
| GET | `/api/nodes` | Bearer | `get_registry()` | worker nodes + liveness |
| GET | `/api/jobs` | Bearer | `db.list_jobs` | watched jobs (running + recent) |
| GET | `/api/events` | Bearer | `read_recent_events` | poll event deltas (`?since=offset`) |
| GET | `/api/events/stream` | `?token=` | `event_stream_frames` | **SSE** live push (EventSource) |

### Write (thin adapters over the same services Telegram calls)

| Method | Path | Auth | Backing call | Purpose |
|---|---|---|---|---|
| POST | `/api/instructions` | Bearer + `Idempotency-Key` | `submit_instruction` | send a message/task |
| POST | `/api/sessions` | Bearer + `Idempotency-Key` | `create_session` | new session (`origin.channel="web"`) |
| POST | `/api/sessions/{id}/bind` | Bearer | `bind_active` | bind session to a chat |
| POST | `/api/sessions/{id}/stop` | Bearer | `cancel_task` + `mark_cancelled` | cancel running task |
| POST | `/api/sessions/{id}/compact` | Bearer | `compact_session` | compact context |
| POST | `/api/sessions/{id}/close` | Bearer | `close_session` (off-thread) | close session |
| POST | `/api/sessions/{id}/restore` | Bearer | `restore_session` | reopen a closed session |
| POST | `/api/sessions/{id}/model` | Bearer | `set_model` | pin / clear model |
| POST | `/api/sessions/{id}/inspect` | Bearer | `NodeInspector` | repo/dir/git inspect (read-only) |
| POST | `/api/git/status` | Bearer | `GitAutomationService` | git status summary |
| POST | `/api/git/commit` | Bearer | `GitAutomationService` | commit task changes |
| POST | `/api/git/commit_all` | Bearer | `GitAutomationService` | commit all staged |

### Static (the Web UI)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | none\* | `web/dist/index.html` with the token injected |
| GET | `/assets/*` | none\* | JS/CSS/img (StaticFiles; traversal-safe) |
| GET | `/{path}` | none\* | SPA fallback → index (confined to `web/dist`) |

\* The UI files are unauthenticated by design — the token is injected into the page
(`window.__DASHBOARD_TOKEN__`) so a tailnet device needs no prompt, while `/api/*`
still enforce the token. Safe because only tailnet devices can reach the port. The SPA
file resolver is confined to `web/dist` (no `..`/`%2e%2e` traversal — see the
path-traversal fix). The interactive docs (`/docs`, `/redoc`, `/openapi.json`) are
**disabled by default** (they'd leak the API shape); set `CONTROL_API_DOCS=true` to
re-enable them for local development.

---

## 2b. Manager / Case surface (M2/M3, flag-gated)

On top of the plain task/session surface above, the gateway can run a **Manager**:
a Claude session bound to one durable **Case**, which can dispatch **worker**
sessions into the same Case and authoritatively close it. This is invoked, not
autonomous-by-default — nothing here runs unless something calls `/api/manager`.

Gated behind `MANAGER_ROLE_ENABLED` (see `docs/ENV_FEATURE_FLAGS.md`); OFF ⇒ these
routes 409. Current live status (which flags are ON right now) is `.ai/CONTEXT.md`'s
job, not this file's — this table only describes what the surface *is*.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/manager` | Boot a Manager: opens one Case, delivers the objective as its first assignment turn. |
| POST | `/api/cases/{id}/close` | Authoritative Case close — refuses on unmet criteria / open child work / pending approval (never a bare error, a structured `{ok:false, reason}`). |
| POST | `/api/cases/{id}/review` | Record a Manager review verdict (`accepted`\|`rework_requested`\|`waived`) on a Case. |
| GET | `/api/flows`, `/api/flows/{id}` | Read-only `flow_runs` records — the low-level per-turn ledger. |
| GET | `/api/work`, `/api/work/{id}`, `/api/work/{id}/timeline`, `/api/work/{id}/graph` | Read-only Case-level projections over `flow_runs`+`flow_links`+`flow_events` — the Work surface the Web UI's Case view reads. |

The Manager's stable identity (role prompt, allowed decisions, tool profile
`manager_v1`) is `docs/harness/roles/manager.md`, loaded via
`src/core/roles.py::load_manager_role()`; the per-invocation objective/Case/branch
is delivered as a first user turn, never folded into the system prompt (kept
provider-neutral — `src/core/roles.py` imports no Claude SDK types; the Claude
adapter lives in `src/backends/claude_role_adapter.py`). A worker dispatched by a
Manager **joins** the Manager's Case (`membership:worker`) rather than opening a
child Case — see `docs/dictionary/words_&_relations.md` for the Case/Task/Session
vocabulary this table assumes.

For the loop this surface drives (dispatch → worker joins → review → close) see
`docs/harness/dispatch_pipeline.md` and `docs/Task_Harness_v0.7_AUTOMATION.md`.

---

## 3. Keeping this map honest

- The **always-current detail view** is FastAPI's auto-generated schema — run with
  `CONTROL_API_DOCS=true` and open `/docs` (Swagger) or `/openapi.json`.
- The **enforcement gate** `tests/test_u6_interface_enforcement.py` proves no interface
  mutates session lifecycle state directly (everything goes through `SessionService`) —
  that is the machine-checkable form of "many *equal* interfaces."
- When you add/remove a route, update the table above and (if it changes the topology)
  the diagram. Deploy steps live in `docs/CONTROL_SURFACE_DEPLOY_RUNBOOK.md`.
