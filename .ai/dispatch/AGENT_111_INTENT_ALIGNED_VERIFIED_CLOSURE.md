```yaml
job_id: AGENT_111_INTENT_ALIGNED_VERIFIED_CLOSURE
created_at: "2026-11-09T22:22:08.839169+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A111             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-09T22:26:45.559708+00:00"
```

# DISPATCH — A111 · One bounded objective, intent preserved, independently verified closure

**Level:** 3 (agent authority, DB invariants, distributed recovery, service boundaries).
**Type:** staged engineering + empirical review calibration + production acceptance.
**Authored:** 2026-11-10, Europe/Vilnius. **Packet:** ready; implementation not started.
**Depends on:** no prerequisite for P0/P1. Later gates below name their actual dependencies.
**Branch:** this planning change is docs-only on `main`. Each implementation stage uses one
`feat/a111-<stage>` branch, PR, self-merge and gateway deployment where required. Finish each
stage before starting the next branch. Do not carry another loop's changes.

> The operator needs useful delegation: a Manager carries a bounded objective to an honest
> conclusion without rescue prompts, without quietly substituting a narrower question, proxy
> measurement, or easier deliverable. A technically correct substep is not objective closure.
> The requested deliverable in this authoring turn is this executable dispatch plan, not a
> claim that runtime enforcement or hands-off acceptance has already been implemented.

## Why (intent)

Deliver both outcomes together:

1. **Dependable execution:** one invocation dispatches, waits without inference, receives results,
   reviews, repairs and closes the same Case, or escalates a genuine blocker within agreed limits.
2. **Trustworthy acceptance:** successful closure requires evidence for the owner's actual
   objective, its milestone contribution and its project direction. Neither the executor nor a
   Manager's unsupported declaration can manufacture acceptance.

Root cause established in this tree: semantic review is largely role prose; the existing central
closure path verifies caller-supplied reconciliation and lifecycle conditions, not demonstrated
fitness to owner intent. The advancement gate rewards activity counts. The existing Scientific
Governor is a review convention, not a mandatory authority check. Distribution/deployment and
whole-loop production acceptance remain incomplete. This is not evidence of intentional sabotage.

Success is fewer invalid acceptances AND less operator rescue at acceptable cost. A gate that
creates endless reviews, blocks sound negative research, or demands ritual rework also fails.

## CONTEXT — verified baseline and limits

Inspected `main` at `118a8b7` on 2026-11-10; gateway `/health` reports `49e9bd0`, coverage true,
SDK turn and USD ceilings null. No live data was mutated and no worker restarted.

| Existing seam | Observed behavior | Consequence |
|---|---|---|
| `src/control/db.py:6913` `open_case` | Stores objective/criteria; does not establish immutable owner provenance or authorized revisions. `update_flow_run` at 6643 permits objective updates. | Preserve raw owner input separately from derived interpretation; bind reviews to a revision. |
| `src/control/db.py:7075` `close_case` | Central terminal operation; checks child Cases, approvals, criterion labels and latest Case-level review. | Strengthen this operation; do not introduce another closer. Latest verdict alone cannot cover every submitted task. |
| `src/orchestrator.py:6211` `close_case` | Requires Manager continuation plan; optional advancement gate allows two dispatches, rework, or a 40-character attestation. | Activity and text length cannot prove value. Replace the heuristic when the evidence gate cuts over. |
| `src/orchestrator.py:6359`, `src/control/routes/cases.py:340` | Review accepts verdict/reason/optional task id. Tagged review acknowledges the task's inbox message. | Evolve the same review path; preserve acknowledgement semantics without treating acknowledgement as acceptance. |
| `src/control/routes/cases.py:317`, `control_api.py:1456` | Shared bearer auth; route supplies actor=`manager`, not an authenticated session principal. | Tool allowlists and an actor string do not enforce reviewer independence. Scoped authority is required. |
| `src/core/task_state_truth.py:76` | `completed` is a terminal execution result. | Keep execution truth distinct from delivery acceptance and Case satisfaction. |
| `docs/harness/roles/manager.md` | Already requires immediate/milestone/project alignment and relevance before rigor. | Encode and enforce that contract; another long admonition is not the solution. |
| `docs/harness/scientific_governor_contract.md`, `src/core/roles.py:226` | Existing six-question reviewer, dispatched through `dispatch_worker` as a worker; no general runtime role promotion. | Reuse the reviewer contract and worker substrate; do not assume its role loader is an active integration. |
| `tests/test_scientific_governor.py:318,389` | Fixtures render prose/example refs; live check always skips even when enabled. Rendered file contains rubric as well as input. | These prove fixture wiring, not model discrimination or evidence inspection. Keep answer keys out of reviewer input. |
| `scripts/oracle/governor_metrics.py` | Standalone JSONL metrics helper, not runtime acceptance authority. | Reuse useful fields; derive production metrics from canonical review records, not a competing ledger. |
| A102/A105/A109/A110 | One carrier, signal, inbox and result-consumption path already shipped; remaining promotion/natural acceptance documented. | Reuse and close existing packets on their evidence; do not rebuild them. |

Setup: repo `.venv`, Python >=3.11, `pyproject.toml`; React/Vite UI under `web/` with pnpm.
Read `.ai/CONTEXT.md`, the human dispatch ledger and fresh git before each implementation stage.
The old PM2 architecture paragraphs and several A94–A96 line numbers/continuation assumptions
are stale; actual Docker split, source and A104 inbox own those mechanics.

## Architecture decisions — fixed for this milestone

### A. One acceptance policy in the existing coordinator

Keep the current flow: owner invocation → Case → `dispatch_worker` → durable turn queue →
worker result → agent inbox → `record_review` → `close_case`. Extend these seams. A submission
operation is a phase of this flow, not a second task coordinator, scheduler or completion API.

Three distinct facts must be visible in API/UI and reports:

- **Execution:** queued/running/terminal/recovering, owned by the existing turn/task machinery.
- **Submission acceptance:** pending review / accepted / rework / insufficient, scoped to one
  contract revision and evidence version, owned by canonical review records.
- **Case outcome:** objective satisfied, explicitly cancelled/abandoned, or still open/blocked.
  Existing terminal status can remain compatible; disposition must distinguish success from cleanup.

A worker may finish an execution and submit evidence. It cannot accept its own delivery. The
Manager can accept a bounded worker subtask after checking it, but that does not accept the
parent objective. Every enrolled Case's successful closure needs a fresh independent review of
the assembled outcome, including work done directly by the Manager. Independent review is
mandatory per Case closure, not another model call on every message, tool use or small subtask.

Cancellation, orphan cleanup and an owner's decision to stop must remain possible without PASS;
they must never project as successful objective completion. Incomplete work and quota interruption
remain recoverable/blocked. Preserve actual terminal execution facts while recording this distinction.

### B. Preserve intent; allow authorized learning

At ingress, capture the owner's original request losslessly with source/message identity and hash.
Store the Manager's interpretation separately. Link the immediate task to its Case, milestone and
versioned project-goal reference; include accepted prior evidence and constraints. Do not infer
missing ORACLE goals or silently treat a summary as the owner's words.

Use stable criterion IDs, not only matching strings. For each criterion record what observable
result or knowledge would satisfy it, its required evidence, material assumptions, and scope.
For research, record the scientific question/object, population/denominator, intervention or
operation, training objective when relevant, evaluation and intended downstream decision.
For engineering, record the user-visible behavior and deployment seams. Require only applicable
fields; unknown material fields are explicit, not invented.

The Manager may refine implementation plans and hypotheses within this contract. Changes to
target, population, acceptance, authority or claimed knowledge require a proposed revision with
impact/difference and owner authorization. Preserve all revisions and invalidate stale reviews.
An owner may delegate a defined class of revisions explicitly; it is never implied by a model's
claim that a proxy is equivalent. Emergency stop never requires such a revision.

Check alignment before dispatch and again at material replanning/measurement changes using
this same contract: state the subtask's contribution, the remaining parent obligations and
the decision its result will support. No second "intent gate" service. An adapter must not
accept a new population/metric/target under an old contract identity. Semantic drift hidden
inside otherwise well-formed work still needs evidence review; deterministic field validation
cannot discover every misleading interpretation. Put a review checkpoint before an expensive
or irreversible experiment when its formulation is load-bearing, rather than discovering a
wrong measurement framework only at final closure. Reuse the same reviewer/receipt machinery.

### C. Evidence submissions and independent challenge

Integrate the proposed four closure questions into the existing six-question Scientific Governor
protocol, not a second reviewer or a second checklist:

| Submission must establish | Existing review lens |
|---|---|
| Owner need, new behavior/knowledge and contribution across horizons | Alignment + Continuity |
| Actual operation, inputs, outputs, inspected artifacts and reproducible result | Validity + Evidence |
| Strongest way correct work could still miss the objective; executed discriminating challenge | Validity + Future simulation |
| Unresolved original scope; why this task is terminal without claiming its parent is satisfied | Evidence + Decision |

Submit criterion→claim→evidence→check mappings with immutable commit/run/data/artifact identities.
Evidence receipts include operation, environment/data version, exit/result, time and output digest;
existence, a commit, a QA label or green unit tests alone are not acceptance proof. Reuse durable
job/run/artifact records. Do not add a shell-execution endpoint or execute commands copied from
an untrusted report. Reruns use existing bounded jobs and permitted read-only inspection.

Before seeing the persuasive summary, an independently booted reviewer gets controller-built
owner/contract/prior-evidence context and actual artifact references. It states the expected
outcome and strongest mismatch, then inspects evidence and the claim. It must execute, or
independently inspect a trustworthy executed receipt for, a discriminating check tied to that
mismatch. A mandatory unexecuted challenge yields INSUFFICIENT, never PASS. Protected data,
paid runs and destructive experiments require existing authority; lack of it blocks that claim
instead of provoking an unauthorized experiment.

Preserve steelman-first, cheapest useful repair, BLOCKER/MATERIAL/RESIDUAL/OPPORTUNITY, and
one review + one material-repair verification. No reviewer recursion or forced second dispatch.
Sound negative findings, justified uncertainty and genuinely equivalent prior evidence can pass
when they meet the agreed question. New knowledge does not require new code or a favorable result.

### D. Authority and consistency are engineering requirements

Enforce submit/review/revise/waive/close permissions server-side. Reuse existing identity mechanisms
where they truly bind a caller. Because shared bearer auth currently does not, add the minimum
controller-issued, expiring assignment capability needed for these operations: bind caller session,
Case, role/action and assignment; bind a verdict additionally to submission, contract revision and
evidence digest. Never trust body actor/role/session strings or possession of a common worker token
as an acceptance principal. Author and reviewer session/assignment must differ. The Manager may
not mint a reviewer identity or convert an unresolved blocker to success by `waived`.

The operator's authenticated override/revision must be separately attributable, reasoned and
audited; a waiver is visibly a waiver, not verified evidence. No routine operator approval per
Case is intended. A71 per-node credentials complement this work but do not establish per-reviewer
authority. Coordinate its credential lifecycle; do not build a second general authentication stack.
If a fresh audit finds no way to isolate the scoped capability from an author with the available
worker environment, block protected activation and surface that specific gap before claiming safety.

Threat model: authorized API workflow, trusted controller/storage and worker OS. Arbitrary host
shell/DB access or a stolen controller credential can bypass software policy; role isolation is
not a sandbox. Claim enforceability within this boundary, never impossibility of malicious bypass.

Use the canonical DB and existing Case events/artifact links; P1 owns the additive migration and
bounded indexed acceptance records. One short transaction checks revision, evidence version,
per-criterion/per-submission verdicts, unresolved material findings and lifecycle gates, then
records acceptance/closure atomically. No DB transaction is held during model/tool execution.
Freeze reviewed artifacts by immutable reference; external live dependencies require recorded
freshness checks. A new submission/revision/material change makes prior approval inapplicable.
One task's later accept cannot erase another task's unresolved rework. Replay is idempotent;
lost response, concurrent close and crash between review and close cannot create success.

### E. Reuse map and exclusions that prevent competing implementations

| Existing work | Ownership under A111 |
|---|---|
| A102, A105 | Keep their deployment/natural acceptance gates in their packets. P4/P6 consume their evidence. |
| A94 | Sole S1 client, battery runtime, decision log and replay work; scores are advisory and are not ground truth. Rebase stale two-wake/continuation assumptions onto A104. |
| A95 | Sole semantic dispatch preflight, after A94 qualifies. Deterministic contract/authority checks in P1/P2 do not depend on a classifier. |
| A96 | Optional cheap rework routing only after its existing precision/live gates; never acceptance authority and never a second repair loop. Not on A111's mandatory critical path. |
| Existing Scientific Governor | Sole independent closure reviewer contract for this milestone. P3 evolves it, rather than adding an "Intent Governor" or recursive reviewer. |
| Governor programme G1/G3/G4 | This packet owns the bounded-Case contract/closure subset, benchmark subset and independent reviewer integration. Future G jobs consume this implementation, adding only remaining programme scope. |
| Governor G2/G5/G6+, external adapters | Programme direction, campaigns and external canonicalization remain separate, unimplemented scope. No outer Governor is needed to enforce Case acceptance. |
| ORACLE | Supplies domain contracts, evaluators and authorized historical evidence. No market research, model training or domain-truth rewrite in this harness task. |
| `.ai/dispatch` state | Planning/job registry only. Its `done` does not become runtime scientific truth. Record A111 engineering and empirical gates separately. |

## TASK — ordered stages and verifiable exits

### P0 — Baseline, bypass map and benchmark contract (first implementation PR)

Read the named seams and trace every successful/unsuccessful terminal writer and projection:
Case close/state/cleanup, review and waiver, direct DB update helpers, Manager invocation,
worker result, inbox acknowledgement, jobs, MCP roles, UI, Telegram and federation. Record a
compact caller→authority→store→projection map here. Inspect backend-specific role/credential
propagation for Claude, Codex and OpenCode; shared process environment is not a principal.

Add offline reproductions before fixes: unsupported criterion labels can satisfy the existing
gate; unrelated latest accept can hide earlier rework; count gate accepts pointless activity;
closure without a required independent receipt; and normal valid/cancel/recovery controls.
Use real temp DB/API/MCP seams, with fake agents and no live dispatch. Each claimed gap must be
confirmed or corrected against the tree, not preserved merely because this packet predicts it.

Freeze the initial benchmark/label protocol and P1 schema/API shapes in the existing
`scientific_governor_contract.md`; capture the owner's exact request for new protected Cases.
Do not retrofit reconstructed owner intent as immutable historical fact. P0 exit: reproductions,
writer map, traceability inventory and explicit implementation scope. No open-ended redesign.

### P1 — Durable intent, evidence and authority (depends on P0)

Implement B/D in the existing invocation/Case/dispatch/review surfaces, using typed Pydantic
structures and one additive migration with upgrade/rollback posture. Bind submissions and
reviews to immutable contract/evidence versions; implement scoped review authority and operator
revision/override attribution. Preserve current execution states and result transport.

Test author self-accept, forged role/caller, wrong Case, expired capability, stale revision,
artifact replacement, lost response, duplicate requests, DB unavailable and migration/restart.
Bound storage growth and retention of raw request/evidence references without deleting canonical
session fallbacks. Do not put credentials or bulk transcripts in event payloads.
Exit: those guarantees hold at DB and HTTP boundaries in isolated tests; no production enforcement.

### P2 — One enforceable acceptance/closure transition (depends on P1)

Extend `record_review`/`MeshDB.close_case` to enforce current receipts, all required obligations
and independent Case-level verdict. Review `waived`, operator-close, terminal-state helpers,
orphan cleanup, generic flow mutation and federation imports for bypasses. Legacy calls in a
protected Case must reach the same policy or fail structurally; body reconciliation is evidence
submission, never authorization. Unprotected historical Cases keep an explicitly labelled legacy
status; no silent fabricated approvals or mass history rewrite.

Replace the activity-count advancement gate, its flag and agent hints with the evidence policy;
do not stack both. A one-dispatch valid result must pass; pointless extra dispatches cannot help.
Use one per-Case enrollment/version, captured at creation. Rollout off stops enrolling new Cases;
it cannot silently exempt protected ones. Explicit audited owner cancellation remains available.

Keep result acknowledgement independent of successful acceptance: a pending reviewer/rework
obligation survives consumption and is reconstructible. Rework uses the same Case and queue,
with one logical dispatch identity per repair; do not resurrect continuation tokens/wait ledgers.
Exit: direct/API/MCP bypass, multi-task, concurrency, recovery and historical-compatibility tests
pass; architecture contract below is met on the merged revision.

### P3 — Independent reviewer and executable benchmark (depends on P2)

Dispatch a fresh review-only worker through existing `dispatch_worker` with controller-assembled
context, no author-context reuse and no grant to accept itself, close the Case or rewrite intent.
Use the existing Scientific Governor role; add a typed verdict/evidence wrapper to its current
protocol. Do not invent a generic role factory. Route its result via the same inbox; validate
the scoped reviewer receipt before the Manager can close. Critical missing evidence fails closed;
S1 outage merely omits advisory scores. Preserve review budget and stop controls.

Evolve `tests/test_scientific_governor.py` fixtures into an executable artifact-backed benchmark
with hidden labels. Existing fake SHAs and explicit "this is a proxy" prose are wiring fixtures,
not historical proof. Test polished misleading summaries and sound results in matched pairs.
Use real checked-in artifacts for offline challenge execution; separately evaluate actual model
judgments under an authorized bounded run. Replace the always-skip placeholder with an actual
opt-in runner, while keeping ordinary pytest entirely offline.

Required matrix: wrong narrower target; correct arithmetic/wrong scientific question; aggregate
metric substituted for operational usefulness; old work falsely equated despite changed
population/objective/operation/evaluation; differing training objectives despite same scoring
columns; development vs protected data; omitted parent criteria; undeployed correct code;
quota/restart; valid full result; valid negative research; authorized contract revision;
genuinely equivalent prior work; and residual uncertainty that must not block. The ORACLE-like
examples supplied in the request are hypothetical until backed by owner-authorized artifacts.

Exit: deterministic checks reject missing/stale/forged evidence; model benchmark separately
reports critical-mismatch recall, unsupported-accept rate, false blockers, localization, cost,
latency and abstention. No pretend live test or synthetic fixture count can close the empirical gate.

### P4 — Complete deployed loop and cross-layer truth (depends on P3; A102/A105 gates apply)

Reconcile actual controller/worker builds, active capabilities and runtime flags. Consume A102
promotion evidence and A105 natural latency/observation evidence without changing their criteria
or manufacturing paid proof turns. Worker promotion requires the operator's decision plus drain
and ancestry checks. Gateway deploy follows the existing Docker workflow, never a global lock.

Prove round, per-turn, time and resource budgets at the owning layer; distinguish SDK API-dollar
ceilings from subscription cost and whole-Case spend. Existing round caps/null SDK ceilings are
not a verified all-backend Case budget. Reuse canonical usage and admission/cancellation for
aggregate reservation/accounting; bound simultaneous outstanding work and explicitly reserve
possible in-flight overrun. Unknown usage cannot count as zero or permit unlimited new dispatch.
If no hard monetary guarantee is possible for a provider, state it and enforce an agreed hard
work/time/concurrency bound; never advertise a dollar ceiling that cannot be enforced.

Project the three facts in A on existing Case/session UI/API surfaces; show pending review,
specific rework/blocker, accepted revision, cancellation and unknown outcome honestly. Preserve
stop/retry and no premature release after uncertain cancellation. Extend canonical Case brief
and role boot to reconstruct contracts, submissions, findings and repair/review obligations.
Exit: fault-injected isolated workflow survives dropped hints, duplicate result, busy operator
session, reviewer interruption, quota pause, controller restart and Manager context replacement.
Same Case/lineage, no lost obligation, duplicate spend or false success; all promised backends
covered or explicitly unavailable, not silently dropped from the claim.

### P5 — Calibrate useful judgment with the operator (depends on P3; can precede P4 rollout)

Engineering correctness does not settle scientific relevance. Use an owner-labelled development
set and a held-out matched set; reviewers never see answer keys. Labels identify the actual
decision, why the proxy fails or is equivalent, decisive evidence, and material vs residual risk.
Record disagreement; past Manager acceptances are noisy examples, not authoritative labels.

Proposed pilot floor: at least 30 held-out submissions, including at least 10 critical mismatch
and 10 valid controls across the matrix; zero accepts on the named critical sentinel failures,
critical-mismatch recall >=90%, and false blockers <=10% on valid controls. Report raw counts,
denominators, intervals and per-class failures; this small pilot cannot certify general safety.
Owner confirms meaningful cases and thresholds before activation. Freeze prompts/model/contract
version before evaluation; do not tune on held-out failures and keep calling them held-out.

Knobs: reviewer context, discriminating-check selection, materiality, model, pass/time budget,
and later S1 thresholds. Change one justified factor at a time, maximum two tuning rounds per
evaluation cycle, then escalate a specific unresolved issue. Optimize escaped defects + rescue
turns + false blockers + cost per verified objective, not acceptance rate or number of reviews.
If S1 qualifies through A94/A95, reuse its one runtime and replay output; if it fails, leave it
off and continue the mandatory evidence gate. A96 remains optional with its original gates.
Exit: explicit owner calibration decision, measured verdict and bounded pilot budget. A failed
classifier experiment may close A94 but cannot close A111's unmet objective.

### P6 — Production acceptance on useful work (depends on P4 + P5)

Owner selects one useful bounded engineering objective and one reasoning/research objective
with accessible evidence and agreed resource limits. ORACLE is optional; its specific acceptance
stays blocked until its owner supplies actual contracts/artifacts. Do not conduct market research
or touch protected datasets to demonstrate the harness.

Each pilot has one initial invocation and no rescue prompt. A legitimate reserved decision is
recorded separately from a rescue; report both rather than advertising zero intervention by
excluding inconvenient turns. Include a material rework path plus a sound first-pass acceptance;
the rework case may be a labelled isolated challenge, not deliberately corrupted production work.
Exercise crash/context replacement in isolation first; only inject live faults with explicit
authorization. Record which real production and isolated seams were observed separately.

Evidence follows original request → contract → dispatch → worker result → inbox wake → artifact
inspection/challenge → review → repair if needed → current independent verdict → atomic closure
→ UI/API outcome. Capture deployment versions, actual timings, usage/budget/overrun, operator
touches, and remaining limitations. Observe for 24 hours for duplicate wake/closure or stranded
review obligations; existing A105 acceptance still owns its natural three-completion criterion.

Exit: both useful objectives satisfy their authorized contracts, including negative findings
where appropriate; no fabricated success, rescue dependency or unenforced promised bound. A
genuine blocker produces a safe, useful outcome but does not count as successful-closure proof.

## Service boundaries — mandatory implementation checks

Before closing P1–P4, fill actual measured limits beside each touched receiver/consumer here.
Initial ceilings for the new submission/review work are four active DB mutations and two
concurrent independent reviews globally, one per Case; admission must reject/backpressure
excess rather than accumulate an unbounded in-memory waiter list. Preserve permits until the
underlying DB/tool work ends, even if the HTTP waiter is cancelled.

- **Memory at N=100:** only admitted work runs; no inline artifact contents. Start with a 64 KiB
  serialized submission, <=32 criteria and <=64 evidence refs; oversized contracts require
  explicit decomposition or owner-visible limit change, never silent truncation. Reference
  fetches stream with byte/decompression limits. Measure peak overhead with hostile inputs.
- **Input:** strict typed schema, bounded identifiers/strings/ref counts, allowlisted URI/path
  resolution and no arbitrary command execution. Corrupt/missing evidence gets structured errors.
- **Timeout:** five-second ingress body deadline, bounded DB lock/cooperative query budget,
  asynchronous review with a persisted deadline <=120 seconds excluding authorized queued jobs.
  Timeout leaves a recoverable pending/insufficient obligation; never PASS or unlimited retry.
  Document native IO/provider termination limits, clean process groups, and test lost responses.
- **Backing resources:** unavailable canonical DB means refuse mutation; missing artifact,
  node, model or evaluator means explicit recoverable failure/insufficient evidence. S1 alone
  fails open because it is advisory. No fallback to an unverified success or a second store.
- **Data efficiency:** indexed, bounded batched reads over obligations/revisions; no N+1 per
  artifact/task and no oldest-N event scan as state. Size retention/indexes using measured data.

No new limit is claimed as implemented by this packet. Any deferred boundary requires a concrete
reason and scope note in CONTEXT before its implementation stage can close.

## Implementation map and verification targets

Use symbol lookup before reading/editing these modules; line numbers above are the inspected
baseline, not a permanent API. New acceptance helpers belong under existing `src/control/`,
not a parallel application. Exact new filenames are chosen in P0's bounded schema/API design.

| Stage | Primary files to read/change | Focused verification |
|---|---|---|
| P0/P1 | `src/control/db.py`, `src/control/routes/cases.py`, `src/control/control_api.py`, `src/orchestrator.py`, `scripts/mcp_manager.py`, `src/core/roles.py`, actual backend/MCP identity propagation | New regression module for contracts/authority plus real temp-DB migration tests; extend `tests/test_case_closure.py`, `tests/test_review_emitter.py` only for affected behavior. |
| P2 | Same closure/review DB/API/MCP seams; generic status/cleanup writers discovered in P0; affected role hints/flag definitions | Targeted acceptance/bypass/race tests; `tests/test_case_lifecycle_unwedge.py`, `tests/test_agent_inbox_delivery.py` where touched; architecture contract on merged HEAD. |
| P3/P5 | `docs/harness/scientific_governor_contract.md`, `docs/harness/roles/scientific_governor.md`, `src/core/roles.py`, existing dispatch/result seams, `tests/test_scientific_governor.py`; reuse `scripts/oracle/governor_metrics.py` fields and A94 replay if present | Offline artifact/challenge and strict verdict-schema tests; separately authorized real reviewer benchmark with hidden labels. No skipped test is a model evaluation. |
| P4 | Existing Case brief/boot, quota/admission/cancel/queue/inbox seams; `web/src/` actual Case/session consumers; relevant backend capability propagation | Select only touched module tests; offline cross-layer fake-carrier faults; pnpm related Vitest/typecheck/build for touched UI. Read-only health and deployment version evidence. |
| P6 | Existing deployment/observer tools and the selected objective's own artifacts | Explicit owner-authorized pilot; timestamped DB/API/UI trace and 24h read-only follow-up. No blanket pytest/e2e run or paid proof turn. |

Run `.venv/bin/pytest <explicit-targets> --tb=short` using the targeted-test selector and
inspect its collection before execution if a module's isolation is uncertain. Never run the
full suite, paid CLI or real e2e to validate these stages. Existing test guards remain intact.

## ACCEPTANCE — whole dispatch, not merely its substeps

1. P0–P6 evidence is attached, deployed revisions identified, A102/A105 original gates accounted for.
2. Contract revision and evidence identity bind every acceptance; raw owner intent survives
   interpretation, authorized revision, restart and context replacement. Missing provenance is visible.
3. Author self-approval, shared-token role spoof, stale review, unrelated verdict, waived-away
   blocker, cleanup and legacy/generic writes cannot produce protected successful closure.
4. Execution completion/inbox consumption remain functional before review; quota/cancel/unknown
   outcomes cannot silently satisfy acceptance. Fresh DB reconstructions preserve obligations.
5. Real discriminating checks distinguish objective satisfaction from correct-looking substitution;
   valid first-pass, negative and equivalent-prior-work controls pass without ritual extra work.
6. Deterministic fault/race/migration checks pass; separately measured reviewer behavior meets the
   owner-agreed P5 thresholds. Offline wiring tests are never presented as model-quality evidence.
7. P6 useful-work observations demonstrate the original hands-off objective and honest bounds.
8. Single closer/queue/inbox/reviewer/S1 runtime; old count heuristic retired; projections and
   existing specs agree. Failed optional S1 work creates no duplicate mechanism or closure blocker.

## RESERVED DECISIONS — engineering vs creative/empirical

| Decision | Owner/default | Work that can continue |
|---|---|---|
| ORACLE's actual question, legitimate equivalence and scientific validity | Operator/domain owner; supplied prose examples are illustrative, not inspected incidents. | Generic contract, artifact-backed illustrative tests, authority and recovery engineering. |
| Material objective ambiguity or change of scientific target/acceptance | Operator, or explicitly delegated revision authority. Preserve original until resolved. | In-scope reversible work; no quiet narrowing. |
| Reviewer label disagreements and acceptable false-block/escape tradeoff | Operator calibrates P5 examples/thresholds; proposed floor above is not an empirical result. | P0–P4 isolated work and bounded benchmark preparation. |
| Worker restart/promotion | Existing operator decision, drain and ancestry evidence required. | Gateway-only changes and isolated tests. |
| Paid model evaluation, provider egress, production protected-Case enrollment and pilot limits | Operator approves concrete model/data/budget proposal; no authorization inferred from a planning request. | Offline tests; preserve A94 key/DB-copy and existing egress reservations. |
| Deterministic schema, transaction/idempotency design, single-path integration and truthful UI | Implementer owns within these invariants. | Execute without repeatedly asking routine design questions. |

Creative iteration is allowed on hypotheses, experiments and repair strategies within the
agreed question; it does not authorize relabelling a substep as the objective. A reviewer finding
is a contestable evidence claim, not domain truth. Resolve genuine disputes once with inspected
evidence, then escalate a material disagreement instead of looping reviewers.

## TYPE / SCOPE OUT

The current change authors and registers this plan only. Runtime implementation is the staged
work above, with normal code PRs and checks. No speculative outer Governor/campaign runtime,
new message bus, scheduler, job-status store, generic role factory, model-selection classifier,
ORACLE research, hidden historical relabelling, or claim of infallible semantic verification.
No cross-project files are available or presumed. No worker restart, paid run or production
activation is authorized merely by creating this packet.

## End-state contract

This checks retirement of the known competing activity heuristic and table ownership. It is
necessary but not sufficient: P0's writer map and P2/P4 bypass/integration tests establish the
behavior the static checker cannot. Extend the contract to actual added acceptance tables in
P1 without weakening these assertions. Fixture seeding in `tests/` is not a production writer.
The existing offline `backfill_stale_cases.py` is permitted only for its inspected legacy
cancellation path, never evidence of successful closure; P0 must preserve that distinction.
No empty store rule is evidence that a gate exists.

```arch-contract
scope: [src, config, scripts, tests, docs/harness/roles]
retired: [manager_advancement_gate_enabled, MANAGER_ADVANCEMENT_GATE, exhaustion_attestation]
stores:
  flow_runs: {writers: [src/control/db.py, 'tests/*', scripts/backfill_stale_cases.py]}
  flow_events: {writers: [src/control/db.py, 'tests/*', scripts/backfill_stale_cases.py]}
```

## TRAIL / EVIDENCE

Keep lifecycle, decisions and burndown in this packet. Put necessary separate evidence under
`evidence/A111_<stage>_*`, linked both ways. Evolve the existing Scientific Governor contract
for durable design; do not create a competing intent-gate spec or another roadmap.
Record stage PRs, targeted commands/results, baseline/current revisions, fault traces, benchmark
inputs/hidden labels/output identities, owner decisions, pilot timelines and cross-layer limits.
Never list a report's existence as proof of the underlying scientific claim.

Planning verification is recorded below; it does not satisfy runtime acceptance. `status` remains
ready until implementation starts and cannot become done merely because the plan is committed.

### Planning verification — 2026-11-10

- Existing offline baseline: `.venv/bin/pytest tests/test_case_closure.py tests/test_review_emitter.py tests/test_scientific_governor.py --tb=short`
  → **51 passed, 7 skipped** in 9.71s. Skips are the seven unwired live reviewer cases; there
  were two dependency deprecation warnings. This is baseline mechanics, not intent-gate proof.
- Dispatch registry: 111 packets, 111 state blocks, no missing-state/proof/stale-active gaps;
  A111 remains ready. Generated views are produced by the existing tool/hook.
- Architecture contract is intentionally unmet on baseline HEAD because the count heuristic
  and exhaustion attestation still exist (30 retirement occurrences across the declared scope);
  implementation must retire them before A111 is done. The static contract does not certify
  semantic review or authorize direct DB edits.
- No ORACLE incident artifacts were supplied during authoring. Existing illustrative fixtures
  and the user's examples do not become verified historical cases through inclusion here.

## Milestone (burndown)

- [ ] P0: complete writer/bypass map; baseline failures and valid controls; benchmark contract.
- [ ] P1: immutable provenance, authorized revisions, typed evidence, scoped authority, migration.
- [ ] P2: atomic current-evidence acceptance/closure; per-obligation rework; bypass tests; old heuristic retired.
- [ ] P3: independent existing reviewer integrated; artifact-backed challenges; actual opt-in model runner.
- [ ] P4: deployed build/capability reconciliation; A102/A105 evidence; truthful UI and verified limits/recovery.
- [ ] P5: owner labels, held-out measured judgment, bounded tuning and activation decision.
- [ ] P6: two useful objectives, no rescue dependency; separate isolated fault evidence; 24h observation.
- [ ] Final: architecture check, targeted tests, deployments, known limits and operator touches reconciled.

## Closure (fill only after the objective is achieved)

Open. A blocked promotion, missing domain evidence, failed quality benchmark or unobserved natural
completion stays visibly open. A stage may finish; the dispatch closes only on whole-objective
acceptance above, not on a commit, test count or an approved-looking report.
