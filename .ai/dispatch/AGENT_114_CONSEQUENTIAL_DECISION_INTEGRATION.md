```yaml
job_id: AGENT_114_CONSEQUENTIAL_DECISION_INTEGRATION
created_at: "2026-11-13T13:35:52.954860+00:00"        # CANONICAL — set once at dispatch, never derive again
status: dead              # ready | active | blocked | done | dead
owner: ""
depends_on: [113]
results_ref: DISPATCH_LOG.md#A114             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-26T10:20:01.902661+00:00"
```

# DISPATCH — A114 · Integrate consequential decisions into actual SDK use

**Level:** 3. **Type:** production integration and consumer-level verification.
**Authored:** 2026-11-13, Europe/Vilnius. **Reviewed:** 2026-11-17.
**Status:** ready for offline implementation; production authority/activation are unproven.
**Execution issue:** [GitHub #294](https://github.com/nydiokar/AI-team/issues/294).
**Branch:** one `feat/a114-decision-integration` branch/worktree, PR and self-merge.
**Depends on:** A113's proven prototype; coordinate A111's authority/evidence contract and
A71/A75's credential boundaries. A71 authenticates nodes and A75 changes token exposure;
neither supplies an owner principal. Engineering may start offline; activation is gated below.

## Read this first

- [A113](AGENT_113_CONSEQUENTIAL_DECISION_CONTROL_PROTOTYPE.md), its Closure and
  [durable contract](../../docs/harness/consequential_decision_control.md).
- `.ai/CONTEXT.md`, `DISPATCH_LOG.md`, current git; A111, A71, A75 and the actual auth code.
- `sdk_governor_ceilings` / runtime flag writes in `src/control/db.py`,
  `src/control/controller_state.py`, `src/worker/controller_state_client.py`,
  `src/backends/claude_driver.py` process creation and `ClaudeAgentOptions`,
  `src/control/control_api.py` auth and `routes/admin.py` configuration/approval writes.
- Existing carrier-owned session/turn admission, `ApprovalService`, Case review/closure and
  relevant targeted tests. Use symbol lookup before reading named definitions.

## Why and objective

A113 proves a local transactional control, not an active protection in AI-Team. Finish the
engineering obligation by protecting **one real declared consequential setting**:
`CLAUDE_SDK_MAX_BUDGET_USD` for SDK work in explicitly opted-in projects, including warm reuse.
An unauthorized or stale decision must cause no native SDK launch **or prompt/query send**.
An authorized boot must
use exactly the approved effective budget, including explicit null if intentionally chosen.
The ordinary unconfigured workflow stays compatible. Do not build another prototype phase.

**Scope of value versus scope of protection:** registry overrides are global; fallback config/env
is worker-process local (`sdk_governor_ceilings()` has no project argument). A project declaration
controls use of that existing source, not a new per-project budget override. Derive project/Case/
target-carrier scope from canonical session/routing records, not caller body fields. Bind the
decision to the target worker's actual resolved value/source, including its fallback environment;
the controller's own fallback is not proof of worker consumption. Test different controller and
worker env values and all affected enrolled projects when a shared override changes.

This pilot completes one real integration path for FM-1. It does not certify scientific loss
choices, palette quality, aggregate project spending, plan quota or actual dollar-limit enforcement.
Fake SDK tests prove arguments and admission; native budget semantics need separately authorized
evidence before claiming an economic bound. Other domains keep explicit consumer/evaluator adapters.

A113 already proved immutable explained packets, current scoped/revocable authority,
material generations, separate deterministic evaluation, same-transaction consumer effects,
provisional isolation and crash/retry behavior. **Do not rebuild** its palette consumer,
invent a generic policy engine or add another queue/reviewer. Reuse its invariants/tests;
move only the necessary production metadata into MeshDB alongside canonical config/Case
references. The simulation DB and host-capability registry are not production dependencies.

## Task — shortest complete path

1. **Close the actual authority and writer boundary.** Map all budget and declaration writers,
   effective registry/env/null/default reads, controller snapshots, direct/alternate SDK boots,
   warm reuse and respawns. Establish a verified owner/delegate principal in the existing auth
   surface; body `resolved_by`, role names and a shared worker/dashboard bearer are insufficient.
   Consume A111 authority when available. Reuse/coordinate the relevant A71/A75 credential work;
   do not claim an owner-only route while agents can recover the owner credential from served
   HTML or their environment. If that credential separation is not yet available, complete the
   isolated integration/tests and state the exact activation blocker. No competing auth stack.
   Persist declared policy, explanation/revisions, decision and delegation with reported write
   failures. Existing approvals may notify/link; caught writes and post-commit callbacks cannot
   establish admission or successful execution. Enrollment cannot disappear on malformed policy,
   unavailable store/controller, task override or a configuration exception.

2. **Guard protected turn admission and process creation with one immutable boot specification.** Resolve the
   actual effective budget once through the existing canonical precedence; bind its source,
   project/objective/policy/value generations, explanation, authority and evaluation plan.
   Hand that admitted value/revision through the existing carrier path to the exact
   `ClaudeAgentOptions(max_budget_usd=...)` consumer. The worker must reject missing, stale,
   replayed-for-another-scope or mismatched receipts and cannot re-resolve a different value.
   A warm process is not an exemption: before each new protected prompt/query, verify its boot
   value/revision and current scoped authorization are compatible with the admitted work. On
   newly enrolled, stale, revoked, differently scoped or unknown warm state, hold the new work
   with a clear reason; no silent reuse, automatic restart, or unrelated in-flight interruption.
   Check before any automatic close/respawn side effect too. Preserve explicit stop/cancel and
   recovery controls. Unconfigured warm sessions retain existing behavior. Reuse durable turn/
   session ownership and reconciliation for crash/unknown outcomes, rather than adding a queue.
   No claim of exactly-once native process launch without a tested identity/recovery mechanism.
   Specify one durable admission point: expiry/revocation/material change before admission blocks;
   an already admitted immutable operation may finish or follow an explicit cancellation protocol.
   Replaying a completed receipt only reads history; it cannot launch or send again. A new turn
   is a new admission even when its process is reused. Test change/revocation at each side of
   admission and lost acknowledgements; do not mix historical receipts with live eligibility.
   Sandbox work may create a provisional boot plan and evaluate it offline; it cannot launch
   paid/native work under the authoritative project without satisfying the same admission gate.
   Evaluation can check configured cost bounds; economic sufficiency is explicit N/A with reason
   unless the project supplies a real evaluator. Approval is not scientific/Case acceptance.

3. **Finish the operator/recovery path and prove the real consumer.** Use existing Case/settings/
   approval surfaces to show the packet, required owner, exact blocked dependency, current/stale
   status, durable decision and observed boot receipt. Feed receipts into A111's single review/
   closure contract where applicable; never infer accepted objectives from process completion.
   Add an additive migration/backward-compatibility test, concise operator instructions and
   rollback behavior that holds enrolled work on a build that cannot enforce its declaration.
   Test failure/retry/revocation and audit read paths; keep sensitive config/credentials out of
   logs. Complete service bounds before exposing new handlers. Avoid new generic dashboards.

## Acceptance — actual consumer proof

- Offline tests drive the real API/config → MeshDB → controller/worker → SDK-options creation
  path using a fake SDK **only at the native process boundary**. Assert launch **and send/query**
  counts zero when blocked, including previously booted sessions, enrollment after boot and
  warm reuse following policy/value changes or revocation. Inspect actual `ClaudeAgentOptions`
  and the admitted revision/process identity when allowed. A mock gate returning
  `blocked` or a stored approval alone is insufficient.
- Exercise registry override, env fallback, compiled/default/null semantics; changes and
  reversions, unrelated settings, alternate direct boots, worker snapshot/controller failure,
  malicious body identity, wrong project/delegate/self-approval, expired/revoked grants,
  malformed/missing policy and attempted provisional promotion. No prohibited launch occurs.
- Crash before/after decision commit, boot admission, native launch and receipt persistence;
  simulate lost acknowledgement, duplicate/concurrent retries and recovery. Inspect real
  MeshDB state and native-boundary identities; never relaunch an unknown live process blindly.
- Missing/failed evaluation cannot become passed by accepting a decision. An N/A reason is
  visible. Decision history, failed/blocked dependency and original-revision receipts remain
  inspectable after restart. A111 closure stays separate from decision authorization.
- Unconfigured projects retain existing native SDK arguments, normal turn/retry and warm reuse.
  Ordinary config updates do not force unrelated approvals. Invalid opted-in state fails closed.
  Shared-override changes invalidate affected enrolled use only. Missing controller state cannot
  turn an enrolled project into opt-out, even on a worker that has never seen its declaration.
  Show how enrollment is durably known or conservatively held across that first-contact seam.
- Exposed handlers have concurrency limits retained until actual work ends, bounded payloads,
  N=100 memory bounds, caller/server timeouts, structured errors and explicit DB/controller
  outage behavior. Use repo patterns and document any native-I/O limitation in CONTEXT.
- One offline command reproduces the essential integration proof from clean temporary state;
  targeted tests/static checks and CI pass without paid agents. No open critical/high defect.

## Overall definition of done and deployment safety

This obligation is complete when an operator can declare the real setting consequential,
inspect its effects/alternatives, authorize its exact revision, observe protected downstream
consumption, understand/recover a block, revoke future authority, and rely on restart/retry
without a bypass or duplicate unknown launch; unconfigured workflows remain compatible.
The code, migration, focused regression coverage, observability, runbook and fail-safe rollback
must be merged. This is maintainable protection for the named workflow, not a promise to detect
every consequential choice across every domain. Further domain declarations should be small
adapters, not a mandatory third prototype or replacement architecture.

Stage deployment only after offline proof and credential separation. Gateway deployment follows
the existing Compose skill. Worker promotion requires explicit operator authorization, active
session/ancestry checks and drain; paid validation requires a separately authorized natural run.
Production acceptance must verify the observed worker build/receipt/options for the declared
project without inventing execution evidence. If rollout is gated, engineering proof and pending
production acceptance must be separately recorded; do not call the overall obligation deployed.

## Reserved decisions / scope out

Operator owns worker restarts, paid spending and the initial project/budget choice. No universal
gate, semantic classifier, new role/closer/queue, credential framework, palette UI, retraining,
historical approval synthesis or silent production activation. Strategic expansion is separate.

## Trail / milestone

**Review evidence (2026-11-17):** independent review and executable tests confirmed the warm path
returns its original process/options after the resolved budget changes (3.0 → 1.0; one native
start, reused options still 3.0). See
`tests/test_runtime_flag_typed.py::test_warm_sdk_reuse_keeps_original_budget_in_actual_options`.
This is expected current behavior, not a newly introduced production gate. It makes launch-only
acceptance inadequate. A113's historical packet-rendering defect was separately reproduced and
repaired; protected operations still reject the stale/expired revision. Final proof/results and
the executable GitHub handoff are recorded in A113's final adversarial review section.

- [ ] Map real writers/consumers and close the authority boundary.
- [ ] Persist and carry an immutable authorized boot specification through existing pathways.
- [ ] Prove exact SDK consumption, bypass resistance, recovery and ordinary compatibility.
- [ ] Complete operator visibility, operational bounds, migration and rollback evidence.
- [ ] Merge; record deployment/worker/paid gates honestly and satisfy overall done criteria.

## Closure — RETIRED 2026-11-26 (operator abstention, not completion)

The operator decided on **2026-11-26** not to use `CLAUDE_SDK_MAX_BUDGET_USD` at all: a USD budget
cap is too restrictive for this workflow. This job's entire objective is the protected use of that
setting, so the objective is **withdrawn** — not implemented, not activated, and not satisfied.
Status is `dead`, not `done`.

Live state at handoff: gateway `/health` reports `governor.sdk_max_budget_usd: null` (no cap), which
is the desired abstained state; no flag write or restart is needed. Do not set, pin, enable or test
this knob, and do not treat A114 as pending work. `docs/backend/ENV_FEATURE_FLAGS.md` records the
abstention so no later agent follows the old `ops_flag.sh set CLAUDE_SDK_MAX_BUDGET_USD 2.5`
instruction.

The 2026-11-17 review evidence above remains historical: it explains why launch-only guards were
inadequate for the *old* objective; it is not an acceptance proof and does not make this job a
candidate for `done`. Full remaining-work handoff and 2026-11-26 Kanebra report:
[issue #308](https://github.com/nydiokar/AI-team/issues/308).
