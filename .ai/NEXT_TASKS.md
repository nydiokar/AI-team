# Next Tasks

**Active plan:** State Separation — `docs/STATE_SEPARATION_PLAN.md`.
**Orientation:** `.ai/CONTEXT.md` (read that first if you're new to the session).

State Separation supersedes the old standalone "VPS migration Phase 4". VPS
migration is now the end-state of Phases 2–3 (server on VPS, workers on local
machines). The operational cutover steps, when we get there, are in
`docs/PHASE_4_RUNBOOK.md`.

> **⚠ TEST COST GUARD (read before running tests):** tests can invoke the live,
> PAID Claude CLI and burn millions of tokens. `src/core/test_guard.py` blocks
> paid spawns under `AI_TEAM_TEST_MODE`; `tests/conftest.py` forces that mode +
> `MESH_ENABLED=false` + disables the watcher; e2e is deselected unless
> `--run-e2e`. Normal run: `pytest`. Real e2e (OpenCode only):
> `AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`. Claude/Codex are NEVER
> reachable from tests, even with `--run-e2e`.

---

## ▶ Current work — State Separation Phases 0 → 2

### Phase 0 — Prerequisites (partly done — finish this first)

Verified 2026-06-10:
- [x] `mesh.db` is WAL + `busy_timeout=5000`.
- [x] `test_mesh_local.py` exists (note: fails locally only because `.env`
      `WORKER_TOKEN` overrides its hardcoded test token — not a regression).
- [ ] **2 orphan tasks** still `pending`/`claimed` in `mesh_tasks` — mark `failed`.
- [ ] **DB/JSON session mismatch**: DB has **410** sessions, `state/sessions/`
      has **234** JSON files. Decide & reconcile (prune stale DB rows, or
      confirm JSON is the smaller current set) before fully trusting DB as
      canonical. This is the real blocker — don't skip it.
- [ ] Confirm `WORKER_TOKEN` set and `MESH_TAILSCALE_IP` is a **real IP** in
      `.env` (it was previously parsed as a literal comment string).

### Phase 1 — DB as canonical read source — ✅ DONE

Already in code (verified 2026-06-10):
- `SessionStore.get()` reads `db.get_session()` first, JSON fallback
  (`session_store.py:63`).
- `db.get_task_by_session()` exists (`db.py:510`).
- `_recover_stale_busy_sessions` uses the DB to distinguish completed /
  pending-or-claimed / ERROR instead of blindly marking ERROR
  (`orchestrator.py:299`).

Remaining (optional hardening): a focused test that simulates a stale BUSY
session with a completed DB result and asserts recovery → IDLE/AWAITING_INPUT,
not ERROR.

### Phase 2 — Standalone task server — ⏭ NEXT TO BUILD

Goal: task server runs as its own PM2 process; the gateway talks to it over HTTP
instead of embedding it.

1. Add `ai-team-server` entry to `ecosystem.config.js` (uvicorn on
   `task_server:app`, bound to `MESH_TAILSCALE_IP:MESH_TASK_SERVER_PORT`,
   kill timeout 10s, its own out/error logs).
2. Add a thin `server_main.py` entry point (loads `.env`, runs uvicorn).
3. Add a `TaskServerClient` in the gateway (stdlib `urllib`, mirror the worker's
   `_HTTP`): `enqueue_task`, `get_task_status`, `get_health`, `list_nodes`,
   Bearer `WORKER_TOKEN`.
4. Repoint `_dispatch_to_node` / recovery / health from the in-process
   `get_registry()` singleton to `TaskServerClient` (cache node list ~5s TTL).
5. Retire `src/control/embedded_server.py` and the
   `_start/_stop_embedded_task_server()` wiring.

**Risk:** gateway loses the in-process NodeRegistry singleton — discovery becomes
an HTTP round-trip. Mitigate with the short-TTL client cache; the registry
already persists to DB. Deploy order: start `ai-team-server` first, then restart
gateway. Full detail: `docs/STATE_SEPARATION_PLAN.md` §Phase 2.

---

## ⏳ Later in this plan

- **Phase 3 — Standalone workers:** enable `ai-team-worker` PM2 entry; reduce
  gateway in-process workers to 1 (the fallback); `process_task` tries mesh
  first, falls back on failure; respect session affinity. Worker code exists but
  has never run in prod — run it foreground first to shake out token/port/nudge
  issues. (`STATE_SEPARATION_PLAN.md` §Phase 3.)
- **Phase 4 — Fallback + graceful degradation:** define mesh-health criteria; the
  1 embedded worker + JSON path activate only when the mesh is down; fallback can
  run recovery tasks (e.g. "restart the task server"); sync fallback-completed
  tasks to DB on recovery. (`STATE_SEPARATION_PLAN.md` §Phase 4.)
- **VPS cutover:** once Phases 2–3 are solid, execute `docs/PHASE_4_RUNBOOK.md`
  (server→VPS, this PC→worker). Operational, babysit it.

---

## ✅ Completed (history — detail in `docs/PROGRESS_LOG.md`)

- **Phase 8** — mesh DB foundation (`MeshDB`, shadow-write, seed script).
- **Phase 9 Steps 1–3 + B** — task server, worker daemon, orchestrator remote
  routing wired into `process_task`; adversarially reviewed + tested.
- **Phase 9 Step C** — real two-machine test (LP-1 worker over Tailscale, DB-backed
  node picker).
- **Step D1** — task server embedded in gateway (shared in-process registry).
- **D1.5** — observability spine (`src/core/observability.py`, `/metrics`).
- **D2** — worker execution logging (traceback → `error_detail`, `task_failed`).
- **D3** — `/nodes` + `/node` Telegram commands.
- **D4** — `/status` + `/session_list` UX overhaul.
- **D5** — `scripts/fix_session_machine_ids.py`.
- **D6** — PM2 `ai-team-worker` entry made bootable + `PHASE_4_RUNBOOK.md`.

---

## Deferred (valid, lower priority)

- Backend hooks (session-ID detection, PreToolUse security, PostToolUse quality
  gates) — `docs/BACKEND_HOOKS_STRATEGY.md`.
- Codex end-to-end validation.
- OpenCode server cross-machine sessions (needs shared DB mount).
- Postgres migration — trigger: >5 nodes or observed SQLite write contention.
