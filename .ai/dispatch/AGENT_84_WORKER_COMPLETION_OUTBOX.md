```yaml
job_id: AGENT_84_WORKER_COMPLETION_OUTBOX
created_at: "2026-09-24T18:00:20+00:00"
status: active
owner: A84 worker - slice 1 managed-completion consumer
depends_on: [AGENT_82_SESSION_TURN_QUEUE]
results_ref: DISPATCH_LOG.md#A84
evidence: []
updated_at: "2026-10-02T12:57:39.722551+00:00"
```

# DISPATCH — A84 · Worker completion delivery through a durable Case outbox

**Level:** 3 (transactional DB lifecycle, continuation control path, Manager MCP surface) · **Type:** code
**Authored:** 2026-09-24 · **Status:** ready; implementation begins only after A82's reviewed queue contract is available.
**Depends on:** A82. **Reviewed by:** A87 before merge or rollout.
**Branch:** `feat/worker-completion-outbox` + PR; no deploy, flag activation, or worker restart.

> A worker completion must reach its Case Manager even when the Manager is busy, restarted, or had not separately armed a wait. Today completion is reconstructed from mutable Manager-authored wait groups, which produced both silent completions and indefinite waits. The desired outcome is one durable, Case-scoped completion signal per terminal child task, delivered through the existing coalesced continuation transport.

## TASK

1. Read `docs/TBD/WORKER_COMPLETION_NOTIFY_REDESIGN.md` in full, A82's final reviewed queue behavior, every `arm_wait_group` caller, all terminal task writers, and the boot/recovery paths. Produce a concise execution-path inventory in this packet before schema work. Treat the design as a hypothesis: correct it narrowly where current code or tests prove it wrong.
2. Add a migration and transactional DB API which records a child terminal outcome and its Case-scoped completion-outbox row atomically. The invariant is non-negotiable: a managed Case child recorded terminal has exactly one matching outbox row, or neither write commits. Existing legacy/control tasks remain unaffected.
3. Bind a Case worker task to its Case at dispatch using the existing durable task fields only where their semantics are verified. Preserve warm-worker reuse: Case, not worker session, is the scope. Do not blindly populate `parent_task_id` if the current task graph gives it incompatible meaning.
4. Replace wait-group satisfaction/reconciliation only for newly enrolled Cases behind a dedicated default-OFF migration flag. Keep the current continuation transport's coalescing, atomic claim, late Manager binding, round-cap, and respawn protections. Remove the Manager's ability to author the obsolete wait primitive only after the cutover supports its live callers.
5. Implement a case-boundary cutover and boot reconciliation for in-flight Cases. No Case may be stranded between legacy wait groups and the outbox. State and test the exact ownership predicate for an old versus new Case; never infer it solely from the currently enabled flag.
6. Add a bounded liveness backstop for managed worker tasks that lose a terminal report. It must use the same terminal/outbox API, be idempotent and fenced against a late real result. Before implementation, document the chosen timeout, cadence, node truth source, and a 100-concurrent-case memory/concurrency bound. Do not put an unbounded scan on the event loop.
7. Update manager prompts/tool schemas and read models only after proving no active caller needs ALL/quorum semantics that cannot be represented safely. If such a caller exists, preserve its behavior or escalate with evidence; do not silently turn a barrier into wake-per-child.

## ACCEPTANCE

1. A real file-backed SQLite test proves terminal state and outbox insertion are one transaction, including rollback/failure and duplicate terminal reports.
2. E2E fake-carrier tests cover: child finishes before a Manager can arm anything; Manager BUSY then idle; Manager restart/respawn; crash between wake claim and finalization; duplicate/late terminal result; multiple completions coalescing to one Manager wake; out-of-band review suppression; and lost/timeout synthesis followed by a late result.
3. Migration tests seed legacy open Cases with wait groups and new outbox Cases, then prove each drains exactly once without cross-path duplicate wake or stranding.
4. A terminal-writer inventory is attached to the PR and every writer is either routed through the transactional seam or shown to be excluded legacy/control behavior. No `task.finished` path can bypass the invariant for an enrolled Case child.
5. Service-boundary review records concurrency, request-size, timeout, malformed-result and backing-DB behavior for each changed HTTP/IPC boundary. The reaper has a measured/bounded query shape; no timer scans full event logs.
6. Targeted DB, continuation, task-server, worker-result, Manager-tool and A82 queue regression suites pass. Flag OFF is behaviorally compatible for unenrolled and legacy Cases.

## RESERVED DECISIONS

- **R1 — Barrier semantics.** A87 decides from a repository-wide caller sweep whether any supported caller needs a true ALL/NAMED barrier. The default is not to delete a working semantic merely because the proposed outbox does not model it.
- **R2 — Reaper policy.** Choose values only from existing leases/timeouts or a measured new bound; otherwise ship the completion outbox without claiming the liveness backstop complete and leave A84 open.
- **R3 — Cutover marker.** Persist an immutable per-Case mode, rather than keying old/new behavior to a mutable runtime flag. Exact representation follows the schema inspection.

## SCOPE OUT

No durable-workflow-engine adoption, generic peer inbox, worker redeploy, paid/live load test, or automatic activation. Do not alter A82's queue protocol except where an evidence-backed integration conflict requires a jointly reviewed correction.

## TRAIL / EVIDENCE

- Execution-path and terminal-writer inventory; migration SQL; tests; PR; A87 review verdict; explicit deployment/flag follow-up.

---
## Milestone (burndown)

- [ ] Current paths, barrier callers, and terminal writers mapped; A87 design checkpoint passed
- [ ] Atomic outbox migration/API and Case binding proven by rollback/idempotency tests
- [ ] New-Case continuation consumes and coalesces pending outbox rows
- [ ] Legacy/new Case cutover and boot reconciliation proven without stranding/duplicate wakes
- [ ] Bounded lost-worker reaper and late-result fencing proven, or explicit evidence-backed block recorded
- [ ] Manager tools/prompts/read models migrated only after caller compatibility review
- [ ] Scoped regressions and service-boundary checklist pass; A87 accepts behavioral outcome

## Execution record

### Slice 1 — managed-completion effects consumer (A82 final-gate F1, Stage-8 prerequisite) — 2026-10-02, branch `feat/a84-managed-completion-consumer` from `main` @ `10accb5`, commits `4460268` (tests, RED), `0bb9a60` (DB + task server), `e39d9e1` (gateway consumer), `1235042` (notify timeout) — SUBMITTED, NOT ACCEPTED

**Scope.** Only the F1 blocker: managed (protocol-1) completions skipped every legacy post-commit effect. The Case-scoped completion outbox of TASK 1–7 above (wait-group replacement, cutover marker, reaper, Manager tool changes) is NOT built here and stays open.

**Persisted in the completion txn** (`complete_turn`, called by `_commit_managed_result`; the route and the quiescence-with-result path both pass the carrier envelope):
- legacy-shaped `result` JSON (every field the legacy `/result` route stores: files_modified, timing, return_code, driver/cache state, previous native ids, usage, telemetry_invocation_id, error_detail). Raw stdout/stderr are read for reply/usage extraction and never stored;
- transcript enrichment `reply_text` / `files_modified_json` / `usage_json` / `return_code` (`_mesh_complete_task` reply precedence);
- the session `task_events` row (legacy `/result` parity);
- carrier driver state onto the session row, field-scoped (`_COMPLETION_DRIVER_COLUMNS`, legacy `_dispatch_to_node` parity);
- `effects_state='pending'` (migration 42: `effects_state`, `effects_attempts`, `effects_error` + partial index `idx_mesh_tasks_turn_effects ON mesh_tasks(completed_at) WHERE effects_state IN ('pending','notifying','notified')`).

**Terminal-writer inventory (managed rows).** `complete_turn` (completed / failed / cancelled — the turn ran) and `resolve_recovery` (operator / quiescence resolution of a started turn) mark effects. `withdraw_turn`, `close_session_turns` withdrawals, `request_turn_cancel` before start, and `release_turn` cancel with the backend not invoked do NOT: the turn never ran (legacy `cancelled_before_start` parity: no notify, no session update). Legacy rows: column stays NULL, no reader touches them.

**Consumer** (gateway, `TaskOrchestrator._managed_effects_loop`, started next to the turn scheduler): every `FALLBACK_INTERVAL_SEC` (3 s), one index-served read of ≤25 ids (`pending_turn_effects`), rows processed one at a time. It does not depend on any in-process hint, so it works with the task server in another process. While nothing is enrolled it reads only on every 20th pass (≈60 s, plus the first pass), so a row left behind by an unenroll still drains.

Per row:
1. The idempotent effects run first:
   - session `task_history` + preview fields via `project_turn_session`: field-scoped, appended once per task id, preview set only while the turn is the session's `last_task_id`;
   - the summary file and the session event log;
   - the reply enrichment of a result-less outcome;
   - `TelemetryStore.reconcile(turn_id)`;
   - Case `task.finished` through `_record_flow_event(strict, once)` (flag `HARNESS_FLOW_DRIVE`, legacy `_flow_terminal_outcome` parity).
2. The notification runs next. A CAS `pending→notifying` fences it **before** `notify_task_outcome`; the call has a 60 s timeout.
   - Success: `notifying→notified`.
   - Raise: back to `pending` (attempts+1). After `MANAGED_EFFECTS_MAX_ATTEMPTS`=5 failed passes the row goes to `notified` with `notify_failed`, and the final state is `failed` (warning logged).
   - Timeout, or a row found in `notifying` at read (a crash between send and mark): it is closed as outcome-unknown and **never re-sent**.
3. Finalization: with no effect errors, `notified→done` (or `failed`). A failing idempotent effect keeps the row `notified`, so it is retried without re-notifying, up to the same bound, and then ends `failed` with the error recorded.

**Exactly-once.**
- Dedup key: the turn id plus its CAS state.
- Notification: at most once. The only way to lose one is a crash or timeout inside the send window, and that is recorded as `notify_outcome_unknown` / `notify_timeout`.
- Other effects: at least once, and idempotent (history keyed by task id, `once` Case event, COALESCE enrichment, reconcile). The one exception is the per-session debug log line, which can repeat after a crash.

**Carry (o) decision.** The 4c finalizer already folds `wait_resolved` and the token CAS for continuation turns into one txn (`_finalize_producer_token`). This slice consumes no Case wait/outbox state, so there is nothing to fold yet. Carry (o) stays with the Case-outbox part of A84. The `task.finished` emitted here is the legacy signal and the seam A84's `record_task_terminal` would replace.

**§7 service boundary (consumer + changed route).**
- *Concurrency:* there is one consumer loop per gateway (instance lock), and it is sequential. Two consumers cannot double-notify, because of the CAS fence.
- *Memory:* the batch holds ids only, and one row is loaded at a time (≤ ~17 MiB worst case: output + reply). Raw stdout/stderr are not persisted.
- *Request size:* the route is unchanged (`_guard_managed_body` cap + Pydantic bounds). The consumer takes no external input.
- *Timeout:* every DB write goes through `_managed_write` (5 s deadline ⇒ typed error ⇒ row contained, retried next pass). The notifier has a 60 s timeout ⇒ unknown, not retried.
- *Malformed input:* a garbled result or history JSON is treated as empty, and a missing session is skipped.
- *Backing failure:* with the DB unavailable the pass is skipped. Notifier, telemetry and Case-event failures get a bounded retry and then a visible `failed`.
- *Throughput:* N=100 simultaneous completions take ≥4 passes, with the time bounded by the notifier latency. Accepted, because the Wake-Dispatcher and the scheduler are separate tasks.

**Tests** — `tests/test_turn_queue_a84_effects.py` (14):
- Setup: two `MeshDB` connections on one file (gateway / task-server processes), the real `task_server.app` routes, real admission and scheduler, and the real gateway drain. No CLI.
- Cases: E01 Telegram, E02 web, E03 crash after send, E04 crash before fence, E05 / E05b / E05c notifier exhausted / transient / hung, E06 failing effect, E07 legacy untouched, E08 / E08b withdrawn / recovery, E09 Case `task.finished` once, E10 EXPLAIN (partial-index walk, no temp B-tree), E11 loop discovers with no hint.
- RED at `4460268`: 13/13 failed (missing columns/API). GREEN: 14 passed.
- Mutation (scratch edits, reverted): 8/8 killed — fence-unknown close, effects mark, history idempotency, retry bound, envelope dropped, Case event once, recovery mark, effect-error finalize.
- Regression:
  - `tests/test_turn_queue*.py` (incl. new) + `test_push_notifications.py` + `test_telegram_*.py` + `test_codex_managed_carrier_integration.py` + `test_flow_runs.py` + `test_flow_schema_extension.py` + `test_flow_links_events.py` + `test_telemetry_ingestion.py`: **683 passed, 8 skipped**, exit 0.
  - `test_task_server_client.py`, `test_task_server_upload_safety.py`, `test_wake_dispatcher_eventdriven.py`, `test_control_api_wait_group.py`, `test_session_cache_heartbeat.py`: green.

**Residuals / for review.**
1. Every started managed turn notifies, matching legacy, where the same turns were notified via `submit_instruction` / `_task_worker`. That includes continuation, heartbeat and compaction turns.
2. Telegram transport errors are swallowed inside `TelegramInterface.notify_completion` (legacy behaviour), so only a *raising* notifier is retried. Retrying a swallowed partial long-message send would risk duplicates.
3. `sessions.status` is not set from the result: enrolled-session status stays owned by the queue. The legacy `turn.*` gateway telemetry events and the `results/<id>.json` artifact are not emitted for managed turns (not in F1). The task-server `task_failed` ndjson event is not emitted either.
4. Latency: ≤3 s plus drain time. With nothing enrolled, ≤60 s.
5. The CONTEXT.md note for these deferrals is left to the Manager at close.

## Closure (fill on completion)

Record changed files, exact tests/results, the legacy cutover disposition, any deferred reaper bound, and confirmation that no flag was activated or worker restarted.
