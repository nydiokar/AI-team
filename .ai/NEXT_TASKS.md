# Next Tasks

**Purpose:** this file is only the active task queue and short completion ledger.
Use `.ai/CONTEXT.md` for orientation and `docs/PROGRESS_LOG.md` for detailed
history.

**Last updated:** 2026-06-21
**Plan of record:** `docs/STATE_SEPARATION_PLAN.md`

> **Test cost guard:** normal test command is `pytest`. Tests must not invoke
> paid Claude/Codex CLIs. `tests/conftest.py` forces `AI_TEAM_TEST_MODE`; use fake
> or trivial processes for mesh/job tests.

---

## Status

| Item | Status | Notes |
|---|---|---|
| State Separation P0-P3 | DONE | Mesh is live across `kanebra` + `Horse`; gateway restart resilience shipped. |
| P4 fallback/degradation | CHECK CURRENT CODE | Older plan said not started; verify before adding new work. |
| T1 auto-deploy | DONE | Pull-based PM2 deploy agent exists; operator activation may still be manual. |
| T2 long Telegram output | DONE | Worker no longer silently truncates; Telegram splitter handles delivery. |
| T3 watched jobs | DONE | `jobs` table, `/jobs` API, worker watcher, MCP registration, tests exist. |
| T3.1 watched job process identity | DONE | Worker probes PID + process start/command, records `last_checked_at`, and marks mismatches `lost`. |
| T4 worker-restart claim reclaim | DONE | Release endpoint, stale-claim reaper, startup sweep, late-result guard, tests exist. |
| Cockpit M1 (session core) | DONE (branch) | `feat/session-service-m1`: backend `registry.py`, `SessionService` (create/bind), `SessionOrigin` (DB migration 12), `docs/CONTROL_CONTRACT.md`. Telegram byte-identical. Separate track from State Separation. See `docs/M1_CHECKLIST.md` + PROGRESS_LOG 2026-06-21. M2+ deferred. |

---

## Active / Next

### P4 — Graceful Degradation / Fallback Audit

**Why:** old notes claimed P4 was not started, but the code has moved since then.
Before building anything, audit the current implementation and decide whether P4
is complete, partially complete, or obsolete.

**Check:**
- Does the gateway/server host execute locally when no remote node is available?
- Is fallback capacity configurable, and is it not hard-capped at one worker?
- Does `/status` clearly show mesh health and fallback mode?
- If DB/task-server access is degraded, are task/session writes reconciled
  cleanly once the mesh recovers?
- Is `_dispatch_or_run_local` still dead code, wired intentionally, or removable?

**Where to look:** `src/orchestrator.py`, `config/settings.py`,
`src/control/task_server_client.py`, `src/telegram/interface.py`,
`src/core/session_store.py`, and `docs/STATE_SEPARATION_PLAN.md`.

**Acceptance:** update `.ai/CONTEXT.md` and this file with the result of the
audit. If gaps remain, split them into small P4.x tasks here. If no gaps remain,
move P4 to the completed ledger and put details in `docs/PROGRESS_LOG.md`.

### M5 — Mesh Health History / Trend Ledger

**Why:** the self-awareness branch exposes current mesh state (`/status`,
`/nodes`, `/node`, `/metrics`) and emits reconciliation events, but operators
still have to reconstruct trends from logs. Stale-busy count, live-state
freshness, slot utilization, and node availability are important enough to keep
as queryable history once the live mesh sees real incidents.

**Task:** add a lightweight historical ledger for mesh health snapshots and/or
reconciliation events. Keep it separate from `mesh_tasks`; this is operational
telemetry, not task lifecycle state.

**Possible shape:**
- table such as `mesh_health_samples` or append-only event rows keyed by
  timestamp/node/session
- periodic sample of aggregate slot load, stale-live-state nodes, stale-busy
  count, online/offline node count
- compact Telegram or CLI view for recent anomalies
- retention policy so the SQLite DB does not grow without bound

**Acceptance:** an operator can answer "did stale-busy/live-state freshness get
worse over the last hour/day?" without manually reading logs.

---

## Completed Ledger

### D1 — Script/Test DB Safety — DONE

Implemented:
- `scripts/_test_env.py` creates a throwaway env file and temp `MESH_DB_PATH`
  before project config is imported.
- `scripts/test_embedded_server.py`, `scripts/test_mesh_local.py`,
  `scripts/test_routing_integration.py`, and
  `scripts/test_state_separation_phase1.py` use the helper by default.
- Removed the old `.env` rename workaround from the Phase 1 script.

Verification:
- `python scripts/test_mesh_local.py`
- `python scripts/test_embedded_server.py`
- `python scripts/test_routing_integration.py`
- `python scripts/test_state_separation_phase1.py`

### T4 — Reclaim In-Flight Tasks Dropped By Worker Restart — DONE

Implemented:
- `src/control/db.py`: `release_task`, `release_node_claims`,
  `list_stale_claims`, claim incarnation tracking.
- `src/control/task_server.py`: `/tasks/{task_id}/release`, stale-claim reaper,
  late-result guard.
- `src/control/node_registry.py`: releases orphaned claims when a node
  re-registers.
- `src/worker/agent.py`: best-effort release of active claims during drain.
- `tests/test_claim_reaper.py`: DB, API, stale claim, incarnation, and
  idempotency coverage.

Residual risk: a hard OS kill can still interrupt local process cleanup, but the
server-side reaper is the authoritative safety net.

### T3 — Watched Jobs — DONE

Implemented:
- `jobs` table and DB helpers.
- `/jobs`, `/jobs/{id}`, `/jobs/{id}/start`, `/jobs/{id}/done` endpoints.
- Worker `_job_watcher_loop` that spawns, logs, observes, and reports watched
  commands.
- MCP `watch_job` registration path via `scripts/mcp_jobs.py`.
- Completion notification from the task server.
- `tests/test_watched_jobs.py`.
- Resilience follow-up T3.1: `jobs` migration v10 adds
  `last_checked_at`, `last_probe_error`, `last_seen_command`, and
  `last_seen_started_epoch`; the worker probes process identity and reports
  stale/reused PIDs as `lost`.

Important operational detail: a watched job's durable identity is the `job_id`
plus `node_id`, `pid`, `pgid`, `command`, and `log_path`. For a job running on
`Horse`, process verification must happen on `Horse`; the controller only stores
what the worker reports.

### T2 — Long Telegram Output — DONE

Worker output is no longer silently capped at 4000 chars. `_bound_output()` keeps
a large configurable DB-sanity bound, and Telegram chunking handles delivery.
Remote artifacts now mirror `output` into `raw_stdout`.

Tests: `tests/test_output_truncation.py`.

### T1 — Auto-Deploy Main To Server — DONE

Pull-based deploy agent exists in `scripts/auto_deploy.sh` with PM2 entry
`ai-team-deploy`. It fast-forwards `main`, reloads the gateway, health-checks,
and avoids worker auto-restart until worker restart behavior is safe.

Operator note: activation may still require starting/saving the PM2 entry on the
server host.

### State Separation P0-P3 — DONE

Mesh DB foundation, DB-first session reads, standalone task server/client,
worker daemon, real two-machine dispatch, and gateway-restart reattach were
shipped before this cleanup. Detailed history belongs in `docs/PROGRESS_LOG.md`,
not here.

---

## Deferred

- Backend lifecycle hooks: `docs/BACKEND_HOOKS_STRATEGY.md`.
- Codex end-to-end validation.
- OpenCode server cross-machine sessions; likely needs shared DB/mount semantics.
- Postgres migration trigger: more than ~5 nodes or observed SQLite write
  contention.
