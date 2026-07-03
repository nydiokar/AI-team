# Next Tasks

**Purpose:** this file is only the active task queue and short completion ledger.
Use `.ai/CONTEXT.md` for orientation and `docs/PROGRESS_LOG.md` for detailed
history.

**Last updated:** 2026-07-02
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
| M5 mesh health history | DONE | `mesh_health_samples` ledger records throttled slot/load/stale-state samples from heartbeat; `/metrics.history.recent` and `/api/mesh/health` expose recent trend rows. |
| Cockpit M1 (session core) | DONE (branch) | `feat/session-service-m1`: backend `registry.py`, `SessionService` (create/bind), `SessionOrigin` (DB migration 12), `docs/CONTROL_CONTRACT.md`. Telegram byte-identical. Separate track from State Separation. See `docs/M1_CHECKLIST.md` + PROGRESS_LOG 2026-06-21. M2+ deferred. |

---

## Active / Next

### Next Focus Selection — selected 2026-07-02

Chosen from the existing open/deferred task set in `.ai/CONTEXT.md`; no new
tasks are introduced.

1. **#21 Web Push notifications** — ✅ SHIPPED 2026-07-03 on branch
   `feat/operator-signal` (dispatch: `.ai/dispatch/AGENT_8_OPERATOR_SIGNAL.md`,
   adversarial review: `...REVIEW.md`). DB migration 20 (`push_subscriptions`),
   `PushConfig`+VAPID, `src/services/push_service.py` (bounded, non-blocking
   fan-out; 410→disable), unconditional wiring in `notification_service`,
   `/api/push/{subscribe,unsubscribe,status}` (4 KB cap), SW push/notificationclick
   handlers (cache v3), `usePushNotifications`+`PushSetting`. Notification
   fan-out only — NOT connected to approval gates. `pywebpush` is an optional
   extra; absent VAPID ⇒ push disabled, gateway unaffected. Tests:
   `tests/test_push_notifications.py` (15). Operator TODO: set VAPID_* env +
   `pip install -e ".[push]"` + add VAPID vars to `.env.example` (env files were
   not editable from the build environment).
2. **#30/#33 Backend Account + Usage Visibility** — ✅ SHIPPED 2026-07-03 on
   `feat/operator-signal`. `src/services/backend_usage.py` + `GET
   /api/backends/usage` (registry+config+telemetry only), `BackendUsagePanel` in
   System. Honesty-first: configured/observed model + recent token usage from
   telemetry are surfaced; daily/weekly limits, reset time, and account identity
   are ALWAYS null + a reason (no backend emits them); usage-absent is null, not
   fabricated 0. Tests: `tests/test_backend_usage.py` (8). No provider quota was
   invented.
3. **#31/#32 Wire `load_compact_context` into a continuation path** — ✅ SHIPPED
   2026-07-03 on branch `feat/compact-context` (dispatch:
   `.ai/dispatch/AGENT_9_COMPACT_CONTEXT.md`, dispatch review `..._REVIEW.md`,
   build review `AGENT_9_BUILD_REVIEW.md`). The previously dead-but-tested
   `orchestrator.load_compact_context` now has a consumer: an **opt-in**
   `continues: <prior_task_id>` frontmatter/metadata field makes `process_task`
   prepend the prior task's bounded compact context to the prompt as a fenced,
   reference-only `<prior_context>` block, with the live instruction preserved
   verbatim in `<current_instruction>`. Guarded to inject once (instance-local set,
   not `task.metadata`), off the event loop (`asyncio.to_thread`), hard-capped at
   4 KB, fence-escape-hardened (`_defuse_fence`), and a no-op on
   self-ref/unknown/empty/loader-failure. No new gateway state, no parser change,
   no change to tasks without `continues:`. Also works via
   `submit_instruction(extra_metadata=...)`. Docs: `docs/Task_harness_workflow.md`
   §7/§14. Tests: `tests/test_compact_context_injection.py` (11) + unchanged
   `tests/test_context_loader.py` (2) → 13 green.
4. **#5-#9 LLM Turn Observability remaining validation** — #8 done. 2026-07-02 local
   Codex smoke and controlled mesh Codex smoke passed. **#9 still pending**: gateway-
   routed mesh smoke needs a live session (kanebra + Horse online) to verify non-null
   `gateway_node_id` in `llm_turns`. Steps + DB verification query documented in
   `.ai/dispatch/AGENT_10_M3_CLAUDE_TELEMETRY.md` T1 section. Until #9 passes, M1/M2
   are not formally closed.
5. **#10 M3 Claude adapter** — ✅ SHIPPED 2026-07-03 on `feat/m3-claude-telemetry`
   (dispatch `.ai/dispatch/AGENT_10_M3_CLAUDE_TELEMETRY.md`). `ClaudeStreamJsonAdapter`
   + `_maybe_emit_telemetry` at `ClaudeCodeBackend` boundary. 18 tests. Coverage:
   `stream_only`. NOTE: shipped ahead of #9 formal closure; M3 is functional and
   tested but the M1/M2→M3 scheduling gate (#9) is still pending the live smoke.

Recently completed and should remain called out as done: **#34 Stop Task
Behavior**, **#36 Remove Tasks Page / Replace With Jobs**, **#37 Move Job Event
Sequences Out of System**, **#38 Make the System Tab Earn Its Place**, and
**#39 Make worker/session state reporting honest**.

#### Execution handoff: #21 Web Push notifications

Goal: deliver best-effort browser push notifications for task/session terminal
outcomes, using the existing Web UI PWA and notification spine.

Implementation path:
- Read `docs/DEFERRED.md`, `docs/CONTROL_CONTRACT.md` notification section,
  `src/services/notification_service.py`, `src/control/control_api.py`,
  `web/public/sw.js`, `web/src/main.tsx`, `web/src/screens/SystemScreen.tsx`,
  and existing control API tests before editing.
- Add durable push subscription storage in the existing SQLite DB layer. Store
  endpoint, keys, created/updated timestamps, last error, enabled flag, and
  optional coarse user/browser label. Add migration and DB unit tests.
- Add authenticated control API endpoints: subscribe, unsubscribe/disable, and
  current subscription/status. Validate payload shape, cap request size, reject
  malformed subscriptions with structured errors, and keep endpoint idempotent.
- Add VAPID config through existing settings/env patterns. Startup must show
  push unavailable when keys are absent; it must not crash the gateway unless an
  explicitly required mode is later added.
- Add a small push delivery helper used by `NotificationService.notify_task_outcome`.
  Fan out only sanitized `task_notification` success/failure facts with title,
  short body, task/session IDs, and a URL to the relevant session when known.
  Never include prompts, assistant output, file contents, command lines, or raw
  errors.
- Add service-worker `push` and `notificationclick` handlers. Open/focus the
  relevant Web UI URL. Keep the current offline shell and API network-only
  behavior intact.
- Add a quiet System settings control for permission/subscription state. Request
  browser notification permission only from a user click; no auto-prompt on load.
- Verification: targeted backend tests for DB/API/service-boundary cases,
  frontend tests for API adapter/UI state where practical, `cd web && npx tsc -b`,
  focused vitest, and manual browser smoke on localhost/tailnet with a test push
  path or injected fake sender.

Service boundary checklist for #21:
- Concurrency: cap fanout concurrency and per-notification send time; failed
  subscriptions must not block task completion.
- Memory at scale: batch subscription reads and bound payload size; N=100
  subscribers must stay small and predictable.
- Request size: subscribe endpoint must enforce bounded JSON input.
- Timeout: push sends must use short network timeouts.
- Malformed input: reject invalid endpoint/key payloads before DB writes.
- Backing resources: if DB or VAPID config is unavailable, report push disabled
  and keep task execution/Telegram notification working.

#### Execution handoff: #30/#33 Backend Account + Usage Visibility

Goal: give the operator a reliable System/Usage view of backend account and
quota state without fabricating provider data.

Implementation path:
- Read existing backend registry and usage parsing paths:
  `src/backends/registry.py`, `src/backends/codex.py`,
  `src/backends/claude_driver.py`, telemetry projection/store files,
  `web/src/components/timeline/SessionTurns.tsx`, `web/src/screens/SystemScreen.tsx`,
  and raw API/adapters.
- Inventory what each backend can currently prove locally: active backend name,
  configured/selected model, known account identity if available, last observed
  usage/rate-limit fields from telemetry, and explicit unknown coverage reasons.
- Add a read-only backend usage/status endpoint under the existing control API.
  It must aggregate known facts from registry/config/telemetry only. Unknown
  daily/weekly limits, reset times, or identities must be returned as `null`
  plus coverage/reason fields.
- Render a compact System section or dedicated Usage/Limits screen consistent
  with the current infrastructure-focused System page. Avoid noisy cards and
  avoid provider-specific promises the backend cannot prove.
- Tests: API shape with missing data, known Codex telemetry rate-limit fields,
  Claude usage-limit error extraction where already parsed, frontend adapter/UI
  rendering of known vs unknown values.

#### Execution handoff: #5-#9 LLM Turn Observability validation

Goal: close the M1/M2 release-candidate validation without redesigning telemetry.

Implementation path:
- Use `docs/LLM_TURN_OBSERVABILITY_SPEC.md` handoff as the contract. Do not
  start M3 Claude adapter work until #5-#8 are closed and #9 is recorded.
- Capture sanitized deployed Codex fixtures for plain answer, MCP, retry/failure,
  and subagent if supported. If a fact is unsupported, add coverage fixtures
  showing unsupported/unknown rather than inferred values.
- Add cumulative-counter/reset fixtures only if the deployed backend emits such
  usage. Preserve aggregate-only semantics when only final totals exist.
- Run one controlled local Codex smoke and one controlled mesh Codex smoke.
  Inspect graph, diagnostics, and timeline views; scan DB/spool/API outputs for
  privacy sentinels.
- #8 benchmark is already done in `.ai/CONTEXT.md`; keep the recorded result and
  only rerun if code touched ingestion/query/projection performance.
- After validation passes, update `.ai/CONTEXT.md` and this file to mark M1/M2
  shipped; only then schedule #10 M3 Claude adapter.

2026-07-02 validation results on branch `validate/llm-turn-observability-m1m2`:
- Branch switched with `git switch -c validate/llm-turn-observability-m1m2`.
- Local Web/control API was started with `pm2 start ai-team-gateway`; `/health`
  returned `{"status":"ok"}` on `http://127.0.0.1:9003/health`.
- Local Codex smoke used `POST /api/sessions` and `POST /api/instructions`:
  session `49c5c6d1157f`, turn `task_99bc7bec`, reply `LOCAL_CODEX_SMOKE_OK`.
- Local dashboard/API inspection: `/api/turns/task_99bc7bec/graph` returned
  5 nodes/4 edges; diagnostics returned `success`, 1 invocation, 1 process,
  request-level plus session-cumulative Codex usage; `/api/sessions/49c5c6d1157f/timeline`
  returned durable timeline items.
- Controlled worker/controller mesh smoke used temporary ports `9012`/`9011`:
  task `task_mesh_smoke_20260702`, claimed by `smoke-mesh-20260702`, reply
  `MESH_CODEX_SMOKE_OK`. Graph/diagnostics/events APIs returned worker/backend/
  reconciler telemetry and `execution_node_id=smoke-mesh-20260702`.
- Privacy scan command shape: query all `llm_%` SQLite tables, `logs/telemetry_spool`,
  and relevant graph/diagnostics/events/timeline API JSON for
  `PROMPT_SECRET_LLMOBS_LOCAL_20260702`, `PROMPT_SECRET_LLMOBS_MESH_20260702`,
  and `PROMPT_SECRET_LLMOBS_GWMESH_20260702`; result was no sentinel hits and
  `logs/telemetry_spool` had 0 files.
- Cleanup check: `Get-NetTCPConnection -LocalPort 9011,9012,9013,9014` had no
  listening owner after smoke cleanup, and `smoke-mesh-20260702` was marked
  offline via `MeshDB.mark_node_offline`. The only `codex` process still listed
  was the pre-existing process from before validation.
- #8 benchmark was not rerun because no ingestion/query/projection performance
  code changed.
- Remaining blocker: do not mark #9 shipped yet. The completed mesh smoke did
  not include gateway-source events (`gateway_node_id` was null) because it
  bypassed the gateway submit path. A temporary gateway-routed mesh attempt on
  port `9014` failed before submission; rerun a gateway-routed mesh Codex smoke
  through the production controller/gateway path or a stable equivalent, then
  mark M1/M2 shipped and schedule #10.

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

### M5 — Mesh Health History / Trend Ledger — DONE

**Why:** the self-awareness branch exposes current mesh state (`/status`,
`/nodes`, `/node`, `/metrics`) and emits reconciliation events, but operators
still have to reconstruct trends from logs. Stale-busy count, live-state
freshness, slot utilization, and node availability are important enough to keep
as queryable history once the live mesh sees real incidents.

Implemented 2026-07-01:
- Migration 19 adds `mesh_health_samples`, separate from `mesh_tasks`.
- Samples capture sessions busy, pending/claimed tasks, online/total nodes,
  slots used/total/available, active tasks, stale-busy sessions, live-state
  freshness counts, and stale live-state node IDs.
- Samples are recorded from worker heartbeat with per-source throttling and
  retention pruning by age and max rows; `/metrics` stays read-only.
- `/metrics` and `/api/mesh/health` now return recent history so an operator can compare recent
  stale-busy/live-state/slot trends without reading logs.

Verification:
- `pytest tests/test_mesh_health_samples.py tests/test_heartbeat_live_state.py tests/test_mesh_health.py tests/test_task_server_client.py`

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
