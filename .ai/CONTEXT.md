# AI-Team Gateway — Hot Context

**Last Updated:** 2026-06-10
**Branch:** `main`

> This file is the **fast-orientation** doc: what the project is, how it's wired
> *right now*, the active plan, and the immediate next step. It is intentionally
> short. Per-phase build history lives in `docs/PROGRESS_LOG.md`. The detailed
> task breakdown for the active plan lives in `.ai/NEXT_TASKS.md`.

---

## What this project is

A Telegram-controlled gateway for local coding agents (Claude Code, Codex,
OpenCode CLI, OpenCode server). You open a session from Telegram, follow-up
messages route to that session, and each turn resumes the native backend
session. State is file-backed and inspectable, with a SQLite mirror.

Canonical product intent: `.ai/context/production_vision.md`.

---

## Architecture — as it runs today

**One process** (`ai-team-gateway`, PM2). When `MESH_ENABLED=true` it also hosts
the task server embedded on its own event loop.

```
[Telegram] → [Gateway process]
  ├── src/telegram/interface.py     command surface (/status, /nodes, pickers…)
  ├── src/orchestrator.py           task queue, in-process workers, routing, recovery
  ├── src/core/session_store.py     DB-first reads, dual-write to JSON + DB
  ├── src/control/db.py             SQLite mesh DB (WAL, busy_timeout=5000, migrations)
  ├── src/control/embedded_server.py task server, embedded (mesh on)
  ├── src/control/{task_server,node_registry}.py  HTTP API + node registry
  ├── src/worker/agent.py           worker daemon — built, NOT run in prod yet
  └── src/backends/                 claude_code, codex, opencode, opencode-server
```

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

## Where we are NOW

The mesh foundation is **fully built and reviewed** (DB layer, task server, node
registry, worker daemon, orchestrator remote routing, `/nodes` + `/node`,
observability spine, `fix_session_machine_ids.py`). All of it ships behind
`MESH_ENABLED`, which is **off in production** — so today the gateway is still a
single-process, locally-executing gateway, unchanged in behavior.

What is *not* yet done is making the three roles (gateway / task server / worker)
**independent processes** so a gateway restart no longer kills in-flight work.
That is the active plan below.

History of every completed phase (8, 9, Step B/C, D1–D6): `docs/PROGRESS_LOG.md`.

---

## Active plan — State Separation

**Plan of record:** `docs/STATE_SEPARATION_PLAN.md`. This **supersedes** the old
standalone "VPS migration Phase 4" — VPS migration is now simply the end-state of
this plan's Phases 2–3 (server on the VPS, workers on local machines).

Progress against that plan (verified against code on 2026-06-10):

| Phase | Goal | Status |
|-------|------|--------|
| 0 | Prereq checks + DB trust cleanup | **DONE (2026-06-10)** — orphan tasks failed; shadow-write `create()` bug fixed; DB purged from 418→**162 real sessions** (all with task history; 256 test/empty rows removed). Remaining: operator sets real `MESH_TAILSCALE_IP` in `.env` before enabling mesh. |
| 1 | DB as canonical read source + smart recovery | **DONE** — `session_store.get` reads DB-first; `db.get_task_by_session` exists; `_recover_stale_busy_sessions` uses DB (orchestrator.py:299) |
| 2 | Standalone `ai-team-server` process + `TaskServerClient` in gateway | **In progress** — `server_main.py`, `TaskServerClient`, disabled PM2 entry built + isolation-tested (embedded still default). Remaining: repoint dispatch/recovery off `get_registry()`, retire `embedded_server.py`, cutover. |
| 3 | Standalone `ai-team-worker` process; gateway workers → fallback | **Not started** (worker code exists, never run in prod) |
| 4 | Graceful degradation: 1 embedded fallback worker + JSON when mesh down | **Not started** |

---

## ➡️ Immediate next step

Phase 2 scaffolding is built (`server_main.py`, `TaskServerClient`, disabled PM2
entry — all isolation-tested, embedded still default). **Next is the Phase 2
cutover:**

1. Repoint `_process_task_remote` / `_dispatch_to_node` / recovery / health off
   the in-process `get_registry()` singleton onto `TaskServerClient`.
2. Retire `src/control/embedded_server.py` + its `_start/_stop` wiring.
3. Cutover test **with the gateway stopped** (deliberate, together): start
   `ai-team-server`, restart gateway, verify discovery + dispatch over HTTP.

Constraints while the live gateway runs: no `pm2 restart`, no migrations/DB
surgery against prod `state/mesh.db`, no starting `ai-team-server` (would clash
on `:9002`). Test mesh processes via `AI_TEAM_ENV_FILE`=temp env + spare port +
temp `MESH_DB_PATH`.

(Operator action still pending before mesh is enabled for real: set a valid
`MESH_TAILSCALE_IP` in `.env`, or blank it for `127.0.0.1`.)

Per-task detail and acceptance checks: `.ai/NEXT_TASKS.md`.

---

## Architecture rules (do not violate)

- DB is the canonical **read** source; JSON dual-write stays as the ultimate
  fallback and is **never deleted**.
- The gateway must keep **exactly 1** embedded fallback worker that activates only
  when the mesh is broken (task server unreachable or no workers online), so it
  can always run recovery tasks.
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
| `src/core/session_store.py` | DB-first session reads + JSON/DB dual-write |
| `src/control/db.py` | SQLite mesh DB — canonical DB layer |
| `src/control/task_server.py` | FastAPI task server (currently embedded) |
| `src/control/node_registry.py` | node registry + heartbeat expiry |
| `src/worker/agent.py` | worker daemon (built, not yet run in prod) |
| `src/telegram/interface.py` | Telegram command surface |
| `config/settings.py` | all config incl. `MeshConfig` |
| `docs/STATE_SEPARATION_PLAN.md` | **active plan** |
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
