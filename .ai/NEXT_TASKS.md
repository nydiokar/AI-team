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
| T1 | CI/CD: auto-deploy `main` to the server | ✅ DONE (2026-06-11) — enable on Pi5 |
| T2 | Fix truncated Telegram output (long results) | ✅ DONE (2026-06-11) |
| T3 | Watched jobs: notify on long-script completion | ⛔ NOT STARTED — standalone |
| T4 | Reclaim in-flight tasks dropped by a worker restart | ⛔ NOT STARTED — **resilience gap, hit live 2026-06-11** |

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

### T1 — CI/CD: auto-deploy `main` to the server — ✅ DONE (2026-06-11)
- **Shipped:** pull-based poller `scripts/auto_deploy.sh` + `ai-team-deploy` PM2
  entry (cron_restart `*/2 * * * *`, disabled by default). Chose pull-based over
  GitHub Actions→SSH because the Pi5 is behind home NAT (user did not want CI in
  the tailnet). Flow: `git fetch` → ff-only → `pm2 reload ai-team-gateway` →
  poll `/health` (60s) → **roll back + exit non-zero on health failure**.
  Docs-only pushes fast-forward but skip the reload. Refuses any branch but
  `main`; single-flight lock; exports `AI_TEAM_TEST_MODE`.
- **SCOPE — gateway host ONLY.** Not enabled on worker boxes: auto-restarting a
  worker mid-task drops its claim (the **T4** bug). Revisit workers after T4.
- **Operator step (one-time, on the Pi5):** `pm2 start ecosystem.config.js
  --only ai-team-deploy && pm2 save`. Docs: `docs/OPERATIONS_PM2.md` §Auto-Deploy.
- **NOT yet activated** — the PM2 entry must be started on the Pi5 by an operator
  (or the server agent). Until then, deploys remain manual.

<details><summary>Original task spec (for reference)</summary>

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

</details>


---

### T3 — Watched jobs: notify Telegram when a long-running script finishes
- **Symptom / why:** an agent on a worker (e.g. `Horse`) starts a long-running,
  often **detached** script and reports "it's running" — the task turn ends and
  the script then runs owned by nobody. When it finishes/fails, **nobody is
  subscribed**, so the user is never told. The gateway already does a server-
  initiated push on task completion (`_session_reply_text` →
  `app.bot.send_message`); we reuse that channel — this is **not** a new reverse
  portal.
- **The trap to avoid:** do **NOT** model the script as a `mesh_tasks` row. A task
  that stays `claimed` for hours pins a `max_concurrent` slot, keeps the session
  `BUSY`, and feeds the stale-busy reattach loop a task that won't terminate.
  That's a real state-management bug. Watched jobs are a **separate first-class
  entity** (`jobs` table), orthogonal to the task/session lifecycle.
- **The other trap:** do **NOT** auto-notify on every script an agent runs — that
  spams the user on every `npm test`/`ls`. Watching is **opt-in / explicit
  registration only**; default is silent. Detection is by **observing the
  process** (PID/pgid exit), not by trusting the script to call back. No open
  `POST /notify` endpoint for arbitrary processes (that's the portal).
- **What to build:** a `jobs` table (next migration in `src/control/db.py` —
  leave `mesh_tasks` untouched) + a `_job_watcher_loop` on the worker (sibling of
  `_poll_loop`/`_heartbeat_loop`) that spawns detached, reaps by observation, and
  POSTs completion; `/jobs` endpoints on the task server (same Bearer auth); an
  orchestrator branch that turns a terminal `jobs` row into a Telegram push; a
  `/jobs` Telegram command for visibility. Optional: `notify_agent` to auto-
  dispatch a follow-up `resume_session` task on completion. The `jobs` table
  doubles as the seed of the future dashboard.
- **Blast radius:** medium new machinery, **low** interference (jobs never touch
  the semaphore / session-BUSY / reattach loop), lowest future-fuckup surface of
  the options considered.
- **Full spec (goals, explicit no-goals/anti-solutions, schema, build order,
  acceptance):** `docs/WATCHED_JOBS_SPEC.md`. Build in the 6 independently-
  shippable steps in §9.
- **Where to look:** `src/control/db.py` (`_get_migrations`/`_CURRENT_VERSION`),
  `src/worker/agent.py` (`run()` loop registration), `src/control/task_server.py`,
  `src/orchestrator.py` (`_session_reply_text` push path), `src/telegram/
  interface.py`.
- **Acceptance:** unwatched script → zero notifications; registered job → exactly
  one Telegram message (label, status, exit code, log tail) on terminal; while a
  job runs, task slots stay free and the session is not `BUSY`; worker restart
  reconciles `running` jobs; no `mesh_tasks` row ever created for a job. All tests
  honor the **test cost guard** — watched-job tests use trivial real processes
  (`sleep`, exit-N scripts), never a paid backend.

---

### T4 — Reclaim in-flight tasks dropped by a worker restart

#### What happened (live incident, 2026-06-11)
A task (`task_94d78ff9`, session `2ba1b4aee6d2`) was **claimed** by the `Horse`
worker at 17:44:41 local. ~28s later the worker process was **restarted**
(`pm2 restart ai-team-worker` — PM2 `created at 14:45:09.965Z`, `restarts: 1`,
**`unstable restarts: 0`** → a clean commanded restart, NOT a crash; the error
log is empty, no traceback). The user reached the box only via the gateway and
did not run it; the restart was issued from the agent session immediately after
a `git merge`, and the agent then lost context — so the agent restarted the
worker without retaining that it had. Either way the *mechanism* is what matters,
not who pressed the button.

**Result:** the gateway showed `❌ Task failed: Dispatch timeout: no worker
picked up the task within 600s`. That terminal message is **correct given the
state** — but the underlying state was wrong: the task sat `claimed` by a worker
that was no longer running it, and nothing ever freed it.

#### Root cause (traced in code)
- `_handle_task` (`src/worker/agent.py:407`) claims, then `await _execute_task`
  (line 435). A restart kills the process mid-execute.
- On **Windows, `pm2 restart` is effectively a hard kill** — the graceful drain
  (`run()` lines 487-494: wait 30s, then `t.cancel()`) does not run. The
  in-flight coroutine just dies.
- Even the graceful path only does `t.cancel()` locally — it **never tells the
  server the task was abandoned**. The DB row stays `status='claimed',
  claimed_by='Horse'`.
- **There is no reaper.** `claim_task` (`src/control/db.py:404`) only does
  `pending → claimed`; `claimed_at` is written (db.py:106/412) but **never read**
  for staleness. So an orphaned claim is never reset to `pending`.
- The gateway poll (`_dispatch_to_node`, `src/orchestrator.py:2469`) only reacts
  to `completed`/`failed`/`failed_node_offline`. A stuck `claimed` row is invisible
  to it, so it waits the full `oneoff_queue_timeout_sec` (600s) and then
  `fail_task`s — the symptom the user saw.

> **The undesired state = a `claimed` `mesh_tasks` row whose `claimed_by` worker
> is no longer executing it, with nothing to detect or recover it.** This is the
> worker-side analogue of the *gateway*-restart resilience already shipped in
> Phase 3 (detach/reattach) — that work covered the gateway bouncing, NOT the
> worker bouncing. This is the missing half.

#### Fix (design — agreed with the user)
Make a dropped claim **reclaimable** instead of dead:
1. **Worker releases its claims on shutdown (best-effort, fast path).** In the
   drain path (and a `SIGTERM`/`atexit`/`KeyboardInterrupt` hook), for each task
   still in `self._active`, POST a new **`/tasks/{id}/release`** (or reuse a
   "requeue" endpoint) that sets the row back to `status='pending', claimed_by=NULL`
   so another worker (or the restarted one) can re-claim immediately. Must be
   quick + best-effort — a hard kill may skip it, which is why step 2 exists.
2. **Server-side stale-claim reaper (authoritative safety net, covers hard kill).**
   A periodic sweep (task server loop, or piggy-backed on heartbeat handling):
   any row `status='claimed'` whose `claimed_by` node is **offline** (missed
   heartbeats) **OR** whose `claimed_at` is older than a `claim_lease_sec`
   threshold with no progress → reset to `pending` (or `failed_node_offline` if it
   should not be retried). This is the real fix; the worker-side release is just a
   fast path. Mirrors the gateway's `_recover_stale_busy_sessions` posture.
3. **Idempotency / double-execution guard.** If a slow worker finishes a task
   that was already requeued + re-run elsewhere, the late `POST /result` must not
   clobber a newer terminal state. Gate `submit_result` on `claimed_by ==
   payload.node_id` (already partially there, `task_server.py:311`) AND only
   accept results for non-terminal rows; drop/ignore stale posts.
4. **(Optional) shorten the user-visible failure.** Once the reaper frees stale
   claims promptly, a worker that bounces no longer costs the full 600s — the
   freed task is re-dispatched in seconds. Consider lowering the 600s only after
   the reaper exists.

#### Where to look
- `src/worker/agent.py` — `_handle_task` (claim/execute), `run()` drain
  (lines 487-498), `_on_sigterm` (502); add the release-on-shutdown + a Windows
  shutdown hook (PM2 hard-kills on Windows — verify SIGTERM even fires).
- `src/control/db.py` — `claim_task` (404), `claimed_at` (106); add
  `release_task`/`requeue_task` + a `list_stale_claims(lease_sec)` query.
- `src/control/task_server.py` — `/tasks/{id}/claim`, `submit_result` auth gate
  (311); add `/tasks/{id}/release`; run the reaper loop (or expose a sweep the
  gateway calls).
- `src/orchestrator.py` — `_dispatch_to_node` (2469) poll: optionally treat a
  requeued task transparently; `_recover_stale_busy_sessions` (~299) as the
  pattern to mirror.

#### Acceptance
- A worker that is **hard-killed** mid-task (simulate: kill the process, do not
  send SIGTERM) leaves a `claimed` row that the **reaper** resets to `pending`
  within `claim_lease_sec`, and another worker re-claims + completes it — no 600s
  timeout, task succeeds.
- A worker that is **gracefully** stopped releases its in-flight claim
  immediately (faster than the lease) and it is re-claimed.
- A late `POST /result` from a superseded worker does **not** overwrite a newer
  terminal result (idempotency test).
- All tests honor the **test cost guard** — use a fake/trivial backend, never the
  paid Claude CLI. (See `tests/conftest.py`; mirror `test_task_server_client.py`.)

---

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
- Postgres migration — trigger: >5 nodes or observed SQLite write contention.
