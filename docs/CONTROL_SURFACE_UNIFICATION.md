# Control Surface Unification — One Gateway, Many Interfaces

Status: corrective spec (no code changed by this document)
Owner: Nyd
Date: 2026-06-22
Source review: `src/`, `web/`, `main.py`, `dashboard_main.py`, `src/control/dashboard.py`,
  `src/control/embedded_server.py` on branch `feat/webui-ui0`
Companion / supersedes the framing of: `docs/COCKPIT_REFACTOR_SPEC.md §14` Move F,
  `docs/FRONTEND_BACKEND_GAP.md`

---

## 0. The desired state (the only acceptance criterion that matters)

> **One gateway process is aware of everything. Telegram, Web, and any future
> surface (Slack/Discord/mobile) are thin, interchangeable *interfaces* over the
> gateway's in-process services — not separate applications that side-read shared
> files.**

```
            ┌──────────────────── GATEWAY PROCESS (main.py) ────────────────────┐
            │  TaskOrchestrator                                                  │
            │    • session_store / session_service   (lifecycle)                │
            │    • submit_instruction                 (dispatch)                │
            │    • notifier (NotificationService)     (outbound fan-out)        │
            │    • _backends (registry)               (execution)               │
            │    • get_registry() / NodeRegistry      (mesh, in-process)        │
            │                                                                    │
            │   ── interfaces, all holding the SAME live references ──           │
            │   ┌────────────────┐   ┌──────────────────────────────────────┐   │
            │   │ TelegramInterface│  │ ControlAPI  (FastAPI, embedded)      │   │
            │   │ (in-process)     │  │   read:  /api/sessions|tasks|nodes   │   │
            │   └────────────────┘   │   write: /api/instructions|sessions  │   │
            │                        │   push:  /api/events (WS/SSE)        │   │
            │                        └──────────────────────────────────────┘   │
            └────────────────────────────────────┬───────────────────────────────┘
                                                  │ HTTP/WS (browser can't import Python)
                                          ┌───────┴────────┐
                                          │  web/  (React) │   ← pure static client
                                          └────────────────┘
```

Done = `python main.py` is the **only** long-running process for a single-box
deployment. There is no `dashboard_main.py`. The web UI is served by — and talks
only to — the gateway's own API. Telegram and Web are siblings.

---

## 1. What is actually fine (do NOT re-do this)

The service abstraction from `COCKPIT_REFACTOR_SPEC.md` was built correctly and is
**not** the problem. Verified present and transport-neutral:

| Seam | Lives in | Status |
|---|---|---|
| Session lifecycle | `src/services/session_service.py` (`SessionService`, `CommandResult`) | ✅ transport-neutral, wired at `orchestrator.py:62` |
| Task dispatch | `orchestrator.submit_instruction(...)` | ✅ transport-neutral |
| Outbound notify | `src/services/notification_service.py` | ✅ fan-out seam, reads `telegram_interface` dynamically |
| Backend set | `src/backends/registry.py` (`build_backends`) | ✅ one declaration site (`orchestrator.py:64`) |
| Read view-model | `src/core/view_models.py` (`SessionView`) | ✅ JSON-ready |
| Event envelope | `src/core/observability.py` (`emit_event` NDJSON) | ✅ canonical, feeds all surfaces |
| Embedded HTTP-in-gateway pattern | `src/control/embedded_server.py` (`EmbeddedTaskServer`) | ✅ **proven in prod** |

**The middle layer the operator asked for already exists.** Telegram already calls
through it (`self.orchestrator.session_service`, `submit_instruction`, `notifier`).

---

## 2. What is actually wrong (the root cause of "3 apps")

The error is **not** a missing service abstraction. It is a **process-topology and
symmetry** error in how the *read/web* surface was attached:

**R1 — The read API was built as a separate, file-side-reading process.**
`src/control/dashboard.py` is a standalone FastAPI app launched by its own
entrypoint `dashboard_main.py`. It cannot call the orchestrator — it has no
reference to it — so it **re-reads `state/mesh.db` and `logs/events.ndjson`** and
even **re-derives node liveness itself** (`dashboard.py:135 _annotate_node_liveness`)
*because it is in a different process from the registry that owns that state.* That
re-derivation is a symptom: a sibling interface would just read the live singleton,
exactly as the task server now does after it was embedded (`embedded_server.py:4-14`).

**R2 — Asymmetry between interfaces.** Telegram is an *in-process* interface with a
live `orchestrator` handle (read **and** write). The web surface is an *out-of-process*
viewer with **no** handle (read **only**). They are not peers. This asymmetry is the
thing that *feels* like "Telegram is the app and web is a bolt-on" — because it is.

**R3 — The plan deferred the write/WS surface (Move F) and shipped only the
read-only dashboard as a placeholder.** `COCKPIT_REFACTOR_SPEC.md §14` lists Move F
("Write + WS/SSE surface… A *gateway* must accept commands; dashboard is read-only by
design") as deferred. So the *only* thing the browser ever got was the read-only
side-process. The plan was internally consistent — it just never scheduled the step
that turns "a read-only dashboard process" into "the gateway's own interface."

**R4 — No single ControlAPI module owns the HTTP surface as an interface.** There is
no analogue to `TelegramInterface` for HTTP. `dashboard.py` is the closest thing, but
it's framed as a "dashboard app," not as "the gateway's HTTP interface."

> Net: the abstraction is sound; the **wiring/topology** drifted. We do not need to
> rebuild services. We need to (a) embed the HTTP surface in the gateway process like
> the task server already is, (b) give it write + push so it's a *full* interface, and
> (c) delete the standalone dashboard process.

---

## 3. The fix as a milestone ladder (build order)

Each step is independently shippable and revertible. The first two deliver the
operator's literal ask ("one process"); the rest fill the web interface out to parity
with Telegram. **Nothing here changes a service in `src/services/` — they are reused.**

| Step | Name | Outcome | Depends on |
|---|---|---|---|
| **U1** | Embed the control API in the gateway | One process serves Telegram + HTTP; web talks to gateway, not dashboard | — |
| **U2** | Retire the standalone dashboard | `dashboard_main.py` gone; `dashboard.py` folded into the embedded ControlAPI | U1 |
| **U3** | Write surface (Move F, write half) | HTTP endpoints that call `submit_instruction` + `SessionService` create/bind/stop; idempotency-keyed | U1 |
| **U4** | Push surface (Move F, push half) | WS/SSE event push from the same in-process event stream, beside the existing poll | U1 |
| **U5** | Serve `web/` from the gateway in prod | Built `web/dist` served by the gateway; one origin, no vite in prod | U1 |
| **U6** | Telegram parity / symmetry pass | Telegram routes its remaining direct-store touches through services so both interfaces are provably equal | U3 |

`U1 + U2 + U5` = the operator's "one app" goal. `U3 + U4` = the web UI stops being
read-only. `U6` = the abstraction is *enforced*, not just *available*.

---

## 4. Step U1 — Embed the control API in the gateway (blueprint)

**Goal:** the gateway process hosts the HTTP control surface on its own event loop,
holding the same live `orchestrator` / `session_service` / `notifier` / registry
references Telegram holds — exactly as `EmbeddedTaskServer` already does for the mesh
task server.

### 4.1 The pattern already exists — copy it
`src/control/embedded_server.py` runs `uvicorn.Server.serve()` as an asyncio task on
the gateway loop, disables uvicorn's signal handlers (gateway owns signals), and is
lifecycle-managed by the orchestrator (`start()`/`stop()`). **U1 is a second instance
of this pattern for the control/web app.** Do not invent a new mechanism.

### 4.2 New module: `src/control/control_api.py`
A FastAPI app factory that takes the **live orchestrator** so handlers call services
directly (no file side-reads, no second `SessionStore`):

```python
def build_control_api(orchestrator) -> FastAPI:
    app = FastAPI(title="AI-Team Control API")
    # read endpoints call orchestrator.session_service / get_registry() — the SAME
    # singletons the dispatch path uses, so no liveness re-derivation is needed.
    # auth: reuse dashboard.py's bearer (DASHBOARD_TOKEN -> WORKER_TOKEN).
    ...
    return app
```

Move the *read* handlers from `dashboard.py` here, but change their data source from
"open the DB / read the file" to "call the injected orchestrator's services":
- `/api/sessions` → `orchestrator.session_service.list_views()`
- `/api/nodes` → `get_registry()` (in-process; liveness is already fresh → **delete**
  the `_annotate_node_liveness` workaround, the reason it existed is gone)
- `/api/tasks` → existing DB read (fine; it's a query) or a future task service
- `/api/events` → `observability.read_recent_events` (unchanged for now; U4 adds push)

### 4.3 New embedder: `EmbeddedControlServer` (or generalize `EmbeddedTaskServer`)
Same shape as `EmbeddedTaskServer`, but `app = build_control_api(orchestrator)` and a
different port (`DASHBOARD_PORT`, default 9003). Orchestrator owns its lifecycle.

### 4.4 Wiring in `orchestrator.start()` / `stop()`
Mount it next to the embedded task server (`orchestrator.py:905` area, guarded by a
config flag e.g. `CONTROL_API_ENABLED`, default on for the gateway). On `stop()`,
shut it down like the task server.

### 4.5 Done = exactly
- `python main.py` serves `/api/sessions|tasks|nodes|events` on `DASHBOARD_PORT`.
- Those endpoints return the **same shapes** `dashboard.py` returned (web adapters in
  `web/src/transport/` keep working unchanged).
- `/api/nodes` liveness is correct **without** `_annotate_node_liveness` (proves the
  in-process registry is the source).
- Telegram behavior byte-identical.

### 4.6 Do NOT
- Do not add write endpoints here (that's U3).
- Do not construct a second `SessionStore` — use `orchestrator.session_service.store`.
- Do not keep `dashboard_main.py` working "as well" — U2 deletes it; U1 just stops
  *requiring* it.

---

## 5. Step U2 — Retire the standalone dashboard

- Delete `dashboard_main.py`.
- Delete `src/control/dashboard.py` **after** its read handlers + auth + the inline
  HTML fallback are absorbed by `control_api.py`. (Keep the tiny inline HTML only if
  you still want a no-build fallback page; otherwise drop it — `web/` is the UI.)
- Remove any PM2 entry / docs that start the dashboard separately.
- Update `web/README.md`: the run instruction becomes "start the gateway
  (`python main.py`); `npm run dev` proxies `/api` to the gateway's `DASHBOARD_PORT`."

Done = grep for `dashboard_main`, `dashboard:app`, "read-only dashboard process"
returns nothing live; the only process is the gateway.

---

## 6. Step U3 — Write surface (the write half of Move F)

**Goal:** the web interface can *do* things, making it a peer of Telegram. Each
endpoint is a thin HTTP adapter over an **existing** service call — no new business
logic.

| Endpoint | Calls (existing) | Notes |
|---|---|---|
| `POST /api/instructions` | `orchestrator.submit_instruction(...)` | body carries `session_id`/`cwd`/`description`; **idempotency-keyed** (client sends a key; dedupe on it) |
| `POST /api/sessions` | `session_service.create_session(...)` | pass `origin=SessionOrigin(channel="web")` — the seam B.0 reserved for exactly this |
| `POST /api/sessions/{id}/bind` | `session_service.bind_active(...)` | |
| `POST /api/sessions/{id}/stop` | `orchestrator.cancel_task(...)` | |
| `POST /api/sessions/{id}/compact` | `orchestrator.compact_session(...)` | |

Rules (carried from `COCKPIT_REFACTOR_SPEC` Move B / `CommandResult` design):
- No transport may write session state directly — it goes through `SessionService`.
- Endpoints return the `CommandResult` shape (`ok` + machine `reason` code); the web
  client maps codes to wording (no prose in the API), same contract Telegram honors.
- This is the moment `SessionOrigin(channel="web")` stops being theoretical.

---

## 7. Step U4 — Push surface (the push half of Move F)

Add WS or SSE at `/api/events/stream` that pushes new events from the **same
in-process event stream** the gateway already writes, *beside* the existing
`/api/events?since=` poll (keep poll as the gap-recovery fallback — the contract says
events are not replayed; clients refresh state from the read endpoints on a gap).

Because the control API is now in-process (U1), this can subscribe to events in
memory rather than tailing a file. If that subscription seam doesn't exist yet, the
cheap interim is: keep tailing `logs/events.ndjson` and push deltas. Either is fine;
the canonical event-name mapping is Move I in the existing spec and can ride here or
stay backend-side.

---

## 8. Step U5 — Serve `web/` from the gateway (prod)

- Dev stays as-is: `npm run dev` (vite, port 5180) proxies `/api` → gateway port.
- Prod: gateway mounts `web/dist` as static files at `/` (FastAPI `StaticFiles`), so
  one origin serves both the UI and the API. No vite, no separate web process in prod.
- Done = open the gateway's port in a browser → the React app loads and talks to the
  same origin's `/api`.

---

## 9. Step U6 — Symmetry / enforcement pass (so this can't rot again)

The abstraction is currently *available* but not *enforced* — Telegram still reaches
into orchestrator internals in places (`orchestrator._backends`, direct
`session_store.bind/get` at the sites `COCKPIT_REFACTOR_SPEC` B.3 lists as "optional,
later"). Finish those:
- Route Telegram's remaining direct `session_store` mutations through `SessionService`
  (the `bind(...)-after-get(...)` sites).
- Confirm both interfaces use **only**: `session_service.*`, `submit_instruction`,
  `notifier`, `view_models`, registry — never the store or `_backends` directly.

Acceptance for the whole effort: **a grep proves no interface (`src/telegram/`,
`src/control/control_api.py`) writes session state except via `SessionService`, and
both interfaces obtain identical capabilities.** That is the machine-checkable form of
"one gateway, many equal interfaces."

---

## 10. Scope discipline (carried from COCKPIT_REFACTOR_SPEC §13)

- These services are **reused, not rebuilt**. If a handler needs logic that isn't on a
  service, **add it to the service** (so Telegram could call it too) — never inline it
  in the HTTP layer. Inlining business logic in an interface is exactly the drift that
  produced `dashboard.py`.
- Still **dropped** (do not build): `tool.*` events, `task.progress`, per-session
  `connection_unknown`, session `archived`, token streaming. See
  `docs/FRONTEND_BACKEND_GAP.md`.
- Each step = one commit, one revert line. U1/U2 are the load-bearing ones; ship and
  validate them before U3+.

---

## 11. One-paragraph brief for the implementing agent

> The service layer (`SessionService`, `submit_instruction`, `NotificationService`,
> backend registry, `SessionView`, `observability`) is correct and transport-neutral —
> reuse it, do not rebuild it. The defect is topology: the web/read API
> (`src/control/dashboard.py` + `dashboard_main.py`) runs as a **separate process** that
> side-reads `state/mesh.db` and `logs/events.ndjson`, so it can't call the orchestrator
> and isn't a peer of the in-process `TelegramInterface`. Fix it by following the
> **existing** `src/control/embedded_server.py` pattern: build a `control_api.py` FastAPI
> app that takes the live `orchestrator`, embed it on the gateway loop
> (`EmbeddedControlServer`), point its read handlers at `orchestrator.session_service` /
> `get_registry()` (deleting the `_annotate_node_liveness` workaround), delete
> `dashboard_main.py`/`dashboard.py`, add write endpoints that thinly wrap
> `submit_instruction` + `SessionService` (origin `channel="web"`) returning the
> `CommandResult` contract, add a WS/SSE push beside the poll, and serve `web/dist` from
> the gateway in prod. End state: `python main.py` is the only process; Telegram and Web
> are equal thin interfaces over one gateway.
