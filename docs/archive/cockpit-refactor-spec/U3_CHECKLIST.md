# U3 Checklist — Write surface (the write half of Move F)

Execution doc for Step U3 of `docs/CONTROL_SURFACE_UNIFICATION.md`. The embedded
Control API (U1) gains write endpoints so the Web UI can *act* — making it a peer of
Telegram. **Each endpoint is a thin HTTP adapter over an EXISTING service/orchestrator
call. No new business logic. No WS (that's U4), no static serving (U5).**

How to use: each box has `Done =` and `Revert`. Edit-then-implement if a box is wrong.

Baseline: branch `feat/control-surface-unify` @ U2 (0da1b13).

Ground truth (verified):
- `orchestrator.submit_instruction(description, *, session_id, cwd, source, ...) -> task_id`
  — Telegram session send sets `last_user_message` + `status=BUSY`, calls with
  `source="telegram_session"`, then saves `last_task_id` (interface.py:323-332).
  Web mirrors this with `source="web_session"`.
- `session_service.create_session(*, backend, repo_path, chat_id=None, owner_user_id=None,
  node_id="__local__", model=None, origin=SessionOrigin, bind_chat=True) -> CommandResult`.
- `session_service.bind_active(chat_id, session_id) -> CommandResult`.
- `orchestrator.cancel_task(task_id) -> bool`.
- `orchestrator.compact_session(session_id) -> ExecutionResult`.
- `CommandResult(ok, reason, session)` — NO prose; transport maps `reason` to wording.

---

- [x] **U3.1 — Pydantic request models + a uniform response envelope**
  In `control_api.py`: request bodies (`InstructionBody`, `CreateSessionBody`,
  `BindBody`) and a helper that renders a `CommandResult`/outcome as JSON:
  `{ "ok": bool, "reason": str, "session": <SessionView.to_dict()|null>, ... }`.
  On reject map `reason` → HTTP 4xx (`unknown_backend`→400, `session_not_found`→404)
  but keep `ok/reason` in the body so the client maps wording (no prose in API).
  - **Done =** models import clean; helper turns a `CommandResult` into the envelope
    with the session rendered via `SessionView.from_session(...).to_dict()`.
  - **Revert =** remove the models + helper.

- [x] **U3.2 — Idempotency key (in-process)**
  Endpoints that create work (`POST /api/instructions`, `POST /api/sessions`) accept
  an `Idempotency-Key` header. Keep a bounded in-process dict {key → prior JSON
  response} on the app; a repeated key returns the stored response without re-acting.
  Bounded (e.g. last 512 keys, FIFO). In-process is sufficient — the gateway is one
  process (that's the whole point of U1); no DB table.
  - **Done =** same key + same endpoint twice → one side effect, identical response.
  - **Revert =** drop the dict + header read (endpoints still work, just not idempotent).

- [x] **U3.3 — `POST /api/instructions`** (send a message/instruction)
  Body: `{ description, session_id?, cwd?, target_files? }`. If `session_id` given,
  mirror the Telegram session path: load session (404 if missing), set
  `last_user_message` + `status=BUSY` + save, then
  `await submit_instruction(description, session_id=, cwd=session.repo_path or body.cwd,
  source="web_session")`, save `last_task_id`. No `session_id` → one-off
  (`source="web_oneoff"`). Return `{ ok, task_id, session? }`.
  - **Done =** returns a task_id; with a session_id the session goes BUSY and carries
    `last_task_id`; appears in `/api/tasks` (eventually) and the event stream.
  - **Revert =** remove the route.

- [x] **U3.4 — `POST /api/sessions`** (create) + **`POST /api/sessions/{id}/bind`**
  Create: body `{ backend, repo_path, model?, node_id? }` →
  `session_service.create_session(..., origin=SessionOrigin(channel="web", kind="user"),
  bind_chat=False)` (web has no telegram chat_id). Return the envelope; the new
  session reads back with `origin_channel="web"`. Bind: thin wrap of `bind_active`
  (web binding is a no-op without chat_id today, but keep the route for symmetry —
  document it accepts an optional `chat_id`).
  - **Done =** create returns a session with `origin_channel="web"`, visible in
    `/api/sessions`; unknown_backend → 400 with `reason`.
  - **Revert =** remove the routes.

- [x] **U3.5 — `POST /api/sessions/{id}/stop`** + **`POST /api/sessions/{id}/compact`**
  Stop: load session (404), `cancel_task(session.last_task_id)` → `{ ok, cancelled }`.
  Compact: `await compact_session(id)` → `{ ok, output?, errors? }` (ExecutionResult,
  not CommandResult — render its success/errors).
  - **Done =** stop returns whether a cancel signal was set; compact returns the
    backend result shape.
  - **Revert =** remove the routes.

- [x] **U3.6 — Tests** (`tests/test_control_api_write.py`)
  Stub orchestrator exposing the real `SessionService` + async `submit_instruction`/
  `compact_session` + `cancel_task` spies. Cover: auth required on every write;
  create tags `origin_channel="web"`; unknown_backend→400; instructions returns a
  task_id and flips the session BUSY; idempotency key dedupes; stop calls cancel_task
  with `last_task_id`. **No paid CLI** (spies only).
  - **Done =** `pytest tests/test_control_api_write.py` green; no network/CLI.
  - **Revert =** delete the file.

- [x] **U3.7 — Update `docs/CONTROL_CONTRACT.md`**
  Add the write endpoints to the surface section (they wrap §4a/§4b; `channel="web"`).
  - **Done =** contract lists the write routes + the no-prose `reason` rule.
  - **Revert =** revert the doc edit.

---

**U3 acceptance gate:** the Web UI can create a session (`origin_channel="web"`), send
an instruction (session goes BUSY, task flows through the same queue + event stream as
Telegram), and stop/compact it — all through the in-process Control API, calling the
same services Telegram calls. **Do NOT** add WS push (U4), static serving (U5), or any
business logic not already on a service (if missing, add it to the service so Telegram
could call it too).
