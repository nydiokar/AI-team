```yaml
job_id: AGENT_90_BACKEND_LOCALITY_ABSTRACTION
created_at: "2026-09-26T17:25:14.000000+00:00"
status: ready
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A90
evidence: []
updated_at: "2026-09-26T17:25:14.000000+00:00"
```

# DISPATCH — A90 · Backend interface locality abstraction (architectural refactor)

**Level:** 3 (cross-cutting interface change, all backends, careful migration) · **Type:** architectural refactor
**Status:** ready (authored, not dispatched — operator decides timing)
**Branch:** `feat/backend-locality-abstraction` + PR + self-merge
**Depends on:** — (A89 is the pragmatic precursor; A82 must not be in conflict at dispatch time — check feat/session-turn-queue)

> **Why this packet exists.** A89 fixed turn cancellation for remote sessions by patching the
> orchestrator to know about `session.machine_id` — it works, but it inverts the abstraction.
> The `CodingBackend` interface (`src/core/interfaces.py`) is supposed to be the universal adapter
> layer: the orchestrator calls `backend.cancel(session)` and the backend handles it, regardless of
> where the session is running. Right now `cancel()`, and historically `close()`, had silent
> in-process locality assumptions that the orchestrator had to patch around at the call site. This
> job refactors the backend interface so those assumptions are visible, testable, and handled at the
> right layer — and documents which methods are still locality-blind as a safety record.

---

## Context: the locality problem

Every backend implementation has methods that operate on **live in-process state**: a session pool,
an asyncio loop, an HTTP client, a process dict. These methods are correct when the session was
opened in the same process. They silently do nothing when called from a different process (the
gateway container) on a session that lives in a remote worker.

### Current state of each method (as of A89)

| Method | Interface layer | Actual behavior for remote session | Fixed |
|---|---|---|---|
| `cancel(session)` | `CodingBackend.cancel` | gateway → `_sessions.get()` → None → no-op | ✅ A89 (orchestrator-level workaround) |
| `close(session)` | `CodingBackend.close` | gateway → `_sessions.get()` / `_base_urls.get()` → no-op | ✅ pre-A89 (orchestrator-level workaround via `_dispatch_remote_close`) |
| `compact_session(session)` | `CodingBackend.compact_session` | orchestrator detects `machine_id` and calls `_process_task_remote` BEFORE touching the backend | ✅ correct (orchestrator owns the routing, not patching the backend) |
| `create_session` / `resume_session` | `CodingBackend.create_session` / `resume_session` | only ever called on the OWNING process (local in-process path or through `_process_task_remote` on the remote side) | ✅ correct by design |

### What's wrong with the orchestrator-level workarounds

A89 added this to `cancel_task()`:
```python
if backend_name != "codex" and getattr(session, "machine_id", ""):
    self._enqueue_remote_cancel_turn(session)
```

And pre-A89 for close, `SessionService` has:
```python
elif not is_local:
    if self._remote_close_dispatcher is not None:
        self._remote_close_dispatcher(s)
```

Both are **orchestrator/service layer patching the backend's blind spot**. The orchestrator should
not need to know whether `cancel(session)` will reach the right process — that is the backend's
contract to fulfil. When a third backend is added, whoever writes it must know to also register a
"remote cancel" path in the orchestrator, which is easy to miss (as happened with the original
OpenCode cancel gap).

### The right fix

`CodingBackend.cancel(session)` should work correctly regardless of where the session is running.
Two viable approaches (evaluated below); this job picks one and implements it.

---

## Task

### 1. Choose an approach and justify it in a design note

**Option A — Dispatch hook injected into the backend**  
The backend receives a callable (e.g. `remote_dispatch: Callable[[Session, str], None]`) at
construction time. When `cancel()` detects it doesn't own the session locally, it calls the hook.
The orchestrator injects `_enqueue_remote_cancel_turn` as the hook. The backend's contract is
fulfilled; the orchestrator is not duplicating topology logic.

*Pros:* backend stays testable in isolation (inject a spy); no circular imports; minimal interface
change; the pattern mirrors how `SessionService` already gets `_remote_close_dispatcher`.  
*Cons:* the hook is not part of the abstract interface (can't be enforced statically); each backend
that has in-process state needs wiring.

**Option B — `cancel()` calls `_sessions.get()` and returns a boolean "did I handle it"**  
The interface changes: `cancel(session) -> bool`. `True` = handled; `False` = caller should try
alternative routing. The orchestrator checks the return value and enqueues the remote task if needed.

*Pros:* no new dependencies injected into backends.  
*Cons:* the orchestrator STILL needs to know about remote dispatch — just triggered by a return
value instead of a `machine_id` check. The abstraction is still inverted; you just moved the leak
from a pre-check to a post-check.

**Option C — `CodingBackend` receives a `NodeDispatcher` at construction time**  
`NodeDispatcher` is a light interface (`enqueue_control_task(session, action, payload)`) injected
into every backend at construction. When `cancel()` finds no local session, it calls
`dispatcher.enqueue_control_task(session, "cancel_turn", ...)` directly.

*Pros:* the backend interface contract is fully self-contained; the orchestrator's `cancel_task()`
reduces to just `backend.cancel(session)` with no `machine_id` check; testable with a mock
dispatcher.  
*Cons:* requires passing a dispatcher into every backend constructor; interface change is wider.

**Recommended: Option A.** It mirrors the proven `_remote_close_dispatcher` pattern already in
use in `SessionService`, imposes minimal interface change, and makes the injected hook testable. The
only departure from Option C is that it's a callable not a named interface — document it clearly.

### 2. Implement

Regardless of which option is chosen, the outcome must be:

1. **`cancel(session)` on any backend works correctly whether called in-process or from the
   gateway container** — the SDK interrupt (or equivalent) reaches the process that owns the
   session without the orchestrator or service layer knowing about remote dispatch.

2. **The orchestrator's `cancel_task()` removes its `machine_id` check and remote-enqueue
   call** — those belong in the backend, not here. After this job the call site is:
   ```python
   if backend is not None:
       backend.cancel(session)
   # No _enqueue_remote_cancel_turn, no machine_id guard here
   ```

3. **`close(session)` is refactored on the same pass** — the `_remote_close_dispatcher` injection
   in `SessionService` moves into the backend's `close()` implementation via the same hook
   mechanism. This removes the `is_local` check from `session_service.py:163-180`.

4. **All four backend files are updated**: `claude_driver.py`, `claude_code.py`, `opencode.py`,
   `codex_native.py`. For each, the `cancel()` and `close()` implementations either:
   - Use in-process state as before (correct — session is local), OR  
   - Delegate to the injected hook when the session is not local

5. **Construction wiring**: every place a backend is instantiated (`orchestrator.py.__init__`,
   `src/worker/agent.py`) injects the appropriate hook. The gateway's hook is
   `_enqueue_remote_cancel_turn`; the worker's hook is `None` (worker is always local to its own
   sessions).

6. **Tests**: each backend's `cancel()` and `close()` are tested with:
   - local session (in-process): no hook called
   - remote session (machine_id set, not in local pool): hook called with correct payload
   - no hook injected (worker construction): no error raised

7. **Remove the now-redundant orchestrator and SessionService workarounds** once the backend
   implementations are verified.

### 3. Audit `compact_session` for consistency

`compact_session` is already correctly handled in the orchestrator (mesh-routes before touching the
backend). Document whether this should also be moved into the backend or whether the orchestrator
owning this routing is architecturally correct (it may be: compaction creates a Task and goes
through the full execution pipeline, which is legitimately the orchestrator's job).

### 4. Write a one-page `docs/BACKEND_LOCALITY_CONTRACT.md`

What the `CodingBackend` interface guarantees about locality:
- Which methods are always called on the session-owning process (create/resume)
- Which methods may be called cross-process and how they handle it (cancel, close)
- How the dispatch hook works and how to wire a new backend
- What "no hook injected" means (worker context — all sessions are local)

This is the missing contract that caused A89.

---

## Constraints

- **No behavior change** — `cancel_task()` and `SessionService.close_session()` must produce
  identical observable outcomes after the refactor.
- **Byte-identical for single-process deployments** — when `session.machine_id` is empty (local
  session), no hook is called; behavior is unchanged.
- **No new flags** — this is a refactor, not a feature. If anything fails, the test catches it.
- **pytest touched modules only** — TEST COST GUARD. Never the full suite.
- **Branch + PR + self-merge** as usual. Do not carry A82's `feat/session-turn-queue` edits.
- Do not restart the worker reflexively — any worker-side construction change activates on the
  next planned worker restart, surfaced to the operator.

---

## Done When

- `cancel(session)` and `close(session)` on all four backends are locality-aware without the
  orchestrator or service layer knowing about remote dispatch.
- `cancel_task()` in `orchestrator.py` has no `machine_id` guard or `_enqueue_remote_cancel_turn`
  call — it is just `backend.cancel(session)`.
- `SessionService.close_session()` has no `is_local` / `_remote_close_dispatcher` branch — it is
  just `backend.close(session)`.
- All targeted tests green; the four-backend test matrix (local/remote × cancel/close) is covered.
- `docs/BACKEND_LOCALITY_CONTRACT.md` written.
- A89's pragmatic workaround is replaced, not just duplicated.
- Set `evidence:` to test report + PR and `status: done`.

---

## Context for the dispatched agent: what A89 already did

A89 (PR #171, `bdb804b`) added two artefacts you will delete or supersede:

- `src/orchestrator.py` — `_enqueue_remote_cancel_turn()` method and its call site in
  `cancel_task()` (the `backend_name != "codex" and machine_id` guard).
- `src/worker/agent.py` — `cancel_turn` action handler and its routing in `_execute_one_task_loop`.

The worker-side `cancel_turn` handler is correct and should be **kept** — it is the remote
delivery mechanism regardless of which layer triggers it. What moves is who decides to trigger it:
currently the orchestrator decides; after this job the backend decides.
