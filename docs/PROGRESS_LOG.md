# Progress Log

## 2026-06-10 — Doc cleanup

- Rewrote `.ai/CONTEXT.md` into a short hot-context doc (was an 880-line history
  scroll). Phase-by-phase build history moved here.
- Rewrote `.ai/NEXT_TASKS.md` to lead with the **active** plan (State Separation
  Phases 0→2) instead of stale completed-D items.
- Established plan of record: **State Separation** (`docs/STATE_SEPARATION_PLAN.md`)
  supersedes the standalone "VPS migration Phase 4"; VPS cutover
  (`docs/PHASE_4_RUNBOOK.md`) is the end-state of State Sep Phases 2–3.
- Verified against code: State Sep **Phase 1 is already done** (DB-first reads in
  `session_store.py:63`, `db.get_task_by_session` at `db.py:510`, DB-aware
  `_recover_stale_busy_sessions` at `orchestrator.py:299`). Phase 0 still has 2
  orphan `mesh_tasks` rows and a DB(410)/JSON(234) session-count mismatch to
  reconcile.

## Mesh build history (Phases 8–9, Steps B/C, D1–D6)

Condensed from the former `.ai/CONTEXT.md`. All shipped behind `MESH_ENABLED`
(off in prod).

- **Phase 8 — mesh DB foundation:** `src/control/db.py` (`MeshDB`: SQLite WAL,
  write lock, per-thread conns, versioned migrations); `MeshConfig` in
  `config/settings.py`; `session_store._shadow_write()` mirrors every save to DB;
  orchestrator `_mesh_enqueue_task`/`_mesh_complete_task`; `seed_db_from_json.py`
  (backfilled 149 sessions / 794 tasks / 799 events). JSON authoritative, DB a
  shadow copy.
- **Phase 9 Steps 1–3:** `task_server.py` (FastAPI, 9 endpoints, Bearer auth),
  `node_registry.py` (heartbeat expiry, offline failover, DB persistence),
  `worker/{config,agent}.py` (register, poll+backoff, nudge listener, heartbeat,
  SIGTERM drain), orchestrator `_run_backend_local`/`_dispatch_to_node`/
  `_dispatch_or_run_local`. Adversarial review found 14 issues; criticals fixed
  (double-execution via self-claim of shadow rows, session payload embedding,
  real drain, claim-verified result submission, offline-task async scan,
  re-registration on 404, structured failure instead of RuntimeError, nudge
  validation).
- **Phase 9 Step B:** wired remote routing into `process_task` —
  `route_remote = MESH_ENABLED and session.machine_id`; `_process_task_remote`
  (fails loudly if pinned node offline, no silent local fallback);
  `backend_session_id` propagated worker→task_server→DB→gateway→session.
  Verified 18/18 + 24/24 tests.
- **Phase 9 Step C (2026-06-07):** real two-machine test. Worker advertises
  `projects_root`+`repos` (migrations v2/v3); `_mesh_online_nodes()` reads shared
  DB (cross-process); Telegram node picker (backend→node→repo); `route_remote`
  only when `machine_id != local hostname`; FastAPI `on_event`→`lifespan`.
- **D1 (2026-06-07):** task server embedded in the gateway
  (`embedded_server.py`, `EmbeddedTaskServer` on the gateway event loop);
  `get_registry()` now a shared in-process singleton; `ai-team-task-server` PM2
  entry removed.
- **D1.5:** observability spine (`src/core/observability.py` — bracketed context
  format, redaction, `emit_event` NDJSON, authed `GET /metrics`).
- **D2:** worker execution logging (full traceback → `error_detail`, concise
  `errors[0]`, `task_failed` event; node_id on every line). Not yet validated on
  the real two-machine failing-task path.
- **D3:** `/nodes` and `/node <id>` Telegram commands (DB-backed).
- **D4 (2026-06-07):** `/status` + `/session_list` compact UX overhaul.
- **D5 (2026-06-07):** `scripts/fix_session_machine_ids.py` (dry-run default,
  `--apply`, idempotent, per-file atomic write).
- **D6:** `ai-team-worker` PM2 entry made bootable; `docs/PHASE_4_RUNBOOK.md` added.

## 2026-03-22

### Completed

- Re-centered the repo around the actual product: a Telegram session-first coding gateway
- Added shared path validation and path suggestions for session creation
- Added Telegram commands for session directory listing, session cancellation, `/run`, and `/say`
- Tightened session ownership checks and session state transitions
- Removed prompt rewriting from the active execution path so Claude Code / Codex stay in control of their own runtime
- Stopped surfacing the old local agent-layer as if it were active product behavior
- Added focused tests for path resolution and Telegram session flow
- Removed several stale tests and docs that described the older agent-template/orchestrator product

### Current Gate

- Run a live end-to-end Telegram session resume test against Claude Code

### Notes

- LLAMA mediator is still present, but now explicitly treated as a dormant future layer rather than the active product path
- The docs set was reduced to a small canonical publish-facing surface
