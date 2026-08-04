# AI-team

A self-hosted control plane for local coding agents. Run Claude Code, Codex, or OpenCode
on your own machines; control them from a web UI or Telegram; let a manager agent coordinate
parallel workers and review their output — all without leaving your infrastructure.

Python 3.11+ · FastAPI · SQLite · React 19 · MIT

---

## What it does

You post an objective. A **manager** agent breaks it into tasks and dispatches **workers**
to handle them on whichever machines you have available. The manager waits on the workers,
reviews their committed diffs in git, requests rework if needed, and closes the job only
when every acceptance criterion is met. You watch the whole thing from a web UI on your
phone.

If you just want individual sessions without the orchestration, that works too — the manager
layer is opt-in and flag-gated.

## Interfaces

**Web UI** — a mobile-first React SPA served by the gateway. Shows live sessions and their
turn timelines, the Case roster and dispatch lineage, node and mesh health, per-backend cost
breakdown, and a cost explorer. Streams updates over server-sent events; supports web push
notifications.

**Telegram** — an optional secondary surface over the same orchestrator. Send instructions,
check status, and get notifications from your phone without exposing the web UI to the open
internet.

**Control API** — a REST + SSE surface at `/api/*`. The web UI is a client of this API; you
can drive it from scripts or other tools with the same bearer token.

## Key capabilities

| What | How |
|---|---|
| **Native session resume** | The gateway resumes the backend's own session per turn — no transcript replay, no context loss. |
| **Multi-machine mesh** | Enable `MESH_ENABLED` to route tasks to worker nodes on other machines. Pinned sessions stay on the machine that owns their native backend. |
| **Manager / Case orchestration** | One manager, one durable Case in SQLite, N workers. The manager dispatches, waits, reviews git diffs, and closes — or escalates to you. |
| **Durable waits** | Wait state is an append-only event on the Case, not in memory. A gateway restart or manager crash does not lose an in-flight job. |
| **Cost visibility** | Per-turn token usage and cost tracked across backends. Daily, session, and per-Case budget alerts. |
| **Everything flag-gated** | New behaviour lands behind flags that default OFF. Flag-off is byte-identical to prior behaviour — safe to run against real repositories. |

## Getting started

Prerequisites: Python 3.11+, pnpm (for the web UI), and at least one supported agent CLI
(Claude Code, Codex, or OpenCode).

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,telegram]"
cp .env.example .env
```

Set the workspace boundary in `.env` before the gateway can touch files:

```env
CLAUDE_BASE_CWD=/home/you/projects
CLAUDE_ALLOWED_ROOT=/home/you/projects
```

Build the UI and run the pre-flight check:

```bash
pnpm --dir web install
pnpm --dir web build
.venv/bin/python main.py doctor
```

Start the gateway, then open `http://127.0.0.1:9003/` in your browser:

```bash
.venv/bin/python main.py
```

Check liveness: `curl http://127.0.0.1:9003/health`

For a full first-session walkthrough see [`docs/QUICK_START.md`](docs/QUICK_START.md).
For a production deployment (auth token, network binding) see the
[operations runbook](docs/RUNBOOKS/OPERATIONS_PM2.md).

## Architecture overview

```
   ┌──────────── GATEWAY (one process, one machine) ─────────────┐
   │                                                             │
   │   ├─ Control API   /api/*  · web UI at /                   │
   │   ├─ Mesh server   worker claim / heartbeat / telemetry     │
   │   └─ Telegram bot  optional, same orchestrator              │
   └────────────────┬────────────────────────┬───────────────────┘
                    │ browser / SSE          │ HTTP mesh protocol
           ┌────────┴──────┐       ┌─────────┴──────────────┐
           │  Operator UI  │       │  Worker nodes           │
           │  phone/laptop │       │  separate machines      │
           └───────────────┘       │  own .env, own backends │
                                   └────────────────────────┘
```

State is DB-canonical: sessions, tasks, Case membership, dispatch lineage, and a per-turn
conversation and artifact store all live in SQLite (WAL). Per-session JSON files are a
never-deleted fallback, not the source of truth. Restarting the gateway does not lose a Case.

Full process and API map: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Agent backends

| Backend | Notes |
|---|---|
| **Claude Code** | SDK driver (recommended) and legacy CLI driver |
| **Codex** | Stable, full telemetry |
| **OpenCode** | Local server and CLI modes |

## Feature flags

Behaviour-changing work lands behind flags with a byte-identical-when-off contract.
The main ones:

| Flag | Effect |
|---|---|
| `MESH_ENABLED` | Route tasks to remote worker nodes |
| `MANAGER_ROLE_ENABLED` | Manager role, `/api/manager`, Case decision surface |
| `CASE_CONTINUATION_ENABLED` | Autonomous Case continuation — incurs real spend; explicit operator decision |
| `HARNESS_FLOW_DRIVE` | Authoritative Case writes; required for Case attach and worker JOIN |

Full inventory: [`docs/ENV_FEATURE_FLAGS.md`](docs/ENV_FEATURE_FLAGS.md).

## Tech stack

**Gateway** — Python 3.11+, FastAPI, Uvicorn, Pydantic v2, `claude-agent-sdk`, anyio,
SQLite (WAL, versioned migrations), MCP over stdio, PM2.

**Web UI** — React 19, TypeScript, Vite, Tailwind v4, TanStack Query, Zustand, Radix, Vitest.

**Quality** — pytest, Vitest, ruff / black / mypy, pre-commit, GitHub Actions CI. End-to-end
tests that invoke a paid CLI are opt-in behind an explicit flag — never run automatically.

## Documentation

| Topic | Link |
|---|---|
| First session walkthrough | [`docs/QUICK_START.md`](docs/QUICK_START.md) |
| Runtime and API map | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| Environment flags reference | [`docs/ENV_FEATURE_FLAGS.md`](docs/ENV_FEATURE_FLAGS.md) |
| Manager / worker harness | [`docs/harness/dispatch_pipeline.md`](docs/harness/dispatch_pipeline.md) |
| Web UI development | [`web/README.md`](web/README.md) |
| Full doc index | [`docs/INDEX.md`](docs/INDEX.md) |
