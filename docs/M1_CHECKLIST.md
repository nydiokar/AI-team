# Milestone M1 — Execution Checklist

Working doc for **building** M1. Rationale, blueprints, and the OpenClaw review live in
`docs/COCKPIT_REFACTOR_SPEC.md` (§5 Move B, §3 Move A, §6 Move D). This file is the
**ordered, tickable task list** — follow it top to bottom and tick each box when its
acceptance line passes.

---

## How to use this doc (read once, then obey)

1. **Do the boxes in order. Tick `- [x]` only when that box's acceptance passes.**
2. **The checklist is the scope. If a change is not a box here, it is out of scope.**
   Deferred ≠ "do a little of it." Don't start the Web UI, scoping modes, new tables,
   caching, or neighboring refactors — those are M2+ (see bottom).
3. **If a box is wrong or a detail is missing:** do **not** improvise around it. Edit this
   doc — fix the box or add a sub-box — *then* implement. The doc converges; the code
   doesn't drift. A doc edit is visible in the diff; silent improvisation is not.
4. **If you hit a real fork the doc can't answer** (e.g. node pinning behaves differently
   than the spec claims): stop, write what you found under that step's `Notes`, and surface
   it. A surprised assumption is a signal, not a green light to redesign.
5. Each step is its own commit and is independently revertible (the `Revert` line).

**One-line invariant:** *M1 is an extraction + a doc, not a feature. Telegram behavior must
stay byte-identical. If you're adding capability users can see, you've left M1.*
(Originally said "No DB migration"; Step 2 added one **additive, defaulted** column —
migration 12 for `origin` — after the no-migration premise proved false. See Step 2 FORK.
No user-visible behavior change; old rows backfill to the prior default.)

---

## Step 0 — Baseline (no production code)

- [x] Branch `feat/session-service-m1` off `main`.
- [x] Confirm cost guard active (`tests/conftest.py`, `test_cost_guard`) — no step may
      invoke the paid CLI.
- [x] Record green baseline: `pytest tests/test_telegram_session_flow.py -q` passes now.
      Save the run; Step 4 must match it.

**Done = exactly:** baseline recorded green. **Not:** any source edit yet.
**Notes:**
- Cost guard: `tests/conftest.py` forces `AI_TEAM_TEST_MODE=1` + `MESH_ENABLED=false` at
  import time, and `src/core/test_guard.py` exists and blocks paid backend spawns. There is
  **no test literally named `test_cost_guard`** — the guard is the conftest mechanism +
  `test_guard.py` module. Box checked on that basis (the doc's parenthetical name is loose).
- **Baseline is NOT fully green.** `pytest tests/test_telegram_session_flow.py -q` →
  **21 passed, 1 failed**. The one failure is pre-existing on `main` (no source edited yet):
  `test_node_live_state_helpers_format_db_rows` — asserts `_node_load_text(row) ==
  "slots 2/4, active 2"` but the helper returns `"slots 2/4, active 2 (stale)"` because the
  test's `row` fixture carries no heartbeat/`live_state_ts`, so the staleness check fires.
  This is a **time-independent, deterministic** failure in the test fixture, unrelated to M1.
- **Baseline of record for the Step 4 gate:** `21 passed, 1 failed
  (test_node_live_state_helpers_format_db_rows)`. Step 4 must reproduce this exact set — the
  four named create/node-pin/close tests must stay **passing** and no *new* failure may appear.

---

## Step 1 — Backend registry (consolidation, zero behavior change)

- [x] Create `src/backends/registry.py` per spec §3 A.1: name→factory map +
      `build_backends()`, `valid_backend_names()`, `is_valid_backend()`, `DEFAULT_BACKEND`.
      **No icon/label fields** (display is a surface concern).
- [x] `orchestrator.py:59` → `self._backends = build_backends()`.
- [x] `worker/agent.py:385` `_make_backends()` → `return build_backends()`.
- [x] `telegram/interface.py:2252` and `:2380` → `_valid_backends = valid_backend_names()`.
- [x] New `tests/test_backend_registry.py`: keys == `valid_backend_names()`; every value is
      a `CodingBackend`; `DEFAULT_BACKEND in valid_backend_names()`.
- [x] `pytest tests/ -k "backend or components" -q` green.

**Done = exactly:** backend set declared in one file; tests green.
**Do NOT touch:** the `interface.py:800/829/852/874` icon branches (presentation — leave
verbatim); the `CodingBackend` ABC; any adapter file.
**Revert:** restore the two dict literals. Independent of later steps.
**Notes:**
- Real paths differ from the checklist's: orchestrator is `src/orchestrator.py` (not
  `src/core/orchestrator.py`); the literal was at `orchestrator.py:59`, `_make_backends` at
  `worker/agent.py:385`, the two valid-name tuples at `interface.py:2252/2380` — all matched.
- Removed the now-unused `from src.backends import ...` class import in `orchestrator.py:30`
  (replaced with `from src.backends.registry import build_backends`); the four classes had no
  other reference there. `worker/agent.py` keeps its local import, now of `build_backends`.
- Added `from src.backends.registry import valid_backend_names` to `interface.py`; both
  tuples → `valid_backend_names()`. Order verified **identical** (`claude, codex, opencode,
  opencode-server`), so the `backend not in _valid_backends` checks are unchanged.
- Icon branches (`interface.py:800/829/852/874`), the `CodingBackend` ABC, and all adapter
  files were **not touched**.
- `pytest tests/ -k "backend or components" -q` → **74 passed, 0 failed**. (The pre-existing
  `test_node_live_state_helpers_format_db_rows` failure is not matched by this `-k` filter.)

---

## Step 2 — `SessionOrigin` field (additive, back-compatible)

- [x] Add `SessionOrigin` dataclass to `src/core/interfaces.py` (spec §B.0):
      `channel="telegram"`, `kind="user"`.
- [x] Add `origin: Optional[SessionOrigin] = None` to `Session`; in `__post_init__`,
      default `None` → `SessionOrigin()`.
- [x] `session_store.py` `_to_dict`: write `"origin": {"channel":..., "kind":...}`.
      `_from_dict`: read it, defaulting to `SessionOrigin()` when the key is absent.
- [x] ~~Verify `db.py:upsert_session` tolerates the extra key~~ → **premise was wrong; added
      DB column via migration 12 instead.** See FORK below.
- [x] Test: session with no origin round-trips to `("telegram","user")`; a `("web","user")`
      origin survives save→load on **both JSON and DB mirror**
      (`tests/test_session_origin.py`).
- [x] Load a pre-M1 session dict with no `origin` key — confirm it still loads (back-compat).

**Done = exactly:** field persists both ways; old files still load; tests green.
**Do NOT:** add scoping modes, a key-string format, or any routing logic — `origin` is a
descriptive tag only.
**Revert:** drop the field + serialization lines + migration 12; old files/rows unaffected.
**Notes / FORK (operator-approved deviation from spec):**
- **The "No DB schema migration / No new DB column" fence was based on a false premise.**
  The checklist assumed `upsert_session` stores the Session as an opaque JSON blob "like
  `task_history`." It does **not** — the `sessions` table has explicit named columns
  (`db.py` CREATE TABLE + a hand-mapped INSERT); `model` itself needed migration 11.
- **Why JSON-only could not work:** `SessionStore.get()`/`list_all()` read **DB-first** (the
  mesh DB is canonical; `session_store.py:59`). With no `origin` column, a `("web","user")`
  session would always reload as `("telegram","user")` whenever a DB row exists — i.e. in
  all normal operation. The field would be silently inert. That fails this step's own
  "survives on the DB mirror" test.
- **Resolution (approved by operator 2026-06-21 — "if there's a reasonable long-term-good
  solution, do it properly"):** added **migration 12**
  `ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT
  '{"channel":"telegram","kind":"user"}'`, bumped `_CURRENT_VERSION` to 12, following the
  exact precedent of `model` (migration 11): the column is added by ALTER only, **not** in
  CREATE TABLE, so fresh and existing DBs converge through the one migration path. Wired
  `upsert_session` (INSERT + ON CONFLICT + `_origin_json` helper, duck-typed, no core import)
  and the read side flows the JSON string through `_from_dict._parse_origin`.
- Additive + defaulted → **old rows backfill to telegram/user; zero breakage.** Verified the
  migration applies cleanly to a copy of the live `state/mesh.db` (origin column present,
  schema_version=12).
- **Test result:** `tests/test_session_origin.py` 4/4 green (incl. DB-mirror round-trip +
  pre-M1 load). `test_components.py` + `test_queue_persistence.py` + `test_telegram_session_flow.py`
  = 24 passed, 1 failed — the failure is the **pre-existing** `test_node_live_state_helpers_format_db_rows`
  (Step 0 baseline), unchanged.

---

## Step 3 — `SessionService` (the seam) — create + bind only

- [x] Create `src/core/session_service.py` with `CommandResult` and `SessionService`
      (`__init__`, `create_session`, `bind_active`) per spec §B.1.
- [x] **Omit** the `*_view` methods and the `SessionView` import (needs deferred Move C).
- [x] `create_session` preserves all of: backend validation → `store.create` → set `model`
      → set `machine_id` from `node_id` → set `origin` → single `save` → optional `bind`.
- [x] `orchestrator.__init__`: `self.session_service = SessionService(self.session_store)`
      — reuse the existing store; do **not** construct a second `SessionStore`.
- [x] New `tests/test_session_service.py` (no network/CLI):
  - [x] `create_session(backend="claude", repo_path=tmp)` → `ok`, persisted, bound.
  - [x] `create_session(node_id="LP-1")` → `session.machine_id == "LP-1"`.
  - [x] `create_session(model="opus")` → `session.model == "opus"`.
  - [x] `create_session(origin=SessionOrigin("web","user"))` → origin persisted.
  - [x] `create_session(backend="nope")` → `not ok`, `reason=="unknown_backend"`, nothing saved.
  - [x] `bind_active("unknown")` → `reason=="session_not_found"`.
- [x] `pytest tests/test_session_service.py -q` green.

**Done = exactly:** service exists and is unit-tested independently of Telegram.
**Do NOT:** add `switch_backend` (no such flow exists in the codebase), `delete_session`,
`list_views`, dispatch logic, or notification logic. Lifecycle create+bind only.
**Do NOT:** put user-facing prose in `CommandResult` — `reason` is a machine code.
**Revert:** delete the file + the one orchestrator line. Nothing references it yet.
**Notes:**
- `session_service.py` imports `SessionService` directly into `orchestrator.py` (added
  `from src.core.session_service import SessionService`); not re-exported from `src.core`
  to keep the change minimal. Service constructed right after `self.session_store`,
  reusing that instance (no second store).
- `create_session` reproduces the original's set-then-single-`save` semantics: origin set
  unconditionally, model/node_id set conditionally, then exactly one `save`, then optional
  `bind`. `node_id == "__local__"` is treated as "no pin" and does **not** overwrite the
  hostname `store.create` assigns.
- `*_view` methods + `SessionView` import omitted (Move C deferred), as required.
- Added one test beyond the listed set: `__local__` must not clobber the default
  `machine_id` — guards the F1-class bug the spec calls out. **9 passed.**

---

## Step 4 — Point Telegram at the service (the ONLY behavior-touching edit)

- [x] Rewrite the **body** of `TelegramInterface._create_and_bind_session`
      (`interface.py:1064`) to delegate to
      `self.orchestrator.session_service.create_session(backend=..., repo_path=...,
      chat_id=..., owner_user_id=..., node_id=node_id, model=model)` and
      `return result.session`.
- [x] Keep the method **signature identical** → callers (now `:2094/:2290/:2482` after the
      Step 1 import shift) unchanged.
- [x] **Critical gate:** `pytest tests/test_telegram_session_flow.py -q` matches the Step 0
      baseline, incl. `test_session_new_creates_session_and_guides_next_step`,
      `test_session_new_repo_callback_creates_session`,
      `test_session_new_remote_command_uses_db_node_repos` (node pinning), and the
      session-close test. **Any output diff = stop, fix, do not merge.**

**Done = exactly:** create flow routes through the service; Telegram output byte-identical.
**Do NOT touch:** the node/repo picker, the model picker, `submit_instruction` dispatch, or
any other handler. One method body changes; nothing else.
**Revert:** restore the original 4-statement body. Service then sits unused (still valid for
a future surface) — partial revert is safe.
**Notes:**
- Method body now delegates to `self.orchestrator.session_service.create_session(...)` and
  returns `result.session`. Signature unchanged; all 3 production call sites untouched.
- **Test harness change (necessary, not behavioral):** `_DummyOrchestrator` in
  `test_telegram_session_flow.py` previously had no `session_service`. Since the wrapper now
  reaches `self.orchestrator.session_service`, the dummy must mirror the real orchestrator:
  it now constructs `SessionService(SessionStore())`. The store honors the test-isolated
  `_SESSIONS_DIR`/`_BINDINGS_FILE` the `isolated_session_store` fixture monkeypatches, so the
  test reads remain consistent. **No behavioral assertion in any test changed.**
- **Gate result = exact Step 0 baseline:** `tests/test_telegram_session_flow.py` →
  **21 passed, 1 failed** — the one failure is the pre-existing
  `test_node_live_state_helpers_format_db_rows`. All four named gate tests **pass**.
- Full suite: 260 passed, 15 skipped, **7 failed** — all 7 pre-existing on `main`
  (6 are a `python-multipart`/FastAPI form-import `RuntimeError` in endpoint/heartbeat/mesh
  tests, verified by re-running them on `main`; the 7th is the known live-state staleness
  test). **Zero new failures introduced by M1.**

---

## Step 5 — `docs/CONTROL_CONTRACT.md` (the durable artifact)

Write the doc per spec §6, grounded in what M1 actually shipped:

- [ ] **Event envelope** — canonical fields from `observability.emit_event`; "unknown
      fields are opaque." Note the OpenClaw parallel and that the **DB read model is the
      gap-recovery path** (their "events not replayed" → our `db.list_*`).
- [ ] **Event catalog** — grep `emit_event(`/`_emit_event(` in `orchestrator.py`,
      `notification_service.py`, `task_server.py`; one line per event; mark Telegram-consumed.
- [ ] **Inbound command surface** — the two entry points:
      `SessionService.{create_session,bind_active}` (lifecycle) and
      `orchestrator.submit_instruction(...)` (dispatch). Rule: *no transport writes session
      state directly; no transport puts prose in `CommandResult`.*
- [ ] **`SessionOrigin`** — `channel`/`kind`, defaults, descriptive-not-routing (contrast
      OpenClaw scoping, explicitly not adopted).
- [ ] **Backend extension** — "add a backend = one edit in `src/backends/registry.py`."
- [ ] **Read model pointer** — `db.list_sessions/list_tasks/list_nodes`; `SessionView`
      marked *planned, built with the Web UI*.
- [ ] **Reserved workflow events** — list `review.requested/completed`, `handoff.created`,
      `approval.requested/granted` as **reserved, not emitted yet**. No code.

**Done = exactly:** a reader can answer "how do I add a surface / a backend / a workflow
event?" from this doc alone.
**Do NOT:** implement any reserved event, WS endpoint, or workflow command here. Doc only.
**Notes:**

---

## M1 Definition of Done (tick when all steps closed)

- [ ] Backends declared in one place; adding one = one edit.
- [ ] `Session` carries `origin`; old files/rows load; one additive defaulted column
      (migration 12) — no destructive/behavior-changing migration. (See Step 2 FORK.)
- [ ] `SessionService.create_session/bind_active` exist, tested, reuse the orchestrator store.
- [ ] Telegram create flow routes through the service; `test_telegram_session_flow.py`
      unchanged vs. baseline.
- [ ] `CONTROL_CONTRACT.md` covers events, the two inbound entry points, `SessionOrigin`,
      backend extension, read model, reserved workflow events.
- [ ] **Exit check:** a Web UI author could create a session via
      `SessionService.create_session(origin=SessionOrigin("web"))`, dispatch via
      `submit_instruction`, and render `events.ndjson` + `db.list_*` — **with no further
      core refactor.** (Verify by reasoning, not by building the UI.)

---

## OUT OF M1 — do not start these here (they have their own milestone)

- **M2:** `SessionView` DTO (Move C) + read methods + first read-only Web dashboard.
- **M3:** WS/HTTP transport with handshake/auth — borrow OpenClaw's `req/res/event` framing then.
- **M4:** workflow commands that emit the reserved events.
- **Never (until pain proves otherwise):** new Task/Run/Review tables, layer re-org,
  caching tiers, session scoping modes, plugin SDK, ACP/A2A bridges.

If working on M1 surfaces a strong reason to pull one of these forward, that is a
**proposal to the operator**, recorded under the relevant step's Notes — not a license to
build it.
