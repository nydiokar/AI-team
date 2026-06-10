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

### Phase 0 — Prerequisites — ✅ DONE (2026-06-10)

- [x] `mesh.db` is WAL + `busy_timeout=5000`.
- [x] `test_mesh_local.py` exists (note: fails locally only because `.env`
      `WORKER_TOKEN` overrides its hardcoded test token — not a regression).
- [x] **2 orphan tasks** (`task_8cc6b7c4`, `task_73f11521`, both
      `resume_session`/`pending`, never claimable since mesh isn't running)
      marked `failed` via `db.fail_task`.
- [x] **DB/JSON session mismatch investigated and reconciled.** Was DB 410 /
      JSON 234. Two independent causes:
      - **183 only-in-DB** = benign history: seeded rows (`seed_db_from_json.py`)
        plus old sessions whose JSON was later pruned off disk. DB is correctly a
        superset. Left as-is.
      - **7 only-in-JSON** = a real **shadow-write gap**: `SessionStore.create()`
        wrote the JSON file but never shadow-wrote to the DB, so a session
        created-then-closed with no task (no intervening `save()`) never reached
        the DB. Root-cause **fixed**: `create()` now calls `_shadow_write()`
        (`session_store.py:53-54`). The 7 stragglers were backfilled.
      - Result after backfill: only-in-JSON = 0; orphan tasks = 0.
      - Backup before writes: `state/mesh.db.bak-phase0-20260610`.
- [x] **DB trust cleanup (so 3-process split starts from clean state).** Profiled
      all 418 DB sessions: only **162** had real task history; the other 256 were
      45 test/fixture leftovers (`.test_session_artifacts/*`, `/test/repo`,
      `test-pc`/`other-pc`/`trial-node`, `solastic`, `urban_mage`) + 215 abandoned
      zero-task shells. **Purged all 256 zero-task sessions** (+34 orphan
      `task_events`), VACUUM'd. DB now = **162 sessions, every one with ≥1 task**;
      0 orphans; live JSON working set (234 files) untouched. Backup:
      `state/mesh.db.bak-cleanup-20260610-181929`. Script: `scripts/analyze_sessions.py`.
      Note: the pytest suite already isolates the DB per-test (conftest
      `_isolate_db`, added in Phase 1); the leaked rows came from **standalone
      dev/test scripts** run directly against the prod DB — see follow-up below.
- [ ] Confirm `WORKER_TOKEN` set and `MESH_TAILSCALE_IP` is a **real IP** in
      `.env` (was parsed as a literal comment string). *Operator action — do
      before flipping `MESH_ENABLED=true`; not a code task.*

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

### Phase 2 — Standalone task server — 🔨 IN PROGRESS

Goal: task server runs as its own PM2 process; the gateway talks to it over HTTP
instead of embedding it. Building **incrementally** — embedded path stays intact
until the standalone path is proven, so the running gateway is never broken.

**Done (2026-06-10, scaffolding — additive, embedded still default):**
- [x] `server_main.py` — thin PM2 entry (mirrors `worker_main.py`): loads `.env`,
      inits observability as `node=controller`, runs `uvicorn` on
      `src.control.task_server:app`, binds `MESH_TAILSCALE_IP or 127.0.0.1`
      : `MESH_TASK_SERVER_PORT`.
- [x] `src/control/task_server_client.py` — `TaskServerClient` (stdlib `urllib`,
      Bearer `WORKER_TOKEN`): `get_health`/`is_healthy`, `list_nodes` (5s TTL
      cache, returns stale cache on transient failure), `get_node`, `nudge`,
      `get_task_status`. Failures degrade to None/[] so an unreachable server
      reads as "mesh unhealthy" (Phase 4 trigger), not a crash.
- [x] `ai-team-server` PM2 entry added to `ecosystem.config.js` — **disabled by
      default**; comments warn not to run embedded + standalone at once (port
      clash on `MESH_TASK_SERVER_PORT`).
- [x] Verified in isolation (temp env file via `AI_TEAM_ENV_FILE`, port 9099,
      temp DB — never touched prod or the live gateway): server boots, migrates,
      serves; client sees a node registered over HTTP; bad token → `[]`.
      `tests/test_task_server_client.py` (8/8).

**Remaining (the actual cutover — the risky part):**
- [ ] Repoint `_process_task_remote` / `_dispatch_to_node` / recovery / health
      from the in-process `get_registry()` singleton to `TaskServerClient`.
      (orchestrator.py:1490, 1519, 2402-2407 call `get_registry()` directly today.)
- [ ] Retire `src/control/embedded_server.py` and the
      `_start/_stop_embedded_task_server()` wiring (orchestrator.py:506, 598,
      610-645).
- [ ] Cutover test (gateway stopped): start `ai-team-server`, restart gateway,
      confirm node discovery + dispatch work over HTTP. Deploy order: server
      first, then gateway.

**Risk:** this deliberately re-opens the cross-process registry gap that D1
closed (dispatch reads the in-memory registry; standalone moves it out). The
short-TTL client cache mitigates discovery cost; the registry already persists to
DB as a backstop. Full detail: `docs/STATE_SEPARATION_PLAN.md` §Phase 2.

> **Testing mesh processes without touching prod:** point `AI_TEAM_ENV_FILE` at a
> throwaway `.env` (config loads it with `override=True`, so it beats process env
> vars), use a spare port + a temp `MESH_DB_PATH`. This is how the scaffolding
> above was verified while the live gateway kept running on `:9002`.

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

- **Standalone dev/test scripts default to prod `state/mesh.db`.** The pytest
  suite is isolated, but `scripts/test_*.py` and ad-hoc runs are not — that's how
  the 45 junk sessions leaked in. Give those scripts a `MESH_DB_PATH` override (or
  a shared `--test-db` helper) so they can never write prod state again.
- Backend hooks (session-ID detection, PreToolUse security, PostToolUse quality
  gates) — `docs/BACKEND_HOOKS_STRATEGY.md`.
- Codex end-to-end validation.
- OpenCode server cross-machine sessions (needs shared DB mount).
- Postgres migration — trigger: >5 nodes or observed SQLite write contention.
