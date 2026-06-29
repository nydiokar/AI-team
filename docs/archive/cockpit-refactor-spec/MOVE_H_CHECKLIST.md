# Move H — Approval consumer (durable gate, not a blocked coroutine)

Scope fence per COCKPIT_REFACTOR_SPEC §13: **only the boxes below are in scope.**
Baseline: branch feat/webui-ui0 @ baba1d1 (G′). Backend deps in system python.

Spec row (§14): *"M4 emits approval.requested/granted but nothing waits on them.
H adds the object + pending queue + a path that *blocks* on the decision + a
resolve endpoint."*

## Design decision (the part that earns or loses the room)
"Blocks on the decision" is **logical, not a held coroutine.** The wrong design is
`await asyncio.Event()` inside submit_instruction — it pins a worker slot for the
human-response window AND silently evaporates on gateway restart (violates
mesh-restart-resilience: in-memory event is gone). The RIGHT design mirrors the
proven mesh_tasks pattern: an approval is a **durable DB record with a state
machine** (pending → approved | rejected | expired). The gated action is recorded
pending and NOT dispatched; *resolution is what triggers dispatch.* This survives
restart (the row is in SQLite) and rebuilds the UI queue after any restart.

CONTROL_CONTRACT §7 invariant honored: WorkflowService stays STATELESS (events
only). The approval OBJECT/QUEUE is new state H owns in db.py + a thin
ApprovalService — the workflow EVENTS (approval.requested/granted) are still
emitted via the existing WorkflowService, unchanged.

---

## Box 1 — approvals table (migration 13)
- [ ] db.py migration 13: `CREATE TABLE approvals` — id, session_id, task_id,
  action, risk, reversible, status (pending|approved|rejected|expired),
  requested_by, resolved_by, payload (JSON: the gated dispatch args), created_at,
  resolved_at, expires_at. Bump _CURRENT_VERSION to 13.
- [ ] DAO: create_approval / get_approval / list_approvals(status?) /
  resolve_approval(id, status, resolved_by) — mirror enqueue_task/list_tasks style,
  write-locked. resolve_approval is a guarded transition (only pending → terminal).

Done = exactly: table + 4 DAO methods + the version bump. No service wiring yet.
Do NOT touch: other tables, the _DDL block (use a numbered migration).
Revert: drop migration 13 + the DAO methods.

## Box 2 — ApprovalService (the gate logic)
- [ ] src/services/approval_service.py: request(session_id, action, payload, risk,
  reversible) → persists pending + emits approval.requested via WorkflowService;
  returns the approval id. resolve(id, decision, resolved_by) → guarded DB
  transition + emits approval.granted(granted=decision=="approved") via
  WorkflowService; returns CommandResult (reason codes: not_found, already_resolved).
- [ ] pending() / get() read-throughs for the queue + a resolve callback hook so a
  caller can act ON approval (dispatch the gated action). Callback is OPTIONAL and
  injected — the service does not import the orchestrator (no cycle).
- [ ] Unit tests: request creates pending + emits; resolve flips state + emits +
  fires callback on approve, not on reject; double-resolve → already_resolved;
  expired never resolves.

Done = exactly: the service + tests. No HTTP, no orchestrator edit.
Do NOT touch: WorkflowService internals (call its existing methods).
Revert: delete the service + test.

## Box 3 — HTTP surface (control_api)
- [ ] GET /api/approvals?status=pending → {approvals:[...]} (the queryable queue).
- [ ] POST /api/approvals/{id}/resolve {decision:"approved"|"rejected"} →
  {ok, reason, approval}. reason codes → status: not_found 404, already_resolved 409.
- [ ] (Optional demo seam) POST /api/approvals to request one, so the queue is
  exercisable end-to-end without a backend that emits them yet. Behind the same auth.
- [ ] Tests in test_control_api.py: list empty, request→pending, resolve→approved,
  resolve-twice→409, resolve-missing→404.

Done = exactly: the 2 (+1) endpoints + tests.
Do NOT touch: instruction/session endpoints.
Revert: drop the routes.

## Box 4 — frontend consumes approvals (this is UI-3's approval slice)
- [ ] rawApi RawApproval + apiClient.approvals()/resolveApproval(id, decision)
  (idempotency-keyed on resolve).
- [ ] approvalAdapter: RawApproval → domain ApprovalRequest (already in models.ts).
- [ ] useApprovals() hook (poll) + useResolveApproval() mutation.
- [ ] Timeline approval card (SessionTimeline): wire the disabled Approve/Reject
  buttons to useResolveApproval; remove the "Wired in UI-3 (Move H)" placeholder.
- [ ] tsc + vitest (+ approval adapter test) + vite build green.

Done = exactly: approval card round-trips approve/reject from the phone.
Do NOT touch: Composer, event stream, Tasks sections.
Revert: re-disable the buttons.

## Gate
- [x] Backend pytest green (8 approval_service + 5 control_api approvals; 55 total, no regression).
- [x] Live: POST request → GET pending shows it → POST resolve approved → pending empties;
  SURVIVES a gateway restart (appr_af15337eff34 still pending after kill+restart — the
  whole point of the durable design); double-resolve → 409.
- [x] Frontend tsc + vitest (21) + vite build green.

ALL BOXES DONE 2026-06-24. Approval round-trips approve/reject from the phone;
durable across restart (migration 13, approvals table).
