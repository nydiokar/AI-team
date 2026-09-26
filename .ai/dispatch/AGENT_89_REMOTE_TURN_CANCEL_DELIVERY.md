```yaml
job_id: AGENT_89_REMOTE_TURN_CANCEL_DELIVERY
created_at: "2026-09-26T17:25:14.000000+00:00"
status: done
owner: "A87 (self-directed, operator-observed)"
depends_on: []
results_ref: DISPATCH_LOG.md#A89
evidence:
  - "tests/test_remote_cancel_turn.py"
updated_at: "2026-09-26T17:28:41.465590+00:00"
```

# DISPATCH — A89 · Remote turn cancel delivery (PR #171)

**Level:** 2 (bug fix, two-file patch, no schema change) · **Type:** emergency fix + post-mortem
**Status:** done — merged, gateway live, worker restart pending (operator decision)
**Branch:** `feat/fix-remote-session-cancel` → merged to `main` as `bdb804b`
**Depends on:** —

> **Why this packet exists.** The operator reported that pressing Stop on a running Claude session
> was not reacting. This job documents the root cause, the fix that was applied, why it was applied
> at the wrong layer (and therefore spawned A90), and the precise boundary of what is now working vs.
> what still needs a worker restart to activate.

---

## Root Cause

The Docker controller/worker split (PRs #162/#163, deployed 2026-09-25) exposed a silent locality
assumption in `ClaudeSDKClientDriver.cancel(session)`:

```python
# src/backends/claude_driver.py:1508
def cancel(self, session: Session) -> None:
    with self._lock:
        sdk_sess = self._sessions.get(session.session_id)   # ← in-process dict
    if sdk_sess is not None:
        sdk_sess.cancel_inflight()                          # ← never reached for remote sessions
```

`self._sessions` is populated only by sessions opened **in this process**. With
`GATEWAY_LOCAL_EXECUTION_ENABLED=false`, the gateway container runs zero Claude turns locally —
all sessions live in the worker process. The gateway's `_sessions` dict is always empty for them.
`cancel()` returned silently without interrupting anything.

The cancel event was eventually set and the DB task was marked `failed`, so the gateway gave up
waiting — but the Claude CLI process on the worker kept generating to completion. From the operator's
perspective: pressing Stop appeared to do nothing.

### Why it surfaced now

In the original single-process PM2 setup (gateway + in-process workers on the same host, same
Python process), `self._sessions` was populated — cancel worked. The mesh remote-worker path existed
before Docker (Horse was a separate node via PM2), but the session being cancelled yesterday was
likely on the local kanebra worker which was previously in-process. The Docker migration made
**every** session remote for the controller, making the bug consistently observable.

### Confirmed by evidence

Session corpus: 5,156 claude sessions, 753 codex, 83 opencode, 51 opencode-server — **all** have
`machine_id` set (kanebra or Horse). Zero sessions with empty `machine_id` in the live state
directory. Every running claude turn goes to the remote worker process.

---

## Fix Applied (PR #171)

Two changes, minimal diff:

### 1. `src/orchestrator.py` — `cancel_task()` + `_enqueue_remote_cancel_turn()`

When `cancel_task()` sees a session with `machine_id` set and `backend != codex`, it now also
enqueues a fire-and-forget `cancel_turn` control task to the owning worker node — identical in
shape to the existing `cancel_codex` and `_dispatch_remote_close()` patterns:

```python
# After the existing backend.cancel(session) call:
if backend_name != "codex" and getattr(session, "machine_id", ""):
    self._enqueue_remote_cancel_turn(session)
```

`_enqueue_remote_cancel_turn()` writes a `cancel_turn` task row pinned to `session.machine_id`
in the mesh DB. The worker picks it up on its next poll (no slot consumed — same out-of-slot
routing as `close_session`).

### 2. `src/worker/agent.py` — `cancel_turn` action handler

```python
if action == "cancel_turn":
    # In _execute_task() — called before cancel_codex / close_session branches
    session = _make_session_from_payload(payload)
    if session is not None:
        backend = backends.get(session.backend or "claude")
        canceller = getattr(backend, "cancel", None)
        if callable(canceller):
            await asyncio.to_thread(canceller, session)
    return {"success": True, "output": "cancel_turn requested", ...}
```

Routed out-of-slot in the worker's task loop (does not consume a turn slot — would deadlock waiting
behind the very turn it is trying to stop).

### Coverage

The fix covers **all non-codex backends** (claude, opencode, opencode-server). For each:
- claude: `cancel_inflight()` → `client.interrupt()` (SDK interrupt)
- opencode: `OpenCodeServerBackend.cancel()` → HTTP DELETE to local opencode server
- opencode-server: same

Codex is excluded because it already has `cancel_codex` dispatched by `_dispatch_to_node`'s own
poll loop.

### Backends NOT affected

- `close()` — already handled by `_dispatch_remote_close()` via `SessionService` (correct layer)
- `compact_session` — already handled by the orchestrator's own mesh routing at line 6402
  (routes through `_process_task_remote` when `session.machine_id` is set)

---

## Tests

`tests/test_remote_cancel_turn.py` — 4 new targeted tests:

| Test | What it proves |
|---|---|
| `test_local_session_cancel_calls_backend_cancel_only` | No remote enqueue for sessions without machine_id |
| `test_remote_session_cancel_enqueues_cancel_turn` | Remote enqueue fires for mesh-pinned sessions |
| `test_remote_codex_session_does_not_enqueue_cancel_turn` | Codex exclusion respected |
| `test_enqueue_remote_cancel_turn_no_db_is_silent` | DB failure does not raise (cancel_task must never fail) |

All 4 pass. Existing `test_session_cancellation.py` unaffected.

---

## Deployment State

| Component | State | Notes |
|---|---|---|
| Gateway | ✅ live — `bdb804b` | Restarted 2026-09-26 after merge |
| Worker (PM2 `ai-team-worker`) | ⚠️ **pending restart** | Needs restart to pick up the `cancel_turn` handler in `agent.py`. Without it the gateway enqueues the task correctly but the worker falls through to normal execution. |

**To fully activate:** `pm2 restart ai-team-worker` — operator decision per policy (worker restart
disrupts live sessions). Until then, the gateway correctly enqueues the `cancel_turn` task; it
sits pending until the worker picks it up on restart.

---

## Why This Was Fixed at the Wrong Layer (→ A90)

The correct fix would have been inside `ClaudeSDKClientDriver.cancel()` itself — the backend
implementation should detect that it doesn't own the session and route the interrupt to wherever
the session actually lives. Instead, the fix lives in the orchestrator (`cancel_task`), which now
needs to know about `session.machine_id` to paper over the backend's blind spot.

This inverts the abstraction: the orchestrator is supposed to call `backend.cancel(session)` and
be done — it should not need to know whether the backend can reach that session or not. The
`CodingBackend` interface contract (`src/core/interfaces.py:313`) says "best-effort cancellation of
a running backend session" — it should mean that regardless of topology.

A90 documents and tracks the architectural refactor to move this concern back into the right layer.

---

## Closure

- ✅ Root cause identified and confirmed against live session corpus
- ✅ Fix in place gateway-side; `cancel_turn` handler written for worker side
- ✅ 4 targeted tests, all green
- ✅ PR #171 merged to `main` at `bdb804b`, gateway restarted
- ⚠️ Worker restart required to fully activate — operator decision
- 📋 Architectural debt logged as A90
