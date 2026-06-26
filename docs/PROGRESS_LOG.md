# Progress Log

## 2026-06-26 — M1/M2 observability autonomous validation pass

Follow-up validation on the M1/M2 release candidate from 2026-06-25.

Closed during this pass:

- Re-ran the focused observability gate after changes:
  `80 passed in 2.50s`.
- Captured real Codex CLI 0.140 JSONL shapes for:
  plain answer, shell tool, MCP tool, and invalid-model failure.
- Added sanitized deployed-shape fixtures under `tests/fixtures/telemetry/`.
- Extended the Codex adapter to map deployed `mcp_tool_call` events to
  `tool.call.*` telemetry using only bounded `server.tool` names and category
  `mcp`; arguments/results are still discarded.
- Ran a real local Codex backend smoke through `CodexBackend` +
  `DatabaseTelemetrySink`: process spawn/exit, shell tool lifecycle, aggregate
  usage, and projection metrics were persisted.
- Ran a real worker-path smoke through `src.worker.agent._execute_task` with
  Codex: invocation lifecycle, process lifecycle, shell tool lifecycle, usage,
  and projected metrics were persisted in one turn.
- Privacy scan over generated telemetry SQLite DBs and existing spool files
  found none of the sentinel strings (`PROMPT_SECRET`, `SOURCE_SECRET`,
  `TOOL_ARG_SECRET`, `TOOL_RESULT_SECRET`, `MODEL_RESPONSE_SECRET`,
  `API_KEY_SECRET`).

Performance measurements:

- One 1,000-event SQLite ingestion batch: ~16,392 events/sec.
- 5,000-event dashboard detail reads: `list_events` ~84.5 ms,
  `diagnostics` ~0.6 ms, `graph` ~88.7 ms.
- Repeated controller-side 50-event batches with projection rebuild on each
  batch are slower than the aspirational `<5 ms p95` batch-overhead target
  (`p95` ~160.6 ms at 5,000 accumulated events). Larger 200-event batches do
  not remove the rebuild cost (`p95` ~248.6 ms). This is not worker task
  blocking, but high-cardinality production telemetry should get incremental or
  deferred projection rebuild before relying on very large detailed tool streams.

Remaining before calling this fully deployed:

1. Run a true two-process/two-node mesh smoke with the task server and worker
   processes online. This host currently has only `ai-team-gateway` in PM2, so
   the autonomous pass validated the worker execution path locally rather than a
   live controller/worker topology.
2. Decide whether to optimize controller projection rebuild before M3, or accept
   the current behavior for normal Codex aggregate/tool volumes and track the
   high-cardinality case separately.

## 2026-06-25 — LLM turn observability M1/M2 release candidate

Implemented the privacy-preserving turn accounting system specified in
`docs/LLM_TURN_OBSERVABILITY_SPEC.md`.

Shipped:

- typed allowlisted telemetry events and immutable correlation context;
- local and mesh Codex turn/invocation/process lifecycle;
- streaming Codex tool and aggregate usage adapter;
- SQLite event store and deterministic turn projections;
- retry, timeout, duplicate-process, coverage, and context-continuity metrics;
- authenticated turn APIs plus graph, diagnostic table, and timeline dashboard;
- idempotent HTTP batching, bounded spool/replay, retention, and reconciliation;
- maintenance commands:
  `python main.py telemetry-reconcile [--turn-id ID] [--since HOURS]` and
  `python main.py telemetry-cleanup [--event-days N] [--summary-days N]`.

Validation: 76 focused observability/dashboard/mesh tests pass, including the
real worker `_execute_task` path with a telemetry-aware fake backend. No paid
backend or external network is used by these tests.

Next release gates:

1. additional deployed-Codex sanitized fixtures;
2. real local and mesh Codex smoke tests;
3. ingestion/query/concurrency performance measurements;
4. then M3 Claude adapter work under the existing schema.

Handoff warning: preserve the unrelated uncommitted
`src/core/process_utils.py` modification.

## 2026-06-21 — Cockpit M4: workflow events (review / handoff / approval)

**Milestone: the reserved workflow vocabulary (CONTROL_CONTRACT §7) is now a
real, transport-neutral inbound entry point — the third beside SessionService
(lifecycle) and submit_instruction (dispatch).** Branch: `feat/session-service-m1`.

Shipped:
- **`src/services/workflow_service.py`** — `WorkflowService`, stateless by
  construction (no store, no tables, no engine — honors the §7 rule literally).
  Five methods emit the canonical events `review.requested/completed`,
  `handoff.created`, `approval.requested/granted`, each correlated to a
  `session_id` (+ optional `task_id`) and returning a machine-code
  `CommandResult`. The names live as constants + a `WORKFLOW_EVENTS` set so they
  can't fork across surfaces. `run.*` from §7 is intentionally NOT re-emitted —
  it maps to the existing `<backend>_finished` events.
- Wired into `orchestrator.workflow_service`; exported from `src.services`.
- The M3 dashboard surfaces these with no change (it renders any event).
- **Design choices recorded:** review verdicts constrained to
  {approved, changes_requested, rejected}; session-existence validation is a
  deliberate non-goal (these are provenance events; coupling to the store would
  break statelessness). A surface that must change state calls §4a/§4b too.

Docs: `CONTROL_CONTRACT.md` updated — §2 catalog, §4 (now three entry points +
4c), §7 (implemented, not reserved), §8, status → v2. New tests:
`test_workflow_service.py` (12). Full suite = 308 passed, 7 pre-existing
failures, 0 new.

## 2026-06-21 — Cockpit M3: read-only web dashboard (the second surface)

**Milestone: a second surface beside Telegram, proving the M1 contract holds —
built entirely on the read model + event stream, with zero core change.**
Branch: `feat/session-service-m1`.

Shipped:
- **`src/control/dashboard.py`** — a read-only FastAPI app. State from
  `SessionService.list_views()` (M2 SessionView) + `db.list_tasks/list_nodes`;
  live deltas from `events.ndjson`. JSON read endpoints (`/api/sessions|tasks|
  nodes|events`) + a self-contained HTML shell at `/`. Bearer auth via
  `DASHBOARD_TOKEN` (falls back to `WORKER_TOKEN`). **No inbound command path,
  no forms** — which also sidesteps the optional `python-multipart` dep the task
  server's upload routes need (the source of the 6 pre-existing suite failures).
- **`observability.read_recent_events(limit, since_offset)`** — the canonical
  read-side accessor for the event stream (inbound symmetry to `emit_event`).
  Returns `{events, offset}`; the client polls `?since=<offset>` for deltas.
  Per the contract, gap recovery is NOT a replay — the client refreshes state
  from the read endpoints. Hardened against rotation: a stale offset past EOF
  re-reads the tail instead of going silent.
- **`dashboard_main.py`** (launcher, mirrors `server_main.py`) +
  `MeshConfig.dashboard_port` (9003) / `dashboard_token` config.
- XSS-safe client rendering (all event/session values escaped before innerHTML).

Docs: `CONTROL_CONTRACT.md` §8 now points at the dashboard as the reference
second-surface implementation. New tests: `test_dashboard.py` (13). Full suite =
296 passed, 7 pre-existing failures, 0 new.

## 2026-06-21 — Cockpit M2: SessionView read model (Move C)

**Milestone: one read shape for "what the operator sees about a session."**
Builds the deferred Move C now that M3's Web dashboard is the second reader that
justifies it. Branch: `feat/session-service-m1` (continues the cockpit line).

Shipped:
- **`SessionView`** (`src/core/view_models.py`) — frozen DTO derived from
  `Session`, never persisted. Carries the raw `backend` string + the derived
  booleans every surface recomputes (`needs_input`, `is_active`) + the session
  `origin` (channel/kind). Rendering (icons/labels) stays in each surface.
  `to_dict()` is JSON-ready for the Web UI / WebSocket.
- **`SessionService.list_views()` / `active_view(chat_id)`** — the read methods
  the M1 service deliberately omitted; `active_view` delegates to
  `store.get_active` so the stale-CLOSED-binding cleanup is preserved.
- **Deviation from spec C.1 (recorded):** added `origin_channel`/`origin_kind`
  to the DTO. The spec predates `SessionOrigin` being a real `Session` field
  (M1 Step 2); a provenance-aware dashboard wants it, and it's pure read.

Telegram list-handler adoption is opt-in (spec C.2) and intentionally NOT done —
zero behavior change. `docs/CONTROL_CONTRACT.md` §6 updated (was "planned/M2").
New tests: `test_view_models.py` (24). Tests: `pytest tests/test_view_models.py
tests/test_session_service.py` green; full suite = 283 passed, 7 pre-existing
failures (M1-baseline FastAPI form-import + live-state staleness), 0 new.

## 2026-06-21 — Cockpit M1: transport-neutral session core

**Milestone: the gateway is ready for a second surface (Web UI) with no further
core refactor.** A documented extraction, not a feature — Telegram behavior is
byte-identical to the pre-M1 baseline (`tests/test_telegram_session_flow.py`
gate). Branch: `feat/session-service-m1`. Built top-to-bottom against
`docs/M1_CHECKLIST.md` (the anti-scope-escape mechanism); rationale in
`docs/COCKPIT_REFACTOR_SPEC.md`.

Shipped:
- **Backend registry** (`src/backends/registry.py`) — the backend set is declared
  once (`build_backends/valid_backend_names/is_valid_backend/DEFAULT_BACKEND`);
  orchestrator, worker, and the Telegram validation paths all derive from it.
  Adding a backend = one edit. (Adversarial review caught a *third* hardcoded
  validation tuple at `interface.py:2085` the spec miscounted — now also routed
  through `valid_backend_names()`.)
- **`SessionOrigin`** — a descriptive `{channel, kind}` tag on `Session`
  (defaults `telegram/user`). Persisted in JSON **and** the DB mirror via
  **migration 12** (additive, defaulted column; old rows backfill; revert-safe).
  The spec's "no DB column" premise was wrong — `SessionStore` reads DB-first, so
  a JSON-only tag would have been inert. Descriptive, *not* routing — scoping
  modes deliberately not adopted.
- **`SessionService`** (`src/services/session_service.py`) — transport-neutral
  lifecycle (`create_session`, `bind_active`) lifted off the Telegram class; the
  inbound symmetry to `NotificationService`. Returns a machine-code
  `CommandResult` (no prose). Wired into `orchestrator.__init__`, reusing the one
  `SessionStore`. Telegram's `_create_and_bind_session` is now a thin wrapper.
- **`docs/CONTROL_CONTRACT.md`** — the durable artifact: event envelope + catalog,
  the two inbound entry points, `SessionOrigin`, backend extension, the `db.list_*`
  read model (`SessionView` marked planned/M2), and reserved workflow event names.

Deferred to M2+: `SessionView` DTO + read methods, WS/HTTP transport, workflow
commands. New tests: `test_backend_registry.py`, `test_session_origin.py`,
`test_session_service.py`. Tests: `pytest tests/test_telegram_session_flow.py
tests/test_session_service.py tests/test_session_origin.py
tests/test_backend_registry.py` (gate at baseline).

## 2026-06-18 — Watched job process-identity resilience

T3.1 is complete. Watched jobs now persist worker probe fields
(`last_checked_at`, `last_probe_error`, `last_seen_command`,
`last_seen_started_epoch`) and the worker verifies PID identity with process
start time plus command where the host can provide it. A reused PID or mismatched
process is reported as `lost` instead of being left indefinitely `running`.
Telegram `/jobs` now shows probe freshness/errors for running jobs.

Tests: `.venv/bin/python -m pytest tests/test_watched_jobs.py`.

## 2026-06-11 — Mesh goes live: two-machine execution + gateway restart resilience

**Milestone: the State Separation architecture is proven in production.** The
mesh now runs split across two real machines — gateway + embedded task server on
the Pi5 (`kanebra`), worker daemon on this PC (`Horse`) — and a task survives a
gateway restart end-to-end: dispatched → gateway restarts mid-flight → worker
keeps running → gateway reattaches on startup → delivers the worker's **real**
result to Telegram. This retires the long-standing "blocked on 2nd machine"
caveat on Phase 3.

**The restart-cancel bug (fixed).** A gateway restart used to mark in-flight
remote tasks `failed` ("interrupted by gateway restart"), fabricating a terminal
state the worker never produced. Root cause: the gateway owned task lifecycle via
an in-memory poll loop + cancel event; shutdown fired that event and wrote
`fail_task`. The fix separates two states (the "websocket model"): the DB row is
the task's truth, owned by the worker; the gateway's poll loop is a detachable
subscriber. Three layers changed in `src/orchestrator.py`:

1. **`_dispatch_to_node`** — on shutdown (`task.id in _shutdown_interrupted_tasks`)
   it no longer calls `db.fail_task`; it returns a result tagged `detached=True`
   and leaves the DB row `claimed`.
2. **`_process_task_remote`** — a `detached` result leaves the session **BUSY**
   (no ERROR/CANCELLED) so startup recovery can reattach.
3. **`_task_worker`** — a `detached` result short-circuits the completion path:
   no Telegram "Task failed", no failure artifact, no `_mesh_complete_task`.
4. **Startup reattach** — `_recover_stale_busy_sessions` no longer skips remote
   sessions; a remote session whose task is still `claimed`/`pending` spawns
   `_reattach_remote_task`, which polls the DB to a terminal state and reports the
   worker's real result (bounded by `oneoff_queue_timeout_sec`).

**Two delivery bugs in recovery (fixed, were pre-existing in
`_recover_completed_session`):**
- It sent a hardcoded *"Task completed while gateway was restarting — session
  restored."* placeholder instead of the worker's actual output. Now delivers the
  real reply text (via `_session_reply_text` + changed-file list, prefixed
  `_(recovered after a gateway restart)_`).
- It never propagated `backend_session_id` from the worker's result, so a
  recovered session couldn't resume the remote backend (started cold instead).
  Now restores `session.backend_session_id` exactly as the live path does.

Commits: `f7b0777` (reattach scaffolding), `f887ba1` (detach in `_task_worker`),
`5bc9137` (recovery delivery + `backend_session_id`). Verified: full
`test_claude_session_backend.py` green; live two-machine restart test delivered
the real joke + resumed the session.

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
