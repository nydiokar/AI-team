```yaml
job_id: AGENT_84_WORKER_COMPLETION_OUTBOX
created_at: "2026-09-24T18:00:20+00:00"
status: ready
owner: ""
depends_on: [AGENT_82_SESSION_TURN_QUEUE]
results_ref: DISPATCH_LOG.md#A84
evidence: []
updated_at: "2026-09-24T18:00:20+00:00"
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

## Closure (fill on completion)

Record changed files, exact tests/results, the legacy cutover disposition, any deferred reaper bound, and confirmation that no flag was activated or worker restarted.
