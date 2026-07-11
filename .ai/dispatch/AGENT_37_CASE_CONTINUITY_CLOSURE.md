# A37 — M2.5 Job 2: Case continuity, honest stages & closure semantics

**Level:** 3 (code) · **Branch:** `feat/m2.5-case-admission` (continues A36) · **Date:** 2026-07-11 · **Status:** `dispatched`
**Reads with:** [`.ai/workflow_architecture_audit.md`](../workflow_architecture_audit.md) §E Job 2 + §A,
`AGENT_36_CASE_ADMISSION.md`, `docs/Task_Harness_v0.7_AUTOMATION.md` §3 (M2.5) + §4 (F8),
`src/orchestrator.py` (`_flow_stage_transition`/`_flow_terminal_outcome`), `src/control/work_read_model.py`.

---

## Objective-lock (bounded)

Make **`Task finished != Case completed`** true. Stop the loop from auto-stamping
`impl_review`/`closure` and auto-closing the Case on a single task's success. A task's terminal
outcome updates **task** state only; **Case status changes only via an authoritative closer**
(operator now, Manager/reviewer at M3.1/M3.2). Retire the "most-recent-link" cosmetic mask so
the read model tells the truth about a Case's N tasks/sessions.

Depends on A36 (a Case now spans turns; this makes it *close* honestly).

---

## Current verified baseline (audit-confirmed)

- Loop auto-stamps `execution` (`orchestrator.py:2435`), **`impl_review`** (`:2501`), **`closure`**
  (`:2518`) on **every** task — `impl_review` though the code itself admits no reviewer exists
  (`:2015-2018`).
- `_flow_terminal_outcome` (`:2524`, single caller at task-end) sets `flow_runs.status='closed'`
  on the one task's success (`'blocked'` on failure). This is the **only** status write site
  (`db.update_flow_run(..., status=...)` at `:2035`).
- Read model resolves a session to its **most-recent** case (`db.py:1650-1653`) — masks the
  shatter rather than showing all links.
- **Honesty credit:** `plan`/`plan_review`/`review.*` are never emitted — do **not** add them
  here; they belong to M3.2 when a reviewer exists.

## Scope

1. **Remove auto stage-stamps** `impl_review` and `closure` from the worker loop (`:2501`,
   `:2518`). `execution` stays (it is a real transition). No stage is written for a phase that
   did not happen.
2. **Decouple task outcome from Case status:** `_flow_terminal_outcome` stops writing
   `flow_runs.status`. The task's mesh terminal state already lives in `mesh_tasks`/task records;
   emit a `task.finished` **flow_event** (audit trail) but leave `flow_runs.status` untouched.
3. **Authoritative closure primitive:** `db.close_case(flow_run_id, outcome, actor)` +
   `orchestrator` seam — sets `status` (`closed`/`blocked`) and emits `flow.closed` /
   `flow.status_changed` **only** when called by an authoritative actor. Guard: a Case cannot
   close while it has unresolved required approval / user-input / open child work (structural
   check now; review-gate arrives M3.2).
3b. **Completion-criteria closure contract (MAX salvage — see `PRIOR_ART_MAX_REUSE.md` Tier B).**
   A close is **not** just "an authoritative actor said so" — that is still a rubber-stamp and
   loses to the #1 scar (hallucinated success). `open_case` may carry an optional machine-/
   human-checkable `completion_criteria` (persisted on the Case, see A36). `close_case` must
   **reconcile** it: either the closer records each criterion **met**, or explicitly **waives**
   it with an actor + reason (mirrors the existing `waived_findings` field). A Case with unmet,
   unwaived criteria **cannot reach `closed`**. This job only stores + demands the reconciliation
   at close time; **M3.2 automates a reviewer *verifying* the criteria** (do not build the
   verifier here). Absent criteria ⇒ behaves as item 3 alone (back-compatible).
4. **Pause/resume reuse the same `flow_run_id`** — no replacement Case on resume.
5. **Read model truth (retire the mask):** `work_read_model` groups a Case's N `task` links and
   shows **all** session affiliations for a session (not latest-only); the Web Work inbox lists
   **Cases (objectives)**, and standalone (Case-less) turns do not appear as Cases. Update
   `db.list_session_case_links` docstring + the A30 "most-recent" resolver accordingly.

## Non-goals

- Creating the reviewer role or emitting `review.*` (M3.2 / Job 6).
- The durable relay / Manager-resume (M3.3 / Job 4).
- Manager control tools (M3.1 / Job 5). This job only makes closure *honest and authoritative-only*.

## Dependencies

**A36** (Case admission) — a Case must span turns before "close once, honestly" is meaningful.

## Affected components

`src/orchestrator.py` (remove auto-stamps, `_flow_terminal_outcome` → task-only + `close_case`
seam), `src/control/db.py` (`close_case`, docstring fix), `src/control/work_read_model.py`
(grouping + all-affiliations), `web/` Work surface (list Cases, group tasks/sessions).

## Implementation intent

Closure becomes an **event**, not a **side effect**. The timeline reflects only transitions that
actually occurred; the Work UI shows one Case = one objective with its real tasks/sessions.

## Service boundary checklist (CLAUDE.md §7)

- **Closure authority:** `close_case` must reject a close that leaves required work unresolved
  (structured error, not a silent close). Document the check.
- **Malformed input:** closing an already-closed / unknown Case ⇒ idempotent no-op / structured
  error, never a crash.
- **Backing-resource failure:** status/event writes stay best-effort/isolated from task execution.

## Acceptance criteria

- A completed worker task leaves its Case **`open`** (not `closed`); `flow_runs.status` unchanged
  by task-end.
- A Case timeline shows **no** `impl_review`/`closure` stage unless a real closer emitted it.
- A Case opened with `completion_criteria` **cannot** reach `closed` until each criterion is
  recorded met or explicitly waived-with-reason; the reconciliation is visible in `flow_events`.
- Web Work inbox lists Cases (objectives); a standalone session's turns produce **no** Case row;
  one Case row groups its N tasks + its sessions.
- `close_case` refuses to close a Case with an unresolved required approval.
- `HARNESS_FLOW_DRIVE` OFF ⇒ byte-identical.

## Required evidence

- Timeline JSON of a real completed turn showing honest stages (no fabricated review/closure).
- SQL: a worker task completes → its Case row still `status IS NULL`/open.
- Screenshot/JSON of the Work inbox showing one Case grouping multiple tasks + a standalone
  session absent from the Case list.
- `pytest` + `vitest` green.

---

## Milestone

- **F1 — honest stages.** ✅ Removed the loop's auto `impl_review` + `closure` stage stamps
  (`orchestrator.py`); `execution` stays (a real transition). No stage is written for a phase
  that did not happen.
- **F2 — task outcome decoupled from Case status.** `_flow_terminal_outcome` rewritten task-only:
  emits one append-only `task.finished` event (outcome success/failed) onto the task's owning
  Case, resolving the Case id from `_FLOW_RUN_META_KEY` (birth) **or** `_CASE_ID_META_KEY`
  (attached turn) so both first and Nth turn leave an audit trail. **It no longer writes
  `flow_runs.status`.** A completed/failed task leaves its Case OPEN.
- **F3 — authoritative `db.close_case`** (the ONLY status→terminal write path) + `CaseCloseBlocked`
  structured refusal. Guards: open child flow, unresolved (pending) approval linked to the Case
  (single indexed JOIN — no N+1), and **completion_criteria reconciliation** (each criterion
  recorded `met` or `waived`-with-reason via `_parse_completion_criteria`/`_criterion_resolved`/
  `_unreconciled_criteria`). Idempotent (already-closed → False). Orchestrator `close_case` seam
  returns `{ok,closed,reason}` and clears the durable session affiliation of every linked session
  on a real close (A36 item 4; only if the session still points at this Case).
- **F4 — pause/resume reuse.** No new code needed — A36 admission already guarantees it: a paused
  Case (status NULL/`blocked`, both OPEN) is re-found by `find_open_case_for_session`, so a resume
  turn re-attaches to the SAME `flow_run_id` (test `test_resume_reuses_same_case`).
- **F5 — read-model truth.** Item-5 acceptance is met by A36 + the existing read model: standalone
  turns create no `flow_run` ⇒ absent from `build_work_list` (no fake Case rows); `build_case_ledger`
  groups a Case's N `task`/`session` links. Retired the "most-recent-link" **mask** narrative in
  `db.list_session_case_links` + `work_read_model.build_session_affiliations` docstrings (the shatter
  is fixed at the source; `sessions.current_case_id` is now the authoritative current-Case pointer).
- **F6 — cross-seam fix (`scripts/mcp_manager.py`, the live A33/A35 path).** `wait_for_worker` polled
  `flow_runs.status` to detect worker completion; A37 stops task-end from writing status, which would
  hang the poll until timeout. Added `_terminal_task_event`: when status is non-terminal, poll
  `/api/work/{id}/timeline` for the authoritative `task.finished` event and return DONE/ATTENTION by
  its outcome (Case stays OPEN — the reply tells the Manager to close via `close_case`). Preserves the
  opt-in, operator-tested manager path under honest closure. 2 new tests.
- **F7 — tests.** New `tests/test_case_closure.py` (19: criteria helpers, close success/idempotent/
  cancel/invalid/unknown, guards for open-child/pending-approval/unmet-criteria/waive-without-reason,
  orch seam ok/blocked/unknown/moved-on, resume-reuse). Updated the 2 A29 terminal tests to the new
  honest behavior (task.finished + status untouched). **Full suite: 905 passed** (2 pre-existing env
  fails unrelated).
- **Adversarial review (`/code-review --fix`):** 2 findings, both fixed — (1) the `wait_for_worker`
  cross-seam regression above; (2) N+1 in the approval close-guard → single indexed JOIN (CLAUDE.md §8).

## Closure

**Status: built** on `feat/m2.5-case-admission` (continues A36). Acceptance met — evidence: a worker
task completes → its Case row stays `status IS NULL` (open); events are `flow.created` + `task.attached`
+ `task.finished` with **no** fabricated `impl_review`/`closure`/`flow.closed`; `close_case` refuses a
Case with unmet `completion_criteria` (`{ok:false, reason:"…not reconciled…"}`) and closes it once each
criterion is met/waived. Flag OFF ⇒ byte-identical. **M2.5 (A36+A37) complete — unblocks M3.1.**
Deferred to their owning stages (unchanged): reviewer role + `review.*` (M3.2), durable relay/Manager-
resume (M3.3), Manager control tools (M3.1), Web Work-inbox visual polish (frontend, no backend gap).
