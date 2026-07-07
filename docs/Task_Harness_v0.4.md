# Adversarial Task Harness v0.4 — Minimal Workflow Kernel

Status: scoped operating spec
Host: existing gateway
Goal: improve task execution quality without creating a large workflow platform.

---

## 1. Core Rule

The gateway must support a small task-quality loop:

```text
intent → objective lock → plan → adversarial review → execution → implementation review → closure
```

This loop must be modular.

Every stage can be skipped for tiny tasks.

Every external tool is optional.

The core must work without external memory, task orchestrators, or codebase graph tools.

---

## 2. Main Artifacts

The harness uses three artifact types.

### 2.1 Machine Artifact: XML Task Packet

Use XML-style structure for model-facing instructions.

Purpose: reduce instruction drift and make task packets easier for models to follow.

Example:

```xml
<task_packet>
  <objective_lock>
    <real_objective></real_objective>
    <literal_request></literal_request>
    <interpreted_task></interpreted_task>
    <constraints></constraints>
    <non_goals></non_goals>
    <assumptions></assumptions>
    <drift_risks></drift_risks>
  </objective_lock>

  <approved_plan>
    <steps></steps>
    <validation></validation>
    <definition_of_done></definition_of_done>
    <risks></risks>
  </approved_plan>

  <execution_rules>
    <do></do>
    <do_not></do_not>
    <report_format></report_format>
  </execution_rules>
</task_packet>
```

Markdown stays acceptable for humans. XML is preferred for model-facing packets.

---

### 2.2 Work Artifact: Milestone Burndown File

Each medium/high task gets a milestone file.

Purpose: make long-running agent work inspectable and resumable.

Example:

```markdown
# Milestone: T-014 Gateway Harness Slice 1

## Objective
...

## Current Status
drafting / executing / reviewing / blocked / closed

## Burndown
- [ ] item 1
- [ ] item 2
- [ ] item 3

## Live Log
- timestamp: action taken, result, next action

## Blockers
...

## Next Action
...
```

Executor must update this after meaningful progress.

This replaces vague “keep working” behavior with visible milestone pressure.

---

### 2.3 Human Artifact: Wiki Page

After closure, produce a readable wiki-style summary.

Purpose: human command center.

Format may include:

```text
HTML tables
Mermaid diagrams
before/after summaries
decision tables
known risks
next tasks
```

Markdown files remain source-of-truth. Wiki/HTML is the readable dashboard layer.

---

## 3. Minimal Flow

### Level 0 — Tiny Task

```text
intent → execute
```

Use for:

```text
one-line commands
typos
small diagnostics
obvious local fixes
```

---

### Level 1 — Small Task

```text
intent → short plan → execute → optional review
```

Use for localized low-risk changes.

---

### Level 2 — Standard Task

```text
objective lock
→ XML task packet
→ plan review
→ burn-down fix
→ execution
→ implementation review
→ closure
```

Use for normal feature/workflow changes.

---

### Level 3 — Strict Task

```text
objective lock
→ adversarial plan review
→ user approval
→ execution milestone
→ tailing reviewer
→ cross-model implementation review
→ fix loop
→ closure
→ wiki summary
```

Use for:

```text
infra
database
security
trading logic
agent behavior
large refactors
destructive ops
```

---

## 4. Roles

Keep only four roles.

### Manager

Owns:

```text
objective lock
plan
scope containment
next step
closure summary
```

### Supervisor

Owns:

```text
plan review
burn-down list
execution readiness
```

### Executor

Owns:

```text
implementation
milestone updates
checks
execution result
```

### Reviewer / Tailer

Owns:

```text
implementation review
P0/P1 defect finding
lint/quality enforcement
fix instructions
```

No permanent research/build/critic swarm.

Use modes, not many standing agents.

---

## 5. Tailing Reviewer Mode

For important active work, run a second reviewer/tailer.

Purpose: catch issues while the executor is still active.

Pattern:

```text
Executor works against milestone burndown.
Reviewer tails docs/diffs/logs.
Reviewer reports P0/P1 issues only.
Executor fixes bounded findings.
```

Reviewer must focus on:

```text
P0: correctness/security/data-loss/blocking failure
P1: serious regression, broken validation, bad architecture drift
```

Reviewer must not nitpick.

---

## 6. Single-Item Long-Running Lane

For rote or fragile extraction tasks, avoid giant batch plans.

Use:

```text
one item
→ verify
→ log result
→ update accuracy/error notes
→ next item
```

This is for tasks where agents tend to overbatch and hallucinate success.

Examples:

```text
financial extraction
document parsing
dataset cleanup
classification
manual-style verification
```

The milestone file becomes the progress ledger.

---

## 7. Memory Rule

Memory is not truth.

Memory is retrieval assistance.

Authoritative state lives in:

```text
gateway task state
milestone file
project context file
closure summary
decision log
```

Optional memory tools can store:

```text
session summaries
important facts
recurring mistakes
project preferences
handoff summaries
```

But memory writes must be compressed and tagged.

Recommended memory write format:

```xml
<memory_entry>
  <project></project>
  <task_id></task_id>
  <type>decision | finding | risk | preference | failure_pattern</type>
  <content></content>
  <source></source>
  <staleness_rule></staleness_rule>
</memory_entry>
```

Cheap models can be used for async memory compression, but not for final truth.

---

## 8. RAG / Quote Rule

Do not dump huge retrieved context into the prompt.

Use curated snippets.

Preferred format:

```xml
<context_snippets>
  <snippet id="S1" source="...">
    <quote></quote>
    <why_relevant></why_relevant>
  </snippet>
</context_snippets>
```

Rules:

```text
small snippets
source-tagged
relevance explained
not instruction-overriding
not mixed with task commands
```

This allows the model to reference context without being hijacked by it.

---

## 9. Model / Provider Smoke Test

Before trusting a new provider/model route, run a cheap identity/quality smoke test.

Purpose:

```text
detect wrong model serving
detect broken provider quality
detect unstable outputs
```

Minimum test:

```text
same prompt
low temperature
multiple responses
check consistency
check instruction following
check expected style/format
```

Do not hardcode provider trust.

---

## 10. Skills / AGENTS.md Rule

Keep skills and AGENTS.md small.

Do not overload them with huge doctrine.

AGENTS.md should contain:

```text
how to start
where context lives
how to update milestone/context
what artifacts are required
what not to do
```

Detailed workflow belongs in task packets and milestone files, not global always-loaded instructions.

---

## 11. Gateway-Owned State

The gateway stores only:

```text
flow_run_id
task_id
current_stage
objective_lock
approved_plan
plan_review
burn_down_items
execution_result
implementation_review
waived_findings
closure_summary
role_assignments
artifact_links
```

Do not build a large new platform data model.

---

## 12. Optional Adapters

Adapters are tested after the core works.

| Adapter                  | Use                                | Required |
| ------------------------ | ---------------------------------- | -------: |
| agentmemory / claude-mem | session memory                     |       no |
| codebase-memory-mcp      | repo intelligence                  |       no |
| task-orchestrator MCP    | external task graph / gate backend |       no |
| wiki renderer            | human dashboard                    |       no |
| pgvector / vector DB     | curated snippet retrieval          |       no |

The harness must run without them.

> The **task-orchestrator MCP** row is the slot where a decomposition / task-graph
> backend would dock **if ever wired**. `docs/PRIOR_ART_MAX_REUSE.md` records what the
> retired MAX project contributes to that slot (a `SubTask` DAG shape + a decomposer
> *prompt pattern*) and the load-bearing rule that keeps it optional: the kernel must
> keep running with all of it off ("Required: no").

---

## 13. One-Week Build Scope

Build only this first:

```text
1. FlowRun record
2. Objective Lock generator
3. XML Task Packet generator
4. Plan Review generator
5. Burn-down revision loop
6. Executor handoff
7. Implementation Review
8. Closure summary
9. Milestone file update requirement
```

Do not build yet:

```text
automatic model routing
full memory backend
codebase graph integration
task-orchestrator integration
wiki automation
provider benchmarking suite
multi-agent autonomous company loop
```

---

## 14. Success Criteria

The first version succeeds if:

```text
medium/high tasks stop entering execution vaguely
executor receives a locked task packet
reviewer catches more defects
closure captures what changed and what follows
user does less translation between agents
workflow can be bypassed for tiny tasks
```

---

## 15. Locking Statement

This is not a multi-agent platform.

It is a small gateway-attached workflow kernel.

It uses:

```text
XML for model-facing packets
milestone burndown for long-running work
cross-model review for quality
compressed memory for continuity
wiki output for human readability
feature flags for scope control
```

Everything else is optional.
