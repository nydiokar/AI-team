# AI-Team Gateway — Hot Context

**Last Updated:** 2026-07-02 (LLM turn observability local + controlled worker/controller mesh smoke recorded; full M1/M2 ship still blocked on gateway-routed mesh acceptance)

## Remaining work across all open specs (swept from unarchived docs)

### Open checklist items

The stale restart state issue has a durable read-model fix path as of 2026-07-01:
`src/core/task_state_truth.py` derives honest task/job state from DB rows,
worker live_state, node incarnation, telemetry, and stale evidence; `/api/sessions/{id}/timeline`
exposes the bounded durable sequence; Session Detail renders stale/unknown/detached/recovered
explicitly instead of silently showing running/failed. Covered by targeted backend and web tests.

### LLM Turn Observability — remaining validation (M1/M2)

| # | Task | Source | Depends on | Scope |
|---|---|---|---|---|
| 5 | Capture sanitized fixtures from deployed Codex (plain answer, MCP, retry, subagent) | `docs/LLM_TURN_OBSERVABILITY_SPEC.md` §handoff | Deployed Codex | Testing |
| 6 | Add cumulative-counter/reset fixtures if backend emits cumulative usage | §handoff #2 | #5 | Testing |
| 7 | Real local + mesh Codex smoke tests; inspect dashboard + privacy scan | §handoff #3 | #5, #6 | Validation |
| 8 | SQLite ingestion/query/concurrency benchmarks (§16.5) | §handoff #4 | #7 | Perf ✅ done — 16k evt/s ingestion, ~85ms query; projection rebuild below aspirational target (noted for M3) |
| 9 | After #5–#8 pass, mark M1/M2 shipped, begin M3 | §handoff #5 | #5–#8 | Process |

2026-07-02 validation pass on branch `validate/llm-turn-observability-m1m2`:
- Local Codex smoke passed through Web/control API: `POST /api/sessions`, `POST /api/instructions`; session `49c5c6d1157f`, turn `task_99bc7bec`, reply `LOCAL_CODEX_SMOKE_OK`.
- Local graph/diagnostics/timeline API inspection passed: `/api/turns/task_99bc7bec/graph` returned 5 nodes/4 edges; diagnostics reported `success`, 1 invocation, 1 process, request-level plus session-cumulative Codex usage; `/api/sessions/49c5c6d1157f/timeline` returned durable timeline items.
- Controlled worker/controller mesh Codex smoke passed on temporary local ports `9012`/`9011`: task `task_mesh_smoke_20260702`, claimed by `smoke-mesh-20260702`, reply `MESH_CODEX_SMOKE_OK`; graph/diagnostics/events APIs reported worker/backend/reconciler telemetry with `execution_node_id=smoke-mesh-20260702`.
- Privacy scan passed for sentinels `PROMPT_SECRET_LLMOBS_LOCAL_20260702`, `PROMPT_SECRET_LLMOBS_MESH_20260702`, and `PROMPT_SECRET_LLMOBS_GWMESH_20260702` across all `llm_%` tables, `logs/telemetry_spool` (0 files), and relevant turn graph/diagnostics/events/timeline API JSON.
- Temporary smoke processes were cleaned up; ports `9011`-`9014` had no listening owner afterward, and the temporary node `smoke-mesh-20260702` was marked offline via `MeshDB.mark_node_offline`. The only `codex` process remaining was pre-existing before these smokes.
- #8 benchmark was not rerun; no ingestion/query/projection performance code changed.
- Do **not** mark #9 shipped yet: the gateway-routed temporary mesh attempt did not reach a usable embedded task server on alternate port `9014`, so the completed mesh smoke lacks gateway-source events (`gateway_node_id` is null). Before scheduling #10 M3 Claude adapter, run/pass a gateway-routed mesh Codex smoke through the production controller/gateway path or a stable equivalent.

### LLM Turn Observability — future milestones

| # | Task | Source | Depends on | Scope |
|---|---|---|---|---|
| 10 | **M3** — Claude adapter (stream-json parser, hook integration, coverage UI) | §9.5 | #9 | Backend ✅ SHIPPED 2026-07-03 on `feat/m3-claude-telemetry` (dispatch `.ai/dispatch/AGENT_10_M3_CLAUDE_TELEMETRY.md`). `ClaudeStreamJsonAdapter` post-processes `raw_stdout` at `ClaudeCodeBackend` public method boundary covering SDK + PrintResume paths. 18 tests. Token semantics: `includes_cache`. Double-count guard. Tool call mapping (name+category, input/content never stored). Coverage: `stream_only` (hooks not wired). NOTE: #9 gateway-routed mesh smoke is still pending live session — see T1 in dispatch. M3 is shipped but M1/M2 not yet formally closed pending #9. |
| 11 | **M4** — OpenCode CLI/server | §9.6–9.7 | #10 | Backend | DEFERRED FOR NOW |

### Web UI Feature Requests / UX Issues

| 30 | **Backend usage limits view** — surface current backends (Codex, Claude) account info + usage limits (daily, weekly, reset time). Either in System page or a dedicated page | `CONTEXT.md` | Backend + Frontend |
| 31 | **load_compact_context useful context** — ✅ WIRED 2026-07-03 (`feat/compact-context`, dispatch `.ai/dispatch/AGENT_9_COMPACT_CONTEXT.md`). The helper is now consumed: an opt-in `continues: <prior_task_id>` frontmatter/metadata field makes `process_task` prepend the prior task's bounded compact context (fenced, reference-only) to the prompt. Tests: `tests/test_compact_context_injection.py`. | `CONTEXT.md` | Backend ✅ |
| 32 | **Wire compact context into workflows** — ✅ DONE 2026-07-03. Consumer decided and built: `orchestrator.process_task` reads `task.metadata["continues"]` (opt-in) → prepends `load_compact_context` output as a fenced `<prior_context>` block, original instruction preserved verbatim in `<current_instruction>`. No new gateway state; no parser change; absence of `continues:` = byte-identical to before. Fence-escape hardened (`_defuse_fence`). Documented in `docs/Task_harness_workflow.md` §7/§14. | `CONTEXT.md` | Backend + Workflow ✅ |
| 33 | **Backend Account + Usage Visibility** — Add a clear place to view current backend/account state (Codex, Claude) with active account identity, current usage, daily/weekly limits/quotas/reset times. Show explicitly when limits are unknown. Either in System tab or a dedicated Usage/Limits page. Do not invent quota data. | `CONTEXT.md` | Backend + Frontend |
| 34 | **Fix Stop Task Behavior** — ✅ Stop Task no longer makes the Web UI treat the session as closed: `cancelled` is a run outcome, not lifecycle. `SessionView.is_active` keeps cancelled sessions active/resumable, frontend lifecycle maps only `closed` to closed, and Close Session remains the separate explicit action. Covered by `test_view_models.py`, `test_control_api_write.py`, and web adapter tests. | `CONTEXT.md` | Backend + Frontend ✅ |
| 35 | **Add Per-Project "Current Focus" Panel** — Panel showing current roadmap/direction/active focus per project. Reads CONTEXT.md as source of truth. Detects recent session/job activity. Shows last updated, source file, whether auto or manually edited. Operational: current direction, state, next action. Use local/cheap model for summarization if needed. | `CONTEXT.md` | Backend + Frontend | DEFFER UNTIL WORKFLOW IS SETTLED
| 36 | **Remove Tasks Page / Replace With Jobs** — ✅ Done for current architecture. `/tasks` redirects, `web/src/screens/TasksScreen.tsx` was removed, session-owned jobs render in Session Detail, and System shows only unowned operator jobs. Project-local job cards remain dependent on a future project identity in job rows; do not fabricate project ownership. | `CONTEXT.md` | Frontend ✅ |
| 37 | **Move Job Event Sequences Out of System** — ✅ Done. Session/job/task history is owned by durable `/api/sessions/{id}/timeline` and rendered in Session Detail; live SSE remains operational-only; System activity filters session-owned progress out by default. | `CONTEXT.md` | Backend + Frontend ✅ |
| 38 | **Make the System Tab Earn Its Place** — ✅ Done for the available sources. System is infrastructure-focused: unowned jobs, mesh health/reconcile status, nodes, and infra live activity. Account/quota visibility remains separately tracked in #30/#33 because no reliable quota source exists yet, and the UI must show unknown rather than invented limits. | `CONTEXT.md` | Frontend ✅ / Backend source-limited |
| 39 | **Make worker/session state reporting honest** — ✅ Done for the read model and Web rendering. The backend distinguishes accepted/queued/claimed/worker_running/backend_running/waiting/cancel/cancelled/completed/failed/detached/stale_claim/worker_unknown/recovered; restart/stale/incarnation/detached/recovered/lost-job cases are targeted-test covered through the timeline API and frontend presentation. | `CONTEXT.md` | Backend + Frontend ✅ |

**Current local jobs topology note (2026-06-30):** Horse/this PC may run the Web UI gateway locally on `127.0.0.1:9003` while MCP/worker jobs register against the remote controller from `CONTROLLER_URL` (currently the older Telegram-serving server). In that split, the local gateway has no local `:9002` task server and its SQLite jobs table can be empty even when jobs exist remotely. The local gateway now merges remote controller jobs into `/api/jobs` and polls remote terminal jobs so matching local sessions get the watched-job turn/agent continuation. Live smoke passed with `job_217c415b56dc`: visible in System -> Jobs and projected into session `b696d1040c4b`; watched-job DB turn timestamps are forced to the local session-history timestamp so the WebUI chat shows local time.

### Deliberately deferred (from `docs/DEFERRED.md`)

| # | Task | Notes | Scope |
|---|---|---|---|
| 21 | **Web Push notifications** — VAPID keypair, subscribe endpoint, event emitter. PWA is push-ready. | `docs/DEFERRED.md` | Backend + Frontend |
| 22 | **Token streaming** (`message.delta`) — dropped for v1; timeline shows per-turn summary | ⛔ DROP `docs/FRONTEND_BACKEND_GAP.md` | Frontend |
| 23 | **Diff hunks / file-content preview** — no backend source | `docs/DEFERRED.md` | Backend + Frontend |
| 24 | **Terminal / raw stdout-stderr line stream** — out (security) | `docs/DEFERRED.md` | Backend + Frontend |
| 25 | **Approvals automation** — durable gate exists but inert; auto-emit deferred | `docs/DEFERRED.md` | Backend |

---

**Last Updated:** 2026-06-29
**Branch:** `feat/webui-ui0` (Web UI track — **ladder complete, ready to merge**) — mesh/State-Sep track lives on `main`

> This file is the **fast-orientation** doc: what the project is, how it's wired
> *right now*, the active plan, and the immediate next step. It is intentionally
> short. Per-phase build history lives in `docs/archive/progress/PROGRESS_LOG.md`. The detailed
> task breakdown for the **paused mesh** plan lives in `.ai/NEXT_TASKS.md`. The
> active plan (Web UI) is the ladder in `docs/archive/cockpit-refactor-spec/COCKPIT_REFACTOR_SPEC.md` §14 — see
> the "Web UI track" section below.

> ⚠️ **TEST COST GUARD — READ BEFORE RUNNING ANYTHING.** This project's tests can
> invoke the **live, paid Claude CLI** and previously burned millions of tokens.
> A guard now prevents it, but you must respect the rules:
> - Run tests with plain `pytest` only. Claude is physically unreachable from tests.
> - **NEVER** run the full e2e suite "to verify." Prefer cheap targeted checks
>   (import smoke, direct function calls, `--collect-only`, single skipped-test).
> - Real e2e is OpenCode-only: `AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`.
> - **Do NOT run `python main.py status`** — it acquires the gateway lock and
>   KILLS the live PM2 gateway. Use `curl http://<tailscale-ip>:9002/health`
>   (or `/metrics` with the WORKER_TOKEN bearer) to check the running gateway.

> **TWO PARALLEL TRACKS are in flight — do not conflate them:**
> 1. **Mesh / State Separation** (on `main`) — the runtime/distribution work
>    documented in most of this file below. Only Phase 4 remains. Untouched recently.
> 2. **Web UI / Cockpit** (on `feat/webui-ui0`, the CURRENT active track) — a
>    second surface (mobile web app) over the M1 control contract. This is where
>    recent work happened. **See the "Web UI track" section immediately below.**

---

## What this project is

A Telegram-controlled gateway for local coding agents (Claude Code, Codex,
OpenCode CLI, OpenCode server). You open a session from Telegram, follow-up
messages route to that session, and each turn resumes the native backend
session. State is file-backed and inspectable, with a SQLite mirror.

Canonical product intent: `.ai/context/production_vision.md`.

---

## Web UI track (ACTIVE — branch `feat/webui-ui0`)

A mobile web app (`web/`, React 19 + Vite 8 + Tailwind v4) that is a **second
surface over the same gateway**, consuming the M1 control contract. The gateway
serves it in-process: `python main.py` serves `web/dist` at `/` + `/api/*` on one
tailnet-bound port — the "one process, many interfaces" goal.


**Every rung M1 → UI-6 is shipped, committed, and gate-verified.** Branch
`feat/webui-ui0` is ready to merge to `main`. Gate re-confirmed 2026-06-25:
`cd web && npx tsc -b` clean, 29 vitest pass, `vite build` green, sw.js +
manifest.webmanifest land in `dist/`.

**⚠️ PRIORITY REFRAME (operator, 2026-06-24):** the spec ladder put `H/UI-3
(approvals)` before `UI-4 (files)`. The operator **reprioritized**: approvals
are a **`WorkflowService` feature** — part of a future *workflow-automation* track
that "needs to be thought out better," NOT part of shipping the core
coding-gateway-on-your-phone. H/UI-3 is built and harmless (durable, tested, no
auto-emitter wiring it into any hot path) — **leave it, do not extend it.**

**Deferred (NOT shipping-blockers) — see `docs/DEFERRED.md`:** Web Push
(push-*ready*: PWA + SW shipped, but VAPID secret + subscribe endpoint + emitter
not built), assistant token streaming, diff-hunk/file-content preview, terminal,
and approvals automation (the future workflow-automation track). Design
deliberately later; don't bolt more onto H now.

**Memory pointers (richer detail):** `webui-frontend-backend-sync`,
`webui-ui0-ui1-built` (has the full UI-2/G′/H build notes),
`control-surface-unification` (U-ladder + live-validation log).

---

## Architecture — as it runs today

**One process** (`ai-team-gateway`, PM2). When `MESH_ENABLED=true` it also hosts
the task server embedded on its own event loop.

```
[Telegram] → [Gateway process]
  ├── src/telegram/interface.py     command surface (/status, /nodes, pickers…)
  ├── src/orchestrator.py           task queue, in-process workers, routing, recovery
  ├── src/core/session_service.py   transport-neutral session lifecycle (create/bind) — M1 inbound seam
  ├── src/services/session_store.py DB-first reads, dual-write to JSON + DB
  ├── src/control/db.py             SQLite mesh DB (WAL, busy_timeout=5000, migrations)
  ├── src/control/embedded_server.py task server, embedded (mesh on)
  ├── src/control/{task_server,node_registry}.py  HTTP API + node registry
  ├── src/worker/agent.py           worker daemon — runs as its own process on worker nodes (e.g. Horse)
  └── src/backends/                 claude_code, codex, opencode, opencode-server (set declared in registry.py — M1)
```

**Control contract (M1):** the inbound/outbound boundary is now documented in
`docs/CONTROL_CONTRACT.md` — the event envelope + catalog, the **two** inbound
entry points (`SessionService.create_session/bind_active` for lifecycle,
`orchestrator.submit_instruction` for dispatch), `SessionOrigin`, backend
extension via `registry.py`, and the `db.list_*` read model. A second surface
(Web UI) consumes this with no further core refactor.

State layout:
```
state/sessions/<id>.json              session records (legacy-authoritative, still dual-written)
state/telegram/active_bindings.json   chat_id → session_id
state/summaries/<id>.md               per-session summary
state/mesh.db                         SQLite — read-first by session_store; CANONICAL for conversation + artifacts (migration 17); M5 `mesh_health_samples` trend ledger (migration 19)
results/<task_id>.json                task artifact — now FALLBACK/debug only (DB-canonical since 2026-06-30); droppable
results/reconcile/<task_id>.json      DB-reconcile spool for completed turns if `mesh_tasks` write fails; replayed on startup / next DB-available completion
results/raw/<task_id>.ndjson.gz       gzipped raw_stdout debug stream (when system.slim_artifacts=on)
logs/session_events/<id>.log          per-session NDJSON
logs/events.ndjson                    system-wide event log
```

**Conversation/artifacts are DB-canonical (2026-06-30).** `mesh_tasks` carries the full
untruncated reply + prompt + parsed_output + file_changes + usage (migration 17). Chat
(`/api/sessions/{id}/messages`) and Files/Info tabs (`/api/artifacts*`) read the DB first,
files only as fallback for un-enriched old sessions. The conversation is a **projection of
the task ledger** (no separate turns table). Live write is DB-first, untruncated, all backends.
Migrate + drop the fat files via `docs/RUNBOOK_db_self_sufficient.md`. Full audit + rationale:
`docs/CONVERSATION_DATA_FLOW.md` §0. Memory: `db-self-sufficient-conversation`.

**Config flags that matter:** `MESH_ENABLED` (default `false` — gateway behaves
exactly as pre-mesh), `MESH_SHADOW_WRITE` (default `true`), `WORKER_TOKEN`,
`MESH_TAILSCALE_IP`, `MESH_TASK_SERVER_PORT` (9002).

---

## Background: mesh 

**The mesh is LIVE across two real machines (as of 2026-06-11).** Gateway +
embedded task server run on the **Pi5 (`kanebra`)**; the worker daemon runs on a
separate PC (**`Horse`**). Real tasks dispatch machine-to-machine, and — the part
that was the whole point — a task now **survives a gateway restart end-to-end**:
the worker keeps running, the gateway reattaches on startup and delivers the
worker's real result to Telegram (no fabricated "Task failed"). State Separation
Phases 0–3 are effectively complete.

## Architecture rules (do not violate)

- DB is the canonical **read** source. `state/sessions/<id>.json` dual-write stays
  as the ultimate session fallback and is **never deleted**. NOTE (2026-06-30):
  `results/task_*.json` artifacts are NO LONGER a source — `mesh_tasks` holds the
  full conversation + artifact data (migration 17). The fat artifact files are a
  fallback/debug archive and ARE droppable (see `docs/RUNBOOK_db_self_sufficient.md`);
  the `raw_stdout` debug stream is kept gzipped under `results/raw/` when
  `system.slim_artifacts` is on.
- The server/gateway host keeps its **own embedded worker capacity** (configurable
  pool, default ≥1 — **not** capped at 1) that executes tasks when no remote node
  is available. Prefer remote nodes when online; the server runs work locally when
  none are, so tasks never stall. (Updated 2026-06-11; supersedes the old "exactly
  1 fallback worker" rule.)
- `MESH_ENABLED=false` ⇒ gateway is byte-for-byte the old behavior.
- Session affinity is a hard correctness requirement: a session pinned to a
  machine must execute on that machine. `backend_session_id` is machine-local.
- No uncontrolled autonomous behavior. Ollama is optional/helper-only. Per-turn
  audit data (full reply, files changed, usage) is **mandatory** — it now lives
  canonically in `mesh_tasks` (was the `results/*.json` files; those are now an
  optional debug archive, not the audit source).

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | runtime, task queue, workers, routing, recovery, mesh hooks |
| `src/core/session_service.py` | transport-neutral session lifecycle (create/bind) — M1 inbound seam |
| `src/backends/registry.py` | single declaration site for the backend set — M1 (add a backend = one edit here) |
| `src/services/session_store.py` | DB-first session reads + JSON/DB dual-write |
| `src/control/db.py` | SQLite mesh DB — canonical DB layer |
| `src/control/task_server.py` | FastAPI task server (currently embedded); `/metrics.history.recent` exposes M5 mesh health samples without mutating on read |
| `src/control/node_registry.py` | node registry + heartbeat expiry |
| `src/worker/agent.py` | worker daemon (runs as its own process on worker nodes) |
| `src/telegram/interface.py` | Telegram command surface |
| `config/settings.py` | all config incl. `MeshConfig` |
| `docs/CONTROL_CONTRACT.md` | **M1** — event + inbound-command + backend + read-model contract for a 2nd surface (§6 = conversation/artifact DB read model) |
| `docs/CONVERSATION_DATA_FLOW.md` | **conversation+artifact data flow audit** (§0 = DB-canonical resolution, migration 17) |
| `docs/RUNBOOK_db_self_sufficient.md` | e2e runbook to backfill `mesh_tasks` + drop the fat `results/*.json` files |
| `scripts/backfill_conversation_turns.py` | one-time backfill (`--verify` for parity) — enriches `mesh_tasks` from existing artifacts |
| `docs/archive/cockpit-refactor-spec/COCKPIT_REFACTOR_SPEC.md` / `docs/archive/m1/M1_CHECKLIST.md` | M1 rationale + the executed build checklist |
| `docs/archive/cockpit-refactor-spec/COCKPIT_REFACTOR_SPEC.md` | Web UI ladder (§14) — **all rungs M1→UI-6 done** |
| `docs/DEFERRED.md` | Web UI track — deliberately-not-built future boxes (Web Push, streaming, diff hunks, terminal, approvals automation) |
| `docs/archive/STATE_SEPARATION_PLAN.md` | mesh plan (PAUSED background, not active) |
| `docs/archive/AGENT_MESH_SPEC.md` | mesh design spec |
| `docs/PHASE_4_RUNBOOK.md` | VPS cutover runbook (= State Sep end-state) |
| `docs/archive/progress/PROGRESS_LOG.md` | completed-work history |
| `ecosystem.config.js` | PM2 supervisor config |

---

## Deferred (valid, lower priority)

- Backend lifecycle hooks (session-ID detection, PreToolUse security, PostToolUse
  quality gates) — `docs/BACKEND_HOOKS_STRATEGY.md`.
- Codex end-to-end validation.
- OpenCode server cross-machine sessions (needs shared DB mount).
- Postgres migration — trigger: >5 nodes or observed SQLite write contention.
- **M-Mesh** — distributed event bus (Redis/NATS), shared state store, leader election.
  "DO NOT build until the app is operable." (`docs/archive/control-surface-unification/CONTROL_SURFACE_UNIFICATION.md` §12)
- **ACP / A2A bridges** — deferred from cockpit spec; no consuming surface.
  (`docs/archive/cockpit-refactor-spec/COCKPIT_REFACTOR_SPEC.md` §9)
- **Supervisor agents & workflow engine** — deferred; needs workflow-automation design.
  (`COCKPIT_REFACTOR_SPEC.md` §9)
- **Transport / role / prompt / tool registries** — deferred; no present pain beyond
  `BackendRegistry`. (`COCKPIT_REFACTOR_SPEC.md` §9)
- **Native mobile** — deferred; Web UI is the mobile surface for v1.
  (`COCKPIT_REFACTOR_SPEC.md` §9)
