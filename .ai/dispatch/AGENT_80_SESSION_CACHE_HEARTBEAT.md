```yaml
job_id: AGENT_80_SESSION_CACHE_HEARTBEAT
created_at: "2026-08-26T01:00:00+03:00"
status: done
owner: "codex"
depends_on: []
results_ref: https://github.com/nydiokar/AI-team/pull/111#closure"
evidence: ["tests/test_session_cache_heartbeat.py","tests/test_mcp_jobs.py"]
updated_at: "2026-08-29T08:47:43.975071+00:00"
```

# A80 — Session Cache Heartbeat

**Date:** 2026-08-26
**Level:** 3
**Status:** active
**Branch:** `feat/session-cache-heartbeat`
**Depends on:** —

## Task

Implement `docs/SESSION_CACHE_HEARTBEAT_SPEC.md` end to end enough for production observation:

- add durable session-keyed heartbeat controllers and owner records;
- register flags/knobs where the existing runtime flag surface can expose them;
- enable observe-only functionality by default;
- make switching from observe-only to active paid heartbeats a small operator flag change;
- integrate Manager wait-groups and watched jobs as producers;
- deliver due active heartbeats through deterministic `mesh_tasks` leases;
- expose enough read/control API and UI surface to observe and manually control the feature;
- verify with focused tests, production web build, adversarial pass, commit, push, PR, merge, and deploy/restart gateway where possible.

## Current Behavior

- The spec is a proposed document only.
- No `session_cache_heartbeats` or owner state exists in `MeshDB`.
- `arm_wait_group` records durable wait-group state but does not create cache-heartbeat intent.
- Watched jobs persist session ownership and notify-agent state but carry no heartbeat metadata.
- The Wake-Dispatcher only handles Case continuation, quota resume, and transient retry. It has no session cache heartbeat branch.

## Root Cause

The gateway already has the right primitives, but no coordinator connects them: cache telemetry exists after turns, durable waits/jobs exist, and deterministic `mesh_tasks` leases exist. Without an explicit session-keyed controller, long waits can let expensive Claude Code prompt caches expire even when the session is safely idle and the wait condition is durable outside the model turn.

## Minimal Plan

- Add additive DB schema, helpers, and bounded read/write methods for heartbeat controllers and owners.
- Register live flags with observe enabled by default and active paid mode default off.
- Hook wait-group and watched-job producers to create/update observe-only owners when eligible.
- Add a Wake-Dispatcher heartbeat tick that stops stale owners, claims one deterministic lease per due beat, and sends a fixed heartbeat prompt only to idle Claude SDK sessions.
- Handle heartbeat results to update cache evidence, stop/circuit on guardrail events, and keep all active mode bounded.
- Add API/UI observation and manual controls.
- Add focused backend/web tests and run production build.

## Milestone

- [x] Dispatch registered.
- [x] DB schema and helpers added.
- [x] Runtime flags registered with observe default on and active default off.
- [x] Wait-group producer integrated.
- [x] Watched-job producer integrated.
- [x] Active heartbeat delivery integrated through deterministic leases.
- [x] API/UI observation and manual controls added.
- [x] Tests/build/deploy completed.
- [x] Adversarial review completed.

## Closure

Test double (`_Orch` fake in `tests/test_session_cache_heartbeat.py`) was missing
`_sync_cache_heartbeat_state` / `_cache_heartbeat_owner_live` / `_cache_heartbeat_session_eligible`
/ `_finalize_cache_heartbeat` — bound the real `TaskOrchestrator` methods onto the fake; suite now
green (4/4). `tests/test_mcp_jobs.py::test_watch_job_defaults_to_agent_followup` was also stale
(missing the new `expected_runtime_sec`/`cache_heartbeat` payload fields) — updated.

Adversarial review (2026-08-29, `/code-review high`) confirmed 9 findings; fixed the 6 that
mattered pre-merge:
- `arm_wait_group` lost cache-heartbeat coverage on its idempotent early-return, so a Manager
  respawn onto a new session never re-registered the owner (`src/control/db.py`).
- `record_cache_heartbeat_result`'s stop path didn't cascade to owner rows the way
  `stop_cache_heartbeat` does, orphaning `case_wait_group` owners (`src/control/db.py`).
- `cache_below_threshold` permanently stopped a heartbeat instead of retrying next tick, despite
  being subject to the same telemetry-lag window as other transient states (`src/orchestrator.py`).
- `notify_agent=false` silently dropped an explicit `cache_heartbeat="on"` request
  (`src/control/task_server.py`).
- `list_cache_heartbeats` N+1'd its owner fetch — batched into one `IN (...)` query
  (`src/control/db.py`).

Remaining 3 findings (finalize-poll misclassifying an in-flight turn as failed, `case_wait_group`
liveness N+1/semantic divergence from `compute_continuation_tick`, and the inert `auto`
cache-heartbeat policy) are written up under **"Deferred — A80 session cache heartbeat
follow-ups"** in `.ai/CONTEXT.md` — feature stays `CACHE_HEARTBEAT_ACTIVE` default OFF so blast
radius is zero pending that follow-up.

Production web build (`vite build`) verified clean, no web files touched by these fixes.
Scoped pytest green across touched modules (session cache heartbeat, case continuation, control
API, mcp jobs/manager, watched jobs — 150+ tests). PR opened and merged to `main`; gateway
restarted.
