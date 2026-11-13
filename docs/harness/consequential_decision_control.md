# Consequential decisions: A113 proof and integration contract

2026-11-13. Scope: **offline executable simulation**, not an activated gateway feature.
Implementation: `scripts/prototypes/consequential_decision.py`; proof:
`tests/test_consequential_decision_prototype.py`. Parent:
[A113](../../.ai/dispatch/AGENT_113_CONSEQUENTIAL_DECISION_CONTROL_PROTOTYPE.md).

## Failure, pilot and placement

FM-1 is promotion of a provisional premise into authoritative use. Case closure is too late
to stop that consumption. An approval record alone proves neither identity nor execution.
The baseline test persists a CSS bundle using inherited `#777777` with no decision. This is
a reproduction in a representative fixture, **not a discovered production palette bug**.

The chosen consequential setting is the accent background behind white button text.
The fixture's canonical resolver is `State.accent` (project override) → `#777777` (default).
The sole downstream operation is `PaletteConsumer.publish`, which writes actual CSS bytes to
the authoritative artifact table. Its direct, sandbox, duplicate and retry paths share this
writer. There is no alternate artifact import/export/promotion API. SQLite is the effect,
not a mock of an external publisher. No generated bundle is served by the real UI.

Accessible AI-Team metadata and A111 describe ORACLE as a domain integration, with no supplied
owner identity, evaluation contract or incident artifacts. Other workspace directory names do
not grant permission to inspect private contents. Current gateway settings are accessible,
but shared authentication and remote worker boot make an apparent production approval unsafe.
The palette fixture is reversible, offline, cheap and has a deterministic numerical check.
An actual SDK budget would be more operationally relevant but cannot be exercised live without
crossing paid execution/worker boundaries. No private project or paid session was inspected.

| Placement | Judgment |
|---|---|
| Universal Case/review gate | Reject: blocks routine work and misses earlier consumption |
| Project/workflow opt-in declaration | Choose: material settings and consumer named explicitly |
| Task-local declaration | May strengthen inherited requirements; cannot switch them off |
| Dormant harness capability | Integration target, not a new registry or global workflow |
| Dynamic discovery role | Unnecessary; existing Manager explains, existing owner decides |
| Deterministic guard + advisory semantic review | Choose: enforce known dependencies; escalate ambiguous materiality |

Project requirements combine with task requirements and existing global safety constraints by
addition, never by a task override that removes inherited protection. No declaration means the
existing path. A declared policy that is missing, malformed or unreadable fails closed. This
fixture requires explicit `policy: null` to establish opt-out; absence is an error. An enrolled
fixture cannot remove enrollment through `configure`, even as owner. Production withdrawal
needs an explicit migration/impact decision, not a sandbox switch.

## Tested contract

The host provisions one project DB and a registry of principal capabilities. These are trusted
in-process objects, checked by identity, with unique IDs. Constructing another object called
`owner` does not grant authority. This **simulates authenticated principals**; it is not a new
authentication stack. A malicious Python process with DB/file access or access to the host's
capability registry is outside the boundary. There is no claim of hostile-process isolation.

1. Declare `Policy(setting="accent", revision="policy-1", owner="owner",
   evaluation="contrast-1")`. The other evaluation plan is explicit `na` with a reason.
2. The Manager proposes from the canonical resolver. A frozen packet contains value/source,
   scope, objective/schema/evidence/policy revisions, generation, proposer, expiry, effects,
   at least two meaningful alternatives, uncertainty and evaluation plan. `render` is the
   decision surface. The rendered packet's hash is the revision supplied to resolution.
3. A human owner can accept their own proposal. An agent needs a specific owner-issued,
   revision-scoped, expiring grant and cannot approve its own proposal. Revoking/regranting
   cannot revive the old grant. Decision insertion is terminal and unique per packet;
   conflicting resolutions fail, identical retries retain the original actor/time.
4. Evaluation is independent of approval. The deterministic evaluator computes white-text
   contrast against the packet color, requiring at least 4.5. Navy passes; inherited gray
   has contrast about 4.478 and fails. This checks contrast only, not brand fitness. An N/A
   result retains its reason. Results reference the packet, including the evaluation plan;
   adding a result never changes that packet or invalidates its choice.
5. Publication opens `BEGIN IMMEDIATE`, loads policy, checks current packet/delegation,
   decision and evaluation, and writes CSS from **that packet's value** in the same transaction.
   No second config read supplies execution input. Rejection, cancellation, expiry, absent
   approval, failed evaluation and unavailable storage produce no authoritative artifact.
6. The dependency set is exactly policy, accent, objective, schema revision and evidence revision.
   Material updates advance a generation; reverting a value cannot resurrect old approval.
   A routine note does not invalidate anything. Relevant external evidence must advance its
   declared revision; automatic detection of hidden dependencies is not claimed.
7. Sandbox uses the same resolver but stores `mode=provisional`, no accepted revision. It
   cannot reuse its operation ID for publication, supply artifact bytes, or masquerade as a
   proposal. Authoritative output must be rebuilt through guarded publication.
8. An operation ID identifies one immutable committed effect. Identical retry returns its
   stored receipt; a new revision/mode with that ID conflicts. Later revocation or config
   change cannot undo committed output. Reading an old receipt is not another publication.
   New work needs a new operation and current authorization. Crash before commit rolls back;
   crash after commit replays the one receipt. This proves no duplicate **SQLite** effect,
   not exactly-once execution in a remote system.

Proposals, decisions and evaluations are immutable through the API and survive reopening.
Each decision records actor, revision, grant, outcome and timestamp. Raw SQLite failures
propagate; successful return occurs only after commit. Audit links to Cases are not used as
enforcement. Output artifacts retain revision, value and actual CSS bytes.

## Current architecture and natural integration

Rechecked against `origin/main` `77823e7` on 2026-11-13 (base checkout was `ab851e8`):

| Existing seam | Observed behavior / integration obligation |
|---|---|
| `routes/admin.py` approval resolve | `resolved_by` is body attribution; shared bearer auth in `control_api.py` accepts dashboard or worker token; neither identifies a distinct owner |
| `ApprovalService.request` / `MeshDB.create_approval` | request can report success after a caught DB failure; cannot be the certification record |
| `ApprovalService.resolve` | commits resolution before callback; callback is not durable execution evidence |
| `sdk_governor_ceilings` / runtime flags | registry → env → default; read failures fall back, worker sees cached controller state |
| `ClaudeSDKDriver` session creation → `_SDKSession` → `ClaudeAgentOptions` | budget read is best effort; booted process retains its value; this is the next real bounded consumer |
| `close_case` / `record_review`, Governor contract | objective acceptance is a separate responsibility; A111's revision/authority closure remains planned |
| roles / tool profile source | Manager/Worker boots and least-privilege unknown-role fallback exist; no decision role needed |

The simulation has its own DB because there is no production palette source, safe principal
adapter or atomic production palette publisher to extend. That DB is the sole source **inside
the fixture**, never a shadow of gateway configuration. Do not transplant it as a parallel
runtime store. Production decision metadata belongs in MeshDB next to the existing config and
Case references; approvals may carry attention links only after their durability/identity
contract is made honest. No second queue, closer, scheduler or reviewer is proposed.

The next consumer is **new Claude SDK process creation for an opted-in project with a declared
`CLAUDE_SDK_MAX_BUDGET_USD` decision**. Use the existing canonical budget resolver, including null
and precedence. Bind authorization to the immutable boot specification that the worker actually
consumes. Worker snapshot staleness, fallback and broad exception handling must not erase a
configured requirement. An unconfigured project retains existing behavior; existing live SDK
processes are not silently restarted or retrofitted. A111 consumes decision/evaluation receipts
as evidence; it does not infer objective acceptance from them. This is a design, not proof that
the worker/controller identity, remote revision and launch seams are already integrated.

## Bounds, failures, operations and rollout

Prototype: no HTTP endpoint, IPC receiver, worker or queue. Trusted local calls use a single
SQLite writer with a five-second lock wait, close connections on errors, and have bounded
packet fields (2,000 characters each, 2–8 alternatives) and 128-character operation IDs. There
is no network reader or request body. No hard deadline on native disk I/O is claimed. There
is no production N=100 memory guarantee; the simulation is a bounded test driver, not a service.
Storage is temporary; test children have a ten-second deadline and are reaped. Native DB
commit stalls and power-loss durability on real hardware remain outside the process-crash proof.

Before exposing the integration, the service boundary checklist is mandatory: bounded mutation
admission retained through cancellation; N=100 memory measured or bounded; capped request and
explanation bytes; caller/server deadlines; structured validation/errors; and per-request store
failure without silently opting out. Include remote controller failure and stale snapshots.
Use existing route admission/pooling conventions, not a new global scheduler. Native I/O limits
must be stated rather than implied by an asyncio timeout. These are integration gates, not
deferred requirements for an exposed prototype endpoint (there is none).

Keep only non-secret setting metadata in packets. Block reasons identify setting/owner/use
point without payload logging. Production should project pending/stale/rejected decisions,
evaluation failures and original-revision receipts in the existing Case/approval surface;
record blocked attempts and reversal incidents there. The fixture retains terminal decision
history and results but does not implement production telemetry, retention or review-burden UI.

Rollout: migrate metadata additively, no historical approvals synthesized; prove the opt-in
consumer using fake SDK boundaries first; then a controlled operator-selected project. No-go
until production principal separation, worker revision binding and recovery/bypass proofs pass.
Gateway rollout follows the existing Compose deployment skill. Worker activation needs the
operator's explicit decision and drain checks. Rollback must keep enrolled projects held if
the old build cannot enforce their declarations; disabling a gate is not a safe rollback.
No live activation, production setting mutation or restart occurs in A113.

## Proof, challenge cases and limits

Run from a checkout with the repo's normal `.venv` dev setup:

```bash
.venv/bin/pytest tests/test_consequential_decision_prototype.py --tb=short
```

Every run creates fresh temporary DBs. It includes the counterexample, actual persisted CSS,
positive/negative controls, source overrides, stale revisions, corruption, scope/revocation,
concurrent resolutions/retries, before/after-insert failures, deferred commit failure,
real child-process death before/after commit, reopen/recovery and sandbox promotion attacks.
It launches only the Python crash fixture, never an agent, training run or network call.
Exact observed results are recorded in A113's Closure after the final verification.

| Labeled advisory challenge | Expected action / evidence |
|---|---|
| Palette accent inherited from scaffolding | Consequential: publication must stop; executable fixture |
| Palette changed to an explicit override | Consequential: bind approval to effective override; executable fixture |
| Loss weights inherited equally | Consequential if they encode research trade-offs; transfer analysis only |
| Development note | Routine: no renewed approval; executable fixture |
| Unconfigured project with normal defaults | Compatibility: ordinary publication proceeds; executable fixture |
| Plot-only decorative color | Routine if it has no semantic/accessibility consumer; advisory only |
| Temperature used for a scientific comparison | Ambiguous until objective/evaluation dependencies are known; escalate |

These seven labels are engineering judgments, not calibrated semantic predictions. There is
no classifier, recall/precision claim or observed human comprehension/review-time measurement.
Two executable routine/opt-out fixtures must have zero false blocks. The ordinary path requires
zero decision steps; the configured path has one authorized accept action after packet review
(proposal, evaluation and publication are deterministic engineering steps). No human timed it.

Transfer check: in a training workflow, a domain owner would choose loss weights rather than
letting equal defaults become a training premise. The packet must include effects on competing
objectives, coefficient interactions, alternatives and uncertainty. Bind dataset/objective,
coefficient schema and evaluation-plan revisions; admit training using that exact configuration.
Metrics/holdout results are separate from approval. This fixture proves neither training
validity nor the domain owner's preference. The same separation explains why contrast passing
cannot approve a palette's brand identity.

Prototype-proven: local decision lifecycle and sole SQLite consumer boundary. Design-only:
MeshDB/Case/UI integration, SDK boot spec and staged rollout. Unresolved before activation:
production owner authentication/delegation, remote consumption receipts, operational bounds,
semantic discovery, human comprehension and fit for real project objectives.

## External grounding (consulted 2026-11-13)

- [NIST AI RMF 1.0](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf), GOVERN:
  differentiated responsibility and oversight; it does not specify this transaction design.
- [MLflow Tracking](https://mlflow.org/docs/latest/tracking/): separate parameters, metrics and
  artifacts for reproducibility; recording parameters alone does not authorize their use.
- [Google SRE, Testing for Reliability](https://sre.google/sre-book/testing-reliability/):
  test failure/recovery and state explicit confidence limits; passing tests is not universal reliability.
- [Anthropic, Building Effective Agents](https://www.anthropic.com/research/building-effective-agents):
  start with simple predictable workflows. The article now explicitly cautions that its tooling
  discussion has aged; it is rationale for simplicity, not a current framework prescription.
- [OpenAI, A Practical Guide](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/):
  human intervention for consequential actions. It supplies no proof of informed revision-bound approval.

These sources motivate components. The executable evidence, not these citations, supports the
specific A113 behavior.
