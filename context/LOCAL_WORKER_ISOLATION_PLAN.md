# Local Worker Isolation Plan

**Problem**: The gateway owns the Claude agent's lifecycle. When PM2 restarts the gateway, any in-flight local task dies with it. The user sees "interrupted by gateway restart" — a backend detail that should never surface.

**Root cause**: Kanebra runs local tasks via `_run_backend_local` → `backend.create_session / resume_session` — all inside the gateway's own asyncio event loop. The Claude CLI subprocess is a child of the gateway process. Parent dies, child dies.

**Contrast with Horse (correct)**: `agent.py` runs as a separate PM2 process on Horse. It registers via HTTP, claims tasks, runs Claude independently, posts results to the DB. Gateway restart is invisible to it. On startup the gateway calls `_reattach_remote_task` and picks up the result.

---

## Goal

Kanebra should run `agent.py` as a separate PM2 process alongside the gateway, registering as `node_id=kanebra` (or `local`). The gateway becomes a pure router. Local and remote tasks go through identical paths.

**Result**: gateway restart → agent keeps running → task completes → notification arrives. User sees nothing abnormal.

---

## What Changes

### 1. Run agent.py on Kanebra via PM2

Add a second entry to `ecosystem.config.js` for the local worker:

```js
{
  name: "ai-team-worker-local",
  script: "worker_main.py",
  interpreter: "python3",
  env: {
    NODE_ID: "kanebra",
    CONTROLLER_URL: "http://localhost:8000",
    // ... same env vars Horse uses
  }
}
```

The worker registers itself at `/nodes/register` on startup — same as Horse.

### 2. Remove local dispatch bypass in the gateway

Currently the gateway checks: "is there a capable remote node? if not, fall through to `_run_backend_local`." That fallback is the problem.

Once the local worker is registered as a node, it will appear in the node registry as an online node with the right backends. The existing dispatch logic (`_dispatch_to_node`) will route to it naturally — no special-casing needed.

`_run_backend_local` and the local worker pool (the 4 asyncio coroutines in the gateway) can be retired. The gateway stops being an executor.

### 3. Session recovery becomes uniform

`_reattach_remote_task` already handles "task still in DB as claimed, worker still running." With the local worker now being a proper node, the existing reattach path covers Kanebra too. The `session_recovery_deferred` branch (the silent drop) becomes unreachable for local tasks.

---

## What Does NOT Change

- `agent.py` itself — already production-tested on Horse
- The task server / mesh DB — no schema changes
- Horse's setup — unchanged
- The Telegram interface — unchanged
- The incarnation-ID reaper fix (a085dc2) — still applies, still needed for the re-registration gap

---

## Migration Path

1. Add local worker entry to `ecosystem.config.js`
2. Start it: `pm2 start ecosystem.config.js --only ai-team-worker-local`
3. Verify it registers in the node list alongside Horse
4. Test: queue a task, restart the gateway mid-task, confirm task completes and notification arrives
5. Once confirmed stable: remove the local worker pool from the orchestrator (`_worker_loop`, `_run_backend_local`, the `task_queue`)
6. Update `pm2 save`

Step 5 is the irreversible cut. Do it only after step 4 passes cleanly.

---

## Risk

**Medium**. The behavioral change is significant (gateway loses executor role) but the execution path (agent.py) is already battle-tested. The main risk is configuration drift between the local and remote worker envs. Mitigate by sharing a single worker config template.

The deferred-session silent drop bug is a secondary issue. Once the local worker is a proper node, it disappears naturally. If you want to patch it before this plan lands, treat deferred the same as interrupted (fall through to the notify path at orchestrator.py:364) — one-liner fix, safe to do standalone.

---

## Status

Not started. Depends on nothing. Can be picked up independently.
