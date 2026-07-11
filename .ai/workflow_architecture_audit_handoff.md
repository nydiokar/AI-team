# Architecture Audit Handoff
## Case/Task/Session Model + Workflow-Orchestration Gaps

Act as the senior owner reviewing the existing workflow-orchestration architecture.

Do not implement runtime changes yet.

Your job is to verify the real codebase against the architectural model below, explain what is already correct, identify what was missed or only partially implemented, update the relevant roadmap/specifications, and produce ready-to-dispatch implementation jobs in the correct order.

The output must be actionable. Another agent should be able to take the resulting jobs and implement them without needing this handoff.

---

## 1. The central problem to resolve first

For this project:

```text
Case = flow_run
```

Do not spend time debating the naming.

The real issue is when a Case should exist and how Tasks and Sessions participate in it.

The current suspected defect is:

```text
every Task/turn
-> creates a new Case
```

That collapses managed work into ordinary execution.

The intended model must support all three patterns:

### A. Standalone Session

```text
Session
-> many Tasks
-> no Case
```

Ordinary ad-hoc interaction should not require a managed Case.

### B. Dedicated managed Session

```text
Case X
-> one dedicated Session
-> many Tasks
```

A Session may effectively equal one Case when it is intentionally dedicated to one managed objective.

### C. Managed Case spanning Sessions

```text
Case X
-> Manager Session
-> worker Sessions
-> reviewer Session
-> many Tasks across all participants
```

A Case persists across Tasks and Sessions until the objective is actually closed.

The audit must determine:

- when a Case is created;
- who creates it;
- how its objective is established;
- how a Task joins an existing Case;
- how a Session becomes standalone, Manager, worker, or reviewer for a Case;
- how a Session leaves or switches Cases;
- how a Case resumes later;
- what genuinely closes a Case.

Critical invariant:

```text
Task finished != worker finished != Case completed
```

A worker returning a result must not automatically close the Case.

---

## 2. Good architectural practices to import and verify

The following mechanisms came from the reference designs. Treat them as capabilities to map onto the real repository, not as prescribed names or technologies.

### 2.1 Durable work authority

The gateway should own the durable work model:

```text
Cases
Tasks
Sessions
lineage
events
approvals
artifacts
completion
notification routing
```

Provider sessions and workers execute work; they should not be the sole authority for whether the work exists.

Check whether the existing `flow_runs`, `flow_links`, `flow_events`, Task records, Session records, and Work UI already satisfy this under different names.

Do not propose a second ticket system or duplicate event database if the existing substrate is structurally sufficient.

---

### 2.2 Canonical event model

The system should represent real lifecycle facts such as:

```text
CASE_CREATED
TASK_ATTACHED
SESSION_ATTACHED
WORK_ASSIGNED
AGENT_STARTED
AGENT_PROGRESS
SUBTASK_CREATED
SUBTASK_FINISHED
ARTIFACT_CREATED
REVIEW_REQUESTED
REVIEW_FAILED
REVIEW_PASSED
USER_INPUT_REQUIRED
CASE_PAUSED
CASE_RESUMED
CASE_COMPLETED
CASE_CANCELLED
```

Do not force these exact names.

Determine whether equivalent events already exist and whether they reflect real actions or merely automatic stage decoration.

Pay particular attention to:

- real versus fabricated stage transitions;
- child completion versus Case completion;
- review events before a reviewer role exists;
- closure being emitted because a Task ended rather than because the objective was accepted.

---

### 2.3 Durable relay, wake-up, and reconciliation

Recording an event is not the same as continuing the workflow.

The required behavior is:

```text
worker finishes
-> durable completion is recorded
-> responsible Case is resolved
-> correct Manager/requester is reached
-> work continues
-> delivery is not lost or duplicated
```

Check whether the current system already has equivalents of:

```text
event subscription
delivery/outbox state
requester origin binding
Manager binding
wake or resume
retry
acknowledgement
deduplication
reconciliation
orphan recovery
late-completion handling
```

A live `wait_for_worker` path proves only that an active process can wait.

A Telegram notification proves only that a message can be delivered.

Neither alone proves that a sleeping or replaced Manager can resume the Case correctly.

---

### 2.4 Gateway control plane for the Manager

The Manager should operate through a provider-independent gateway control surface.

Conceptual operations:

```text
create or receive Case
inspect Case
attach Task
attach Session
dispatch worker
inspect worker
read worker events
send worker instruction
await worker
request review
request user input
cancel worker
pause Case
resume Case
publish artifact
close Case
```

Map each operation to what actually exists.

Distinguish:

- data being manually queryable;
- a supported Manager-facing operation existing;
- the operation being safe for autonomous use.

---

### 2.5 Review, approval, and safety state

The architecture should eventually support durable state for:

```text
review requested
review passed
review failed
rework requested
approval required
approval granted
approval rejected
cancellation
pause
resume
limits
```

Check what is already present, what is only named in stages, and what is still future work.

The Case must not close while required review, approval, user input, or child work remains unresolved.

---

### 2.6 Optional provider features

The following are optional implementation choices, not foundations:

```text
OpenAI Manager adapter
Responses API
provider-native subagents
programmatic tool calling
```

The gateway must remain authoritative.

Provider-native subagents should either:

```text
remain ephemeral implementation details
```

or:

```text
be mirrored as first-class workers with lineage, lifecycle, cancellation, cost, and completion
```

Do not place these on the critical path unless the project already intends to.

---

## 3. Map the practices to the existing roadmap

Use the project’s actual milestone names and numbering, but evaluate the architecture in these buckets.

### Existing substrate: likely M1/M2 territory

Expected capabilities:

```text
durable Case record
Task and Session records
flow/task/session links
parent-child lineage
durable events
Work UI
existing notification surfaces
```

Determine for each item whether it is:

```text
already correct
implemented differently but equivalently
partial
miswired
missing
```

If a completed milestone delivered its original scope correctly, do not mark it failed merely because a later requirement exposed a new gap.

When past work needs correction or hardening, create a new additive job linked to the earlier milestone.

---

### Current Manager plumbing: likely Phase 3.0 territory

Expected narrow capabilities:

```text
dispatch_worker
wait_for_worker
```

Determine whether this is only a plumbing proof or whether it accidentally assumes the broken one-Task-per-Case model.

Preserve the value of the plumbing spike if it proves dispatch/wait behavior, but do not let it define Case semantics.

---

### Future Manager role/control surface: likely Phase 3.1 territory

This is where the following should probably land, subject to code evidence:

```text
real Manager role
objective lock
Case ownership
Task/Session affiliation
worker inheritance into the same Case
inspection
follow-up instruction
pause/resume
authoritative closure decision
```

The Case admission and continuity model must be settled before these specifications are finalized.

---

### Review/rework: likely Phase 3.2 territory

Expected capabilities:

```text
review requested
review failed
review passed
rework loop
review events
```

Determine whether any of this was prematurely represented in earlier stages without a real reviewer.

---

### Guardrails and recovery: likely Phase 3.3 territory

Expected capabilities:

```text
approval state
cancellation
kill path
round/turn/cost bounds
crash recovery
stuck-work handling
```

Determine which items belong here versus the earlier durable substrate.

---

### Artifact/specification workflow: likely M4 territory

Expected capabilities:

```text
artifact publication
spec generation
scored review
formal acceptance
```

Determine which parts need event and Case integration added to future specifications.

---

## 4. What to verify in the real implementation

Focus on these questions.

### Case creation

- Does the generic Task enqueue path create a new Case?
- Can a Task exist without a Case?
- Can an explicit managed-work entrypoint create a Case?
- Can a later Task join an existing Case?
- Do worker Tasks inherit the parent Case or create their own?

### Case continuity

- Can one Case contain many Tasks in real write paths?
- Can one Case contain many Sessions in real write paths?
- Can a dedicated Session remain attached to one Case across multiple turns?
- Can a standalone Session stay outside the Case system?
- Can a Session later switch or detach?
- Can a paused Case resume without creating a replacement Case?

### Stage and closure semantics

- Which events actually advance each stage?
- Are planning/review/closure stages being stamped automatically?
- Who is allowed to close a Case?
- Is Case closure distinct from Task or worker completion?

### Event and lineage semantics

- Are lineage and role relationships authoritative?
- Are events durable and scoped to the correct Case/Task/Session?
- Are there real event consumers or only a stored log?
- Are duplicates, retries, and late events handled deliberately?

### Manager continuity

- Can an active Manager receive a worker result?
- Can a sleeping, restarted, or replacement Manager resume the Case?
- Is the Manager bound to a Case durably?
- Can the Manager inspect and redirect workers through supported tools?

### UI truthfulness

- Does the Work UI show one fake Case per turn?
- Does it distinguish standalone Sessions from managed Cases?
- Does the displayed timeline reflect real planning, review, and closure?
- Can the user understand which Tasks and Sessions belong to one objective?

---

## 5. Required classification

For every capability or practice above, classify it as:

```text
Already implemented correctly
Implemented differently but equivalently
Implemented partially and needs hardening
Implemented incidentally but lacks a supported contract
Specified but not implemented
Implemented but absent from the roadmap
Placed in the wrong stage
Missed in completed work and needs an additive follow-up
Belongs in a future specification
Optional and should stay off the critical path
Rejected as unnecessary or duplicative
```

This classification is the core of the audit.

---

## 6. Required deliverables

### A. Plain-language architectural explanation

Explain:

- what the system currently does;
- why one-Task-per-Case is wrong if verified;
- which of the three valid patterns are supported today;
- what the correct Case admission, affiliation, continuity, and closure model should be;
- how this affects Manager, event, relay, review, approval, and UI behavior.

### B. Current-state and target-state diagrams

Produce two Mermaid diagrams:

```text
Current implementation as verified
Target implementation after the planned fixes
```

Show:

- Session;
- Task;
- Case/flow_run;
- Manager;
- workers;
- reviewer;
- events;
- relay/wake-up;
- approval;
- closure.

### C. Practice-to-roadmap mapping

For every useful mechanism from this handoff, state:

```text
what it means
what the project already has
whether the current implementation is equivalent
what is missing
which milestone/specification owns the remaining work
whether completed work needs an additive follow-up
```

### D. Corrections to canonical documents

After the conclusions are stable:

- correct the current-state description;
- update the roadmap dependencies;
- amend future Manager, event, relay, review, approval, and artifact specifications;
- preserve valid completed work;
- add explicit follow-up notes where earlier work was structurally useful but incomplete.

Do not rewrite history merely to make the roadmap look clean.

### E. Ready-to-dispatch implementation jobs

Produce separate, self-contained jobs in dependency order.

Expected job families, subject to the audit:

```text
1. Correct Case admission and Task/Session affiliation
2. Correct Case continuity, stage transitions, and closure semantics
3. Harden canonical event contracts and lineage
4. Add durable relay, wake-up, acknowledgement, and reconciliation
5. Complete the Manager control surface and Manager role
6. Add review and rework
7. Add approval, cancellation, and safety controls
8. Integrate artifact publication and scored review
9. Optional provider adapter/subagent/programmatic-tool work
```

Each job must contain:

```text
objective
current verified baseline
scope
non-goals
dependencies
affected components
implementation intent
acceptance criteria
required evidence
```

Do not reopen historical tasks when a new additive job is the honest fix.

### F. Adversarial conclusion check

Before finalizing document edits and jobs, challenge the main conclusions:

- Could existing code already support the desired model under different names?
- Could the apparent one-Task-per-Case behavior be intentional for one path only?
- Could event delivery already exist outside `flow_events`?
- Could review/approval already exist as generic state?
- Could a proposed new job duplicate existing planned work?
- Could a supposedly missing feature be intentionally out of scope?

Record the strongest counter-evidence and final resolution for each load-bearing conclusion.

---

## 7. Final decision standard

The audit is complete only when the updated documents and generated jobs make these questions answerable:

```text
When is a Case created?
When is no Case created?
How does a Task join an existing Case?
How does a Session participate in or leave a Case?
How does one Case span multiple Tasks and Sessions?
How are worker results returned to the correct Case?
How is a Manager resumed after asynchronous completion?
What events are real and authoritative?
What closes a Case?
Which past milestones remain valid?
Which past stages need additive hardening?
What belongs in each future stage?
What implementation job should run next?
```

Prefer an accurate, slightly uncomfortable architecture over a tidy but fictional one.
