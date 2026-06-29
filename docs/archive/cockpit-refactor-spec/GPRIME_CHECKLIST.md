# Move G′ — Task lifecycle object + sectioned /api/tasks

Scope fence per COCKPIT_REFACTOR_SPEC §13: **only the boxes below are in scope.**
G′ is additive — no DB migration (parentage already exists: `mesh_tasks.session_id`
+ `parent_task_id`, db.py:104). "Deferred" never means "do a little of it."

Spec row (§14): *"`TaskStatus` is 4 states; UI needs the supervised lifecycle.
Extend states + session parentage; reuse existing `tasks` table. /api/tasks returns
sectioned (attention/running/queued/recent) data."*

Baseline: branch feat/webui-ui0 @ 5590cb5 (UI-2). Backend deps in system python.

---

## Box 1 — canonical task lifecycle mapping (backend)
- [ ] New `src/core/task_lifecycle.py`: pure `derive_task_state(mesh_status, session_status) -> str`
  returning the canonical UI state, AND `section_for_state(state) -> str` returning
  one of `attention|running|queued|recent`.
- [ ] States returned match the frontend `TaskState` union (domain/status.ts) EXACTLY:
  queued|dispatching|running|waiting_for_input|waiting_for_approval|succeeded|failed|cancelled|connection_unknown.
- [ ] The session-status overlay is the value-add: when a task's owning session is
  `awaiting_input`, its in-flight task derives `waiting_for_input` (the empty bucket
  the flat mesh status can't reach). Approval state stays gated on Move H (no live
  source yet) — name it, never fabricate it.

Done = exactly: a pure module with the two functions + unit tests; no I/O.
Do NOT touch: the mesh status writer, the dispatch path, TaskStatus enum values.
Revert: delete the file.

## Box 2 — sectioned /api/tasks (backend)
- [ ] `GET /api/tasks?sectioned=true` returns
  `{sections: {attention:[...], running:[...], queued:[...], recent:[...]}}`.
  Each task row gains a derived `ui_state` + `section` field; raw columns untouched.
- [ ] Default (no `sectioned`) stays byte-identical to today (`{tasks:[...]}`) —
  the flat shape UI-2 already consumes. Backward compatible.
- [ ] Session-status overlay: join each task's `session_id` to its SessionView status
  (via session_service.store) to compute the overlay in Box 1. One read, bounded by limit.
- [ ] `recent` = terminal states (succeeded/failed/cancelled), newest first, capped.

Done = exactly: the new query param + shape; flat default unchanged.
Do NOT touch: auth, other endpoints, list_tasks signature.
Revert: drop the `sectioned` branch.

## Box 3 — frontend consumes sections
- [ ] `rawApi.ts`: `RawTaskSections` shape; `apiClient.tasksSectioned()`.
- [ ] `taskAdapter.ts`: `toTaskSections(raw)` → canonical, REUSING `deriveTaskState`
  for the flat path; sectioned path trusts the backend `ui_state`.
- [ ] `useLiveData.useTaskSections()` hook (poll).
- [ ] `TasksScreen` binds sections from the backend; client-side bucket arrays removed.
- [ ] tsc clean; existing 16 vitest pass + new section adapter test; vite build green.

Done = exactly: Tasks screen sectioned from backend truth.
Do NOT touch: SessionDetail, Composer, event stream.
Revert: TasksScreen re-buckets client-side from flat useTasks.

## Gate
- [x] Backend pytest for task_lifecycle + sectioned endpoint green.
      (11 task_lifecycle + 42 control/write; full suite no regression.)
- [x] Live: submit a one-off, GET /api/tasks?sectioned=true shows it in `recent` on completion.
      (task_04d3d6c3 → recent, ui_state=succeeded; flat default unchanged; 11 attention / 39 recent live.)
- [x] Frontend tsc + vitest (18) + vite build green.

ALL BOXES DONE 2026-06-24.
