# AI-team

A self-hosted control plane for coding agents. You give it one objective; a **manager** agent
breaks it into tasks, dispatches them to **worker** agents running as separate processes on other
machines, reviews the work that comes back, and reports to you when the objective is actually done.

The interesting engineering here is not the model calls — it is everything around them: agents
coordinating agents instead of a human shepherding each step, and doing it durably enough that a
crash, a restart, or a lost session does not lose the work in flight.

Python 3.11+ · FastAPI · SQLite · React 19. MIT.

---

## Overview

The unit of work is a **Case**: one operator objective, durable in SQLite, with a manager agent
bound to it. The manager opens the Case, dispatches worker sessions *into* that same Case, waits
on them, reviews their committed diffs, and is the only actor allowed to close the Case. A worker
finishing a task is evidence of progress — it does not close the objective.

Concretely:

- **One gateway process** holds the orchestrator, the HTTP control API, the web UI, the mesh task
  server, and (optionally) a Telegram bot. All interfaces share one live orchestrator, so they see
  the same sessions, the same registry, the same event stream.
- **Workers are separate OS processes on separate machines.** They dial into the gateway's mesh
  port, claim tasks under a lease, and stream telemetry back.
- **State is DB-canonical.** Sessions, tasks, Case membership, dispatch lineage and an append-only
  event ledger live in SQLite (WAL). Restarting the gateway does not lose a Case.
- **Everything new is behind a feature flag**, with the invariant that flag-off is byte-identical
  to the previous behaviour. That is what makes it safe to run this against real repositories.

Agent backends are pluggable: Claude Code (SDK and CLI drivers), Codex, and OpenCode. The gateway
resumes the backend's own native session per turn rather than replaying transcripts itself.

## Architecture

```
   ┌──────────────────────── GATEWAY BOX (one machine) ───────────────────────┐
   │                                                                          │
   │   python main.py  ──►  ONE process (the gateway / controller)            │
   │   │                                                                      │
   │   ├─ TaskOrchestrator      sessions · dispatch · routing · recovery      │
   │   │     • session lifecycle      • node registry (mesh nodes)            │
   │   │     • submit_instruction     • notifier fan-out                      │
   │   │     • invoke_manager / close_case / wake-dispatcher                  │
   │   │                                                                      │
   │   │   ── interfaces, all holding the SAME orchestrator ──                │
   │   ├─ Control API      (:9003)   CONTROL_API_ENABLED                      │
   │   │     • read   /api/sessions|tasks|nodes|work|flows|cost               │
   │   │     • write  /api/instructions|manager|cases/*|sessions/*            │
   │   │     • push   /api/events/stream   (server-sent events)               │
   │   │     • serves web/dist (the React operator UI) at /                   │
   │   ├─ Mesh task server (:9002)   MESH_ENABLED                             │
   │   │     • workers claim/run/report tasks here, under a lease             │
   │   └─ Telegram bot    (in-process, optional secondary surface)            │
   └───────────────┬──────────────────────────────┬───────────────────────────┘
                   │ HTTP + SSE                   │ HTTP (mesh protocol)
          ┌────────┴─────────┐          ┌─────────┴──────────────┐
          │  Operator UI     │          │  WORKER NODES          │
          │  React SPA in    │          │  worker_main.py        │
          │  phone / laptop  │          │  separate machines,    │
          │  browser :9003   │          │  separate processes    │
          └──────────────────┘          │  → CONTROLLER_URL:9002 │
                                        └────────────────────────┘
```

The operator UI and a worker sit at opposite ends: the UI is a *client* that controls the gateway,
a worker is a *compute node* the gateway hands tasks to. They never talk to each other.

Two host variables, and they are not redundant: `CONTROLLER_URL` is set on **worker** boxes and
points at the gateway's mesh port; `CONTROL_API_HOST` is set on the **gateway** and decides which
interface the UI/API binds to. It defaults to the Tailscale IP, then loopback — never `0.0.0.0`.
Binding to the tailnet address is the outer auth layer; `/api/*` still requires a bearer token
on top of it.

## How a task flows through the system

1. **Invocation.** The operator posts an objective to `POST /api/manager`. The gateway opens exactly
   one Case (`flow_runs` row) and boots a manager session bound to it, with the manager role prompt
   and a scoped MCP tool profile.
2. **Orientation and planning.** The manager reads the repository, git history and project context
   before touching anything, then expands the objective into scoped tasks with acceptance criteria.
3. **Dispatch.** The manager calls `dispatch_worker`. That becomes a real gateway session — not an
   in-process subagent — routed over the mesh to a worker node. The worker *joins the manager's
   Case* as a member; the parent→child edge is persisted in `flow_links`, so the whole dispatch tree
   is queryable rather than living in some agent's head.
4. **Waiting without blocking.** Instead of holding a slot on a synchronous poll, the manager arms a
   durable **wait group** (`ANY` / `ALL` / named) over its dispatched workers and returns control. A
   background wake-dispatcher re-enters the Case with a single coalesced review turn once the group
   is satisfied. Delivery is at-least-once, leased so only one manager invocation per Case is ever
   active, and idempotent on redelivery.
5. **Review.** The manager reviews the *committed diff* in git rather than the worker's self-report,
   and records a verdict (`accepted` / `rework_requested` / `waived`) as a durable event. A Case
   cannot close with an unresolved rework request.
6. **Closure.** `close_case` checks the Case's completion criteria and refuses on unmet criteria,
   open child work, or a pending approval — returning a structured `{ok:false, reason}`, not a bare
   error. Only then is the objective done.

Round caps, turn caps and cost ceilings bound the loop; exhausting one escalates to the operator
rather than looping. There is no standing autonomous process — a Case only exists because an
operator invoked one, and continuation is bounded by that invocation.

## Durability and recovery

This is where most of the design effort went.

- **SQLite is canonical** (WAL, busy timeout, versioned migrations) for sessions, tasks, the Case
  graph, per-turn conversation and artifacts. Per-session JSON files are a never-deleted fallback,
  not the source of truth.
- **Leased claims.** A worker claims a task under a lease with an incarnation id. A stale-claim
  reaper releases orphans, so a worker dying mid-task does not strand the Case.
- **Reconcile spool.** DB writes that fail during an outage are spooled to disk and replayed on the
  next startup.
- **Durable wait markers.** A wait is an append-only event on the Case, not in-process state, so a
  manager or gateway crash mid-wait does not lose it — a resumed manager reconciles its outstanding
  waits against the already-durable `task.finished` events.
- **Reconstruction from the DB alone.** `get_case_brief` rebuilds a manager's working context —
  objective, criteria, dispatched workers, review history — from persisted rows.
- **Crash respawn.** If a wake fires on a satisfied Case whose manager session is dead, the harness
  reconstructs the Case, respawns exactly one role-full manager bound to the *same* Case (never a new
  one), re-arms its waits and resumes toward closure — under the same atomic claim lease, so a racing
  tick cannot double-respawn.
- **Restart context restore.** When a worker restart loses a backend driver, the next task gets a
  bounded, size-capped block of the last completed turns prepended so the session resumes coherently.
- **Pinned vs. unpinned routing.** A session pinned to a node is host-or-nothing — the backend session
  id is machine-local, so relocating it would silently lose context. Unpinned work may run anywhere.

## Interfaces

**Operator web UI** — a React 19 / Vite / Tailwind v4 mobile-first SPA, built to `web/dist` and
served by the gateway itself at `/`. It streams live updates over server-sent events
(`/api/events/stream`) with dedupe and an offline-tolerant event log, and covers sessions and their
turn timelines, the Work surface (Case roster, timeline, lineage graph, worker roster), node and
mesh health, backend usage, and a cost explorer with per-Case manager-vs-worker breakdown. Web push
notifications are supported via VAPID.

**Mesh** — the worker-facing HTTP surface on `:9002`: claim, heartbeat, result upload, telemetry
batches, node registry and liveness. Workers run `worker_main.py` on their own machines with their
own `.env`, declaring which backends they can serve.

**Telegram** — an optional secondary command surface over the same orchestrator, useful for driving
and monitoring runs from a phone without exposing the UI.

**Manager tool surface** — an MCP server (`scripts/mcp_manager.py`) exposing the manager's control
verbs (`dispatch_worker`, `wait_for_worker`, `arm_wait_group`, `get_case`, `get_case_brief`,
`record_review`, `open_case`, `close_case`, `release_worker`, and others) as thin wrappers over
Control API endpoints. Tools are scoped per session: only a manager session receives the manager
profile.

## Configuration and feature flags

Configuration is env-driven through `config/settings.py` (Pydantic models). Behaviour-changing work
lands behind flags that default OFF with a byte-identical-when-off contract, so a half-finished
capability can sit on `main` without changing production behaviour.

`GET /api/flags` renders the live effective flag inventory from the runtime registry. Registry-
writable flags can be flipped at runtime with `PUT /api/flags/{FLAG}` without a restart, and fall
back to `.env` and code defaults when no override exists. Flags that are bootstrap-only or
worker-local are reported read-only rather than pretending they can be flipped safely.

The main gates:

| Flag | Effect |
|---|---|
| `MESH_ENABLED` | Route work to remote worker nodes through the registry |
| `CONTROL_API_ENABLED` | The `/api/*` control surface and the web UI |
| `MANAGER_ROLE_ENABLED` | Manager role boot, scoped tool grant, `/api/manager` and the Case decision surface |
| `HARNESS_FLOW_DRIVE` | Authoritative Case/stage writes — required for Case attach and worker JOIN |
| `CASE_CONTINUATION_ENABLED` | Wake-dispatcher autonomous Case continuation (real spend — an explicit operator decision) |
| `DURABLE_RELAY_ENABLED` | Durable worker-wait markers and reconciliation |
| `HARNESS_LEVEL3_GUARD` | Admission gate on task enqueue; rejects over-scoped submits with a clean 409 |
| `QUOTA_COORDINATOR_ENABLED` | Observe-only provider quota-window tracking |

`docs/ENV_FEATURE_FLAGS.md` is the full inventory, including an honest list of knobs that currently
do nothing.

## Status and roadmap

Runs daily on a real deployment: a gateway on a Raspberry Pi 5 with a worker node on a separate
machine, supervised by PM2. Around 120 Python test modules plus a Vitest suite for the UI, with CI
on every push and pull request.

**Built and running**

- Session-first gateway; native backend resume for Claude Code, Codex and OpenCode
- Mesh dispatch to remote workers: lease/claim, heartbeat, node registry, stale-claim reaping,
  pinned/unpinned routing with affinity fallback
- Durable Case substrate: `flow_runs` / `flow_links` / `flow_events`, dispatch lineage, honest
  session affiliations, Work read model
- Manager as an invoked role: `POST /api/manager`, scoped MCP tool profile, worker dispatch into a
  shared Case, review verdict emission, authoritative checked closure
- Autonomous Case continuation: durable wait groups, coalesced wake turns, at-least-once leased
  re-entry, DB-only Case reconstruction, crash respawn, round-cap escalation, operator kill path
- Web UI: sessions, turn timelines, Work/Case surface, cost explorer, node and mesh health, SSE
  streaming, web push
- Per-turn telemetry and usage/cost accounting across backends, with a read-only cost-alert surface

**Current work**

Spec authoring and task-graph decomposition inside a single Case, a quota-window coordinator for
multi-agent rate limits, and cost-alert delivery through the existing push fan-out.

Fully unattended, self-igniting operation is deliberately out of scope: every Case stays bounded by
one operator invocation, by design rather than by omission.

## Tech stack

**Gateway** — Python 3.11+, FastAPI, Uvicorn, Pydantic v2, `claude-agent-sdk`, anyio, SQLite (WAL,
versioned migrations), MCP over stdio for the manager and jobs tool surfaces, PM2 for supervision,
Tailscale for the network boundary.

**Web UI** — React 19, TypeScript, Vite, Tailwind v4, TanStack Query with persistence, Zustand,
Radix primitives, Vitest.

**Quality** — pytest with async support, Vitest, ruff / black / mypy, pre-commit, GitHub Actions CI.
Tests can invoke a paid CLI, so real end-to-end runs are opt-in behind an explicit environment flag.

## Getting started

```powershell
python -m venv .venv
. .venv\Scripts\Activate.ps1
pip install -e ".[dev,test,telegram]"

cd web; npm install; npm run build; cd ..

# set CLAUDE_BASE_CWD and CLAUDE_ALLOWED_ROOT in .env, then:
python main.py doctor
python main.py
```

The Control API and UI come up on `:9003`. Check liveness with
`curl http://127.0.0.1:9003/health` — do not run `python main.py status` against a live gateway, as
it takes the gateway lock.

Full walkthrough: `docs/QUICK_START.md`. Process and HTTP map: `docs/ARCHITECTURE.md`. Doc index:
`docs/INDEX.md`.
