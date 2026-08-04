# AI-Team Gateway

> Your local coding agents, with a control room.

AI-Team Gateway is a session-first control plane for local coding agents. Open a
conversation from the web UI, send work from Telegram when you are away from
your desk, and continue the same native agent session on the next turn—without
turning the system into an opaque autonomous-agent framework.

The gateway is built for work you can inspect: durable session state, task and
artifact history, event logs, bounded workspace access, and an optional mesh
for routing work to other machines.

## What it gives you

| Capability | What it means in practice |
| --- | --- |
| **One session, native resume** | Continue Claude Code, Codex, or OpenCode-backed sessions through their own native resume mechanisms. |
| **A real control surface** | Use the mobile-first web UI as the primary interface; Telegram is an optional secondary interface over the same gateway. |
| **Inspectable state** | SQLite is the canonical read store; session JSON remains a durable file-backed fallback. Events, task history, and artifacts stay available for review. |
| **Bounded execution** | Workspace roots, task timeouts, queue limits, and per-session audit data keep agent work explicit and reviewable. |
| **Optional distributed work** | Enable the mesh only when you need remote worker nodes; pinned sessions stay on the machine that owns their native backend session. |
| **Opt-in Manager/Cases** | A flag-gated Manager can coordinate worker sessions inside a durable Case. It is invoked explicitly—not autonomous by default. |

## The shape of the system

```text
Browser (primary UI) ─┐
                      ├─► AI-Team Gateway ─► Claude Code / Codex / OpenCode
Telegram (optional) ──┘          │
                                 ├─► SQLite + inspectable fallback files
                                 └─► optional remote worker nodes
```

The web UI, Control API, and optional Telegram bot run in one gateway process.
When mesh mode is enabled, the task server runs alongside it and remote workers
connect to that server. Read the [architecture map](ARCHITECTURE.md) for the
full process and HTTP surface.

## Quick start (Linux)

Prerequisites: Python 3.11+, a supported local agent CLI, and pnpm for building
the web UI. Telegram is optional.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,telegram]"
cp .env.example .env
```

Set the workspace boundary in `.env` before starting a gateway that can edit
files:

```env
CLAUDE_BASE_CWD=/home/you/projects
CLAUDE_ALLOWED_ROOT=/home/you/projects
```

Build the UI and check the local environment:

```bash
pnpm --dir web install
pnpm --dir web build
.venv/bin/python main.py doctor
```

Start the gateway, then open `http://127.0.0.1:9003/`:

```bash
.venv/bin/python main.py
```

In a second terminal, use the health endpoint to confirm the running service:

```bash
curl http://127.0.0.1:9003/health
```

For the complete setup and first-session walkthrough, see
[Quick Start](QUICK_START.md). For a production deployment, set an explicit
`DASHBOARD_TOKEN` (or `WORKER_TOKEN`) and bind the Control API only to an
appropriate private interface.

## What this is not

- Not a generic autonomous-agent framework.
- Not a hidden memory or PTY-persistence system.
- Not a router that silently moves machine-pinned sessions to another worker.
- Not a promise that every experimental or legacy component in the repository is
  part of the production path.

The product direction and anti-goals live in
[the production vision](../.ai/context/production_vision.md).

## Operating notes

- The web UI is served by the gateway itself; its development sources live in
  [`web/`](../web/).
- `GET /health` is an unauthenticated liveness check. `/api/*` requires a bearer
  token; API docs are off by default.
- Mesh, Manager/Case orchestration, and other advanced behavior are feature
  gated. Review [the feature-flag reference](ENV_FEATURE_FLAGS.md) before
  activating them—some flags can create paid agent work.
- The current deployment and work-in-progress are deliberately kept out of this
  README. Check [the live context](../.ai/CONTEXT.md) for that information.

## Explore from here

| If you want to… | Start here |
| --- | --- |
| Understand the runtime topology and API | [Architecture](ARCHITECTURE.md) |
| Configure a local or production deployment | [Quick Start](QUICK_START.md) · [Environment flags](ENV_FEATURE_FLAGS.md) |
| Work on the web UI | [Web UI README](../web/README.md) |
| Use the Manager/worker harness | [Dispatch pipeline](harness/dispatch_pipeline.md) |
| Find the document that owns a topic | [Documentation overview](OVERVIEW.md) · [full index](INDEX.md) |
| See current priorities and active jobs | [Repository context](../.ai/CONTEXT.md) · [dispatch log](../.ai/dispatch/DISPATCH_LOG.md) |

## Development checks

Run only the checks relevant to the area you changed:

```bash
.venv/bin/python -m pytest tests/<targeted_test>.py
pnpm --dir web typecheck
pnpm --dir web test
pnpm --dir web build
```

Some end-to-end tests can invoke paid agent CLIs. They are opt-in; see
[the test README](../tests/README_TESTS.md) before running them.
