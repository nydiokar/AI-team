```yaml
job_id: AGENT_70
created_at: "2026-08-04T23:13:48.757530+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: big-pickle
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-08-04T23:14:07.331217+00:00"
```

# DISPATCH — AGENT_70

**Goal:** Close the close-vs-turn race that turned session `99d997c3c8b6`
(`task_ed5283f1`) into a false failure: the Manager called `release_worker` →
`close_session` while the worker's turn was still in-flight, the SDK interrupt
surfaced as `error_during_execution`, and the task was marked failed with the
agent's reply truncated. PR #68/#69 already fixed the *aftermath* (full payload
shipped, salvage detection keeps the session AWAITING_INPUT). This job fixes the
*race itself*: a `close_session` must never kill an in-flight turn.

**Depends on:** none

## Task

1. Map the seams: `scripts/mcp_manager.py::_release_worker` → `POST /api/sessions/{id}/close`
   → `api_close_session` (`src/control/control_api.py`) → `SessionService.close_session`
   → `_dispatch_remote_close` (`src/orchestrator.py`, "remote_close_enqueued") → worker
   `_handle_close_session` / `_execute_task` `close_session` branch (`src/worker/agent.py`) →
   `ClaudeSDKClientDriver.close()` → `cancel_inflight()` → `client.interrupt()`.
2. Decide the guard (recommendation: **defer** the close, not refuse — the Manager's
   `release_worker` intent should still land, just after the turn ends, so `backend_session_id`
   continuity is preserved for the completed turn):
   - Gateway-side: before dispatching the close, if the session has a claimed in-flight
     `mesh_tasks` row, hold the close (bounded wait on that task's terminal state, or mark the
     session `close_pending` and re-attempt) instead of enqueueing the close immediately.
   - Worker-side: in `_handle_close_session`, if the backend driver reports an active in-flight
     turn for this session, wait for it (bounded) before `backend.close()`.
3. Keep the result of the in-flight turn intact: the completing turn must still report its real
   outcome; the close only tears down the process AFTER that.
4. Regression test that reproduces the race: session busy + close dispatched → in-flight turn
   completes unharmed and the close runs after (no `error_during_execution`, no interrupt).

## Done when

- `close_session` never interrupts an in-flight turn (deferred/queued behind it).
- The in-flight turn's real outcome is preserved.
- Deterministic regression test(s) green via plain `pytest` on the touched modules.
- Branch + PR + self-merge per branch policy; gateway restarted; worker restart surfaced to the
  operator (worker-side code only lands on a worker redeploy).
- Set `evidence:` to the test file + merge commit, close in DISPATCH_LOG.
