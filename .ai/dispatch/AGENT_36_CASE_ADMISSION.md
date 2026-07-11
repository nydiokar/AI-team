# A36 — M2.5 Job 1: Case admission & Task/Session affiliation

**Level:** 3 (code, migration) · **Branch:** `feat/m2.5-case-admission` (new) · **Date:** 2026-07-11 · **Status:** `dispatched`
**Reads with:** [`.ai/workflow_architecture_audit.md`](../workflow_architecture_audit.md) §E Job 1 + §A,
`docs/Task_Harness_v0.7_AUTOMATION.md` §3 (M2.5), `src/orchestrator.py` (`_enqueue_task`/`_record_flow_run_start`),
`src/control/db.py` (`create_flow_run`/`create_flow_link`/`list_session_case_links`).

---

## Objective-lock (bounded)

Stop minting a **Case (`flow_run`) per turn**. Make a turn **attach to the session's open Case
if one exists**, otherwise run **Case-less** (Pattern A: standalone session, many Tasks, no
Case). A Case is *created* only by an explicit managed entrypoint (`open_case`, later used by
the Manager role), never unconditionally inside `_enqueue_task`. Give a Session a **durable
Case affiliation** (`current_case_id` + `role`) that survives across turns.

This is a **writer-policy correction over the existing, correct substrate** — `flow_links`
already supports N tasks + N sessions per Case (`db.py:2551`). No schema rebuild.

Flag discipline: all new behavior behind `HARNESS_FLOW_DRIVE` (already the live gate). **OFF ⇒
byte-identical.** With the flag ON, the new admission policy replaces the per-turn mint.

---

## Current verified baseline (audit-confirmed)

- `_enqueue_task` (`orchestrator.py:1643`) → `_record_flow_run_start` (`:1693`) →
  `db.create_flow_run` (`db.py:1463`) mints a **fresh `uuid4` flow_run for every task**. No
  lookup-or-reuse of an open Case exists.
- Same block writes a fresh `session → flow (role 'worker')` link per turn
  (`orchestrator.py:1749`). `create_flow_link` is idempotent on
  `(flow_run_id, entity_type, entity_id, role)` — but that never helps across turns because
  `flow_run_id` differs every turn.
- `Session` (`interfaces.py:179-218`) carries **no** `case_id`/`flow_run_id`/`role` field;
  affiliation is re-derived per read from `list_session_case_links`
  (`db.py:1637-1670`, resolves to the **most-recent** case only — a cosmetic mask).
- Only `'worker'` is ever written for a session; `manager`/`reviewer` never emitted.

## Scope

1. **`db.find_open_case_for_session(session_id) -> Optional[str]`** — newest `flow_run` linked
   to the session (via `flow_links` entity_type='session') whose `status` is NULL/open
   (i.e. not in `_CLOSED_STATUSES`). Read-only; indexed by the existing
   `idx_flow_links_entity`.
2. **`db.open_case(objective, session_id, role='manager', completion_criteria=None) -> str`** —
   explicit Case creation primitive: one `create_flow_run` + a `session` link in the given role +
   a `flow.created` event. This is the ONLY sanctioned Case-birth path going forward. The optional
   `completion_criteria` (MAX salvage — `PRIOR_ART_MAX_REUSE.md` Tier B; the checkable "done"
   condition that fights hallucinated-success) is persisted on the Case (new nullable
   `completion_criteria` column on `flow_runs`, or JSON alongside `objective_lock`) and is later
   demanded by `close_case` in **A37**. Absent ⇒ back-compatible with a criteria-less Case.
3. **`_record_flow_run_start` rewrite (flag-ON path only):**
   - if the task carries a managed-intent marker (from `open_case` / Manager dispatch) → create
     the Case (or use the supplied `flow_run_id`);
   - else if `find_open_case_for_session` returns an open Case → **attach**: emit a
     `task → case (role 'task')` link + `task.attached` event, stash that `flow_run_id` on the
     task, and **do NOT** create a new flow_run or a second `session→worker` link;
   - else (no managed intent, no open Case) → **create nothing** (Case-less standalone turn).
4. **Durable session affiliation:** add nullable `current_case_id` + `case_role` columns to the
   `sessions` table (additive migration) OR persist via a `session_case` link the store reads on
   load; set on attach/open, cleared on Case close (Job 2). Persist through `session_store`
   dual-write.
5. **OFF path untouched** — flag OFF keeps A19's `create_flow_run(task_id, "dispatch_start")`
   byte-identical.

## Non-goals

- Manager role prompt / control tools (M3.1 / audit Job 5).
- Closure & stage-honesty (auto-stamp removal) — that is **A37 (Job 2)**; A36 stops over-*creating*
  Cases, A37 stops over-*closing* them.
- UI grouping (A37 / Job 2). Review/approval (Jobs 6/7).

## Dependencies

None beyond shipped M1/M2. **This is the foundation** — A37 depends on A36; M3.1 depends on both.

## Affected components

`src/orchestrator.py` (`_record_flow_run_start`, admission), `src/control/db.py` (new readers +
`open_case` + migration), `src/core/interfaces.py` + `src/services/session_store.py` (durable
affiliation fields).

## Implementation intent

The per-turn write becomes **`task → case`** (genuinely per-turn) instead of re-minting
**`session → case`** each turn. The session's Case membership is written **once** (on first
attach/open) and read from a durable field, not re-derived-and-masked.

## Service boundary checklist (CLAUDE.md §7 — `_enqueue_task` is the shared choke point)

- **Concurrency:** admission runs inside the existing single-consumer enqueue path; the new
  `find_open_case_for_session` is one indexed SELECT per task — no new unbounded fan-out. Keep
  it best-effort/isolated like the current `_record_flow_run_start` (a failure must never fail
  the task).
- **Malformed input:** a missing/blank `session_id` ⇒ Case-less path (no crash). A stale
  `current_case_id` pointing at a deleted Case ⇒ treat as no open Case (re-derive), never raise.
- **Backing-resource failure:** DB unavailable ⇒ same swallow-and-log as today ⇒ task still runs.
- **Idempotency:** attaching the same task twice is absorbed by the `flow_links` unique key.

## Acceptance criteria

- 10 turns on a **standalone** session ⇒ **0** `flow_runs` created.
- 10 turns on a **Case-attached** session ⇒ **1** `flow_run`, **10** `task` links, **1**
  `session` link (not 10).
- `HARNESS_FLOW_DRIVE` OFF ⇒ byte-identical to A19 (import smoke + existing flow tests green).
- New unit tests: attach-on-open-case; no-new-flow-on-turn-2; standalone-creates-no-case;
  open_case creates exactly one; OFF-path no-op.

## Required evidence

- SQL dump of `flow_runs` + `flow_links` for both scenarios (standalone vs attached) across ≥3
  turns, showing the counts above.
- `pytest` output for the new tests + the existing `test_flow_runs.py` / substrate tests green.

---

## Milestone

_(burndown to be filled by the build agent — F-tags, per-step status)_

## Closure

_(to be filled at build close)_
