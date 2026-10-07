# TASK — Bootstrap the Governor implementation programme in AI-team

## Objective

Instantiate the already-designed `GOVERNOR_OUTER_LOOP_V1` architecture inside the `AI-team` repository as a **durable implementation programme**.

This task is **NOT** to implement the Governor yet.

Your job is to:

1. place the canonical Governor architecture spec in the correct repository-native spec/docs location;
2. inspect AI-team's existing task/dispatch/spec conventions;
3. create the complete dependency-ordered job chain needed to implement the generic harness side;
4. create a programme manifest/index so a future Manager can execute the chain without reconstructing anything from chat history;
5. register the jobs using the repository's native mechanism;
6. commit the design/dispatch artifacts only.

Authoritative design input:
`GOVERNOR_OUTER_LOOP_V1.md`

Treat that document as the architecture to instantiate, not as brainstorming material.

---

# 1. First inspect the repository

Before writing anything, determine:

- where AI-team stores canonical architecture/spec documents;
- how implementation jobs / dispatches / work items are represented;
- how dependencies and statuses are represented;
- whether there is an existing programme/index/roadmap convention;
- how Manager Cases currently consume work;
- whether generated indexes/projections exist and how they are updated;
- what files are hand-authored versus generated.

Do not invent a parallel job system.

Reuse the repository's existing conventions.

If the repository has no adequate programme-manifest convention, add the smallest one consistent with the existing architecture.

---

# 2. Canonical spec

Persist the supplied `GOVERNOR_OUTER_LOOP_V1.md` in the correct AI-team canonical documentation/spec location.

Do NOT silently redesign it.

Allowed edits:

- repository-local links;
- front matter / metadata;
- naming normalization required by repository conventions;
- clarification of implementation references discovered from the actual codebase.

Any semantic change to the architecture must be explicitly reported as:

`DESIGN CONFLICT`

and must not be silently applied.

The canonical logical identity is:

`GOVERNOR_OUTER_LOOP_V1`

There must be one canonical Governor architecture spec, not several competing versions.

---

# 3. Programme scope

The programme must eventually produce this generic hierarchy:

```text
HUMAN
  ↓
GOVERNOR
  ↓
MANAGER CASES
  ↓
WORKERS
```

with an independent, risk-triggered:

```text
EPISTEMIC ADVERSARY
```

and with external projects able to wake/inform Governors through generic adapters.

The Governor is fundamentally an **outer loop around Manager Cases**.

It must support:

- creating Manager Cases;
- supervising existing Manager Cases;
- reactivating on Manager completion/blocking/escalation;
- continuing the same Manager Case;
- requesting independent review;
- spawning successor/parallel Manager Cases;
- parking/closing work;
- bounded resource allocation;
- durable state and crash recovery;
- waiting without inference;
- external semantic wake events through adapters.

Do not reduce the Governor to a `tokens_ingest` event consumer.

---

# 4. Create a programme manifest

Create one durable programme manifest/index with at least:

```text
programme_id
canonical_spec_path
job_id
job_path
job_title
owner_repository
status
dependencies
acceptance_gate
next_eligible_jobs
blocked_reason
programme_completion_criteria
```

The intended future workflow is:

```text
Owner:
"Execute GOVERNOR_OUTER_LOOP_V1."

Manager:
reads manifest
→ finds next eligible job(s)
→ executes them through normal Manager/Worker machinery
→ records completion
→ continues dependency graph
```

No important required work may remain only in prose or chat history.

---

# 5. Create the implementation job chain

Use repository-native naming/IDs, but preserve these logical work packages unless code inspection proves a different split is necessary.

## S1 — System-One decision layer (ALREADY DISPATCHED — reference it, do not re-create) `[S1 addendum 2026-10-05]`

Canonical spec: `docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md` (`SYSTEM_ONE_DECISION_LAYER_V1`). Governor integration: `GOVERNOR_OUTER_LOOP_V1.md` §37.

Existing jobs:
- `.ai/dispatch/AGENT_94_SYSTEM_ONE_CORE_DELIVERY_SCORECARD.md`: core, decision log, replay, delivery scorecard.
- `.ai/dispatch/AGENT_95_SYSTEM_ONE_DISPATCH_PREFLIGHT.md`: dispatch pre-flight.
- `.ai/dispatch/AGENT_96_SYSTEM_ONE_DELIVERY_BOUNCE.md`: bounce; blocked on A94 data.

The programme manifest must list them as the S1 node, with A94 as a dependency of G1's review gates. Every G-job below that has an "S1 slots" line must **reuse the S1 core** (battery = pure `build_state` / `questions` / `gate` + one Jev request + one decision row). It must not introduce a second classifier path, client or decision table.

## G1 — Manager behavioural contract hardening

Purpose:
Make Manager behavior suitable for being governed by an outer loop.

Required scope:

- preserve one explicit objective + DoD;
- relevance / rigor / integration checks on Worker output;
- structured Manager result;
- claim classification:
  - `MECHANICAL_FACT`
  - `SCIENTIFIC_FINDING`
  - `CAUSAL_INTERPRETATION`
  - `DECISION`
  - `UNRESOLVED`
- load-bearing Claim Packet generation;
- scope-drift escalation to Governor;
- closure based on objective convergence, not Worker completion;
- explicit unresolved uncertainty;
- durable resumability/checkpoint behavior.

Constraint:
Do not create a duplicate Manager state ledger if existing Case/task state can represent this.

Acceptance should prove the behavior through tests/fixtures or deterministic output contracts.

S1 slots `[S1]`:
- relevance/rigor review = A94–A96 (depends on A94);
- add the batteries `integration`, `claim_class_check`, `scope_drift` (synthesizes `manager_case.escalated` when drift goes unescalated) and `closure`;
- add the structured worker report block to `worker.md` so the logic-link questions can point at named fields.

See S1 spec §8.

---

## G2 — Governor core outer loop

Purpose:
Implement the generic Governor above existing Manager Cases.

Required scope:

- durable `GovernorCampaign` equivalent;
- distinct Governor role/profile;
- Governor → Manager Case creation;
- Governor supervision of existing Manager Cases;
- Manager completion/blocking/escalation subscription;
- continuation of the same Manager Case;
- successor/parallel Case creation;
- park/close/wait/escalate semantics;
- bounded typed Governor decisions;
- durable Governor decisions;
- idempotent action IDs;
- crash-safe reconstruction/resume;
- no direct Worker dispatch by Governor.

Important:
Reuse existing Case/session/lease/continuation machinery.
Do not introduce a second orchestrator.

Acceptance must include:
Governor creates a Manager Case → Manager completes → Governor wakes → Governor continues or closes → process restart/replay does not duplicate work.

---

## G3 — Epistemic Adversary benchmark + final role contract

Purpose:
Define and benchmark the independent falsification role before trusting it.

Required scope:

- final Claim Packet contract;
- final adversary output contract;
- severity contract:
  - `BLOCKER`
  - `MAJOR`
  - `MINOR`
  - `NOTE`
- claim status:
  - `SURVIVES`
  - `SURVIVES_WITH_LIMITATIONS`
  - `DOWNGRADE`
  - `INVALID`
  - `INSUFFICIENT`
- risk-trigger policy;
- benchmark harness;
- metrics:
  - critical-defect recall
  - blocker precision
  - localization accuracy
  - severity calibration
  - discriminating-test quality
  - noise/redundancy

Use real historical cases where available.
Do not fabricate easy benchmark cases merely to produce a PASS.

If project-specific historical cases live outside AI-team, define the generic benchmark harness/contract here and register the project-specific benchmark-data work as an external dependency/job.

S1 slots `[S1]`:
- benchmark arms for semantic known-failure guards and the `adversary_trigger` policy;
- line-id localization for localization accuracy;
- an S1-assisted finding↔lesson scorer, calibrated against hand labels.

Reuse `scripts/system_one/replay.py` (A94) as the runner.

---

## G4 — Epistemic Adversary implementation

Depends on G3.

Purpose:
Implement the independent falsification path.

Preferred architecture:
specialized independent Case/Profile on existing AI-team machinery unless code evidence proves a distinct runtime primitive is needed.

Required scope:

- independent session/Case lineage;
- bounded Claim Packet input;
- evidence/artifact retrieval;
- standardized verdict/severity output;
- risk-triggered invocation;
- Manager objection-resolution loop;
- unresolved BLOCKER handling;
- no programme-allocation authority.

Acceptance:
Adversary can attack a load-bearing claim independently and return a bounded result that Manager must resolve/downgrade/escalate.

S1 slots `[S1]`:
- risk-triggered invocation = the `adversary_trigger` battery, which is the proposed answer to GOVERNOR §34;
- `attack_routing` (two-stage shortlist over §15.5);
- `claim_packet_screen`.

---

## G5 — Governor control-plane hardening

Depends on G2 and the relevant contracts from G1/G4.

Purpose:
Make the outer loop safe, economical, and observable.

Required scope:

- `GovernorProfile` abstraction;
- stable vs dynamic context builder;
- wake router;
- wake coalescing/dedup;
- internal wake sources:
  - human instruction
  - Manager completion
  - Manager blocked
  - Manager escalation
  - timeout
  - budget threshold
- campaign budget;
- branch/Manager budget;
- Governor activation budget;
- max active Managers / generations / wall time;
- wait-without-inference behavior;
- model/backend/thinking-effort policy;
- least-privilege tools;
- decision audit records;
- failure controls;
- anti-runaway branching;
- anti-endless Manager/Governor bounce;
- concurrent activation protection;
- metrics for Governor usefulness vs overhead.

Acceptance:
A campaign can operate within hard limits, recover from restart, coalesce duplicate wakes, and produce auditable bounded decisions.

S1 slots `[S1]`:
- `wake_materiality`, after the deterministic router (GOVERNOR §37 DC-1);
- `information_gain`;
- `activation_tier`;
- `context_rank`;
- a separate S1 line in the activation budget;
- labels via autonomy Stage 0 self-labelling (run the Governor on every routed wake; non-NOOP/WAIT means useful).

---

## G6 — Generic external project adapter

Depends on G5.

Purpose:
Allow external projects to wake/inform a Governor without putting domain logic into AI-team.

Required generic concepts:

### Semantic event envelope

At minimum:

```text
event_id
source_project
event_type
aggregate/ref
aggregate_revision
occurred_at
produced_at
schema_version
idempotency_key
campaign routing metadata
payload or payload_ref
reconstruction/snapshot_ref
```

### Context/state provider

Governor can request a bounded project-specific state projection.

### Project action/request gateway

Governor sends typed requests.
External project validates/authorizes/mutates its own truth.

Required behavior:

- at-least-once delivery;
- harness deduplication;
- schema validation/versioning;
- stale-precondition failure;
- quarantine unknown schema;
- adapter registration;
- no project-specific imports in Governor core.

Acceptance:
Use a dummy project adapter first and prove end-to-end wake → context → Governor decision → request/result without domain coupling.

---

# 6. Register later cross-project work without implementing it

The AI-team programme manifest should also identify the later external programme boundary, even if those jobs are not owned by AI-team.

At minimum register/reference:

## X1 — `tokens_ingest` Governor adapter

Owned by `tokens_ingest`.

Expected scope:

- read-only research frontier projection;
- durable semantic event outbox;
- authoritative initial event;
- research-specific GovernorProfile/context;
- opaque Manager-result references;
- evaluator/adversary integration;
- guarded project action requests.

## X2 — bounded real research Governor pilot

Cross-project.

Expected scope:

- real semantic event and/or Manager completion;
- at least two plausible frontier alternatives;
- one bounded allocation decision;
- zero live capital;
- one generation initially;
- full audit trail.

## X3 — pilot review / autonomy expansion gate

Cross-project.

Expected outputs:

`HOLD`
`REVISE`
or
`EXPAND_TO_NEXT_STAGE`

Do not implement X1-X3 in this task.
Only record their place in the programme and dependency graph.

---

# 7. Dependency model

Target logical DAG:

```text
                 SPEC / MANIFEST
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
                                  X1
                       tokens_ingest adapter
                                   |
                                  X2
                         bounded real pilot
                                   |
                                  X3
                    review / autonomy gate
```

If repository evidence justifies another split, change the physical jobs but preserve the logical capabilities and dependency rationale.

`[S1]` Add the S1 node: **A94 → G1 review gates**. A94's core is also an input to G3 (arms), G4 (trigger/routing) and G5 (materiality/information gain). A95 depends on A94. A96 depends on A94 *and* its measured precision artefact. G2 and G6 have no S1 dependency.

---

# 8. Every job must be executable without chat context

Each job/dispatch must contain:

- objective;
- why it exists;
- exact in-scope capabilities;
- explicit out-of-scope items;
- prerequisites/dependencies;
- repository areas to inspect;
- architectural invariants;
- required implementation behavior;
- required tests;
- failure cases;
- observability requirements;
- acceptance criteria;
- deliverables;
- what future job it unblocks.

Avoid vague tasks such as "implement Governor support."

A fresh Manager should be able to pick up the job and know exactly what completion means.

---

# 9. Architectural invariants to copy into relevant jobs

Do not violate:

1. Governor is a generic outer loop over Manager Cases.
2. Governor can supervise/continue existing Manager Cases.
3. Manager owns Workers.
4. Governor normally does not dispatch Workers.
5. Project/domain truth remains project-owned.
6. Manager completion is not epistemic acceptance.
7. Adversary falsifies; Governor decides consequences.
8. Governor is dormant when nothing meaningful changes.
9. Waiting on external reality consumes no active inference.
10. Durable state lives outside model memory.
11. Duplicate/stale events/actions are safe.
12. Known failure modes should become deterministic guards where possible.
13. `UNKNOWN` / `INSUFFICIENT` must remain valid outcomes.
14. Hard budgets/authority are mechanically enforced.
15. External projects integrate through adapters rather than Governor-core imports.
16. No live-capital or irreversible authority is introduced by this programme unless separately authorized.
17. `[S1]` System-One authority is asymmetric. It may annotate, soft-refuse with override, or bounce once when gated. It never accepts, closes, decides direction or establishes truth.
18. `[S1]` System-One evaluations fail open to existing behaviour, are pinned to a model version, and are logged one row per decision (no state text).
19. `[S1]` Project-domain content is not sent to the System-One API until the owner rules (GOVERNOR §37 DC-3).

---

# 10. Do not overbuild

Do not introduce without concrete necessity:

- graph database;
- general arbitrary recursive-agent tree;
- second scheduler/orchestrator;
- duplicate domain ledger;
- continuous Governor process;
- direct Governor production/deployment authority;
- trading semantics in AI-team;
- adversarial review on every trivial task.

Use the smallest architecture that satisfies the canonical spec.

---

# 11. Required output of this task

At completion report:

1. canonical Governor spec path;
2. programme manifest path;
3. every created job/dispatch path;
4. final dependency DAG;
5. which jobs are immediately eligible;
6. which jobs are blocked and why;
7. any DESIGN CONFLICTS;
8. any changes made to the supplied spec and why;
9. whether all job artifacts are sufficient for a fresh Manager to execute without chat history;
10. commit SHA;
11. pushed branch, if pushing is part of normal repo workflow;
12. `[S1]` how each G-job's "S1 slots" are represented in its packet, and the owner's ruling status on GOVERNOR §37 DC-1/DC-2/DC-3.

No production Governor functionality should be implemented in this bootstrap task.

The desired final state is:

`GOVERNOR_PROGRAMME_BOOTSTRAP = COMPLETE`
