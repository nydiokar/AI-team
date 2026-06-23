# Architecture — One Gateway, Many Interfaces

A visual + tabular map of the AI-team gateway so the process topology and the HTTP
surface are reviewable in one place. Keep this current when you add/remove a route or
a process. The end state this describes is the goal of
`docs/CONTROL_SURFACE_UNIFICATION.md` (U1–U6, done).

Last updated: 2026-06-24

---

## 1. Process & network topology

There is **one** long-running process on the gateway box: `python main.py`. Telegram,
the Control API (which also serves the Web UI), and the mesh task server are all
coroutines **inside** it, sharing the same live `TaskOrchestrator` — so every interface
sees the same sessions, the same registry, the same event stream. Workers are separate
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
   │   ├─ TelegramInterface  (in-process)   ── only if GATEWAY_TELEGRAM_BOT_TOKEN set │
   │   ├─ Control API        (in-process, U1)  ── only if CONTROL_API_ENABLED=true    │
   │   │     • read:  /api/sessions|tasks|nodes|jobs|events                          │
   │   │     • write: /api/instructions|sessions/*|git/*                             │
   │   │     • push:  /api/events/stream (SSE)                                       │
   │   │     • serves web/dist (the React UI) at /                                   │
   │   │     • binds CONTROL_API_HOST → tailscale_ip → 127.0.0.1 · port 9003         │
   │   └─ Mesh Task Server   (in-process)   ── only if MESH_ENABLED=true              │
   │         • workers claim/run tasks here · port 9002                              │
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
| Telegram | in-process interface | Telegram servers (long-poll) | — |
| Web UI (`web/dist`) | static files in your **browser** | the gateway's Control API | `9003` |
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
| Web UI only (no Telegram) | `GATEWAY_TELEGRAM_BOT_TOKEN=""` |
| Telegram only (no web)    | `CONTROL_API_ENABLED=false` |
| Both (default)            | bot token set + `CONTROL_API_ENABLED=true` |
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

## 3. Keeping this map honest

- The **always-current detail view** is FastAPI's auto-generated schema — run with
  `CONTROL_API_DOCS=true` and open `/docs` (Swagger) or `/openapi.json`.
- The **enforcement gate** `tests/test_u6_interface_enforcement.py` proves no interface
  mutates session lifecycle state directly (everything goes through `SessionService`) —
  that is the machine-checkable form of "many *equal* interfaces."
- When you add/remove a route, update the table above and (if it changes the topology)
  the diagram. Deploy steps live in `docs/CONTROL_SURFACE_DEPLOY_RUNBOOK.md`.
