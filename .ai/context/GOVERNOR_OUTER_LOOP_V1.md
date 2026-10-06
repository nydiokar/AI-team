# GOVERNOR_OUTER_LOOP_V1

**Status:** Canonical design candidate — reconstructed and consolidated from the verified cross-project architecture audit, targeted verification, and the later Manager/Governor/Epistemic-Adversary behavioral design.

**Primary home:** `AI-team` (exact spec directory should follow the repository's existing documentation/spec convention when committed).

**Domain integration example:** `tokens_ingest`.

**Purpose:** Define a generic, durable, event-driven outer loop that governs Manager Cases, can accept semantic events from external projects, preserves project/domain authority, and can progressively replace the human as routine research/programme orchestrator without confusing local task completion with truth.

**Companion spec [S1 addendum 2026-10-05]:** [`docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md`](../../docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md) (`SYSTEM_ONE_DECISION_LAYER_V1`) defines the calibrated System-One layer (TypeSafe Jev). It fills this spec's classifier-shaped slots: §10.3, §15.3/§34, §17, §26. Its integration, and the design conflicts it raises, are summarised in **§37**. Additions marked `[S1]` are owner-authored. Semantic tensions are flagged as DESIGN CONFLICT, never silently applied.

---

## 0. Executive summary

The system should have four distinct reasoning/execution roles:

```text
HUMAN
  sets objective / hard limits / capital / exceptional authority
        |
        v
GOVERNOR
  chooses direction, maintains programme/frontier, allocates attention
        |
        v
MANAGER CASES
  each converges one bounded objective
        |
        v
WORKERS
  execute bounded implementation/research tasks
        |
        v
CANDIDATE CLAIMS / RESULTS
        |
   risk-triggered independent review
        v
EPISTEMIC ADVERSARY
  tries to invalidate load-bearing claims
        |
        v
MANAGER
  resolves / downgrades / integrates
        |
        v
PROJECT EVALUATOR / CANONICAL TRUTH
        |
        v
SEMANTIC EVENT / MANAGER COMPLETION
        |
        v
GOVERNOR wakes and decides what happens next
```

The Governor is **not** defined by `tokens_ingest` events. Its primary abstraction is:

> **A generic outer loop over Manager Cases.**

External project events are only one class of wake condition.

The Governor must be able to:

- create Manager Cases;
- supervise existing Manager Cases;
- wake when a managed Case becomes reviewable/complete/blocked;
- continue the same Case;
- request independent review;
- create successor or parallel Cases;
- park/close work;
- allocate bounded resources;
- wait without consuming inference;
- consume semantic events from external projects through adapters;
- keep durable state outside model conversational memory.

The Governor must **not**:

- dispatch Workers directly in normal operation;
- declare project/domain truth;
- replace project evaluators or deterministic guards;
- mutate live capital or irreversible state unless explicitly authorized;
- continuously consume inference while waiting;
- treat Manager completion as epistemic acceptance.

The primary separation is:

```text
Worker    = bounded task execution
Manager   = bounded objective convergence
Adversary = falsification of load-bearing claims
Governor  = programme/frontier direction and consequences
Human     = intent, hard authority, budget/risk boundaries
```

---

# 1. Why this exists

The current human owner performs two different functions.

## 1.1 Mechanical orchestration

Examples:

- notice work has finished;
- launch the next Manager/Case;
- move context between stages;
- reconnect completed evidence to successor work;
- trigger challenge/review;
- restart research after new evidence appears;
- reallocate effort.

This should largely be automated.

## 1.2 Epistemic judgment

Examples:

- detect that an experiment answered the wrong question;
- detect semantic substitution;
- detect invalid denominator/population selection;
- detect lookahead or causal-time violations;
- detect mismatch between research and serving semantics;
- recognize that "tests passed" does not validate the underlying specification;
- recognize that evidence is valid but insufficient;
- identify when a local result changes the global programme;
- stop downstream work when an upstream premise becomes invalid.

This cannot safely be replaced by mere routing.

The design therefore separates:

```text
ORCHESTRATION
from
EVALUATION / FALSIFICATION
from
PROGRAMME GOVERNANCE
```

Automating orchestration without this separation would only produce incorrect research faster.

---

# 2. Fundamental constraints

Once avoidable human/manual serialization is removed, the system is expected to encounter three fundamental external bottlenecks:

1. **Evaluation quality**
   - Can the system establish what the evidence actually proves?
   - Can it detect that the wrong object/question/population was evaluated?
   - Can it safely return UNKNOWN / INSUFFICIENT rather than forcing PASS/FAIL?

2. **Compute / inference**
   - How much parallel search and review can economically be run?

3. **Real-world validation**
   - What cannot be established synthetically and requires future market time, paper/shadow observation, real execution, physical experiments, or capital?

Human-in-the-loop is not treated as a fundamental fourth bottleneck. It is primarily a consequence of missing outer-loop automation and incomplete evaluator trust.

---

# 3. Evidence-backed existing base

This design intentionally reuses existing machinery rather than inventing a new platform.

## 3.1 `tokens_ingest` already owns research/domain truth

Verified reusable concepts include:

- hypotheses and hypothesis history;
- findings/Atlas/ledger material;
- matrix/frontier state;
- feature and evidence provenance;
- negative knowledge / killed-lane semantics;
- lifecycle/conveyor state;
- RCA/oracle proposal machinery;
- XLI/hypothesis-generation machinery;
- replay and canonical paper/shadow/live paths;
- forward-arm evaluation with `ACCRUE`, `PASS`, `FAIL`, `CONTRADICTION`;
- challenge/assumption-intake machinery;
- paper/live/prospective validation state.

The research project remains authoritative for:

```text
what was tested
what evidence exists
what verdict/evidence tier is valid
what is canonical
what is invalid/quarantined
what may progress to later validation
```

## 3.2 `AI-team` already owns execution-grade agent orchestration

Verified reusable concepts include:

- Worker execution;
- Manager Cases;
- durable Case/flow/task state;
- task leasing;
- manager continuation;
- worker completion handling;
- session reconstruction;
- manager resume/recovery;
- model/backend/thinking-effort selection;
- cancellation/retry/quota handling;
- concurrency controls;
- cost telemetry;
- event streaming.

The harness remains authoritative for:

```text
agent sessions
Cases
leases
retries
continuation
recovery
runtime budgets
model selection
event delivery
```

## 3.3 Missing seam

The missing generic control plane is:

```text
PROJECT DOMAIN STATE / EVALUATION
          |
      semantic events
          |
          v
AI-TEAM GOVERNOR
          |
     Manager Cases
          |
          v
results / references
          |
          v
PROJECT DOMAIN EVALUATION
```

This must be durable, idempotent, restart-safe, and domain-agnostic on the harness side.

---

# 4. Frozen ownership boundaries

| Owner | Authority |
|---|---|
| Project/domain repository | Domain truth, evidence, evaluators, lifecycle, canonicalization, semantic-event production |
| Agent harness | Agent execution, sessions, Cases, dispatch, leases, retries, cancellation, concurrency, runtime budgets, event delivery |
| Governor | Global programme/frontier decisions and Manager-Case supervision |
| Manager | Completion of one bounded objective |
| Worker | Completion of one bounded task |
| Epistemic Adversary | Independent falsification of load-bearing claims |
| Human | Intent, budget/risk boundaries, capital, secrets, irreversible/high-risk authority, exceptional ambiguity |

**Invariant:** Governor may request project actions. It may not directly write project truth.

---

# 5. Target topology

```text
                                 HUMAN
                     objective / budget / authority
                                   |
                                   v
                     +---------------------------+
                     |     GOVERNOR CAMPAIGN     |
                     | logically persistent      |
                     | computationally dormant   |
                     +-------------+-------------+
                                   |
                    +--------------+--------------+
                    |                             |
                    v                             v
              MANAGER CASE A                MANAGER CASE B
                    |                             |
               Workers...                    Workers...
                    |                             |
                    +-------------+---------------+
                                  |
                         Manager result(s)
                                  |
                         risk classification
                                  |
               +------------------+------------------+
               |                                     |
           non-load-bearing                       load-bearing
               |                                     |
               |                                     v
               |                           EPISTEMIC ADVERSARY
               |                                     |
               +------------------+------------------+
                                  |
                               MANAGER
                   resolves / downgrades / integrates
                                  |
                                  v
                        PROJECT-SIDE EVALUATOR
                                  |
                                  v
                       CANONICAL / PROVISIONAL STATE
                                  |
                         semantic project event
                                  |
                                  v
                           GOVERNOR WAKE ROUTER
                                  |
                                  v
                             GOVERNOR wakes
```

External project events are **wake sources**, not the Governor's defining lifecycle.

---

# 6. Governor: core abstraction

## 6.1 Definition

The Governor is a high-level, event-driven control agent responsible for the entire active programme/frontier.

It asks:

> Given everything currently known, are we spending the next unit of research/engineering effort on the highest-value path toward the actual objective?

It does not ask whether every Worker implementation detail is correct unless that detail becomes strategically load-bearing.

## 6.2 The Governor is logically persistent but computationally ephemeral

The campaign persists. The model invocation does not.

```text
event
  -> acquire leased activation
  -> reconstruct campaign state
  -> build bounded context
  -> reason
  -> persist decision/action IDs
  -> dispatch / continue / park / wait / escalate
  -> release activation
  -> no inference until next meaningful event
```

## 6.3 Governor is primarily an outer loop over Manager Cases

```text
Governor
  -> creates/supervises Manager Case
  -> Manager dispatches Workers
  -> Manager becomes reviewable / complete / blocked
  -> Governor wakes
  -> continue same Case / request review / spawn successor / park / close
  -> repeat
```

This must work even with no external project integration.

## 6.4 Governor must supervise an existing Case

The Governor cannot be limited to "spawn Manager, then done."

Required semantics include:

```text
CREATE_MANAGER_CASE
CONTINUE_MANAGER_CASE
REQUEST_MANAGER_REVIEW
SPAWN_SUCCESSOR_CASE
SPAWN_PARALLEL_CASE
PARK_MANAGER_CASE
CLOSE_MANAGER_CASE
WAIT
ESCALATE
STOP_CAMPAIGN
NOOP
```

Exact runtime names may differ, but the semantic contract is mandatory.

## 6.5 Governor does not manage Workers directly

Normal authority:

```text
Governor -> Manager -> Worker
```

Not:

```text
Governor -> Worker
```

---

# 7. Governor behavioural contract

## 7.1 Mission

Preserve the global objective, maintain programme coherence, formulate and decompose meaningful problems, interpret surviving evidence, allocate attention, detect objective drift, and decide what deserves work next.

## 7.2 Semantic parity invariant

The Governor must continuously preserve alignment across:

```text
OBJECTIVE
  -> FORMALIZED PROBLEM
  -> QUESTION
  -> METHOD / EXPERIMENT
  -> EVIDENCE
  -> CLAIM
  -> PROGRAMME CONSEQUENCE / DECISION
```

A technically correct answer to the wrong question is a programme failure.

The Governor should detect when:

- a question no longer serves the parent objective;
- the method does not answer the question;
- the evidence does not represent the intended object;
- the claim exceeds the evidence;
- the strategic decision exceeds the claim.

## 7.3 Problem formulation

The Governor owns the high-level translation from broad objective to well-posed programme questions.

It may:

- decompose;
- narrow;
- merge;
- split;
- reformulate;
- reject malformed questions;
- create successor questions from findings;
- identify missing questions;
- identify when repeated feature-level failures point to a deeper latent hypothesis.

This is different from Manager task decomposition.

## 7.4 Global map

The Governor should maintain/project:

- ultimate objective;
- active programmes/branches;
- known facts;
- unresolved uncertainties;
- invalidated beliefs;
- critical assumptions;
- dependencies;
- contradictions;
- strategic risks;
- promising opportunities;
- parked-on-reality branches;
- current resource allocation;
- owner-authority decisions.

## 7.5 Decision discipline

Before significant allocation, ask:

1. What decision does this work enable?
2. What uncertainty does it reduce?
3. What would change our direction?
4. Is there a cheaper discriminating test?
5. What is the opportunity cost?
6. What happens if the central assumption is false?
7. Is this branch producing new information or only activity?
8. Is this work blocked on reality and therefore supposed to be parked?

Prefer **information gain over activity**.

## 7.6 Altitude control

Stay high by default.

Descend into detail only when:

- Adversary identifies a foundational defect;
- programmes depend on incompatible assumptions;
- Manager cannot resolve a blocker;
- a small implementation choice changes the scientific/economic object;
- an irreversible or capital-bearing boundary is imminent;
- local semantics threaten the global conclusion.

Return to programme altitude after resolution.

## 7.7 Do not collapse Governor into Adversary

Adversary asks:

> Why should we believe this claim?

Governor asks:

> Assuming the surviving evidence is true, what should we do?

---

# 8. Governor Profile

The runtime should support a generic `GovernorProfile` rather than separate Governor infrastructures.

A Profile configures:

```text
MISSION
POINT_OF_VIEW
SUCCESS_DEFINITION
AUTHORITY
PROHIBITIONS
ALLOWED_ACTIONS
AVAILABLE_TOOLS
WAKE_POLICY
CONTEXT_PROVIDERS
EVALUATION_DOCTRINE
RESOURCE_ALLOCATION_DOCTRINE
ESCALATION_RULES
STOP_CONDITIONS
MODEL/BACKEND/THINKING_POLICY
```

Possible profiles include ResearchGovernor, EngineeringGovernor, OperationsGovernor, and ProductGovernor.

---

# 9. Governor activation context

Every invocation should receive two categories of context.

## 9.1 Stable context

- campaign objective;
- profile/doctrine;
- authority boundaries;
- budget envelope;
- architecture/role invariants;
- stop/escalation rules.

## 9.2 Dynamic activation context

- wake reason;
- changed Manager Cases;
- Manager results since last activation;
- current frontier summary;
- unresolved contradictions;
- waiting branches;
- remaining budget;
- project-state delta;
- previous Governor decisions;
- references for deeper retrieval.

Do not send full repositories or complete histories by default.

---

# 10. Governor wake model

## 10.1 Generic wake sources

Internal:

- human instruction;
- managed Manager Case completed;
- managed Manager Case blocked;
- Manager escalation;
- branch/campaign timeout;
- budget threshold;
- explicit reconsideration condition.

External:

- semantic project events delivered through adapters.

## 10.2 Do not wake on low-level noise

Do not wake Governor for individual Worker completion, file writes, raw SSE frames, process exits, or routine progress messages.

## 10.3 Wake router

The wake router should deterministically:

- validate event envelope;
- route to campaign;
- deduplicate;
- coalesce;
- batch;
- check stale revisions;
- check campaign state;
- check budget;
- check whether Governor is already active;
- determine immediate vs deferred activation.

Only after this should model inference be spent.

`[S1]` After these deterministic steps, a System-One **wake-materiality** check ("is this a meaningful change for this campaign?") may decide immediate vs deferred activation. It is metered separately and never runs on parked or waiting branches without a new event. See §37 and **DESIGN CONFLICT DC-1**.

---

# 11. Governor durable state

Governor state belongs in the harness control plane.

Minimum durable campaign state:

```text
campaign_id
profile_id/version
frozen objective
authority envelope
budget envelope
campaign state/revision
managed Manager Case IDs
branch references
consumed event IDs / watermarks
previous decision IDs
outstanding waits
expected future events
reconsideration conditions
generation counter
spend/cost counters
stop condition
owner escalation state
```

Do **not** copy external domain truth into Governor state.

---

# 12. Governor decision record

Every activation must persist a concise, auditable decision record.

Minimum:

```text
decision_id
campaign_id
activation_id
trigger_event_ids
campaign_revision
project_state_revision(s)
manager_case_refs
frontier_refs
options considered
selected action
rejected alternatives + reasons
evidence references
assumptions
expected information/value gain
budget allocation
required next evidence
stop/reconsideration condition
expected next event
authority mode
model/backend/thinking configuration
runtime/inference cost
action/result references
```

Do not persist hidden chain-of-thought. Persist decision rationale sufficient for audit.

---

# 13. Manager behavioural contract

## 13.1 Mission

Move **one explicit bounded objective** to genuine convergence.

Manager is optimistic about execution and conservative about claims.

## 13.2 Responsibilities

- preserve objective, DoD, constraints;
- decompose into bounded, falsifiable Worker tasks;
- dispatch Workers efficiently;
- integrate results;
- resolve local blockers/contradictions;
- preserve durable evidence/resumability;
- stop when objective is genuinely complete.

## 13.3 Review gates

**Relevance:** Did the Worker answer the dispatched question?

**Rigor:** Does the committed evidence support the claims?

**Integration:** What previous facts, interpretations, decisions, assumptions or artifacts does this result preserve, supersede, invalidate, or leave unresolved?

## 13.4 Claim discipline

Important outputs should be classified as:

```text
MECHANICAL_FACT
SCIENTIFIC_FINDING
CAUSAL_INTERPRETATION
DECISION
UNRESOLVED
```

Do not equate tests passing with scientific validity, reproduction with correctness, artifact existence with completion, canonical implementation with correct specification, or Manager confidence with evidence.

## 13.5 Scope discipline

Manager cannot silently broaden/redefine the objective. Interesting adjacent issues are recorded and escalated to Governor unless they directly block current completion.

## 13.6 Closure

Manager closes only when objective/DoD is covered, load-bearing claims received required review, unresolved uncertainty is explicit, decisions are separate from executed actions, and remaining work genuinely belongs outside the Case.

"All Workers finished" is not closure.

## 13.7 Work graph

If useful, expose OPEN / DONE / BLOCKED / INVALIDATED / SUPERSEDED as a **projection over authoritative Case/task state**, not a duplicate Manager ledger unless existing state proves insufficient.

---

# 14. Manager result contract

A Manager result should contain at minimum:

```text
manager_case_id
objective/question answered
completion status
claim(s)
claim classification
evidence refs
artifact refs
assumptions
guard/admissibility results
evaluator verdict refs where available
limitations
non-entitlements
unresolved questions
proposed successor options
challenge requirement
branch cost
remaining uncertainty
```

Manager completion is **not** a canonical truth verdict.

---

# 15. Epistemic Adversary

## 15.1 Mission

Determine whether a load-bearing claim deserves belief.

> Find the smallest assumption which, if false, materially changes the conclusion.

The Adversary does not manage the programme, allocate resources, invent busy-work, disagree for sport, replace deterministic guards, or demand certainty beyond the decision's needs.

## 15.2 Initial runtime model

Prefer a specialized independent Manager Case/Profile using existing Case/session machinery unless repository implementation proves a distinct runtime primitive is required.

## 15.3 Risk-triggered invocation

Strong triggers include:

- candidate canonical finding;
- broad hypothesis kill/revival;
- major architecture choice;
- paper/live progression;
- capital-bearing boundary;
- strategy promotion/retirement;
- surprising result;
- new evaluator/major assumption;
- semantic-object substitution risk;
- programme closure with load-bearing claims;
- result affecting multiple branches.

`[S1]` Trigger evaluation is implemented as the S1 **`adversary_trigger` battery**: one calibrated yes/no per trigger above, aggregated with max. The invocation threshold is fitted on the G3 benchmark for critical-defect recall at a fixed invocation rate. **Attack routing** (S1 two-stage shortlist over §15.5) focuses the Adversary on the top attack classes first. See S1 spec §8.

## 15.4 Claim Packet

```text
CLAIM
DECISION_SUPPORTED
OBJECT_ACTUALLY_MEASURED
POPULATION / DENOMINATOR
METHOD
CONTROLS
ASSUMPTIONS
EVIDENCE_REFS / ARTIFACT_REFS
KNOWN_LIMITATIONS
NON_ENTITLEMENTS
```

## 15.5 Attack classes

- semantic substitution;
- denominator/population drift;
- selection/survivorship/censoring bias;
- lookahead/future-path leakage;
- invalid controls;
- unrealistic execution assumptions;
- incomplete lifecycle/policy;
- hidden defaults/fallbacks;
- state/timestamp/domain mismatch;
- reconstruction substrate != runtime substrate;
- accounting/reporting artifacts;
- correlation interpreted as causal mechanism;
- uncertainty represented as certainty;
- local workaround generalized into universal rule;
- missing evidence making claim untestable;
- correct implementation of an incorrect specification.

## 15.6 Output

Claim status:

```text
SURVIVES
SURVIVES_WITH_LIMITATIONS
DOWNGRADE
INVALID
INSUFFICIENT
```

Severity:

```text
BLOCKER
MAJOR
MINOR
NOTE
```

Every BLOCKER/MAJOR includes the affected claim, failing assumption, materiality, supporting evidence, smallest discriminating verification, and what survives either outcome.

Manager resolves through evidence, smallest discriminating verification, downgrade, unresolved state, or explicit authorized override.

---

# 16. Adversary benchmark

Build benchmark cases from real historical scars where available:

- first-tick used as executable entry;
- dataset last mark used as strategy exit;
- resolved-only denominator;
- research feature vs serving feature mismatch;
- decision-clock mismatch;
- complete-forward-tape leakage;
- dropped unpriceable losers;
- stop/config mismatch;
- snapshot reconstruction mistaken for fire-time state;
- statistically uncertain result mislabeled as invalid methodology.

Metrics:

- Critical defect recall
- Blocker precision
- Localization accuracy
- Severity calibration
- Discriminating-test quality
- Noise/redundancy

Primary optimization: high critical-defect recall **and** high blocker precision.

---

# 17. Evaluation and trust stack

```text
DETERMINISTIC GUARDS
    |
    v
EVIDENCE / DOMAIN EVALUATOR
    |
    v
RISK-TRIGGERED EPISTEMIC ADVERSARY
    |
    v
PROJECT CANONICAL / PROVISIONAL STATE
    |
    v
SEMANTIC EVENT
    |
    v
GOVERNOR CONSEQUENCE DECISION
```

Known failure modes should become deterministic guards where possible.

`[S1]` Known failure modes that are **semantic** (wrong question answered, object substituted, claim exceeding evidence) cannot be expressed as regex or code. They become **S1 semantic guards**: pinned, versioned System-One question batteries with recorded thresholds. They sit between deterministic guards and the evaluator/Adversary, and are never the sole authority for a BLOCKER. See **DESIGN CONFLICT DC-2** in §37.

The evaluator answers: **What are we entitled to conclude?**

It must support:

```text
SUPPORTED
REFUTED
INVALID
UNKNOWN
INSUFFICIENT
CONTRADICTION
NEEDS_REALITY
```

The Governor answers: **What does that imply for programme direction?**

---

# 18. Resource allocation and the "diamond" problem

The Governor cannot know whether a breakthrough is one iteration away. Model opportunity cost, not certainty.

Each branch should expose:

- why it exists;
- current hypothesis;
- value if successful;
- evidence accumulated;
- information gained recently;
- uncertainty remaining;
- failed approaches;
- cost of another iteration;
- competing branches;
- whether blocked on reality;
- termination/reopen condition.

Decision:

> Is the next unit of research better spent here than elsewhere?

Branches waiting on external reality should be PARKED and consume essentially no inference.

---

# 19. Inference economics and budgets

At minimum:

| Budget | Set by | Enforced by |
|---|---|---|
| Campaign | Human / policy | Harness |
| Branch/Manager | Governor within campaign | Harness |
| Governor activation | Profile/runtime policy | Harness |

Configure capability, do not hard-code:

```text
Workers   -> cheapest sufficient capability
Managers  -> stronger decomposition/review capability
Adversary -> strong independent reasoning when triggered
Governor  -> strongest permitted capability for high-leverage decisions
Reality   -> spend only where synthetic evidence cannot decide
```

Waiting on reality means PARK + reconsideration condition + no active Governor inference.

---

# 20. External project adapter model

AI-team must remain domain-agnostic.

A project integrates through:

1. semantic event producer;
2. bounded state/context provider;
3. guarded action/request gateway;
4. project-appropriate GovernorProfile.

## 20.1 Semantic event envelope

```text
event_id
source_project
event_type
aggregate_id / reference
aggregate_revision
occurred_at
produced_at
schema_version
idempotency_key
campaign_routing_metadata
payload or payload_ref
snapshot/reconstruction_ref
```

Use at-least-once delivery plus harness deduplication.

## 20.2 Context provider

Project exposes bounded, versioned state. Governor retrieves deeper detail by reference. Harness never becomes authoritative for domain truth.

## 20.3 Project action gateway

Governor sends requests, not direct mutations. Project validates, authorizes, accepts/rejects, mutates authoritative state if allowed, and emits resulting semantic event.

Unknown schemas or stale preconditions fail closed.

---

# 21. Example: `tokens_ingest` research adapter

`tokens_ingest` owns findings, hypotheses/matrix/frontier, provenance, evaluators, validation stage, canonicalization, paper/live truth, and semantic research events.

## 21.1 Read-only frontier projection

Derived from authoritative state, never a writable shadow ledger.

Minimum branch projection:

```text
branch_id
hypothesis/finding IDs
lineage
parent/child/family links if known
status
validation stage
evidence tier
current evaluator verdict
guard/admissibility state
evidence/provenance refs
open Manager Case refs
cost/information history
reopen condition
contradictions/dependencies
blocked-on-reality state
as_of_revision
```

No graph database is required merely for representation.

## 21.2 Semantic research events

Examples:

```text
research.evidence_evaluated
research.contradiction_detected
research.finding_state_changed
research.branch_ready
research.branch_parked
research.prospective_gate_resolved
research.reality_wait_started
research.assumption_invalidated
research.frontier_stalled
```

Pilot may begin with one authoritative event only.

## 21.3 Manager result return

Harness returns opaque references:

```text
campaign_id
branch_id
manager_case_id
result_id
evidence_refs
artifact_refs
claimed_scope
idempotency_key
```

Research side resolves/evaluates them. AI-team must not parse trading metrics into truth.

## 21.4 Canonicalization

`REQUEST_CANONICALIZATION` is only a request. Research side resolves refs, enforces guards, requires evaluator/evidence tier/challenge state, accepts or rejects, and emits a semantic event.

Pilot one should not need automatic canonical write authority.

---

# 22. Internal vs external wakes

Internal harness wake examples:

```text
manager_case.completed
manager_case.blocked
manager_case.escalated
campaign.timeout
budget.threshold
human.instruction
```

External semantic wake examples:

```text
research.evidence_evaluated
research.contradiction_detected
```

Both route through one Governor wake abstraction.

---

# 23. SOLID / architectural separation

Keep these separate:

```text
Governor reasoning
!= wake/event transport
!= campaign persistence
!= Manager execution
!= project/domain adapter
!= budget enforcement
!= observability
!= project evaluator
```

Single Responsibility: Governor decides; router routes; store persists; Manager executes objective; project evaluates truth.

Dependency Inversion: Governor consumes generic event/context/action contracts, not `tokens_ingest` semantics.

Open/Closed: adding a project means adapter + event schemas + GovernorProfile, not Governor runtime modification.

---

# 24. Failure model

Explicitly cover:

- duplicate semantic event;
- duplicate Manager completion;
- stale event;
- stale Manager result;
- Governor crash;
- Manager crash;
- Worker crash;
- partial project outage;
- partial harness outage;
- branch never returns;
- event storm;
- runaway branching;
- endless Governor/Manager bounce;
- repeated zero-information turns;
- reopening dead branch without valid reopen condition;
- budget exhaustion;
- Governor self-expands objective;
- Governor micromanages Workers;
- Governor treats Manager confidence as evidence;
- Governor burns inference while waiting for reality;
- malformed/unknown external schema;
- concurrent conflicting Governor activations;
- canonical finding later invalidated;
- reality evidence arrives after direction changed;
- local valid result becomes globally irrelevant;
- evaluator contradiction;
- owner override invalidates normal policy.

Prefer mechanical protections over prompt-only protections.

---

# 25. Failure / recovery ownership examples

| Failure | Recovery owner |
|---|---|
| Duplicate external event | Harness receipt/idempotency |
| Stale project event/action | Project preconditions reject; Governor refreshes |
| Governor crash | Harness lease/reaper reactivates from durable state |
| Manager/Worker crash | Existing Case/task resume machinery |
| Duplicate Manager continuation | Harness idempotent Case/action IDs |
| Branch never returns | Harness timeout; Governor park/escalate |
| Contradictory Manager results | Evaluator/Adversary resolve; Governor does not vote-count |
| Canonical assumption invalidated | Project event; Governor re-evaluates dependents |
| Event storm | Wake router coalesces/batches |
| Runaway branching | Campaign/branch hard caps |
| Budget exhausted | Harness refuses new work |
| Waiting on reality | PARK + semantic wake later |
| Unknown schema | quarantine/fail closed |

---

# 26. Observability: prove the Governor is useful

Measure:

- useful decisions per inference cost;
- owner disagreement rate;
- decisions later reversed;
- duplicate work spawned/avoided;
- dead branches continued;
- promising branches prematurely killed;
- correct PARK/WAIT decisions;
- contradictions noticed;
- semantic-drift incidents caught/missed;
- information gained per cost;
- Manager work rejected because objective/question was wrong;
- unnecessary Governor activations;
- average activations per resolved objective;
- budget adherence;
- stop-condition adherence;
- challenge coverage.

Manager metrics:

- wrong-question acceptance;
- unsupported-claim acceptance;
- Worker rework rate;
- unnecessary dispatch;
- unresolved blockers at closure;
- DoD/objective coverage.

Adversary metrics come from its benchmark.

`[S1]` These metrics require labelling at scale. They are computed from the S1 decision log (`system_one_decisions`) plus S1-labelled ledger features (for example, owner corrections classified as execution / logic / strategy and attributed to the layer that should have caught them). For G5, autonomy Stage 0 provides self-labelled activations: run the Governor on every routed wake; non-NOOP/WAIT means useful. See S1 spec §7–§8.

---

# 27. Autonomy ladder

## Stage 0 — Observe

Governor reads events/Case results and produces audited recommendations only.

## Stage 1 — Bounded zero-capital dispatch

Governor may open constrained Manager Cases, continue existing Cases, request independent review, and park/close within campaign rules.

## Stage 2 — Multi-generation bounded programme

Governor may allocate across branches and continue several generations under fixed budget.

## Stage 3 — Time/budget-bounded campaigns

Governor runs until budget cap, deadline, frontier exhaustion, or stop/escalation condition.

## Stage 4 — Safe prospective progression

Project policies may permit automated paper/prospective progression.

Live capital, production arming, secrets, and irreversible authority remain separately controlled unless explicitly changed later.

---

# 28. Minimum viable Governor pilot

The first pilot is an experiment on the Governor, not a profitability experiment.

Minimum:

```text
one campaign
one owner-defined objective
one Governor profile
one Governor activation per coalesced event batch
at most two plausible frontier alternatives
max one active Manager initially
one research generation
fixed Manager round cap
fixed inference/cost budget
zero live capital
no production arm/deploy
no direct canonical write
mandatory audit record
final owner review
```

Give the Governor at least two plausible choices so it must perform allocation, not merely call a Manager.

Success means no duplicate dispatch, sensible branch selection/parking, bounded cost, correct Manager supervision, accurate state reconstruction, good decision records, correct handling of insufficient evidence, and useful owner agreement/disagreement evidence.

---

# 29. Implementation programme

Convert this into repository-native jobs/dispatches before broad implementation.

## G0 — Canonical spec + programme manifest

Commit this spec into AI-team's canonical docs/spec location; register dependency graph; freeze ownership/role boundaries.

## S1 — System-One decision layer (cross-cutting enabler) `[S1]`

Already dispatched as **A94** (core + decision log + replay + delivery scorecard), **A95** (dispatch pre-flight) and **A96** (bounce; blocked on A94 data). The G-jobs **reuse** this core and add batteries; they must not build a second classifier path. Spec: `docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md`.

## G1 — Manager behavioural hardening

Implement relevance/rigor/integration review, structured Manager result, claim classification, Claim Packet creation, closure semantics, scope-drift escalation, durable resumability.

`[S1]` The relevance/rigor review gate is S1 Moves 1–3 (A94–A96). G1 adds these batteries on the same core:
- `integration`: does the result supersede or contradict prior facts?
- `claim_class_check`: S1 re-classifies each claim; disagreement with the Manager flags a claim that may exceed its evidence.
- `scope_drift`: if drift is detected and the Manager did not escalate, the harness synthesizes `manager_case.escalated`.
- `closure`: DoD coverage and unlisted uncertainty.

G1 also adds the structured worker report block (OUTCOME / CLAIMS / ROOT CAUSE / CHANGES / ANOMALIES / OPEN QUESTIONS) to `worker.md`.

## G2 — Governor core outer loop

Implement generic AI-team Governor:

- GovernorCampaign;
- distinct Governor role/profile;
- Governor -> Manager Case creation;
- supervision/continuation of existing Manager Cases;
- completion subscriptions;
- successor/review/park/close semantics;
- durable campaign/decision state;
- idempotent actions;
- crash-safe resumption.

No external project events yet.

## G3 — Adversary benchmark + final role contract

Build benchmark from historical failures; freeze Claim Packet, output/severity, risk-trigger policy, minimum performance thresholds.

`[S1]` The benchmark includes S1 arms:
- semantic known-failure guards (one pinned battery per historical lesson);
- the `adversary_trigger` policy;
- line-id localization (the TypeSafe semantic-find pattern) for **localization accuracy**;
- an S1-assisted scorer ("is finding F the same defect as lesson S?"), calibrated against hand labels.

## G4 — Epistemic Adversary implementation

Prefer specialized independent Case/Profile on existing machinery. Implement risk-triggered invocation, independent lineage, bounded Claim Packet, evidence retrieval, standardized output, Manager objection-resolution loop.

Depends on G3.

`[S1]` Risk-triggered invocation = the S1 `adversary_trigger` battery (resolves §34). G4 also adds the S1 `attack_routing` and `claim_packet_screen` batteries (OBJECT_ACTUALLY_MEASURED vs CLAIM).

## G5 — Governor control-plane hardening

Implement GovernorProfile, wake router, context builder, campaign/branch/activation budgets, event coalescing, observability, failure controls, model/backend/thinking selection policy.

Depends on G2 and relevant G1/G4 contracts.

`[S1]` G5 adds these S1 batteries, labelled via autonomy Stage 0 self-labelling:
- `wake_materiality`, after the deterministic router (DC-1);
- `information_gain`: zero-information streaks → PARK/ESCALATE;
- `activation_tier`: Governor model/thinking tier by leverage;
- `context_rank`: dynamic context selection.

The activation budget gets a separate S1 metering line.

## G6 — Generic external project adapter

Implement semantic event envelope, event receipt/dedup, context/state provider interface, project action-request interface, adapter routing, schema versioning, stale-precondition behavior. Test with dummy adapter first.

## G7 — `tokens_ingest` research adapter

Implement read-only frontier projection, durable semantic outbox, initial authoritative event, research-specific GovernorProfile/context, opaque Manager-result references, evaluator/adversary integration, guarded project action requests.

Do not modify generic Governor runtime for trading.

## G8 — Bounded real Governor pilot

Run genuine event/Manager completion, multiple plausible frontier alternatives, one bounded allocation, zero live capital, one generation, full audit trail.

## G9 — Pilot adversarial review + autonomy verdict

Evaluate Governor decision quality, cost, duplication, missed/incorrect branches, Manager outcomes, adversarial outcomes, owner disagreement, failure/recovery behavior.

Decision: HOLD / REVISE / EXPAND_TO_STAGE_2.

---

# 30. Programme dependency graph

```text
                 G0 SPEC / MANIFEST
                  |
        +---------+---------+
        |                   |
       G1                  G2
 Manager hardening      Governor core
        |                   |
        |          +--------+--------+
        |          |                 |
        |         G3                |
        |   Adversary benchmark     |
        |          |                 |
        |         G4                |
        |   Adversary runtime       |
        |          |                 |
        +----------+--------+--------+
                           |
                          G5
              Governor control plane
                           |
                          G6
               External project adapter
                           |
                          G7
               tokens_ingest adapter
                           |
                          G8
                 bounded real pilot
                           |
                          G9
          review + autonomy expansion verdict
```

Parallelization may change only where repository dependencies prove it safe.

`[S1]` Cross-cutting dependency: **S1 core (A94)** precedes G1's review gates and supplies the battery runtime for G3 (benchmark arms), G4 (trigger/routing) and G5 (materiality/information gain). G2 and G6 have no S1 dependency.

```text
S1 core (A94) ──► G1 gates (A94–A96 + G1 batteries)
      └────────► G3 arms ─► G4 trigger/routing ─► G5 materiality/info-gain
```

---

# 31. Programme manifest requirements

Create one durable manifest containing:

```text
programme_id
canonical_spec_path
job IDs/paths
job owner repository
dependency DAG
status
acceptance criteria
next eligible jobs
blocked reasons
programme completion criteria
```

Desired future workflow:

```text
Owner:
"Execute GOVERNOR_OUTER_LOOP_V1."

Manager:
reads manifest
-> finds next eligible job(s)
-> executes via Manager/Worker machinery
-> records completion
-> continues DAG
```

No important required work should exist only in chat history.

---

# 32. Non-goals

This design does not require:

- graph database;
- duplicate research ledger in AI-team;
- continuous Governor model session;
- arbitrary recursive agent trees;
- trading semantics in AI-team;
- Governor direct Worker dispatch;
- Governor direct canonical truth mutation;
- unlimited inference;
- automatic live capital;
- adversarial review of every trivial result.

---

# 33. Critical invariants

1. Workers search/implement.
2. Managers converge bounded objectives.
3. Adversaries falsify load-bearing claims.
4. Governors manage programme direction/frontier.
5. Humans own intent and hard authority.
6. Governor is a generic outer loop over Manager Cases.
7. External project events are wake sources, not the Governor's definition.
8. Governor can supervise/continue existing Manager Cases.
9. Governor is event-driven and dormant while nothing meaningful changes.
10. Durable state exists outside model memory.
11. Project/domain truth remains project-owned.
12. Manager completion is not truth.
13. Known failures become deterministic guards where possible.
14. Unknown failures are attacked through independent adversarial review.
15. UNKNOWN / INSUFFICIENT are valid outcomes.
16. Weak branches lose resources.
17. Promising branches can gain resources.
18. Waiting branches burn no inference.
19. Findings and failures should propagate.
20. Reality is used only where synthetic evidence cannot decide.
21. No important claim should silently broaden beyond its evidence.
22. No local success should substitute for progress toward the global objective.
23. Governor decisions themselves are measurable/evaluated.
24. Autonomy expands only after evidence justifies it.
25. Fail closed on stale state, unknown schema, or violated authority.

---

# 34. Open questions to settle during repository implementation

These are implementation questions, not reasons to reopen the architecture:

- Exact AI-team canonical spec path/convention.
- Exact DB/table representation for GovernorCampaign/Decision/EventReceipt.
- Whether Governor can reuse Manager session machinery with a new permission profile or needs a small distinct session type.
- Native naming for Manager continuation actions.
- Exact context-size/cost budgeting mechanism.
- Exact risk classifier that triggers Adversary. `[S1]` *Proposed answer:* the S1 `adversary_trigger` battery (§15.3 note; S1 spec §8). Its threshold is fitted on the G3 benchmark (target ≥ 90% critical-defect recall at ≤ 30% invocation rate). Final acceptance is in G3.
- Exact benchmark threshold required before Adversary is trusted.
- Exact generic adapter transport consistent with existing infrastructure.
- Exact `tokens_ingest` first semantic event after verifying authoritative persistence transition.
- Exact Stage 1 owner-review boundary.

Resolve these from repository evidence, not speculative redesign.

---

# 35. Architecture acceptance criteria

The architecture is correctly instantiated when:

- AI-team can run a Governor campaign without `tokens_ingest`;
- Governor can create and later continue/supervise a Manager Case;
- Manager still exclusively owns Worker orchestration;
- Governor state survives restart;
- duplicate wake cannot duplicate Manager work;
- waiting consumes no Governor inference;
- external projects integrate through adapters rather than runtime imports;
- project truth never becomes AI-team truth;
- Manager result does not automatically become canonical finding;
- load-bearing claims can be independently attacked;
- budget and authority are mechanically enforced;
- every Governor decision is auditable;
- first bounded pilot can run without live capital.

---

# 36. Final conceptual model

```text
HUMAN
  "What outcome matters, and what are the hard limits?"
        |
        v
GOVERNOR
  "What problem/question deserves attention next,
   and what does the surviving evidence imply?"
        |
        v
MANAGER
  "How do I converge this bounded objective correctly?"
        |
        v
WORKERS
  "Perform these bounded tasks and produce evidence."
        |
        v
ADVERSARY (when risk-triggered)
  "Why should we believe the load-bearing claim?"
        |
        v
PROJECT EVALUATOR / DOMAIN TRUTH
  "What are we actually entitled to conclude?"
        |
        v
GOVERNOR
  "Given that, what should we do next?"
```

That separation is the core design.

---

# 37. `[S1]` System-One decision layer: integration addendum (2026-10-05)

**What it is.** A thin, calibrated System-One layer (TypeSafe Jev `jev-1.13.0`, pinned). You send a
state plus typed questions (choice / score / yes-no probability) and get back calibrated
probabilities in about 70–500 ms at $0.042 per 1M input tokens. It never generates text, never
decides direction, never establishes truth, and never accepts work. Canonical spec:
`docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md`.

**Why it belongs in this architecture.** This spec separates System Two judgment (Governor,
Adversary, Manager) from mechanics. It leaves classifier-shaped holes where cheap, calibrated
judgment is needed *before* System Two is invoked:
- the materiality step of the wake router (§10.3, invariant 8);
- the risk trigger for the Adversary (§15.3, open question §34);
- semantic known-failure guards (§17, invariant 13);
- measurable Governor and Manager decisions (§26, invariant 23).

S1 fills those holes so that System Two stays dormant until something matters, which is invariants 8,
9 and 18 enforced mechanically per §24 rather than by prompt.

**Placement.** S1 core = A94, prerequisite of the G1 review gates. G-jobs add batteries on the same
core: G1 (integration, claim-class check, scope drift, closure), G3 (benchmark arms, localization),
G4 (trigger, attack routing, Claim Packet screen) and G5 (materiality, information gain, activation
tier, context rank). G2/G6 have none.

**Authority.** Asymmetric. S1 may annotate, soft-refuse with Manager override (A95), or bounce once
(A96, gated on measured precision). It never accepts, closes, merges, deploys, chooses models or
allocates budget.

**DESIGN CONFLICTS — owner ruling required** (default: proposal applies provisionally, flagged):

- **DC-1. Invariant 9 / §6.2 / §10.3 ("no inference while waiting"; deterministic router).** An S1
  materiality check is inference, though about 1/5000th of a Governor activation.
  *Proposal:* "inference" in invariant 9 means System-Two activations. S1 checks are permitted only on
  events that passed deterministic routing, are metered as a separate line in the activation budget,
  and never run on parked or waiting branches absent a new event.
- **DC-2. Invariant 13 ("known failures become deterministic guards").** S1 semantic guards are
  calibrated, not deterministic.
  *Proposal:* add a distinct trust-stack tier, "semantic guards", between deterministic guards and the
  evaluator/Adversary. Model version and threshold are recorded per decision, and a semantic guard is
  never the sole authority for a BLOCKER.
- **DC-3. §4 ownership / X1 (project-domain content to a third-party API).**
  *Default:* forbidden. S1 operates only on harness-generic content (envelopes, worker reports, Case
  objective and criteria, computed facts) until the owner rules for X1.

**Measurement hook.** Every S1 evaluation writes one row to `system_one_decisions` (refs + answers,
no state text). Batteries are pure functions, so the same code runs live, over history (replay on a
read-only DB copy) and inside the G3 benchmark. That makes before/after measurement automatic.
