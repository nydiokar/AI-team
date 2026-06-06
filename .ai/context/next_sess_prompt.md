# Handoff prompt — next session (Phase 9 Step B: wire mesh routing into process_task)

## What this project is

A Telegram-controlled gateway for local coding agents (Claude Code, Codex, OpenCode).
The user sends messages from their phone, tasks execute on their PC, results come back to Telegram.

Long-term direction: move the control plane to a VPS, with worker nodes (PC, laptop, etc.)
pulling tasks from a central SQLite task DB. Full spec: `docs/AGENT_MESH_SPEC.md`.

**Read `.ai/CONTEXT.md` (Phase 9 section) and `.ai/NEXT_TASKS.md` before doing anything** —
they document exactly what's built, what was adversarially reviewed and fixed, and why the
remaining work is scoped the way it is.

---

## Where things stand (commit `0debb7a`, all smoke-tested, working tree clean)

Phase 9 Steps 1–3 are **done, adversarially reviewed (14 issues found, 9 critical/high fixed),
and smoke-tested** (`python scripts/test_mesh_local.py` — 18/18 passing):

- `src/control/task_server.py` — FastAPI app, 9 endpoints, Bearer auth, MeshDB-backed
- `src/control/node_registry.py` — NodeRegistry: heartbeat expiry, offline failover, DB persistence
- `src/worker/{config,agent}.py` — full worker daemon (register/poll/claim/heartbeat/drain/nudge)
- `src/orchestrator.py` — `_run_backend_local`, `_dispatch_to_node`, `_dispatch_or_run_local` exist
  and are correct, but **`_dispatch_or_run_local` is NOT called from `process_task` yet** — that's
  this session's job (see below).
- `_mesh_enqueue_task` self-claims its own shadow-written rows immediately after insert, so
  `MESH_ENABLED=false` (default) is **provably** identical to pre-mesh behavior — `process_task`
  itself is completely untouched. This is load-bearing: don't break this guarantee while you wire
  routing in.

---

## Your job this session: Phase 9 Step B — wire `_dispatch_or_run_local` into `process_task`

This is the one piece of real, higher-risk work standing between "mesh exists" and "mesh actually
routes tasks to remote workers." Read `_dispatch_or_run_local` and `process_task` in
`src/orchestrator.py` first — understand what `process_task` currently does end-to-end (retries,
timeouts, heartbeats, artifact writing, session updates, Telegram replies) before changing anything.

**The core design problem you need to solve:**
`process_task` has retry/timeout/heartbeat machinery built around the assumption that execution is
local and synchronous-ish. Routing to a remote worker means: enqueue → poll for completion (the
worker posts results back via `/tasks/{id}/result`, asynchronously, on its own schedule) → resume
`process_task`'s post-execution flow (artifact writing, session update, Telegram reply) once the
result lands. You have two honest options, both named in NEXT_TASKS.md:

(a) Duplicate the surrounding machinery (retry/timeout/heartbeat/artifact/reply) inside the
    remote-dispatch branch, accepting some near-term duplication for lower risk to the local path, or
(b) Extract that machinery into shared helpers both paths call, which is more work but avoids drift
    between local and remote behavior over time.

**Recommendation: go with (a) first**, scoped tightly, behind `MESH_ENABLED=true` AND
`session.machine_id` being set (i.e., routing only activates for sessions explicitly pinned to a
remote node — never for ordinary local sessions). This keeps blast radius minimal: a bug in the new
path can only affect sessions someone deliberately pinned to a remote machine for testing. Don't
attempt the bigger refactor (b) until (a) has proven the approach works in practice.

**Hard correctness requirements — do not regress these:**
- `MESH_ENABLED=false` (the default) → zero behavior change. Verify by re-running
  `python scripts/test_mesh_local.py` AND confirming `process_task`'s local path is untouched
  for sessions without `machine_id`.
- Session affinity is non-negotiable: a session with `machine_id` set MUST execute on that
  specific node, or fail loudly — never silently fall back to local execution (that would
  silently corrupt `backend_session_id` continuity, since backend sessions are machine-local).
- Don't let a remote-dispatch failure break the local path for other sessions. Isolate it.

---

## How to test without a second machine (do this first, before any live trial)

You don't need Tailscale, a VPS, or a second device to validate the routing wiring:

1. Extend `scripts/test_mesh_local.py` (or write a sibling script) to simulate a full
   create-session → enqueue → worker-claims → worker-posts-result → `process_task` resumes →
   artifact written → session updated cycle, all in-process against an isolated trial DB.
2. Only after that passes, try the live two-process trial described in `.ai/NEXT_TASKS.md`
   "Recommended rollout — safe trial sequence" Step A (task server + worker on localhost,
   isolated `mesh_trial.db`, different ports — zero risk to the live gateway).
3. Real two-machine testing (NEXT_TASKS.md Step C) comes LAST, once you're confident in the
   wiring — at that point send a Telegram message to a session pinned to that node's
   `machine_id` and watch it route through DB → worker → result → Telegram.

---

## Important constraints

- **Do not change local-path behavior for sessions without `machine_id`.** That's the whole point —
  zero regression for the 99% of sessions that stay local.
- **Do not add new DB tables or change the schema** unless strictly required — all needed methods
  already exist in `src/control/db.py` (`enqueue_task`, `claim_task`, `complete_task`, `fail_task`,
  `get_pending_tasks`, `append_event`, etc).
- **Do adversarial review of your own change** before calling it done — this codebase has a track
  record of subtle async/locking/double-execution bugs (see the 14 issues found in the Phase 9
  review, documented in `.ai/CONTEXT.md`). Don't skip this step.
- The gateway runs under PM2 as `ai-team-gateway`. Restart with `pm2 restart ai-team-gateway --update-env`.
- After finishing: update `.ai/CONTEXT.md` and `.ai/NEXT_TASKS.md`, run the smoke test, commit,
  and write the next handoff prompt (this file) for Step C (real two-machine test).

---

## Key files to read before starting

| File | Why |
|------|-----|
| `.ai/CONTEXT.md` | Full project state, Phase 9 build history, all 14 review findings + fixes |
| `.ai/NEXT_TASKS.md` | Exact rollout sequence (Steps A/B/C) and what's deliberately not done yet |
| `docs/AGENT_MESH_SPEC.md` | Full mesh spec — Sections 5–8, 10 cover routing and session affinity |
| `src/orchestrator.py` | `process_task`, `_task_worker`, `_dispatch_or_run_local`, `_dispatch_to_node`, `_mesh_enqueue_task` (read the docstring on this one — it explains the self-claim trick) |
| `src/control/db.py` | All DB methods available |
| `src/worker/agent.py` | What the worker does once it claims a task — your dispatch path needs to match its expectations (payload shape, result shape) |
| `scripts/test_mesh_local.py` | Existing smoke test — extend this rather than writing from scratch |
