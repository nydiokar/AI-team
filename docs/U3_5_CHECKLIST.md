# U3.5 Checklist — Telegram⇄Web parity (+ U6 enforcement)

Execution doc for the parity pass of `docs/CONTROL_SURFACE_UNIFICATION.md`. Close the
~6 actions Telegram has that the web API lacks, by **extracting the logic off the
Telegram class onto transport-neutral services** and then exposing both Telegram and
web through them. This simultaneously does U6 (enforcement: no interface owns lifecycle
logic).

Principle: **move existing logic, don't redesign it.** Telegram behavior must stay
byte-identical (its handlers become thin callers). No paid CLI in tests.

Baseline: branch `feat/control-surface-unify` @ U3 (4db9001).

Parity gap (verified against interface.py handler registrations):
| Telegram cmd | logic today | target |
|---|---|---|
| /session_close | `backend.close()` + status=CLOSED + unbind, on Telegram class | `SessionService.close_session` |
| /session_restore | CLOSED→IDLE + bind, on Telegram class | `SessionService.restore_session` |
| /model | validate via config.models + set session.model | `SessionService.set_model` |
| /session_dirs, /session_status | inspect via `_inspect()` (inspect_ops/NodeInspector) | `/api/sessions/{id}/inspect` thin wrap |
| /commit, /commit_all, /git_status | `GitAutomationService` (already a service) | `/api/git/*` thin wrap |
| /jobs | watched-job listing | `/api/jobs` (read; via db/job state) |

---

### Tier 1 — lifecycle extraction (also U6). Highest value.

- [x] **P1 — `SessionService.close_session(session_id, *, host=None) -> CommandResult`**
  Faithful extraction of `_handle_session_close` core (interface.py:2753-2778, minus
  Telegram messaging/permission/unbind-by-chat): if `backend_session_id`, call
  `backend.close(session)` only when local (machine_id empty or == host); remote → skip
  (log); clear `backend_session_id`; status=CLOSED; save. Needs backend access — pass a
  `backend_resolver` callable or the orchestrator's `_backends` in. Returns the session.
  Telegram unbind-by-chat stays in Telegram (it's chat-specific), called after.
  - **Done =** method closes a local session (backend.close called, status CLOSED,
    backend_session_id cleared); remote session skips backend.close. Unit-tested with a
    fake backend.  **Revert =** remove method.

- [x] **P2 — `SessionService.restore_session(session_id) -> CommandResult`**
  Extraction of restore core (interface.py:2799-2804): reject if not CLOSED
  (`reason="not_closed"`), else status=IDLE + save. Bind-to-chat stays in Telegram.
  - **Done =** CLOSED→IDLE+saved; non-closed → `ok=false, reason=not_closed`.
  - **Revert =** remove method.

- [x] **P3 — `SessionService.set_model(session_id, model) -> CommandResult`**
  Extraction of `_handle_model_command` set-path (interface.py:2671-2692): resolve via
  `config.models.validate(backend, model)`; if `None` and not advisory →
  `reason="unknown_model"`; else set `session.model=resolved` (or None for default) +
  save. Picker UI / labels stay in Telegram.
  - **Done =** valid model pins; unknown non-advisory → reason=unknown_model; advisory
    passes through.  **Revert =** remove method.

- [x] **P4 — Telegram routes through P1–P3 (byte-identical behavior)**
  `_handle_session_close` → `close_session` then its own unbind+message;
  `_handle_session_restore` (+ callback) → `restore_session` then bind+message;
  `_handle_model_command` (+ callback) → `set_model` then label/picker. No behavior
  change.  **Done =** `tests/test_telegram_session_flow.py` (+ model/close tests) green.
  **Revert =** restore the handler bodies.

- [x] **P5 — Web endpoints for P1–P3**
  `POST /api/sessions/{id}/close` → close_session (host=gethostname, backends via
  orchestrator); `POST /api/sessions/{id}/restore` → restore_session;
  `POST /api/sessions/{id}/model` body `{model}` → set_model. Envelope + reason→4xx
  (`not_closed`→409, `unknown_model`→400). Idempotency not needed (idempotent already).
  - **Done =** endpoints round-trip; web session can close→restore→set model.
  - **Revert =** remove routes.

### Tier 2 — inspect / git / jobs (thin wraps over existing services).

- [ ] **P6 — `POST /api/sessions/{id}/inspect`** body `{op, path?, limit?}`
  Wrap the same inspect path Telegram uses (`inspect_ops` / NodeInspector, routed to the
  session's owning node). Ops: `list_dirs`, `git_status`, `session_dirs`. Read-only.
  - **Done =** list_dirs returns dirs for a local session; unknown op → 400.
  - **Revert =** remove route.

- [ ] **P7 — `GET /api/jobs`** — watched-job listing (read), same source as `/jobs`.
  - **Done =** returns a list (possibly empty) under auth.  **Revert =** remove route.

- [ ] **P8 — `POST /api/git/{commit|commit_all|status}`** over `GitAutomationService`
  (already a clean service). body carries `task_id` + flags as the CLI/Telegram paths do.
  GUARDED: commit creates a branch by default (mirror Telegram defaults). 
  - **Done =** status returns the summary dict; commit returns the service result shape.
  - **Revert =** remove routes.

### Close-out

- [ ] **P9 — Tests** `tests/test_session_service_lifecycle.py` (P1–P3 units) +
  extend `tests/test_control_api_write.py` (P5 endpoints, P6 inspect happy path with a
  fake inspector). No paid CLI.
  - **Done =** all green + existing telegram/control tests green.

- [ ] **P10 — Docs** update `CONTROL_CONTRACT.md` surface list + this checklist;
  refresh the parity table in chat. **Done =** contract lists all parity endpoints.

---

**Acceptance gate:** every Telegram action has a web equivalent OR is explicitly
documented as Telegram-only-by-design (e.g. chat-binding, message rendering). Lifecycle
logic (create/close/restore/model/bind) lives on `SessionService`, not on any interface
— grep proves no interface mutates `session.status`/`session.model`/`backend_session_id`
directly except through the service. That grep IS U6.
