# U1 Checklist — Embed the Control API in the gateway process

Execution doc for Step U1 of `docs/CONTROL_SURFACE_UNIFICATION.md`. One process
serves Telegram + the HTTP control surface; the web UI talks to the gateway, not to
`dashboard_main.py`. **Extraction + embedding only — no new business logic, no write
endpoints (those are U3).**

How to use: each box has `Done =` (exact acceptance) and `Revert`. If a box is wrong,
edit this doc then implement — never code around it silently.

Baseline: branch `feat/control-surface-unify` off `main@5f246b7`.

---

- [x] **U1.1 — New module `src/control/control_api.py`: `build_control_api(orchestrator)`**
  A FastAPI app factory taking the **live orchestrator**. Read endpoints call the
  orchestrator's in-process services, NOT the DB/files directly:
  - `GET /health` → `{"status":"ok"}`
  - `GET /api/sessions` → `orchestrator.session_service.list_views(limit)` → `[v.to_dict()]`
  - `GET /api/nodes` → `get_registry()` live nodes (in-process; liveness already fresh)
  - `GET /api/tasks` → `get_db().list_tasks(limit)` (a query; fine to keep on DB)
  - `GET /api/events?since=` → `observability.read_recent_events(...)` (unchanged)
  - Auth: bearer reused from `dashboard.py` (`DASHBOARD_TOKEN` → `WORKER_TOKEN`).
  - **Done =** module imports clean; `build_control_api(orch)` returns a FastAPI app;
    `/api/sessions` returns the SAME shape `dashboard.py` returned (web adapters in
    `web/src/transport/` unchanged); `/api/nodes` is correct WITHOUT
    `_annotate_node_liveness` (registry is in-process).
  - **Revert =** delete the file.

- [x] **U1.2 — `EmbeddedControlServer` (clone of `EmbeddedTaskServer`)**
  In `src/control/embedded_server.py` add a sibling class (or a small generalization)
  that runs `uvicorn.Server.serve()` for `build_control_api(orchestrator)` on the
  gateway loop, with `install_signal_handlers = lambda: None` and the same
  start-poll/stop semantics. Takes the orchestrator so it can build the app.
  - **Done =** class mirrors `EmbeddedTaskServer` lifecycle (start polls `.started`,
    stop sets `should_exit` + awaits); no signal hijack.
  - **Revert =** delete the class.

- [x] **U1.3 — Config flag `CONTROL_API_ENABLED` (default True for gateway)**
  `config/settings.py`: add `control_api_enabled: bool = True` near `dashboard_port`,
  parsed in `reload_from_env` like the others. Reuse `dashboard_port` (9003) and
  `dashboard_token`.
  - **Done =** `config.mesh.control_api_enabled` reads the env var; default on.
  - **Revert =** remove the field + its env parse line.

- [x] **U1.4 — Wire into `orchestrator.start()` / `stop()`**
  Add `_start_embedded_control_api()` / `_stop_embedded_control_api()` mirroring the
  task-server pair (`orchestrator.py:905/943`); init `self._embedded_control_api = None`
  in `__init__` (next to `_embedded_task_server`, line 76). Call start after
  `_start_embedded_task_server()` (line 791); call stop in `stop()` near line 887.
  Guarded by `config.mesh.control_api_enabled`; failure logs loudly but does NOT take
  the gateway down (same try/except as the task server).
  - **Done =** `python main.py` serves `/api/sessions|tasks|nodes|events` on
    `DASHBOARD_PORT`; gateway start/stop is clean; Telegram behavior unchanged.
  - **Revert =** remove the two methods, the init line, and the two call sites.

- [ ] **U1.5 — Point `web/` dev proxy at the gateway, not the dashboard**
  `web/vite.config.ts`: `/api` proxy target stays `DASHBOARD_PORT` (9003) — now served
  by the gateway. Update `web/README.md` run steps: "start the gateway
  (`python main.py`); `npm run dev` proxies `/api` to the gateway." (web/ lives on
  `feat/webui-ui0`; this box is a note to apply when the branches reconcile — do NOT
  copy web/ into this branch.)
  - **Done =** README + (when merged) vite config reflect the gateway as the API host.
  - **Revert =** restore the prior README wording.

- [x] **U1.6 — Tests**
  `tests/test_control_api.py`: build the app with a fake/minimal orchestrator exposing
  `session_service.list_views()`; assert `/api/sessions` returns the expected dict
  shape and that auth rejects a bad token. Do NOT hit the paid CLI (see test guard).
  - **Done =** `pytest tests/test_control_api.py` green; no network/CLI.
  - **Revert =** delete the test file.

---

## U2 — Retire the standalone dashboard (done with U1)

- [x] **U2.1** — `git rm dashboard_main.py src/control/dashboard.py`.
- [x] **U2.2** — Preserve the unique `observability.read_recent_events` regression
  suite: `test_dashboard.py` → `tests/test_observability_events.py`, stripped of the
  HTTP-endpoint tests now covered by `test_control_api.py`.
- [x] **U2.3** — `docs/CONTROL_CONTRACT.md` reference impl now points at
  `control_api.py` / `EmbeddedControlServer`. No PM2 entry existed for the dashboard
  (it was launched manually), so `ecosystem.config.js` needed no change.
- **Done =** no live `from src.control import dashboard` / `dashboard:app` /
  `dashboard_main`; only the gateway process. **Revert =** `git revert` the U2 commit.

---

**U1 acceptance gate (all boxes):** `python main.py` is sufficient to serve the read
API the web UI needs; `dashboard_main.py` is no longer *required* (its deletion is U2);
Telegram byte-identical; `_annotate_node_liveness` is provably unnecessary in the
embedded path. **Do NOT** add write endpoints, WS push, or static serving here — those
are U3/U4/U5.
