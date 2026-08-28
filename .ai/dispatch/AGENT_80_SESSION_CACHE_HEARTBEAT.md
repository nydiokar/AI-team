```yaml
job_id: AGENT_80_SESSION_CACHE_HEARTBEAT
created_at: "2026-08-26T01:00:00+03:00"
status: active
owner: "codex"
depends_on: []
results_ref: ".ai/dispatch/AGENT_80_SESSION_CACHE_HEARTBEAT.md#closure"
evidence: []
updated_at: "2026-08-26T01:00:00+03:00"
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
- [ ] DB schema and helpers added.
- [ ] Runtime flags registered with observe default on and active default off.
- [ ] Wait-group producer integrated.
- [ ] Watched-job producer integrated.
- [ ] Active heartbeat delivery integrated through deterministic leases.
- [ ] API/UI observation and manual controls added.
- [ ] Tests/build/deploy completed.
- [ ] Adversarial review completed.

## Closure

Pending.
