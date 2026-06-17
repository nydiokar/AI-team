# Next Tasks

**Purpose:** this file is only the active task queue and short completion ledger.
Use `.ai/CONTEXT.md` for orientation and `docs/PROGRESS_LOG.md` for detailed
history.

**Last updated:** 2026-06-17
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
| T4 worker-restart claim reclaim | DONE | Release endpoint, stale-claim reaper, startup sweep, late-result guard, tests exist. |

---

## Active / Next

### T3.1 — Watched Job Resilience: Prove The Running Process Is Still The Same Job

**Why:** watched jobs now store `job_id`, `node_id`, `pid`, `pgid`, `command`,
`started_at`, `started_epoch`, and `log_path`. That is enough to identify a job,
but after a worker restart the current reconciliation mostly asks "does this PID
exist?". PID reuse or a replaced process can make a stale row look alive.

**Goal:** make long-running watched jobs auditable after restarts. The worker
should be able to say "this exact process still belongs to this job" or mark the
job `lost`/`failed` instead of leaving the controller blind.

**Recommended shape:**
- Add durable liveness fields to `jobs`: `last_checked_at`, optionally
  `last_seen_command`, `last_seen_started_epoch` / platform process creation
  time, and maybe `last_probe_error`.
- On each watcher pass, update `last_checked_at` for running jobs.
- Verify PID identity with more than existence:
  - Windows: query `Win32_Process` for `ProcessId`, `CreationDate`, `CommandLine`.
  - Unix: use process start time from `/proc/<pid>/stat` or `ps`, plus command.
- If PID exists but identity does not match the stored job, mark the job `lost`
  and include the observed process details in `tail` or an error field.
- Surface these fields in `GET /jobs`, Telegram `/jobs`, and/or the CLI query
  operators use during incident checks.

**Where to look:** `src/worker/agent.py` (`_job_watcher_loop`, `_pid_alive`),
`src/control/db.py` (`jobs` migration/helpers), `src/control/task_server.py`
(`/jobs` endpoints), `src/telegram/interface.py` (`/jobs` output), and
`docs/WATCHED_JOBS_SPEC.md`.

**Acceptance:**
- A normal running watched job shows recent `last_checked_at` updates.
- A worker restart can re-identify an existing job by PID + start identity.
- A reused PID or mismatched command is marked `lost`/`failed`, not left
  indefinitely `running`.
- Tests use trivial long-lived processes only.

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

### D1 — Script/Test DB Safety

**Why:** pytest isolates DB state, but standalone scripts and ad-hoc commands can
still touch prod `state/mesh.db`. This previously leaked junk sessions.

**Task:** give standalone dev/test scripts an explicit temp DB path or
`MESH_DB_PATH` override by default, especially `scripts/test_*.py`.

**Acceptance:** running any script named like a test cannot write to prod
`state/mesh.db` unless explicitly opted in.

---

## Completed Ledger

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

Important operational detail: a watched job's durable identity is the `job_id`
plus `node_id`, `pid`, `pgid`, `command`, and `log_path`. For a job running on
`Horse`, process verification must happen on `Horse`; the controller only stores
what the worker reports.

Follow-up: T3.1 above improves post-restart liveness proof and stale PID
detection.

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
