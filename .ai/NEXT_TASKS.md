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

### Phase 2 — Standalone task server — ✅ DONE

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

**Cutover — ✅ DONE (2026-06-10, gateway stopped for this):**
- [x] **Key realization:** the live remote path `_process_task_remote`
      (orchestrator.py:1215) was *already DB-backed* — it falls through to
      `db.get_node()` for liveness (lines 1528-1533) and `_dispatch_to_node`
      polls the DB for results, taking no behavior from the in-memory registry.
      `_dispatch_or_run_local` (the only hard registry dependency via
      `registry.is_empty()` / `pick_capable()`) is **defined but never called** —
      dead code reserved for Phase 3. So the cutover did NOT require rewriting
      dispatch.
- [x] Added `MeshConfig.embedded_server` (env `MESH_EMBEDDED_SERVER`, **default
      False**). `_start_embedded_task_server()` now skips with a log line unless
      explicitly enabled — so the gateway no longer binds the task-server port;
      the standalone `ai-team-server` owns it. Embedded remains available as the
      single-process / fallback mode.
- [x] `_mesh_online_nodes()` (Telegram `/status`, `/nodes`) already reads
      `db.list_nodes()` — cross-process safe, no change needed.
- [x] **Kept `embedded_server.py`** rather than deleting it — it's now the
      explicit fallback mode the State-Sep plan wants (Phase 4), gated behind the
      flag. Not dead code.
- [x] Cutover integration test (standalone server + temp DB/port via
      `AI_TEAM_ENV_FILE`): server healthy → worker registers over HTTP → gateway
      in-process registry empty BUT reads node `online` from shared DB →
      `_start_embedded_task_server()` is a clean no-op (doesn't grab the port).
      Full suite: **138 passed, 13 skipped, 0 failures.**

**Deploy order (when enabling mesh for real):** start `ai-team-server` first,
then `ai-team-gateway`. Do NOT set `MESH_EMBEDDED_SERVER=true` while
`ai-team-server` runs — they'd clash on `MESH_TASK_SERVER_PORT`.

> **Testing mesh processes without touching prod:** point `AI_TEAM_ENV_FILE` at a
> throwaway `.env` (config loads it with `override=True`, so it beats process env
> vars), use a spare port + a temp `MESH_DB_PATH`. This is how the scaffolding
> above was verified while the live gateway kept running on `:9002`.

---

## ⏳ Later in this plan

- **Phase 3 — Standalone workers — PARTIAL (2026-06-10).** Worker daemon proven
  to run end-to-end against the standalone server, locally, no paid backend:
  `scripts/test_worker_loopback.py` drives the real `worker_main.py` +
  `server_main.py` (temp DB/ports) → register → `task_claimed` → execute →
  `task_result_posted` → DB terminal → SIGTERM drain. Execution failed cleanly on
  the `CLAUDE_ALLOWED_ROOT` allowlist, proving the safety boundary holds on the
  remote path. **"Never run in prod" risk retired.**
  - **Remaining (needs Tailscale / 2nd machine):** the gateway only routes to a
    worker when `session.machine_id != socket.gethostname()` (orchestrator.py:1223),
    so single-machine a worker just idles. Real worker execution + reducing the
    gateway pool to the 1 fallback + wiring `_dispatch_or_run_local` all land with
    the Phase 4 two-machine cutover (`docs/PHASE_4_RUNBOOK.md`). Deferred until a
    second node is on the tailnet. (`STATE_SEPARATION_PLAN.md` §Phase 3.)
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
