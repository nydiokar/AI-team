# Adversarial Task Harness v0.5 — Minimal Workflow Kernel

Status: scoped operating spec (revised after adversarial review, 2026-07-03)
Host: existing gateway
Goal: improve task execution quality without building a workflow platform.

> **What changed from v0.4 (read this):** v0.4 contradicted itself — §15 called
> this "not a platform," while §11/§13 specified gateway-owned flow state
> (`current_stage`, a stage machine). In this codebase a task is a **single
> dispatch unit** (`orchestrator.submit_instruction` → one backend turn → one
> `mesh_tasks` row); there is no multi-stage flow object today. v0.5 resolves the
> fork: **the loop is prompt-and-artifact discipline, not gateway state.** The XML
> task packet and the milestone file ARE the state. Gateway-owned flow state is a
> deferred Phase 2 (§16), built only if the discipline proves insufficient.

---

## 0. Decision of record (the fork v0.4 left open)

**The harness adds ZERO new gateway state in v1.** It is enforced by three files
and a dispatch convention, not by a `flow_runs` table or a stage machine.

- The **XML task packet** locks intent + plan + execution rules (model-facing prose).
- The **milestone burndown file** is the resumable progress ledger.
- The **dispatch convention** (`.ai/dispatch/*` + optional `.task.md` auto-pickup)
  is how a stage handoff happens: one dispatched turn per stage.

Rationale: this project's #1 scar is burned tokens and false-success. A flow
engine multiplies model calls and adds schema that nothing reads. A senior does
not build machinery to fix instruction drift — the fix is a locked packet and a
visible ledger. Everything the gateway already has (`mesh_tasks` ledger,
`load_compact_context`, file-memory) is reused, not duplicated.

---

## 1. Core Rule

The harness is a small task-quality loop:

```text
intent → objective lock → plan → adversarial review → execution → implementation review → closure
```

This loop is **modular and skippable**. Every stage can be skipped for tiny
tasks (see the level rules in §3 — they are concrete, not vibes). Every external
tool is optional. The core works with **no** external memory, task orchestrators,
or codebase-graph tools, and it must never require a paid model call to run
(see the Test Cost Guard, repeated in §9).

---

## 2. Main Artifacts

Three artifact types. None is parsed by code — they are model-facing structure
and human-readable records. Do not build a validator for any of them.

### 2.1 Machine Artifact: XML Task Packet

XML-style structure for **model-facing** instructions. Purpose: reduce
instruction drift. It is *prose in a stable shape*, not a schema with a consumer.

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

Markdown stays acceptable for humans. XML is preferred for the model-facing packet.

### 2.2 Work Artifact: Milestone Burndown File

Each medium/high task gets a milestone file (lives next to the dispatch, e.g.
`.ai/dispatch/<task-id>.milestone.md`). Purpose: make long-running agent work
inspectable and resumable — it is the real state of the run.

```markdown
# Milestone: T-014 Gateway Harness Slice 1

## Objective
...

## Current Status
drafting / executing / reviewing / blocked / closed

## Burndown
- [ ] item 1
- [ ] item 2

## Live Log
- timestamp: action taken, result, next action

## Blockers
...

## Next Action
...
```

The executor **must** update this after meaningful progress. This replaces vague
"keep working" behavior with visible milestone pressure. On resume, this file +
`load_compact_context(task_id)` (§7) is the ground truth — not model memory.

### 2.3 Human Artifact: Wiki Page (optional)

After closure of a **Level 3** task, optionally produce a readable summary
(HTML tables, Mermaid, before/after, decision table, known risks, next tasks).
**Markdown is source of truth; the wiki/HTML layer is optional** (it is listed as
optional in §12 and is never a shipping gate). Do not automate it in v1.

---

## 3. Minimal Flow — with concrete level triggers

The level is chosen by a **rule**, not by feel, so an autonomous agent can pick
it deterministically. When in doubt, escalate one level.

**Level 3 triggers (any one ⇒ strict):** DB migration; auth/security/secret
handling; mesh/distribution/worker code; trading logic; agent-behavior or
autonomy changes; a destructive/irreversible op; a change touching **> ~5 files**
or crossing a service boundary; anything the operator flags as high-risk.

**Level 2 (standard):** a normal localized feature/workflow change that isn't a
Level 3 trigger and isn't a one-liner.

**Level 1 (small):** a localized, low-risk change, single file, obvious fix.

**Level 0 (tiny):** one-line commands, typos, small diagnostics, obvious local fixes.

### Level 0 — Tiny
```text
intent → execute
```

### Level 1 — Small
```text
intent → short plan → execute → optional review
```

### Level 2 — Standard
```text
objective lock → XML task packet → plan review → burn-down fix
→ execution → implementation review → closure
```

### Level 3 — Strict
```text
objective lock → adversarial plan review → user approval → execution milestone
→ checkpoint reviewer → cross-model implementation review → fix loop → closure → (optional) wiki
```

**Cost discipline (mandatory):** each stage is another model call. For Level ≤ 1,
review defaults to **off**. No stage may invoke a paid CLI to "verify" (§9). Cap
the plan↔review↔fix loop at a small, stated number of rounds (default **2**) and
stop — a locked-but-imperfect packet beats an infinite review spiral.

---

## 4. Roles

Four roles. These are **modes**, not standing agents — there is no permanent
research/build/critic swarm. One model plays one role per dispatched turn.

- **Manager** — objective lock, plan, scope containment, next step, closure summary.
- **Supervisor** — plan review, burn-down list, execution readiness.
- **Executor** — implementation, milestone updates, checks, execution result.
- **Reviewer / Tailer** — implementation review, P0/P1 defect finding, quality
  enforcement, bounded fix instructions.

---

## 5. Checkpoint Reviewer Mode (was "tailing reviewer")

> **v0.4 said the reviewer "tails" the executor live. There is no concurrent-agent
> primitive here** — dispatches are sequential single turns, and two live agents on
> one working tree is a merge/race hazard. Reframed as sequential checkpoints.

For important active work, the executor commits at a **milestone checkpoint**, then
a reviewer runs against the **committed diff** (not a live stream):

```text
Executor works a burndown slice → commits at checkpoint → Reviewer reviews the diff.
Reviewer reports P0/P1 only. Executor fixes bounded findings → next slice.
```

Reviewer focus:
```text
P0: correctness / security / data-loss / blocking failure
P1: serious regression, broken validation, bad architecture drift
```
Reviewer must not nitpick. The existing `/code-review` and `/security-review`
skills are the mechanism; the review artifact uses the house F-tag style (§14).

---

## 6. Single-Item Long-Running Lane

For rote or fragile extraction tasks, avoid giant batch plans:

```text
one item → verify → log result → update accuracy/error notes → next item
```

This directly targets the recorded failure where agents overbatch and hallucinate
success (see the `false-success-intent-only` and `single-item` failure pattern).
Examples: financial extraction, document parsing, dataset cleanup, classification,
manual-style verification. The milestone file is the progress ledger.

---

## 7. Memory Rule — reuse what exists, invent nothing

> **v0.4 invented a fresh memory store and "async cheap-model compression." This
> project already has two memory systems.** Do not build a third.

Memory is retrieval assistance, **not** truth. Authoritative state lives in:
```text
mesh_tasks ledger (DB-canonical conversation + artifacts, migration 17)
milestone file
project context file (.ai/CONTEXT.md)
closure summary / decision log
```

The two real memory surfaces to use:
1. **`orchestrator.load_compact_context(task_id)`** — already returns bounded
   prompt/summary/files/usage/errors/constraints from the DB-canonical ledger.
   This is the resume/handoff memory. **Wired (2026-07-03, #31/#32):** a task that
   declares `continues: <prior_task_id>` in its frontmatter/metadata gets this
   prior context prepended to its prompt (fenced, reference-only) by
   `process_task`. See §14 "`continues:` continuation field" for the convention.
   Tasks without `continues:` are byte-identical to before — the injection is
   strictly opt-in.
2. **File-memory** (`MEMORY.md` + `memory/*.md`) — durable facts/decisions/failure
   patterns across sessions.

If (and only if) a fact belongs in file-memory, the recommended write shape is:

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

This is a **write format for file-memory**, not a new database. There is no
required async compression service; if a cheap local model summarizes, it is
best-effort and never the source of truth.

---

## 8. RAG / Quote Rule

Do not dump huge retrieved context into the prompt. Use curated, source-tagged
snippets whose relevance is stated and which never override instructions:

```xml
<context_snippets>
  <snippet id="S1" source="...">
    <quote></quote>
    <why_relevant></why_relevant>
  </snippet>
</context_snippets>
```

Rules: small; source-tagged; relevance explained; not instruction-overriding; not
mixed with task commands.

---

## 9. Provider Smoke Test — onboarding only, NOT a per-task stage

> **v0.4 put this in the task loop. That invites a paid-CLI call on every task —
> a direct Test Cost Guard violation.** It belongs to *provider onboarding*, run
> deliberately, not to per-task flow.

> ⚠️ **TEST COST GUARD.** Tests/flow must not invoke the paid Claude/Codex CLI.
> Never run the full e2e suite "to verify." Real e2e is OpenCode-only
> (`AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`). Never run
> `python main.py status` (kills the live PM2 gateway); check with
> `curl http://127.0.0.1:9003/health`.

When onboarding a new provider/model route (not per task), run a cheap
identity/quality smoke: same prompt, low temperature, multiple responses; check
consistency, instruction-following, expected style/format. Do not hardcode
provider trust. This overlaps the existing LLM-turn-observability smokes and the
backend `doctor` probes — reuse those; don't build a parallel harness. Any paid
run needs explicit operator approval.

---

## 10. Skills / AGENTS.md Rule

Keep skills and AGENTS.md small. AGENTS.md should contain only:
```text
how to start · where context lives · how to update milestone/context
· what artifacts are required · what not to do
```
Detailed workflow lives in task packets and milestone files, **not** in
always-loaded global instructions.

---

## 11. Gateway-Owned State — NONE in v1

> **v0.4 listed a full flow data model here.** In v1 the gateway stores **nothing
> new**. The `flow_run_id / current_stage / plan_review / ...` model is deferred to
> Phase 2 (§16) and is built only if the file-and-dispatch discipline proves
> insufficient in practice.

The harness rides on state that already exists: the `mesh_tasks` ledger, session
records, `load_compact_context`, and the dispatch/milestone files. Do not build a
new platform data model to make v1 work.

---

## 12. Optional Adapters

Tested only after the core works. The harness must run without all of them.

| Adapter                  | Use                                | Required |
| ------------------------ | ---------------------------------- | -------: |
| agentmemory / claude-mem | session memory                     |       no |
| codebase-memory-mcp      | repo intelligence                  |       no |
| task-orchestrator MCP    | external task graph / gate backend |       no |
| wiki renderer            | human dashboard                    |       no |
| pgvector / vector DB     | curated snippet retrieval          |       no |

---

## 13. One-Week Build Scope (v1)

Because v1 is prompt-and-artifact discipline, "build" mostly means **authoring the
generators and conventions**, not gateway code.

Build only this first (v1 shipped on `feat/task-harness`, A9H — `docs/harness/`):
```text
[x] 1. Level-selector rubric (§3 triggers)          → docs/harness/level_rubric.md
[x] 2. Objective-Lock + XML Task-Packet generator   → docs/harness/packet_template.xml
                                                       + generators/draft_packet.md
[x] 3. Plan-Review generator (F-tagged findings)    → generators/adversarial_review.md
[x] 4. Burn-down / milestone template + fix loop     → docs/harness/milestone_template.md
[x] 5. Executor handoff via dispatch convention      → docs/harness/dispatch_pipeline.md
[x] 6. Checkpoint Implementation-Review generator    → generators/adversarial_review.md (§5)
[x] 7. Closure-summary generator                     → generators/closure_summary.md
[x] 8. Milestone-update requirement (dispatch prompt)→ milestone_template.md update-rule
                                                       + dispatch_pipeline.md step 5
[x] 9. Dispatch Pipeline meta-process end-to-end     → docs/harness/dispatch_pipeline.md
       (the `continues:` resume-memory field itself shipped earlier via A9/#31/#32;
        the pipeline now documents how to use it — §7/§14)
+  Auto-pickup safety: Level-3 guard (convention + flag-guarded backstop)
   → orchestrator.py::_harness_level3_allows_autopickup, tests/test_harness_level3_guard.py
```

Do **not** build yet:
```text
gateway flow_runs table / stage machine (Phase 2, §16)
automatic model routing · full memory backend · codebase-graph integration
task-orchestrator integration · wiki automation · provider benchmarking suite
multi-agent autonomous company loop
```

---

## 14. The Dispatch Pipeline (meta-process) — first-class

This is how a task moves from idea to executed change. It is the workflow the
existing `.ai/dispatch/AGENT_8_*` files already follow; here it is codified so it
runs repeatably and, where safe, hands off automatically.

```text
(1) DRAFT        a drafting model ("text engine" role — any capable model, e.g.
                 an OpenCode/cheap route) turns intent + curated context into an
                 XML Task Packet + milestone file.
(2) REVIEW       an adversarial pass challenges assumptions and finds P0/P1 issues,
                 emitting F-tagged findings (F1, F2, …).
(3) FIX          the packet/plan is revised INLINE against each F-tag; a short,
                 stated max of rounds (default 2). Unresolved items become explicit
                 non-goals or logged risks — never silently dropped.
(4) DISPATCH     the finalized packet is written to `.ai/dispatch/<NAME>.md`
                 (+ optional `.task.md` with YAML frontmatter so the file-watcher
                 auto-enqueues it — the existing auto-pickup primitive:
                 `file_watcher.py → _handle_new_task_file → task_parser`).
(5) EXECUTE      an executor agent picks it up, works the burndown, updates the
                 milestone file, commits at checkpoints.
(6) CHECKPOINT   a reviewer reviews the committed diff (§5), P0/P1 only, F-tags;
                 executor fixes bounded findings.
(7) CLOSE        closure summary + milestone → closed; update `.ai/CONTEXT.md` /
                 `.ai/dispatch/DISPATCH_LOG.md`; (Level 3) optional wiki.
```

**F-tag convention (house style, from AGENT_8):** each finding gets a stable id
`[Fn]`, a one-line defect statement, and a concrete failure scenario. The fix is
applied inline at the exact step it guards, and the implementation log records the
outcome per tag (`fixed` / `accepted` / `no change needed`). See
`.ai/dispatch/AGENT_8_OPERATOR_SIGNAL.md` and its `_REVIEW.md` for the reference
shape.

**Auto-pickup boundary (safety):** auto-enqueue via `.task.md` is allowed for
Level ≤ 2. **Level 3 requires the operator-approval stage before dispatch** — no
autonomous pickup of destructive/infra/security/agent-behavior work.

**"Text engine" is a role, not a system.** It means "the model that drafts the
packet." Any route can play it; a cheaper route is fine for DRAFT, a strong route
for REVIEW. Do not build a new service called a text engine.

### `continues:` continuation field (the resume-memory handoff)

When one dispatched turn continues the work of a prior turn, the new task declares
the prior task id in its frontmatter/metadata:

```yaml
---
id: T-014-slice-2
type: fix
priority: medium
continues: task_99bc7bec        # the prior task id whose result to resume from
---
```

Behavior (opt-in, prose convention — **nothing parses a schema for it**):
- The **only** consumer is `orchestrator.process_task`, which reads
  `task.metadata["continues"]` and, when present and non-empty, prepends the prior
  task's bounded compact context (`load_compact_context`, §7) to the prompt as a
  fenced **reference-only** `<prior_context>` block; the live instruction stays
  verbatim inside `<current_instruction>`.
- **Absence of `continues:` = today's behavior** — no loader call, no prompt
  change. There is no auto-detection of "the previous task."
- It degrades to a no-op on self-reference, unknown/empty prior context, or any
  loader failure — a continuation never crashes a turn. The injected block is hard-
  capped (~4 KB) independent of the loader's own field caps.
- `continues:` rides in `task.metadata`, so it also works from
  `submit_instruction(..., extra_metadata={"continues": "<id>"})` (Telegram/CLI/
  Web), not only `.task.md` files.

This closes #31/#32: the harness is the workflow that finally consumes
`load_compact_context`. Coverage: `tests/test_compact_context_injection.py`.

---

## 15. Success Criteria

v1 succeeds if:
```text
medium/high tasks stop entering execution vaguely
the executor receives a locked task packet
the reviewer catches more real defects (P0/P1), not nits
closure captures what changed and what follows next
the operator does less translation between agents
tiny tasks can bypass the whole thing
no stage ever burns a paid CLI call to "verify"
```

---

## 16. Phase 2 (deferred, build only if needed) — Gateway-owned flow

If the file-and-dispatch discipline proves insufficient (e.g. handoffs get lost,
or you need queryable flow status across many tasks), THEN consider promoting the
loop into gateway state: a `flow_runs` record (`flow_run_id`, `task_id`,
`current_stage`, `objective_lock`, `approved_plan`, `plan_review`,
`burn_down_items`, `execution_result`, `implementation_review`, `waived_findings`,
`closure_summary`, `role_assignments`, `artifact_links`), stage transitions, and a
driver. This is the "large platform" v0.4/§15 warned against — do not build it
speculatively.

---

## 17. Locking Statement

This is not a multi-agent platform. It is a small, prompt-and-artifact workflow
kernel that rides on existing gateway state. It uses:
```text
XML for model-facing packets
milestone burndown for long-running, resumable work
sequential checkpoint cross-model review for quality
the existing DB ledger + file-memory for continuity (nothing new invented)
the dispatch/.task.md convention for handoff and auto-pickup
level rules + a cost cap for scope and token control
```
Everything else is optional or deferred.
