```yaml
job_id: 113
created_at: "2026-11-10T14:15:31.501639+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A113             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["scripts/prototypes/consequential_decision.py", "tests/test_consequential_decision_prototype.py", "docs/harness/consequential_decision_control.md"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-13T13:40:19.238775+00:00"
```

# DISPATCH — A113 · Design and prove a consequential-decision control prototype

**Level:** 3 (harness policy, authority, cross-layer workflow, and a bounded runtime prototype).
**Type:** bounded investigation + one offline vertical-slice prototype + adversarial verification + concise integration design.
**Authored:** 2026-11-10, Europe/Vilnius. **Status:** prototype proven offline on 2026-11-13; production integration is A114.
**Depends on:** A112 is complete and provides the failure-mode definition/current-seam assessment. A111 is active/planned: coordinate with its intent/evidence-bound closure contract and reuse it; do not wait for A111 if the prototype can be built as an isolated, compatible slice. Verify all packets and code against current `main` before relying on them.

> **This is the actual work.** The operator does not want another short opinion or a generic
> approval prompt. Act as lead harness engineer: understand the failure, review available project
> examples, decide where and when a control belongs, design a reusable control contract, build one
> bounded project-relevant prototype, prove it catches the intended failure without becoming a
> blanket harness hurdle, and leave a concise design and evidence-backed path for scaling.
> Stop after that prototype and scale-up design. Do not implement the full generalized rollout.

## Lead-engineer review and execution order (2026-11-11)

**Judgment:** preserve the objective; reduce the upfront machinery. The root failure is the
promotion of an unexamined provisional value into authoritative downstream use. A generic
approval prompt or a gate only at Case closure cannot prevent that earlier consumption.
Prefer an explicit project/workflow declaration and a deterministic check at the canonical
consumer's admission/promotion boundary. Task-local policy may add requirements but cannot
remove inherited ones. Semantic discovery/explanation stays advisory; no new dynamic role is
needed for the first proof. The existing Manager proposes; an authorized owner or scoped
delegate decides; the existing review/closure flow assesses objective satisfaction separately.

**Verified constraints:** local code at `ab851e8` has no general revision-bound decision gate;
A111's scoped authority remains planned. `src/control/routes/admin.py` forwards body-supplied
`resolved_by` to `ApprovalService.resolve`; this is attribution, not verified principal identity.
`db.create_approval` catches write failures without reporting failure to `ApprovalService.request`;
Case links/events are best effort; the approval callback runs after resolution commits. Therefore
reuse of approvals alone does not prove authority, atomic persistence, or crash-safe execution.
The known `origin/main` successor `77823e7` changes worker startup/tests, not these seams; refresh
and recheck the actual implementation baseline before code work. Preserve the existing dirty tree.

Execute these steps in order; do not complete a general framework before testing the hypothesis:

1. **Choose one bounded consumer.** Inspect accessible project metadata and the named examples,
   then name the setting, canonical resolver, authority source, promotion/use operation and all
   routes into that operation. Prefer a real safe consumer; if access or identity makes that
   misleading, use one isolated fixture. Do not search unrelated/private project contents.
   Keep the other example as a transferability check, not a second implementation.
2. **Establish the counterexample first.** In a temporary workspace/DB, demonstrate that an
   inherited default can produce an authoritative artifact without an informed decision. Define
   the expected block and valid controls before implementing the guard. No paid execution.
3. **Build the smallest complete slice.** One declaration, immutable explained proposal,
   authorized decision, existing approval substrate where suitable, and one guarded consumer.
   Use a deterministic local evaluator if meaningful. Prefer a rendered packet and executable
   offline scenario over a new UI, endpoint, role, scheduler or registry. Document any minimum
   added persistence and its owner; do not rely on best-effort audit links for enforcement.
4. **Try to break the real boundary.** Exercise direct/alternate calls, changed effective config,
   spoofed identity, restart/retry, conflicting resolutions and persistence failures. Verify the
   consumer uses the exact approved snapshot rather than re-resolving a different value after
   checking. A label saying sandbox is insufficient: isolate output and block its promotion.
5. **Report evidence and stop.** Supply one repeatable offline command, observed outputs and
   assertions, and the small integration design below. Separate prototype proof, design-only
   claims and unresolved production dependencies. No activation or generalized rollout.

**Proof boundary:** trusted test principals may prove the decision state machine, but cannot
prove production caller isolation. If the chosen seam only has shared bearer auth, use the
executable-simulation allowance below and explicitly leave production authority unproven;
do not add a competing authentication stack to avoid that limitation. A human owner may choose
a value they proposed; reject an agent approving its own proposal without the required distinct
authority. Scoped delegation is explicit, current and revocable, never inferred from a role name.

**Cost and claim limits:** keep design and evidence in the existing packet plus one durable
reference design; do not create a document per concern. Record fixture counts/expected blocks
and decision steps; report human review time only if actually observed. A small challenge set
checks coverage of examples and near-misses; it does not establish semantic detection quality,
human comprehension, scientific validity, brand suitability or seamless production integration.

## Operator intent — preserve this objective

The failure is **consequential configuration silently inheriting a provisional default** (FM-1 in
[`docs/harness/AGENT_FAILURE_MODES.md`](../../docs/harness/AGENT_FAILURE_MODES.md)): an agent uses
a convenient/default/inherited value to keep work moving; that value silently becomes the premise
of downstream work, without an informed decision by an authorized party or suitable evaluation.
Execution can look successful relative to the accidental value while failing the owner's real
objective. Examples include loss/objective coefficients in a training workflow and foundational
palette tokens in a visual-first UI. The general issue applies to any consequential parameter,
not just numbers or color.

This control must be available when a project/workflow needs it, without making every Case, review,
or ordinary task stop for human approval. Determine how a project declares the need, how agents
surface/explain candidate decisions, how authorized humans or explicitly delegated authorities
decide, how downstream work is blocked when a required decision is missing or stale, and how
evaluation is captured where meaningful. Assess deterministic declarations/guards against
semantic discovery/explanation; determine whether a dynamic role helps and what authority it may
or may not hold. Build one representative prototype and prove its behavior. The prototype is a
test of the control design, not a claim that all projects or all consequential choices are solved.

## Grounding packet — read before design

Read these first and inspect the referenced current code, not just the prose:

1. `.ai/CONTEXT.md`, `.ai/dispatch/DISPATCH_LOG.md`, `.ai/DOC_MAP.md`, and this repo's latest
   `git status` / recent history.
2. FM-1: `docs/harness/AGENT_FAILURE_MODES.md` — failure statement, examples, control contract,
   critique, current practices, references.
3. A112: `.ai/dispatch/AGENT_112_CONSEQUENTIAL_DECISION_CONTROLS.md` — verified current seam map
   and bounded recommendation. Its important caveats: Case-level operator approval is not
   per-value informed certification; runtime flag typing/source does not record informed choice;
   Governor review is not a universal knob certifier; no general visual-first editor exists.
4. A111: `.ai/dispatch/AGENT_111_INTENT_ALIGNED_VERIFIED_CLOSURE.md` and
   `docs/harness/scientific_governor_contract.md` — preserve one acceptance authority and reuse
   planned evidence-bound review/closure. Verify actual implementation status in code.
5. Relevant current implementation/contracts: `src/orchestrator.py` Case open/review/close;
   `src/control/db.py` Case, approval, and runtime-flag records; `src/services/approval_service.py`;
   `src/core/roles.py`; `src/backends/tool_profile_source.py`; `docs/harness/operating_model.md`;
   `docs/harness/dispatch_pipeline.md`; `docs/harness/loop_config_map.md`;
   `docs/backend/ENV_FEATURE_FLAGS.md`; `docs/harness/roles/manager.md` and `worker.md`.
   Use `scripts/repo_index/symbol_lookup.py --defs-only <Symbol>` for named symbols before broad
   file reads. Verify seams, status, and line references against current `main`.
6. External starting points (use current authoritative sources and cite relevant claims in the
   design): NIST [AI Risk Management Framework 1.0](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf),
   MLflow [Tracking](https://www.mlflow.org/docs/latest/tracking/), Google SRE
   [Testing for Reliability](https://sre.google/sre-book/testing-reliability/), Anthropic
   [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents), and
   OpenAI [A Practical Guide to Building AI Agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-agents/).
   Explain what each source actually contributes; do not cite them as proof that this exact design
   is standard or solved.

## Required investigation and design

### 1. Re-establish the problem in concrete cases

- Trace the two named examples end to end: a training/loss configuration and a visual palette or
  equivalent project configuration. Identify decision owner, parameter provenance, downstream
  consumers, plausible alternatives, failure consequence, and what would count as evidence of a
  good/bad choice.
- Inspect accessible project/workspace context for a suitable real pilot. Compare candidate
  projects by relevance, access, reversibility, measurable outcomes, and cost. Prefer a real
  project context if one is available and safe. If none is accessible, use an isolated representative
  fixture in AI-Team and state that limitation; do not block the design waiting for a project.
- Separate “the agent needs a value to run a sandbox experiment” from “the value is certified as
  the authoritative downstream baseline.” Prove the prototype preserves this distinction.

### 2. Decide the control placement (answer all alternatives)

Evaluate and recommend a clear policy among these, including a layered combination if warranted:

- universal gate on every Case or review;
- workflow/project opt-in policy with per-setting or per-decision declarations;
- task-local gate for one decision;
- a general harness capability available to workflows but dormant unless invoked;
- a dynamic agent/role that discovers and explains consequential decisions;
- deterministic schema/policy declarations and runtime guards, with semantic discovery as an
  advisory/escalation layer.

Address whether a dynamic role is needed, where it fits the existing Manager/Worker/Governor
division, how it avoids self-approving its own proposal, and whether it owns a decision or only
surfaces one. Explain how controls compose when project policy, task policy, and global safety
constraints differ. Explicitly identify the default behavior when no project opts in: ordinary
work continues unchanged, except any existing global safety/authority rules.

### 3. Specify the reusable contract

Design the minimum reusable concepts and lifecycle, grounded in existing stores/services where
possible. At minimum resolve:

- **Declaration/materiality:** how a project marks consequential settings and affected objectives/
  downstream actions; deterministic known-field declarations vs semantic discovery; unknown or
  ambiguous cases; risk tiers and escalation threshold.
- **Decision packet:** setting/value, source (owner/policy/inherited/provisional), current and
  proposed alternatives, understandable effects and interactions, uncertainty, affected scope,
  and who is authorized to decide. Do not equate a bare approval click with informed choice.
- **Authority and record:** human or explicitly delegated authority; evidence of what explanation
  and revision they saw; accept/change/delegate/reject; immutable or append-only history; actor and
  timestamp. Specify when another agent may decide and how its authority was delegated.
- **Gate and lifecycle:** exact point before authoritative downstream use; required/missing/stale/
  rejected states; safe exploratory mode; bypass policy and audit; timeout/recovery; no silent
  fallback into a production default.
- **Version/invalidation:** bind choice to setting schema/value, objective/policy revision, and
  relevant evidence. Declare the dependency set explicitly; do not hash the whole repository.
  Invalidate only affected decisions/evaluations on a material change. Bind approval to the
  explanation and evidence available at decision time; bind later evaluation results to that
  approved input snapshot and evaluation-plan revision. Adding a result must not circularly
  invalidate the choice it evaluates. Recheck current delegation/revocation at authoritative use.
- **Evaluation:** required only when the project has a meaningful test; project-specific plans such
  as loss/holdout metrics, palette contrast/accessibility checks, cost/safety bounds, or acceptance
  criteria; explicit reason when evaluation is not applicable. Approval is not evaluation.
- **Operator experience:** show consequential choices, consequences/alternatives, uncertainty,
  impact, and exact blocked dependency in the project's natural surface. Reuse current UI/Case/
  approval surfaces where they fit; do not build a generic configuration dashboard without evidence.
- **Observability:** decisions proposed/approved/rejected/changed, stale records, blocked work,
  evaluation results, reversals/incidents, false blocks, missed consequential choices, and review
  burden. Preserve privacy and avoid logging secrets or sensitive payloads.

### 4. Choose and build one prototype

After the investigation, select the narrowest prototype that can answer the architectural question
with evidence. Record why it is representative and what it cannot prove. The prototype must
exercise the full vertical slice: declare/identify a consequential setting → present an
understandable decision packet → capture an authorized decision → enforce at the chosen downstream
boundary → run/record applicable evaluation or explicit N/A → demonstrate stale/change behavior.
Include a non-opted-in control case that proceeds without the new gate and a provisional sandbox
case that can explore without promoting its value as accepted.

Distinguish an explicitly unconfigured project from an opted-in project whose policy cannot be
read or is malformed: only the former takes the compatibility path. Resolve effective values
through the existing canonical source, including defaults/overrides; a proposed payload is not
proof of what the consumer uses. Make freshness check and durable admission atomic, or consume
an immutable admitted snapshot with a defined change/cancellation rule. Approval timeout,
rejection, cancellation and Case cleanup must never imply certification. Persisted approval is
not a successful effect: prove retry-safe admission after a crash between decision and use,
without claiming exactly-once external execution from the existing callback.

Prefer existing Case/approval/config authorities and schemas. No second queue, duplicate Case
closer, competing reviewer, or shadow configuration source. If a minimal new record/service is
necessary, justify ownership and compatibility; keep the feature opt-in and bounded to the pilot.
Do not enable the prototype for all projects or production workflows by default.

If implementation changes `src/` or config: use one `feat/a113-<slug>` branch and PR; verify the
working tree before branching and do not carry other edits. Follow repository branch/merge rules.
Test only touched modules with plain targeted `pytest` and appropriate frontend/static checks; never
run full/e2e or a test that can invoke a paid CLI. For any new endpoint/handler/receiver, include the
service-boundary checklist in the design and implementation. Do not mutate live project settings,
run a paid model/training job, publish a palette, or restart a worker without explicit authorization.

## Verification and acceptance

The prototype is accepted only with reproducible, offline evidence for all of these behaviors:

1. **Opt-out compatibility:** a project/workflow without this control follows the existing path;
   no global Case/review friction is introduced.
2. **Required decision blocks:** a declared consequential setting cannot enter authoritative
   downstream use without a decision by the permitted authority; blocked reason identifies the
   setting and responsible decision point.
3. **Informed decision content:** the decision surface contains value/source, meaningful
   alternatives and effects, scope/revision, uncertainty, and the available evaluation plan.
4. **Authority cannot be forged:** unauthorized principal/role and agent self-approval without
   required independent authority fail closed; a specifically delegated decision works only within
   its declared scope and validity. State whether caller authentication is exercised or simulated.
5. **Staleness is handled:** changing the value, objective, schema, or relevant evidence invalidates
   only the affected certification/evaluation; unrelated changes do not cause blanket re-approval.
6. **Exploration remains possible:** a clearly provisional sandbox run is allowed, marked
   provisional in artifacts/read models, and cannot be confused with accepted baseline output.
7. **Evaluation is distinct:** passing a decision gate does not mark evaluation passed; meaningful
   evaluation results are bound to the choice/revision, while explicit N/A is visible and justified.
8. **Recovery and audit:** restart/retry/replay does not duplicate approval or silently lose the
   gate; decision history, actor, revision, and outcome remain inspectable.
9. **Negative and adversarial cases:** malformed/unknown setting, stale authority, concurrent
   conflicting decisions, partial persistence failure, missing explanation, and bypass attempts
   have explicit expected behavior and regression coverage. Include policy-load failure, revoked
   delegation, effective-value overrides, change between check and use, and attempted sandbox
   artifact promotion; assert absence of the prohibited consumer effect, not just an error label.
10. **No harmful overreach:** demonstrate a routine non-consequential setting and an unconfigured
   project proceed normally. Report false blocks over the enumerated fixtures and the number of
   decision steps; distinguish observed human burden from estimates. Do not claim semantic
   recall/precision from a handful of fixtures.

Use deterministic tests for schemas, authority, version binding, gate behavior and persistence.
For semantic materiality/explanation, create a small labeled challenge set from the concrete
examples and plausible near-misses, report limitations and disagreements, and do not make a model
classifier the sole blocker until calibrated. If semantic behavior cannot be credibly assessed in
this job, keep it advisory and state the next evidence required.

## Scale-up design and stop point

Deliver a concise reference design for generalization: architecture/data flow, ownership and
state model, policy declaration format, API/UI seams, permissions, migration/backward compatibility,
rollout and rollback, security/privacy, service boundaries, performance/concurrency bounds,
failure recovery, observability, test/evaluation plan, operator workflow, and staged rollout with
explicit go/no-go evidence. Classify each feature as prototype-proven, design-only, or unresolved.
Include a decision table for universal vs opt-in controls and deterministic vs semantic controls.
Cover these concerns together where they share a mechanism; no separate subsystem or exhaustive
future implementation specification is required. Name the exact next consumer/authority seam and
the evidence needed to integrate it with A111; compatibility by design is not integration proof.

**Stop after** the prototype passes its stated acceptance checks and the scale-up specification,
risks, and next implementation gates are complete. Do not turn the prototype into a universal
framework or roll it out across projects in this dispatch. If evidence shows the prototype design
is wrong, fix/re-scope within this objective rather than report a paper design as success.

## Deliverables

1. A durable design/spec in `docs/harness/` that supersedes/extends FM-1 with tested policy and
   architecture. Update the failure-mode ledger with the proven control and remaining blind spots.
2. A working, opt-in prototype integrated with existing AI-Team authority surfaces, unless the
   investigation proves a code prototype would misrepresent the target; if so, deliver the smallest
   executable simulation and justify the decision.
3. Focused regression tests/evaluation fixtures and exact reproducible verification commands/results.
4. An evidence-backed scale-up plan and explicit stop/activation conditions.
5. Update this packet's Milestone/Closure, evidence state, DISPATCH_LOG row, and `.ai/CONTEXT.md`
   only if there is a genuine new active priority. Record dependencies and deferrals honestly.

## SCOPE OUT

- Universal mandatory certification on every Case, review, configuration field, or project.
- Full visual-first configuration editor or project-wide role/tool registry redesign.
- General semantic classifier presented as reliable without measured calibration.
- Production activation or migration of existing project settings.
- Paid model training/inference as proof; do not use a real training run unless separately approved.
- Replacing A111's acceptance/closure authority or creating a parallel queue, closer, or Governor.

## Milestone (burndown)

- [x] Lead-engineer packet review: narrow proof-first execution, verified approval/identity gaps,
      consumer-boundary enforcement, snapshot/promotion safety and explicit simulation limits.
- [x] Re-ground FM-1 and current AI-Team seams; resolve contradictions against code/git.
- [x] Inspect candidate project contexts and select/justify representative pilot or isolated fixture.
- [x] Compare universal/opt-in/task-local and deterministic/semantic/role-based placements; record recommendation.
- [x] Complete control lifecycle, state/authority/revision/evaluation, UX, risk, and scale-up design.
- [x] Implement one opt-in end-to-end prototype or justify an executable simulation.
- [x] Add adversarial regression/evaluation fixtures and run only targeted safe checks.
- [x] Demonstrate opt-out, required block, informed authorization, stale revision, exploration,
      evaluation distinction, recovery, audit, and non-overblocking.
- [x] Publish evidence-backed scale-up/rollout plan; stop at the prototype boundary.
- [x] Close with exact results, known gaps, and operator decisions (if any).

## Trail / evidence

Starting doctrine: [`docs/harness/AGENT_FAILURE_MODES.md`](../../docs/harness/AGENT_FAILURE_MODES.md).
Prior code map: [`AGENT_112_CONSEQUENTIAL_DECISION_CONTROLS.md`](AGENT_112_CONSEQUENTIAL_DECISION_CONTROLS.md).
Add implementation evidence under `.ai/dispatch/evidence/` only when needed and link it both ways.

## Closure — prototype proven (2026-11-13)

**Result:** accepted within the packet's explicit executable-simulation allowance. The real
palette setting/consumer does not exist in AI-Team and shared dashboard/worker authentication
cannot certify an independent owner. The minimal safe pilot is one isolated palette fixture,
not a fake claim of gateway integration. Production sources were rechecked on `77823e7`;
the original dirty `ab851e8` checkout, other worktrees and live sessions were preserved.
The supplied A112 packet and failure-mode ledger were untracked at baseline; they are included
as grounding dependencies, not recreated investigations. Retired Level-3 guard claims were
corrected in the pipeline/config-map docs rather than used as an imaginary protection.

**What changed:** `scripts/prototypes/consequential_decision.py` implements one canonical
fixture resolver and SQLite CSS publisher, immutable explained proposals, trusted test
principals, owner/scoped delegated decisions, material generations, separate contrast/N/A
evaluation and an atomic publication guard. One test module exercises actual persisted bytes;
one [durable reference](../../docs/harness/consequential_decision_control.md) owns policy,
architecture, authority limits, service bounds, challenge labels and rollout requirements.
No production import, endpoint, schema migration, setting mutation, deployment or paid run.

**Root cause and baseline:** the unguarded fixture promotes inherited `#777777` into a
persisted CSS bundle without any decision. Its baseline test passed before implementation
(`1 passed in 33.86s`). The matching guarded default fixture produces no artifact without
approval; even an approved gray packet fails its separate white-text contrast evaluation.
An approved navy snapshot produces exactly the stored packet's color and revision in CSS.

**Diagnosis/fixes:** the first combined prototype run exposed an overly short 250ms SQLite
lock wait; use the repository's five-second lock budget and guarded terminal decisions.
Review found value-change/reversion could resurrect certification; a generation now advances
only on the declared material dependency set. Initial process-death checks encountered mixed
module versions because code was edited during that run; fixed-source reruns below prove both
crash points. SQLite initialization also avoids `executescript`'s implicit commit. Deferred
commit failure, conflicting resolutions, concurrent retries and revoked/reissued grant attacks
are regression-covered. No unresolved critical/high defect remains inside the stated boundary.

**Verification (offline, repo venv):**

```bash
.venv/bin/pytest tests/test_consequential_decision_prototype.py --tb=short
```

Final essential proof: **51 passed in 6.23s**, fresh temporary state after test type annotations;
the same essential proof also passed in 17.23s. Earlier complete proof:
50 passed in 39.40s. Independent repeat with existing approval/flag compatibility checks:

```bash
.venv/bin/pytest tests/test_consequential_decision_prototype.py tests/test_approval_service.py tests/test_runtime_flag_typed.py --tb=short
```

**92 passed, 2 warnings in 27.47s** before the final additional scoped-grant negative test.
The warnings are dependency deprecations from FastAPI/Starlette/httpx/AnyIO. No full/e2e suite
was launched locally. Target selector resolves the prototype to this single new test module.
Ruff and Black checks pass for both new Python files; mypy with
`--explicit-package-bases --follow-imports=skip scripts/prototypes/consequential_decision.py`
passes. `git diff --check` passes. `rg` confirms no prototype import in `src/` or `config/`.

**Consumer evidence:** tests assert no forbidden artifact after unauthorized, missing, stale,
expired, rejected, cancelled, malformed and failing-store attempts. They inspect committed CSS
and decision rows after reopening; kill real Python child processes after artifact insertion
before commit and after commit; verify rollback/retry and one durable effect. Both executable
routine/opt-out controls have zero false blocks. Configured flow needs one explicit decision;
human review time/comprehension was not measured. Seven advisory challenge labels are not a
semantic quality benchmark. No mocks substitute for the publication effect.

**Limits:** test principals simulate caller isolation. The fixture DB/file/host is trusted;
there is no remote publisher, native SDK boot, production policy enforcement, UI journey,
semantic detector or scientific/brand correctness claim. Historical receipt retrieval does
not reexecute or retroactively undo a committed effect. Native I/O/power-loss behavior and
production N=100 bounds remain integration gates, not properties proven here. No new runtime
handler exists, so service boundary exposure is not deferred under a live endpoint.

**Registry audit:** strict audit reports the pre-existing A67 private-security proof-path gap;
no missing state blocks or new proof gaps. Do not publish private A67 evidence to silence it.

**Continuation:** after the corrected proof and repeat/regression run passed, created
[A114](AGENT_114_CONSEQUENTIAL_DECISION_INTEGRATION.md). Its three stages finish owner authority,
canonical SDK budget → immutable boot specification → actual worker/SDK consumption, then
operator recovery/observability and safe deployment. It defines overall workflow completion,
coordinates A111/A71/A75, and must not rebuild this local mechanism or add a parallel queue.
A114 is authored only, not implemented in this session.

**Delivery:** implementation commit `a7f7cd6` and final review/type-annotation follow-up on
`feat/a113-decision-control`, [PR #285](https://github.com/nydiokar/AI-team/pull/285).
