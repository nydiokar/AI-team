# AI-Team Gateway — Hot Context

**Last Updated:** 2026-06-25
**Branch:** `feat/webui-ui0` (Web UI track — **ladder complete, ready to merge**) — mesh/State-Sep track lives on `main`

> This file is the **fast-orientation** doc: what the project is, how it's wired
> *right now*, the active plan, and the immediate next step. It is intentionally
> short. Per-phase build history lives in `docs/PROGRESS_LOG.md`. The detailed
> task breakdown for the **paused mesh** plan lives in `.ai/NEXT_TASKS.md`. The
> active plan (Web UI) is the ladder in `docs/COCKPIT_REFACTOR_SPEC.md` §14 — see
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
> - Always spawn the gateway process ONLY trough **pm2 (ecosystem.config)** and use
>   **`python main.py`**

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

**Plan of record / ladder (single source of truth):** `docs/COCKPIT_REFACTOR_SPEC.md`
§14. Build order was `M1 → UI-0 → UI-1 → F → I → UI-2 → G′ → H → UI-3 → UI-4 →
UI-5 → UI-6`. The control-surface unification that embedded the web API into the
gateway is `docs/CONTROL_SURFACE_UNIFICATION.md` (U1..U6, all done).

**Status as of 2026-06-25 — THE LADDER IS COMPLETE (all committed on `feat/webui-ui0`):**

| Rung | What | Status |
|------|------|--------|
| F / I | write + SSE control API; canonical event adapter | ✅ done (U1–U5, embedded `src/control/control_api.py`) |
| UI-0/UI-1 | TS domain + adapters + fixtures; live Sessions/System | ✅ done |
| **UI-2** | live write surface, SSE transport, real session timeline | ✅ done (`5590cb5`) — send/stop, idempotency, SSE all live-verified |
| **G′** | task lifecycle object + sectioned `/api/tasks` | ✅ done (`baba1d1`) |
| **H + UI-3** | durable approval gate + approval card | ✅ done (`3dc00c7`) — **see priority note** |
| **UI-4** | **Files & artifacts** | ✅ done — artifact listing API + live FilesScreen (`docs/UI4_CHECKLIST.md`) |
| UI-5 | logs / health / terminal | ✅ done — live activity feed on System screen off the SSE stream (`docs/UI5_CHECKLIST.md`) |
| **UI-6** | hardening, a11y, PWA, push | ✅ done (`9c452e8`, gate-verified live `8a0ee76`) — installable PWA, offline shell, a11y pass, install affordance (`docs/UI6_CHECKLIST.md`) |

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
  ├── src/core/session_store.py     DB-first reads, dual-write to JSON + DB
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
state/mesh.db                         SQLite — now read-first by session_store
results/<task_id>.json                full task artifact
logs/session_events/<id>.log          per-session NDJSON
logs/events.ndjson                    system-wide event log
```

**Config flags that matter:** `MESH_ENABLED` (default `false` — gateway behaves
exactly as pre-mesh), `MESH_SHADOW_WRITE` (default `true`), `WORKER_TOKEN`,
`MESH_TAILSCALE_IP`, `MESH_TASK_SERVER_PORT` (9002).

---

## Background: mesh / State-Separation track (PAUSED — not the current work)

> This is NOT where we are now. The current work is the **Web UI track** on
> `feat/webui-ui0` (see that section above — building UI-4 next). The mesh content
> below is **paused background context**: it last advanced 2026-06-11, lives on
> `main`, and is parked at Phase 4. Kept here only so an agent has the runtime
> picture; do not treat it as the active plan.

**The mesh is LIVE across two real machines (as of 2026-06-11).** Gateway +
embedded task server run on the **Pi5 (`kanebra`)**; the worker daemon runs on a
separate PC (**`Horse`**). Real tasks dispatch machine-to-machine, and — the part
that was the whole point — a task now **survives a gateway restart end-to-end**:
the worker keeps running, the gateway reattaches on startup and delivers the
worker's real result to Telegram (no fabricated "Task failed"). State Separation
Phases 0–3 are effectively complete.

The only remaining work **on this (paused) mesh track** is **Phase 4 — graceful
degradation / fallback** (see the mesh plan + `.ai/NEXT_TASKS.md`). It is not
scheduled against the current Web UI work.

**Cockpit M1 (2026-06-21, `feat/session-service-m1`):** a separate, completed
track preparing the gateway for a second surface (Web UI). It added a
transport-neutral `SessionService` (lifecycle create/bind off the Telegram
class), a single backend `registry.py`, a descriptive `SessionOrigin` tag on
`Session` (persisted via DB migration 12), and `docs/CONTROL_CONTRACT.md`.
Telegram behavior is byte-identical (gate matches the pre-M1 baseline). Scope
discipline lived in `docs/M1_CHECKLIST.md`. M2+ (SessionView DTO, WS/HTTP
transport, workflow events) remain deferred.

History of every completed phase (8, 9, Step B/C, D1–D6) + the 2026-06-11
restart-resilience milestone: `docs/PROGRESS_LOG.md`.

---

## Mesh plan reference — State Separation (PAUSED background, not active)

> Reference only — the **active plan is the Web UI track** (top of file). This is
> the parked mesh plan, kept for the runtime picture.

**Plan of record (for the mesh track):** `docs/STATE_SEPARATION_PLAN.md`. This
**supersedes** the old
standalone "VPS migration Phase 4" — VPS migration is now simply the end-state of
this plan's Phases 2–3 (server on the VPS, workers on local machines).

Progress against that plan (verified against code on 2026-06-10):

| Phase | Goal | Status |
|-------|------|--------|
| 4 | Graceful degradation: 1 embedded fallback worker + JSON when mesh down | **Not started — the only remaining work on the (paused) mesh track** |

---

## ➡️ Immediate next step

**ACTIVE track (Web UI, branch `feat/webui-ui0`): LADDER COMPLETE — merge to
`main`.** Every rung M1 → UI-6 is shipped, committed, and gate-verified (see the
Web UI status table above). UI-6 (installable PWA + offline shell + a11y +
install affordance) landed in `9c452e8` and was gate-verified live in `8a0ee76`.
The remaining work on this branch is **merge it to `main`** and close it out.
Genuinely additive future work (Web Push, streaming, diff hunks, terminal,
approvals automation) is parked in `docs/DEFERRED.md` — none blocks shipping.

**Mesh track (paused, branch `main`):** the two-machine cutover is **done and
live** — gateway+server on the Pi5, worker on `Horse`, restart-resilient. The only
remaining mesh plan work is Phase 4 (graceful degradation / fallback). Full,
dispatch-ready task definitions with acceptance checks are in `.ai/NEXT_TASKS.md`
(§Phase 4).

Current run mode: gateway + embedded task server in one process on the Pi5
(`MESH_EMBEDDED_SERVER=true`), worker daemon as a separate process on `Horse`.

**Deploy note (important):** the gateway runs on the **Pi5**. Code changed on a
worker machine must be pushed, then `git pull` + gateway restart **on the Pi5**;
confirm with `git log -1` on the Pi5. A fix that's on `origin/main` but not yet
pulled+restarted on the Pi5 will look like it "didn't work."

Per-task detail and acceptance checks: `.ai/NEXT_TASKS.md`.

---

## Architecture rules (do not violate)

- DB is the canonical **read** source; JSON dual-write stays as the ultimate
  fallback and is **never deleted**.
- The server/gateway host keeps its **own embedded worker capacity** (configurable
  pool, default ≥1 — **not** capped at 1) that executes tasks when no remote node
  is available. Prefer remote nodes when online; the server runs work locally when
  none are, so tasks never stall. (Updated 2026-06-11; supersedes the old "exactly
  1 fallback worker" rule.)
- `MESH_ENABLED=false` ⇒ gateway is byte-for-byte the old behavior.
- Session affinity is a hard correctness requirement: a session pinned to a
  machine must execute on that machine. `backend_session_id` is machine-local.
- No uncontrolled autonomous behavior. Ollama is optional/helper-only. Artifacts
  are mandatory for audit.

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | runtime, task queue, workers, routing, recovery, mesh hooks |
| `src/core/session_service.py` | transport-neutral session lifecycle (create/bind) — M1 inbound seam |
| `src/backends/registry.py` | single declaration site for the backend set — M1 (add a backend = one edit here) |
| `src/core/session_store.py` | DB-first session reads + JSON/DB dual-write |
| `src/control/db.py` | SQLite mesh DB — canonical DB layer |
| `src/control/task_server.py` | FastAPI task server (currently embedded) |
| `src/control/node_registry.py` | node registry + heartbeat expiry |
| `src/worker/agent.py` | worker daemon (runs as its own process on worker nodes) |
| `src/telegram/interface.py` | Telegram command surface |
| `config/settings.py` | all config incl. `MeshConfig` |
| `docs/CONTROL_CONTRACT.md` | **M1** — event + inbound-command + backend + read-model contract for a 2nd surface |
| `docs/COCKPIT_REFACTOR_SPEC.md` / `docs/M1_CHECKLIST.md` | M1 rationale + the executed build checklist |
| `docs/COCKPIT_REFACTOR_SPEC.md` | Web UI ladder (§14) — **all rungs M1→UI-6 done** |
| `docs/DEFERRED.md` | Web UI track — deliberately-not-built future boxes (Web Push, streaming, diff hunks, terminal, approvals automation) |
| `docs/STATE_SEPARATION_PLAN.md` | mesh plan (PAUSED background, not active) |
| `docs/AGENT_MESH_SPEC.md` | mesh design spec |
| `docs/PHASE_4_RUNBOOK.md` | VPS cutover runbook (= State Sep end-state) |
| `docs/PROGRESS_LOG.md` | completed-work history |
| `ecosystem.config.js` | PM2 supervisor config |

---

## Deferred (valid, lower priority)

- Backend lifecycle hooks (session-ID detection, PreToolUse security, PostToolUse
  quality gates) — `docs/BACKEND_HOOKS_STRATEGY.md`.
- Codex end-to-end validation.
- OpenCode server cross-machine sessions (needs shared DB mount).
- Postgres migration — trigger: >5 nodes or observed SQLite write contention.
