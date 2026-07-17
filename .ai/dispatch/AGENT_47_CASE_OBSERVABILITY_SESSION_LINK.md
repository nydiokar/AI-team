# AGENT_47 — Case observability: attach the worker session to the Case graph

**Dispatched:** 2026-07-17
**Level:** 3 (substrate/read-model; flag-safe; build on tests)
**Branch:** `feat/case-observable-worker-session` (code ⇒ PR at close, do NOT merge)

## Why (live-surfaced by the A45 run, Case `9f3d34c69c45…`)
A dispatched worker opens a REAL observable session (`case_role='worker'`, `current_case_id=<case>`)
— that half works (PR #23). **But the session→Case relationship is written ONLY to the `sessions`
columns, never to the `flow_links` graph** that the Work read-model / Case view is built from. Result:
opening the Case shows `1 manager session + N undifferentiated role='task' tasks` and **no worker
session, no manager↔worker relay.** You cannot see who did what.

Evidence (DB, Case `9f3d34…`): `flow_links` = `{session:df8b7e024864/manager}`,
`{task:task_c7d4274b/task}`, `{task:task_05bf908a/task}`. The worker session `717441320dcc`
(`case_role='worker'`, `current_case_id=9f3d34…`) is present in `sessions` but **absent from
`flow_links`**; and the manager's own-turn task and the worker's task are indistinguishable (both
`role='task'`).

## Intent (ground before building)
Read: the observable-session dispatch (`scripts/mcp_manager.py::_dispatch_worker`), the admission/join
path that stamps `case_role`/`current_case_id` (A36 branch J in `src/orchestrator.py` +
`src/control/db.py` `open_case`/`find_open_case_for_session`/`create_flow_link`), and the Work
read-model projection (`db.py` work read model / `/api/work/{case}` in `control_api.py`). Determine
the smallest honest fix — likely **write a `flow_links(entity_type='session', entity_id=<worker_sess>,
role='worker')` row when a worker session joins a Case** (mirror the manager-session link that already
exists), and differentiate the worker's task from the manager's own turn (role/marker) so the graph is
legible. Confirm whether the read-model could ALSO surface sessions via `sessions.current_case_id` as
a defensive backstop.

## Objective
Make a Case a coherent, self-explanatory observable unit: **open one Case → see the manager session,
each worker session, and each session's tasks/turns as a navigable graph** — manager relaying, worker
working, per turn.
1. On worker-session join, write the durable `flow_links(session, role='worker')` row (reuse
   `create_flow_link`; idempotent on the existing unique key). This also makes PR #22's session-link
   scan non-empty (see A48 — but per A48 do NOT auto-close; the link is for *observability*, not close).
2. Differentiate the manager's own-turn task from a worker's task in the graph (role or created_by), so
   the two tasks are no longer indistinguishable.
3. Read-model/Case view surfaces worker sessions + their turns under the Case (via the new link; add
   the `current_case_id` backstop only if cheap and honest).
4. Tests: a worker dispatched into a Case produces a `flow_links(session,worker)` row; the Case
   read-model returns the worker session + its task/turns distinct from the manager's; idempotent.

## Completion criteria (ONE string)
A worker session joining a Case writes a durable flow_links(entity_type='session', role='worker') row (idempotent) so the Case graph shows the worker session and its tasks distinct from the manager's own turns; the Work read-model / Case view surfaces manager session + worker session(s) + their turns as one navigable unit; plain-pytest tests cover the session-link write, the read-model surfacing, and idempotency and pass; one feat branch + PR opened (NOT merged).

## Live log
- **2026-07-17 — BUILT via live Manager loop → PR #25 (OPEN, not merged).** Case `62bb3a…`, worker
  rework cycle: first A47 attempt → **`review.rework_requested`** → fix → `review.accepted`.
  `src/orchestrator.py` (+25) writes the `flow_links(session, role='worker')` row on join; **also
  fixed Finding A** — `src/worker/agent.py` adds `role_boot` to the `_make_session_from_payload`
  allowlist (the node round-trip drop) with a dedicated `tests/test_session_payload_roundtrip.py`
  regression. 178+59 lines of tests. **NOTE: not deployed** — this run's own graph still shows workers
  only as `sessions` rows (fix lands for future cases after #25 merges + gateway restart).
