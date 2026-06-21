# Control Contract

Status: v1 — shipped with Milestone M1 (`docs/M1_CHECKLIST.md`)
Audience: an author adding a **second surface** (Web UI), a **new backend**, or a future
**review/handoff workflow** — without re-reading orchestrator internals.

This doc pins the **already-stable** contracts of the gateway so the next agent reads
instead of greps. It is grounded in code as of the M1 branch; line numbers drift, names
don't.

The system has exactly **two halves of one boundary**:

- **Outbound — events.** Core emits a canonical NDJSON event stream; surfaces render it.
  Gap-recovery is a DB read, not a replay.
- **Inbound — commands.** Surfaces issue intent through **two** transport-neutral entry
  points only; no transport writes session state directly.

A new surface is *additive* against both halves. That is the whole point of M1.

---

## 1. Event envelope (stable)

Every event is one JSON object, one line, appended to `logs/events.ndjson` by
`src/core/observability.py:emit_event(...)`. The function never raises — observability must
never break a caller.

**Canonical fields (consumers MAY rely on these):**

| field | type | presence | meaning |
|---|---|---|---|
| `timestamp` | str | always | ISO-8601 local time |
| `event` | str | always | event name (see catalog §2) |
| `node_id` | str | always | hostname or `WORKER_NODE_ID` |
| `task_id` | str | when in a task context | correlates to a `Task` / `mesh_tasks` row |
| `session_id` | str | when in a session context | correlates to a `Session` |

**Rule:** extra fields arrive as `**fields` and vary per event. **Treat unknown fields as
opaque and skip them.** This is the same discipline OpenClaw's WS gateway uses (`{type, id,
method/event, payload, seq}` with opaque payloads); our envelope is the NDJSON equivalent and
predates the comparison.

IDs default from the current `log_context(...)` correlation context, so emitters inside a
task/session block don't repeat them. The envelope is a **superset** of the legacy schema
(`event/status/duration_s/task_type/error_class`) so old readers keep working.

**Gap recovery (important).** Like OpenClaw, **events are not replayed.** A surface that
missed events (reconnect, restart, cold start) does **not** scan the NDJSON tail — it
refreshes state from the **DB read model** (§6). The event stream is for live deltas; the DB
is for "what is true now."

---

## 2. Event catalog

One line per event currently emitted. Source: `emit_event(` / `_emit_event(` in
`orchestrator.py`, `notification_service.py`, `task_server.py` (M1 branch). "TG" = Telegram
currently surfaces it to the operator (via `NotificationService`, §3); most events are
log/stream-only today and a Web UI may choose to render any of them.

### Lifecycle / queue (`orchestrator.py`)

| event | meaning |
|---|---|
| `task_received` | a `*.task.md` file was picked up by the watcher |
| `task_created` | a `Task` was constructed (carries `source`: telegram/runtime/…) |
| `parsed` | task instruction parsed |
| `validated` | task passed pre-dispatch validation |
| `throttled` | queue full; task held under backpressure |
| `dropped_low_priority` | low-priority task dropped because the queue was full |
| `dropped_after_throttle` | task dropped after the throttle window expired |
| `security_violation` | path(s) outside the allowed root were rejected |
| `summarized` | result summary produced |
| `artifacts_written` | result artifacts written to disk |
| `artifacts_error` | writing artifacts failed |
| `task_archived` / `task_archive_failed` | task file archived (or archival failed) |
| `retry` | a turn is being retried (carries `attempt`, `class`, `delay_s`) |
| `timeout` | a turn exceeded its timeout |
| `cancel_requested` | operator requested cancellation |
| `cancelled` | a turn was cancelled (`when`: before_start / during_execution) |
| `session_recreated` | a stale backend session id was dropped and recreated |
| `worker_pool_scaled` / `worker_pool_reload_failed` | worker count changed (or reload failed) |

### Backend execution (dynamic names, `orchestrator.py`)

`<backend>_started` / `<backend>_finished` — emitted per turn, where `<backend>` ∈
{`claude`, `codex`, `opencode`, `opencode-server`} (see `_backend_event_name`). The
`*_finished` event carries `status`, `duration_s`, `error_class`, `backend`.
These map to the **reserved** `run.completed` / `run.failed` workflow names (§7).

### Mesh routing (`orchestrator.py`)

| event | meaning |
|---|---|
| `mesh_dispatch` | task handed to a remote worker node |
| `mesh_result` | a remote worker returned a result |
| `mesh_routing_failed` | could not route to the pinned `machine_id` |

### Worker (`task_server.py`)

| event | meaning |
|---|---|
| `task_failed` | a dispatched task failed on the worker side |

### Notifications (`notification_service.py`) — all TG

| event | meaning |
|---|---|
| `task_notification` *(via `notify_task_outcome`)* | a task finished; carries `status` (success/failed); the operator-facing outcome |
| `heartbeat` | long-running task still alive |
| `error_notification` | an error was surfaced to the operator |

---

## 3. Outbound transport boundary (already correct — do not refactor)

Core never reaches into Telegram for outbound. It calls `NotificationService`
(`src/core/notification_service.py`), which fans each notification to the registered
channel(s):

- `notify_task_outcome` (→ `task_notification`) · `notify_heartbeat` (→ `heartbeat`) ·
  `notify_error` (→ `error_notification`)

Each method (a) emits the corresponding event (§2) and (b) best-effort delivers to the
current channel (Telegram today). (`TelegramInterface.notify_completion` is a Telegram-side
delivery helper, not a `NotificationService` method, and emits no event.) **Adding a second delivery channel (WebSocket → Web UI) =
one new handler inside `NotificationService`** — no orchestrator change. This seam is the
outbound symmetry to the inbound `SessionService` (§4).

---

## 4. Inbound command surface (the ONLY way a surface issues intent)

There are exactly **two** transport-neutral entry points. A new surface calls these; it does
**not** re-implement them.

### 4a. Lifecycle — `SessionService` (`src/core/session_service.py`)

```python
SessionService.create_session(*, backend, repo_path,
                              chat_id=None, owner_user_id=None,
                              node_id="__local__", model=None,
                              origin=None, bind_chat=True) -> CommandResult
SessionService.bind_active(chat_id, session_id) -> CommandResult
```

- Reuses the orchestrator's single `SessionStore` (`orchestrator.session_service`).
- Preserves node pinning (`machine_id` ← `node_id`), model pinning, origin tagging (§5),
  and single-save semantics. `node_id == "__local__"` means "no remote pin."
- Returns `CommandResult(ok, reason, session)` — **a machine code, never prose.** `reason` ∈
  `{"", "unknown_backend", "session_not_found"}` today; each transport maps it to its own
  wording.

### 4b. Dispatch — `orchestrator.submit_instruction(...)`

```python
orchestrator.submit_instruction(description, task_type=None, target_files=None,
                                session_id=None, cwd=None,
                                source="telegram", extra_metadata=None) -> task_id
```

Already transport-neutral; set `source` to your surface name.

### The rule (binding on every transport)

> **No transport writes session state directly.** Create/bind goes through `SessionService`;
> dispatch goes through `submit_instruction`. **No transport puts user-facing prose in a
> `CommandResult`** — `reason` is a stable code; wording lives in the surface.

This mirrors OpenClaw's structured `req/res` (`id` + `ok` + `error`, no prose in the
protocol). Telegram's `_create_and_bind_session` is now a **thin wrapper** over 4a and is the
reference example for a second surface.

---

## 5. `SessionOrigin` — where a session came from

`src/core/interfaces.py`:

```python
@dataclass(frozen=True)
class SessionOrigin:
    channel: str = "telegram"   # "telegram" | "web" | "cli" | future surfaces
    kind: str = "user"          # "user" | "cron" | "subagent" (future workflow)
```

- One optional field on `Session`: `origin: Optional[SessionOrigin] = None`, defaulted in
  `__post_init__` to `SessionOrigin()` → today's `telegram`/`user`.
- Persisted in the session JSON **and** the DB mirror (migration 12 added the `origin`
  column, defaulted so old rows backfill to `telegram`/`user`). It survives the **DB-first**
  read path — which is why a JSON-only tag would have been inert (see M1 Step 2).
- **Descriptive, not routing.** It records provenance; it does **not** select a queue, scope,
  or policy. We adopted the *concept* from OpenClaw's `sessionKey`
  (`agent:<id>:<channel>:<kind>:<id>`) but **explicitly not** their key-string format or
  their four scoping modes (`main`/`per-sender`/`global`/…). Adding scoping modes is a
  speculative non-goal — do not.

A Web UI passes `origin=SessionOrigin("web")`; Telegram passes nothing and gets the default.

---

## 6. Read model (the refresh / gap-recovery path)

Authoritative state is the per-session JSON in `state/sessions/`; the **mesh DB**
(`src/control/db.py`) is the canonical, queryable mirror that `SessionStore` reads
**DB-first**. A surface answers "what is true now?" from:

| method | returns |
|---|---|
| `db.list_sessions(...)` | session rows |
| `db.list_tasks(...)` | task rows |
| `db.list_nodes(...)` | worker node rows |
| `db.get_task(task_id)` · `db.get_task_by_session(session_id, task_id)` | one task row |

A Web UI dashboard renders `events.ndjson` for live deltas (§1) and these reads for state.

**`SessionView` — shipped in M2.** A read-side DTO (`src/core/view_models.py`:
`SessionView.from_session(s)` → JSON-ready operator view, plus
`SessionService.list_views()` / `active_view(chat_id)`) gives every surface one read shape
instead of re-deriving `status`/`needs_input`/`is_active` from `Session` ad hoc. It carries
the raw `backend` string and the session's `origin` (channel/kind); rendering (icons/labels)
stays in each surface. Telegram adoption is opt-in (handlers may switch incrementally);
the Web UI (M3) renders `[v.to_dict() for v in session_service.list_views()]`.

---

## 7. Reserved workflow events (NOT emitted yet — reserved vocabulary)

When a review/handoff/approval workflow is built (M4), it MUST emit from this one vocabulary
so all surfaces share names. **None of these are emitted today. No code exists for them.**

```
review.requested     review.completed
handoff.created
approval.requested   approval.granted
run.failed           run.completed     (map to existing <backend>_finished, §2)
```

**Rule for that future work:** *workflow steps emit events (§1) and call existing services
(§4); they do not mutate state directly and do not require a workflow engine.* Reserving the
names now keeps the eventual implementation consistent and costs nothing.

---

## 8. How do I … (answers from this doc alone)

- **Add a surface (Web UI)?** Consume `events.ndjson` (§1–2) for live deltas; refresh from
  `db.list_*` (§6); issue intent via `SessionService` (§4a) + `submit_instruction` (§4b);
  tag sessions with `SessionOrigin("web")` (§5). Add a delivery handler to
  `NotificationService` for outbound (§3). **No core refactor required.**
- **Add a backend?** One edit: add a `name → factory` entry in
  `src/backends/registry.py`. `build_backends()`, `valid_backend_names()`,
  `is_valid_backend()` all derive from it; `CodingBackend` (`src/core/interfaces.py`) is the
  contract. Display icons live in each surface, not the registry.
- **Add a workflow event?** Use a reserved name from §7; emit via `emit_event` (§1); call
  `SessionService` / `submit_instruction` for any state change (§4). Do not add tables or an
  engine.
