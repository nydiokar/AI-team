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
  `_recover_stale_busy_sessions` at `orchestrator.py:299`).
- **Phase 0 completed.** (1) Fixed root-cause shadow-write bug: `create()` now
  shadow-writes to DB (was JSON-only, the source of 7 only-in-JSON sessions).
  (2) Failed orphan `mesh_tasks` rows. (3) **DB trust cleanup before the
  3-process split:** profiled 418 DB sessions — only 162 had real task history;
  purged the other 256 (45 test/fixture leftovers + 215 abandoned zero-task
  shells) plus 34 orphan `task_events`, then VACUUM. DB now = 162 real sessions,
  0 orphans; live JSON (234 files) untouched. Backups:
  `state/mesh.db.bak-phase0-20260610`, `state/mesh.db.bak-cleanup-20260610-181929`.
  Tool: `scripts/analyze_sessions.py`. Follow-up logged: standalone dev/test
  scripts still default to the prod DB (pytest is already isolated).

## 2026-06-10 — State Separation Phase 2 (standalone task server)

- Scaffolding: `server_main.py` (PM2 entry, mirrors `worker_main.py`),
  `src/control/task_server_client.py` (`TaskServerClient` — urllib, Bearer auth,
  5s TTL node cache, degrades to None/[] when the server is unreachable),
  disabled `ai-team-server` PM2 entry. `tests/test_task_server_client.py` (8).
- Cutover: added `MeshConfig.embedded_server` / `MESH_EMBEDDED_SERVER`
  (default **False**); `_start_embedded_task_server()` now no-ops unless embed is
  explicitly requested, so the gateway stops binding the task-server port and the
  standalone `ai-team-server` owns it.
- Why it was small: the live remote path `_process_task_remote` was already
  DB-backed (node liveness via `db.get_node()`, results via DB polling in
  `_dispatch_to_node`); `_dispatch_or_run_local` (the only hard in-memory-registry
  dependency) is dead code reserved for Phase 3. So no dispatch rewrite was
  needed — discovery survives the process split via the shared DB.
- `embedded_server.py` kept (not deleted) as the explicit single-process /
  fallback mode behind the flag.
- Verified: cutover integration test (standalone server + temp DB/port via
  `AI_TEAM_ENV_FILE`) — gateway in-process registry empty yet reads the node
  online from the shared DB; embedded start is a clean no-op. Full suite 138
  passed / 13 skipped. Gateway was stopped (`pm2 stop ai-team-gateway`) for the
  cutover.

## 2026-06-10 — State Separation Phase 3 (worker loopback proof)

- `scripts/test_worker_loopback.py`: drives the REAL worker daemon
  (`worker_main.py`) against the REAL standalone server (`server_main.py`) on a
  temp DB + temp ports (`AI_TEAM_ENV_FILE`), no paid backend. Proves the full
  pipeline: register → nudge listener → `task_claimed` → execute →
  `task_result_posted` → DB `status=failed claimed_by=<node>` → SIGTERM drain.
- The injected `opencode` task failed cleanly on the `CLAUDE_ALLOWED_ROOT` path
  allowlist (non-repo cwd rejected) — confirms the worker enforces the backend
  safety boundary on the remote-execution path. "Never run in prod" risk retired.
- Test bug found + fixed along the way: first attempt used a backend the worker
  doesn't advertise, which the server's `get_pending_tasks` backend filter
  excludes, so the worker correctly never saw it. Switched to an advertised
  backend (`opencode`).
- Real worker *execution* (vs. this loopback proof) is blocked on a 2nd machine:
  the gateway only routes remotely when `session.machine_id != hostname`
  (orchestrator.py:1223), so single-machine a worker idles. That lands with the
  Phase 4 two-machine cutover, deferred until Tailscale is available.

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
