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

## 🟢 STATUS AT A GLANCE (2026-06-11)

| Phase | What | Status |
|---|---|---|
| 0 | Prereqs + DB cleanup | ✅ DONE |
| 1 | DB canonical read + smart recovery | ✅ DONE (battle-tested live) |
| 2 | Standalone task server + `TaskServerClient` | ✅ DONE |
| 3 | Standalone worker; machine-to-machine dispatch; gateway-restart resilience | ✅ DONE (live on 2 machines 2026-06-11) |
| 4 | Graceful degradation / fallback (server runs work when no nodes) | ⛔ NOT STARTED — last plan piece |
| T1 | CI/CD: auto-deploy `main` to the server | ⛔ NOT STARTED — standalone |
| T2 | Fix truncated Telegram output (long results) | ⛔ NOT STARTED — standalone |

**The mesh is LIVE.** Gateway + embedded task server on the **Pi5 (`kanebra`)**;
worker daemon on **`Horse`**. Tasks dispatch machine-to-machine and survive a
gateway restart (detach on shutdown → reattach on startup → deliver the worker's
real result). Detail: `docs/PROGRESS_LOG.md` (2026-06-11 entry).

> **DEPLOY NOTE — read before testing any gateway change.** The gateway runs on
> the **Pi5**. Code you edit/commit on a worker machine (e.g. `Horse`) only takes
> effect after: push → `git pull` on the Pi5 → **restart the gateway** → confirm
> `git log -1` on the Pi5 matches. A fix on `origin/main` that hasn't been
> pulled+restarted on the Pi5 will look like it "didn't work." (This cost us two
> debugging rounds on 2026-06-11.)

---

## ▶ NEXT: Phase 4 — Graceful degradation / fallback

**Goal:** when no worker nodes are available (none online, or task server
unreachable), the **server/gateway host runs the work itself** instead of failing
tasks — and reconciles state when nodes come back. The server already holds
session outputs/results, so it is a legitimate execution host, not just an
emergency stopgap. This is the last piece of the State Separation plan. Full
design: `docs/STATE_SEPARATION_PLAN.md` §Phase 4.

> **Architecture rule (updated 2026-06-11 — supersedes the old "exactly 1
> fallback worker"):** the server/gateway host keeps its **own embedded worker
> capacity** that runs tasks when no remote node is available. It is a real
> fallback execution host (configurable pool size, default ≥1), NOT a single
> emergency worker. When nodes ARE available, prefer them (load-balance /
> capability route); when none are, the server executes locally so work never
> stalls. The old "must keep exactly 1" cap is removed — the user wants real
> available capacity on the server, scalable.

These tasks are written to be picked up cold by an agent. Do them roughly in
order; each has explicit files + acceptance checks. **Test cost guard applies —
never run the paid Claude CLI from tests (see banner above).**

### P4.1 — Define + expose mesh-health criteria
- **What:** a single source of truth for "is the mesh healthy?". Healthy =
  task server reachable AND at least one node `online` in the DB. Surface it as a
  method (e.g. `MeshHealth.is_healthy()` / on the orchestrator) and in `/status`.
- **Where to look:** `src/control/task_server_client.py` (`is_healthy`,
  `list_nodes` already exist), `src/control/db.py` (`list_nodes`/node status),
  `src/orchestrator.py` (`_mesh_online_nodes`), `src/telegram/interface.py`
  (`/status`).
- **Acceptance:** unit test with a mocked client/DB asserting healthy vs each
  unhealthy case (server down; server up but zero online nodes). `/status` shows a
  clear mesh health line. No paid CLI.

### P4.2 — Server-side embedded worker capacity (replaces "exactly 1")
- **What:** the server/gateway host runs its **own embedded worker pool** that
  executes tasks when no remote node is available. Pool size is **configurable**
  (e.g. `SERVER_FALLBACK_WORKERS`, default ≥1 — NOT hard-capped at 1). Behaviour:
  when ≥1 remote node is online, prefer routing to nodes (load-balance); when none
  are online (P4.1 unhealthy), the server's embedded workers claim and execute the
  work locally so tasks never stall. The server already holds session
  outputs/results, so local execution is first-class, not a degraded hack.
- **Where to look:** `src/orchestrator.py` — `_task_worker`, `start()` pool
  creation (`config.system.max_concurrent_tasks`), `reload_worker_pool`; the mesh
  routing check in `process_task` (`route_remote`); `config/settings.py`
  (`MeshConfig` / system config for the new pool-size setting).
- **Acceptance:** with ≥1 node online, work routes to nodes and the server pool
  stays idle for those tasks. With zero nodes online, the server pool executes the
  task locally and it completes. Pool size honors the config value (e.g. set to 2
  and observe 2 concurrent local executions). Cover with tests using a fake
  backend (no paid CLI).

### P4.3 — Fallback can run recovery tasks
- **What:** when degraded, allow a small set of **recovery actions** to run on the
  fallback worker — minimally "restart/repoint the task server" — so the operator
  can self-heal from Telegram without SSH.
- **Where to look:** `src/telegram/interface.py` (command surface),
  `src/control/embedded_server.py` (fallback server mode), `ecosystem.config.js`
  (process names for restart).
- **Acceptance:** from Telegram while degraded, an operator can trigger the
  recovery action and see a clear result. Guard it behind owner-only auth (match
  existing ownership checks). No destructive action without confirmation.

### P4.4 — Reconcile fallback-completed work when the mesh recovers
- **What:** tasks completed by the fallback worker while degraded must be synced
  to the DB and their sessions reconciled once the mesh is healthy again, so there
  is no split-brain between JSON fallback state and the DB.
- **Where to look:** `src/control/db.py` (task/session upsert), `src/core/
  session_store.py` (dual-write), the recovery path
  `_recover_stale_busy_sessions` / `_reattach_remote_task` in
  `src/orchestrator.py` (2026-06-11 additions — mirror their conventions).
- **Acceptance:** simulate degraded completion (DB unavailable or mesh down) then
  recovery; assert the task + session land in the DB exactly once, session status
  is correct, and no duplicate Telegram notifications fire. Test only (no paid
  CLI).

### P4.5 — Wire the dead `_dispatch_or_run_local` (or delete it)
- **What:** `_dispatch_or_run_local` is defined but never called (was reserved for
  this phase). Either wire it as the fallback decision point (dispatch to mesh
  when healthy, run on the embedded worker when not) or delete it if P4.2 makes it
  redundant. Decide explicitly; don't leave dead code.
- **Where to look:** `src/orchestrator.py` (`_dispatch_or_run_local`,
  `registry.is_empty()`, `pick_capable()`).
- **Acceptance:** no unreferenced dispatch helper remains; whichever path is kept
  has a test.

### P4.6 — Tidy-ups unblocked by going live (low priority, do alongside)
- Give standalone dev/test scripts a `MESH_DB_PATH` override so they can never
  write prod `state/mesh.db` again (this is how 45 junk sessions leaked in —
  see Deferred). `scripts/test_*.py`, ad-hoc runs.
- Optional Phase 1 hardening test: stale BUSY session + completed DB result →
  recovery yields AWAITING_INPUT (not ERROR). (Largely covered now by the
  2026-06-11 restart work, but an explicit regression test is cheap.)

---

## ▶ Standalone tasks (independent of Phase 4 — dispatch any time)

### T1 — CI/CD: auto-deploy `main` to the server
- **Why:** the codebase is worked on from multiple machines (e.g. `Horse`) but the
  gateway runs on the **server (Pi5 `kanebra`)**. Today a change only lands after a
  manual `git pull` + gateway restart on the Pi5 — and forgetting that step has
  already caused "the fix didn't work" false alarms (2026-06-11). Automate it: a
  push to `main` should deploy to the server.
- **What to build:** on push to `main`, the server pulls the new code and restarts
  the affected PM2 processes (`ai-team-gateway`, and `ai-team-server` /
  `ai-team-worker` if their code changed). Two viable shapes — pick based on
  whether the Pi5 is reachable from CI:
  1. **GitHub Actions → SSH deploy** (if the Pi5 is reachable, e.g. over Tailscale
     from a self-hosted runner or via SSH action): on `push: branches: [main]`, SSH
     to the Pi5, `git pull`, `pm2 reload ecosystem.config.js` (or reload only
     changed apps), health-check `/health`, report status.
  2. **Pull-based agent on the Pi5** (if inbound SSH isn't desirable): a small
     systemd timer / PM2 cron on the Pi5 that polls `origin/main`, and on a new
     commit does `git pull` + `pm2 reload` + health check. Simpler/safer for a
     home server behind NAT.
- **Must include:** zero-downtime-ish reload (PM2 `reload`, not `restart`, where
  possible); a **health gate** — if `/health` doesn't come back `ok` after reload,
  log loudly and (option 1) fail the CI job; never auto-deploy a branch other than
  `main`; respect the **test cost guard** (CI must run with `AI_TEAM_TEST_MODE` so
  it can never invoke the paid Claude CLI).
- **Where to look:** `ecosystem.config.js` (process names), `server_main.py` /
  `worker_main.py` (entrypoints), `src/control/task_server.py` (`/health`),
  `docs/OPERATIONS_PM2.md` (existing ops conventions), `docs/PHASE_4_RUNBOOK.md`.
- **Acceptance:** a commit to `main` results in the server running that commit
  (`git log -1` on the Pi5 matches) with processes reloaded and `/health` green,
  **without any manual step**. Document the chosen mechanism in
  `docs/OPERATIONS_PM2.md`. Decide & note: does the worker on `Horse` also
  auto-update, or only the server? (Recommend: server auto-updates; worker nodes
  update on their own cadence to avoid mid-task restarts.)

### T2 — Fix truncated Telegram output (long results cut to one message)
- **Symptom:** a long task result arrives as a single Telegram message cut off at
  the end, with no continuation — the remainder is lost, not sent as follow-up
  messages.
- **Root cause (already traced 2026-06-11):** the Telegram side is NOT the
  problem — `notify_completion` already routes through `_send_long_message` →
  `_split_message` (4096-char chunks), which is correct. The truncation happens
  **upstream on the worker**: `src/worker/agent.py` caps output with
  `(raw.output or "")[:4000]` (and a second `str(raw)[:4000]` in the legacy
  branch) **before** the result is stored in the DB. So the gateway never receives
  the full text and has nothing to split.
- **What to do:** remove or greatly raise the worker-side `[:4000]` cap so the
  **full** backend output reaches the DB result, then let the existing Telegram
  splitter chunk it for delivery. If a cap is kept for DB sanity, make it large
  (e.g. configurable, ≥ a few hundred KB) and ensure it's applied as a safety
  bound, not a silent content truncation. Confirm the artifact JSON
  (`results/<task_id>.json`) also stores the full output.
- **Where to look:** `src/worker/agent.py:202` and the legacy branch ~`:213`
  (`_ER` result dict); `src/control/db.py` (result column — check it isn't itself
  capping); `src/telegram/interface.py:512` `_split_message` /
  `:528` `_send_long_message` (verify, no change expected); `_session_reply_text`
  and `notify_completion` paths in `src/orchestrator.py`.
- **Acceptance:** a task whose output exceeds ~4096 chars is delivered to Telegram
  as **multiple sequential messages** with nothing lost, and the full text is
  present in `results/<task_id>.json` and the DB result. Add a test that feeds a
  long output through the worker→DB→notify path with a fake backend and asserts no
  truncation + correct multi-chunk split (no paid CLI).

---

## ✅ Completed plan work (was "Current work")

### Phase 3 — Standalone worker — ✅ DONE (2026-06-11)

Mesh runs live across two machines (gateway+server on Pi5 `kanebra`, worker on
`Horse`). Machine-to-machine dispatch works AND in-flight remote tasks survive a
gateway restart via detach/reattach. The restart-cancel bug and two recovery
delivery bugs (placeholder message; dropped `backend_session_id`) are fixed.
Commits `f7b0777`, `f887ba1`, `5bc9137`. Detail: `docs/PROGRESS_LOG.md`
(2026-06-11). The earlier "blocked on a 2nd Tailscale node" caveat is retired.

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

## ⏳ Later — beyond the plan

- **VPS cutover (optional end-state):** the mesh is already split across two
  machines. Moving the server to a VPS is now just a *relocation* of the existing
  controller, not new architecture — follow `docs/PHASE_4_RUNBOOK.md` when/if you
  want the gateway off the Pi5. Operational; babysit it.

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
