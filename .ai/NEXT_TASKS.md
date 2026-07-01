# Next Tasks

**Purpose:** this file is only the active task queue and short completion ledger.
Use `.ai/CONTEXT.md` for orientation and `docs/PROGRESS_LOG.md` for detailed
history.

**Last updated:** 2026-07-01
**Plan of record:** `docs/STATE_SEPARATION_PLAN.md`

> **Test cost guard:** normal test command is `pytest`. Tests must not invoke
> paid Claude/Codex CLIs. `tests/conftest.py` forces `AI_TEAM_TEST_MODE`; use fake
> or trivial processes for mesh/job tests.

---

## Status

| Item | Status | Notes |
|---|---|---|
| State Separation P0-P3 | DONE | Mesh is live across `kanebra` + `Horse`; gateway restart resilience shipped. |
| P4 fallback/degradation | DONE / SUPERSEDED | 2026-07-01 audit closed the old Phase 4 plan: status visibility, DB-reconcile spool, and mesh health transition events are implemented; obsolete one-worker fallback language is retired. |
| T1 auto-deploy | DONE | Pull-based PM2 deploy agent exists; operator activation may still be manual. |
| T2 long Telegram output | DONE | Worker no longer silently truncates; Telegram splitter handles delivery. |
| T3 watched jobs | DONE | `jobs` table, `/jobs` API, worker watcher, MCP registration, tests exist. |
| T3.1 watched job process identity | DONE | Worker probes PID + process start/command, records `last_checked_at`, and marks mismatches `lost`. |
| T4 worker-restart claim reclaim | DONE | Release endpoint, stale-claim reaper, startup sweep, late-result guard, tests exist. |
| Cockpit M1 (session core) | DONE (branch) | `feat/session-service-m1`: backend `registry.py`, `SessionService` (create/bind), `SessionOrigin` (DB migration 12), `docs/CONTROL_CONTRACT.md`. Telegram byte-identical. Separate track from State Separation. See `docs/M1_CHECKLIST.md` + PROGRESS_LOG 2026-06-21. M2+ deferred. |

---

## Active / Next

### P4 — Graceful Degradation / Fallback Cleanup — DONE

**Audit result (2026-07-01):** the old Phase 4 plan is partially implemented and
partially superseded. Do not build the archived "exactly 1 fallback worker" plan.

Already true in code:
- Gateway/server host keeps configurable local worker capacity via
  `config.system.max_concurrent_tasks`; it is not hard-capped at one worker.
- `src/services/session_store.py` reads sessions from DB first and falls back to
  JSON files.
- `MESH_EMBEDDED_SERVER` can run the task server in-process for single-process /
  fallback deployments.
- `TaskServerClient` and `MeshHealth` model task-server unavailability as
  degraded health instead of crashing.
- Pinned remote sessions intentionally have **no local fallback** because
  `backend_session_id` is machine-local and session affinity is a hard
  correctness rule.
- `_dispatch_or_run_local` is historical/compatibility routing code; the hot
  session path uses `_process_task_remote` for pinned remote sessions and local
  worker execution otherwise. Do not remove it without a dedicated call-graph
  pass and tests.
- `/status` now reports mesh mode, online/total nodes when known, and local
  fallback worker capacity.
- Completed tasks whose `mesh_tasks` completion/enrichment write fails are now
  spooled under `results/reconcile/<task_id>.json` and replayed on gateway
  startup or the next DB-available completion.
- Mesh health emits one `mesh_degraded` event when the sliding-window detector
  crosses the failure threshold and one `mesh_restored` event when a later
  successful probe clears degradation.

No remaining P4 tasks. The next cleanup target is M5 below.

**Where to look:** `src/orchestrator.py`, `config/settings.py`,
`src/control/task_server_client.py`, `src/control/mesh_health.py`,
`src/control/task_server.py`, `src/control/node_registry.py`,
`src/services/session_store.py`, and `src/telegram/interface.py`.

### P4.1 — Reconcile Fallback-Completed Work After DB/Task-Server Outage — DONE

Implemented 2026-07-01:
- `_mesh_complete_task` writes a durable reconcile spool entry when the DB is
  unavailable or completion/enrichment raises.
- `reconcile_spooled_mesh_completions()` replays bounded spool files, creates the
  missing `mesh_tasks` row when needed, finalizes/enriches it, and marks the
  spool file reconciled.
- Replay runs on gateway startup and opportunistically on the next DB-available
  completion.

Verification:
- `pytest tests/test_mesh_reconcile_spool.py tests/test_usage_propagation.py::test_mesh_complete_task_prefers_structured_usage_when_stdout_has_no_ndjson`

### P4.2 — Mesh-Restored Operator Notification — DONE

Implemented 2026-07-01:
- `MeshHealth.record_check()` emits transition-only `mesh_degraded` and
  `mesh_restored` observability events.
- Repeated failures/successes do not spam events; only state transitions emit.

Verification:
- `pytest tests/test_mesh_health.py`

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
