# Agent and harness failure modes

This is the durable index for recurring agent/workflow failure modes and the controls that
contain them. Each entry describes an observable failure, why it matters, current defenses,
and the next useful control. It is not a task board; implementation work belongs in a dispatch.

## FM-1 — Consequential configuration silently inherits a provisional default

**Class:** decision/provenance failure; omission masked as successful execution.

**Failure statement.** An agent or workflow must choose a parameter that materially determines
whether the objective or downstream work can succeed. It uses a default, placeholder, inherited
value, or arbitrary starting point so construction or a smoke test can proceed. That value then
survives into later work without an explicit decision by an authorized party who understood its
effect. The task can execute correctly relative to that value and still fail the actual objective.
The defect may stay latent until downstream work is expensive or irreversible.

This is not limited to numeric “knobs.” It includes objective/loss definitions and weights,
thresholds, model/backend/role/tool selection, resource budgets, policy choices, color tokens,
and other choices that constrain later work. A parameter is consequential when a plausible
alternative can change objective satisfaction, safety, cost, access, validity, or downstream
architecture. It is not consequential merely because it is configurable.

**Examples.**

- A training harness inherits equal loss weights as a convenient default. Training runs and
  metrics look valid, but the model optimizes the wrong trade-off because the weights encode
  priorities nobody selected. Recording the run later cannot repair the unexamined objective.
- A visual-first UI starts with a palette token set chosen to unblock component work. Contrast,
  brand, or semantic status colors are never approved; every screen inherits the accidental
  choice, making correction broad and potentially inaccessible.
- A role/tool profile defaults to a permissive or insufficient grant. Work proceeds, but either
  the agent gets authority the operator did not intend or cannot perform its assigned task.

**Why agents fail here.** Local completion pressure rewards making the next step runnable. A
default is cheap, plausible, and often technically valid; the work log then records activity,
not the decision's origin or adequacy. The failure is therefore often an omission, not a model
making an obviously nonsensical choice. Human review can repeat it when reviewers see a value
without its alternatives, downstream consequences, or uncertainty.

### Control contract

1. **Discover and classify.** At task/configuration design time, enumerate decision-bearing
   parameters and classify each as consequential or routine, with a short rationale and the
   affected objective/downstream consumers. Use deterministic schema/policy rules where available;
   use semantic review for context-dependent materiality. Do not claim a universal deterministic
   detector exists.
2. **Explain the choice.** For each consequential parameter, expose the proposed value, source
   (owner, policy, inherited config, experiment, or provisional default), permitted range/options,
   plausible alternatives, and what materially changes across them. Include interactions where
   changing one value changes the meaning of another. A raw number or an “approve” button alone is
   not informed choice.
3. **Bind authority and informed decision.** Record an explicit decision by an authorized
   principal (human operator or a named delegated authority) against a versioned objective/config
   revision. The record says whether the value was accepted, changed, or explicitly delegated;
   it carries the explanation/version seen. Silence, task execution, generic task approval, or
   approval of a whole bundle does not certify every consequential parameter implicitly.
4. **Gate consequential use.** Before dependent execution or downstream artifacts are admitted,
   require the decision record for each applicable consequential value. Missing, stale, or
   unauthorized decisions block that dependency and identify the exact unresolved choice. A
   harmless preview/sandbox may proceed only if it is clearly non-authoritative and cannot be
   mistaken for accepted output.
5. **Evaluate where meaningful.** Attach a project-specific validation/evaluation plan and
   result when outcomes can be measured (for example, holdout metrics, safety checks, contrast
   checks, cost bounds, or acceptance criteria). Mark evaluation not applicable with a reason
   when no meaningful test exists. Approval is evidence of an authorized choice, not evidence
   that the choice worked.
6. **Track change and impact.** Bind approvals and evaluations to value, schema, objective, and
   evidence revisions. A material change invalidates affected downstream approval/evaluation and
   triggers impact review; unrelated edits should not cause blanket re-approval.
7. **Learn from outcomes.** Record predicted effect, observed result, and any reversal/incident so
   defaults and decision aids improve. Do not treat a successful run as proof that the decision
   was informed or appropriate.

### Critique and limits

The proposed controls are directionally sound, with two important refinements. First, “surface
all knobs” would create alert fatigue and turn human approval into rubber-stamping. Surface only
decisions whose plausible alternatives can materially change outcomes, and batch related choices
into a comprehensible decision set while preserving per-value provenance. Second, a human stamp
cannot certify understanding by itself. The explanation and alternatives must be available at the
point of decision; high-impact or poorly understood choices may need a short comprehension check,
relevant domain reviewer, or constrained experiment. Avoid asking users to certify values they
cannot reasonably evaluate.

Deterministic controls are strongest for schema validity, ranges, missing decision records,
authority, revision binding, and whether required evaluations exist. Semantic models can propose
materiality classifications and explain trade-offs, but they are fallible and must not be their own
sole authority for consequential choices. Use policy thresholds and calibrated review for
ambiguous cases; default to an explicit escalation/hold when the potential impact is high and
authority is unclear. Do not block harmless exploratory work if it is isolated, labeled
provisional, and prevented from silently becoming the production baseline.

### Existing practice and relation to AI-Team

This control family is established in adjacent disciplines, though names differ: configuration
change review and staged/canary rollout limit blast radius; ML experiment tracking records
parameters, code versions, metrics and artifacts for reproducibility; AI risk frameworks assign
human oversight and decision responsibility; agent guidance recommends checkpoints, ground truth,
and stopping conditions. These practices support the components above but do not by themselves
guarantee informed approval or prevent a provisional default from becoming canonical.

AI-Team already has relevant pieces: dispatch objective/acceptance criteria, Level-3 authoring
approval conventions (the old runtime guard was removed), durable approval records, typed runtime flags, the Scientific Governor review contract,
and planned evidence-bound Case closure (A111). They do not yet establish a general per-parameter
decision contract. Runtime flags expose values but are not, by themselves, provenance, informed
choice, authority, or evaluation. Do not add a second approval queue or reviewer: map this contract
onto the existing Case/review/approval and configuration surfaces, with a single source of truth
for each decision. A112 records that mapping and its limitations.

**A113 proof (2026-11-13):** the [control contract](consequential_decision_control.md) and
`tests/test_consequential_decision_prototype.py` demonstrate a guarded palette consumer in an
isolated SQLite fixture: required informed packet, trusted host authority, revocable scoped
delegation, exact snapshot consumption, separate contrast evaluation, stale/reverted-value
invalidation, provisional-output isolation and crash/retry recovery. Tests inspect actual CSS
and persisted records, including failure before commit. This is an executable simulation;
production authentication, worker consumption and objective fitness remain unproven. It is
not a new approval queue or an active gateway policy. The natural next integration is the
canonical SDK budget resolver through new SDK process creation, coordinated with A111.

**References (checked 2026-11-10):**

- NIST, [AI Risk Management Framework 1.0](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf),
  including governance, assigned responsibility, and human-AI oversight.
- MLflow, [Tracking](https://www.mlflow.org/docs/latest/tracking/), which records run parameters,
  code versions, metrics, and artifacts for comparison and reproducibility.
- Google SRE, [Testing for Reliability](https://sre.google/sre-book/testing-reliability/),
  including canarying configuration/software changes to limit blast radius.
- Anthropic, [Building Effective Agents](https://www.anthropic.com/research/building-effective-agents),
  on ground-truth feedback, human checkpoints, stopping conditions, and compounding agent error.
- OpenAI, [A Practical Guide to Building AI Agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-agents/),
  on layered guardrails, escalation, and adapting controls to identified risks.
