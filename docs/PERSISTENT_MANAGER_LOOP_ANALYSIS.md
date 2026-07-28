# Persistent Manager Loop — Architecture Analysis & Boundary Map

> **⚠️ PARTIALLY SUPERSEDED (2026-07-28).** This is the *opening* analysis of a five-turn design
> session that then **revised three of its own conclusions on evidence.** The converged design is
> [`docs/AUTONOMOUS_CASE_CONTINUATION_DESIGN.md`](AUTONOMOUS_CASE_CONTINUATION_DESIGN.md) (milestone
> contract: `Task_Harness_v0.7_AUTOMATION.md` §M3.4). **Still valid:** §0 (external primary
> sources) and §1–§4 (code-grounded diagnosis — no coded loop, LLM decides post-worker, durable
> state inventory, human-loop boundary). **Superseded — read the design doc instead:**
> - §6 "de-scope M4" → **M4 is HYBRID, not de-scoped** (parked spike; design §7).
> - §7 Job 1's direct `task.finished → inject Manager turn` callback → **replaced** by a
>   condition-gated, coalesced, at-least-once, **`mesh_tasks`-leased** re-entry (design §2–§5).
> - §5/§7 "new per-case round/cost cap / budget columns" → **no new columns**; cap lives in
>   `completion_criteria`, round count = continuation generation (design §3–§4, §6).
> - §7 Job 3 "`dispatch_parallel`" → folded into the parked M4 hybrid spike, not a near-term job.

**Authored:** 2026-07-25
**Status:** Reference doctrine (durable), partially superseded — see banner above. Not a dispatch
packet, not current-focus.
**Question it answers:** Are we building the persistent *outer* management loop that
Anthropic's agent/workflow machinery runs *inside*, or are we redundantly rebuilding
capabilities Anthropic already supplies while still failing to remove the human from the
control loop?

**Owner surface:** this doc holds the *comparison + boundary doctrine*. Live status →
`.ai/CONTEXT.md`. Milestone definitions → `docs/Task_Harness_v0.7_AUTOMATION.md` +
`docs/M3_MANAGER_INVOCATION_SPEC.md`. Job packets → `.ai/dispatch/`.

> **One-line verdict:** the durable *case* layer is correct and ~80% built (M0–M3.3
> merged); the loop's continuity is entirely the LLM agent loop **inside one turn**;
> nothing in code re-enters the loop **across turns**, so the human is still the engine at
> every session/turn boundary — ignition, re-wake after a worker finishes, crash-resume,
> merge, and direction.

---

## 0. External primary sources (what NOT to rebuild)

Grounded in primary sources only (no framework survey; LangGraph explicitly excluded).

### 0.1 Karpathy AutoResearch — the continuous outer loop
- Loop, verbatim (`program.md`): "Tune `train.py`… git commit. Run the experiment… Read
  out the results… Record the results… If val_bpb improved you 'advance' the branch… If
  equal or worse, you git reset back." = *change → execute → evaluate → retain/reject →
  repeat*.
- What makes it continuous: "do NOT pause to ask the human if you should continue… You are
  autonomous… The loop runs until the human interrupts you, period." Stop is **external**,
  not goal-based.
- Safe because the **action space is constrained** (may edit only `train.py`; `prepare.py`
  the judge is immutable) and **git is the durable retain/reject ledger**.
- Framing: remove the human as the bottleneck between human and AI; "any metric reasonably
  efficient to evaluate can be autoresearched."
- Sources: https://github.com/karpathy/autoresearch/blob/master/program.md ·
  https://github.com/karpathy/autoresearch

### 0.2 Claude Agent SDK — the loop it ALREADY provides
- Inner loop out of the box: **"gather context → take action → verify work → repeat."**
- Owned by the SDK (do not rebuild): context compaction on long runs, agentic file search,
  **subagents (parallelism + isolated context)**, tools/MCP plumbing + auth, **sessions**,
  **permissions**, hook-based verification.
- Source: https://claude.com/blog/building-agents-with-the-claude-agent-sdk

### 0.3 Anthropic "Building effective agents" + dynamic workflows
- Distinction, verbatim: **Workflows** = "LLMs and tools orchestrated through predefined
  code paths"; **Agents** = "LLMs dynamically direct their own processes and tool usage."
- Five workflow patterns: prompt chaining, routing, parallelization (sectioning/voting),
  **orchestrator-workers** (dynamic decomposition + synthesis), evaluator-optimizer (loop).
- Rule for *dynamic* vs *fixed*: use fixed workflows when subtasks are known/stable; use
  **orchestrator-workers** (or agent-authored code/Skills) only "when you can't predict the
  subtasks needed."
- Agent-authored code as orchestration ("code execution with MCP"): present MCP servers as
  a code API and let the agent write/run code — decomposition/branching/loop-until-done as
  a program the agent writes; agents can persist reusable Skills.
- Sources: https://www.anthropic.com/engineering/building-effective-agents ·
  https://www.anthropic.com/engineering/code-execution-with-mcp

### 0.4 Three-layer distinction (synthesis)
- **(a) Agent loop** — model↔tool iteration within one session until a stop condition. Owns
  *one bounded task*. SDK-provided.
- **(b) Workflow / task-graph** — structure across calls (sequence/branch/fan-out/dynamic
  decompose+synthesize). Fixed or agent-generated. Owns *how a task is split & recombined*.
- **(c) Persistent manager loop** — owns a **long-lived objective/case** across many runs,
  **durable state across sessions**, **replanning toward a metric**, and **closure**.
  Neither (a) nor (b) provides this. **This is the only layer we should build ourselves.**

### 0.5 Boundary table — who owns what
| Capability | Owner |
|---|---|
| Model↔tool inner loop (gather→act→verify→repeat) | **Claude Agent SDK** |
| Context compaction, sessions, checkpointing, permissions, MCP | **Claude Agent SDK** |
| Subagent fan-out / parallel decomposition | **Claude Agent SDK / dynamic workflow** |
| Dynamic agent-authored workflow (parallel decompose + repeated verify) | **SDK orchestrator-workers / code-exec-with-MCP** |
| Case ownership (one objective, durable membership) | **Ours** |
| Durable state / audit history across sessions & resets | **Ours** |
| Policies, permissions-to-close, budgets, escalation | **Ours** |
| Completion criteria + final closure authority | **Ours** |

Bottom line: **Anthropic runs the agent; we own the case.**

---

## 1. What continuous loop exists today — in CODE, not prompts

**There is NO coded continuous control loop.** Every iteration (dispatch → wait → judge →
replan → dispatch → close) is a decision the Manager **LLM** makes by electing to call the
next MCP tool. The word "continuous" lives in the role *prompt*, not in a mechanism.

Evidence:
- `scripts/mcp_manager.py` is nine independent one-shot MCP tools behind a flat JSON-RPC
  switch (`_dispatch`, `mcp_manager.py:1049`; `_TOOL_IMPLS`, `:1021`). No function chains
  `dispatch → wait → judge → dispatch`.
- `invoke_manager` (`src/orchestrator.py:2244`) is a **one-shot boot**: create session →
  `open_case` → submit **one** first-assignment turn (`:2342`) → return
  `{session_id, case_id, task_id}` (`:2349`). Nothing re-enters it.
- The only loop in the tool surface is the in-turn poll inside `_wait_for_worker`
  (`mcp_manager.py:452`) — it blocks **within one Manager turn** until a `task.finished`
  event or timeout, then returns a text string telling the LLM to decide.
- Role prompt (`docs/harness/roles/manager.md:25-27`): "Work in a continuous case-level
  loop… and continue." = **text handed to the model, not a coded driver.**

So the "loop" is the **SDK agent loop**, bounded by one operator invocation — which is the
*correct* design per §0.2/§0.3, but it means continuity holds only *inside* a live turn.

---

## 2. Who decides what happens after a worker finishes

**(b) The Manager LLM, in its own turn.** Not coded logic, not (only) a human — but a human
is required to *start/continue* the turn.

- `_record_flow_terminal_outcome` (`orchestrator.py:2596`) emits exactly one append-only
  `task.finished` flow_event (`:2621-2627`) and **deliberately writes nothing else** — a
  task ending updates TASK state only; a Case's status changes solely via `close_case`.
- `wait_for_worker` returns a **string telling the LLM to decide** ("Review the committed
  diff… then close the Case authoritatively… or inspect and decide rework/close",
  `mcp_manager.py:490-496`). No code computes a verdict.
- **No auto-wake.** The proactive-turn path (`claude_driver.py:700`) fires only for a
  session continuing *itself* and routes to the **operator** notification channel
  (`task_server.py:981-1023`) — there is **no** path where worker A's `task.finished`
  injects a turn into Manager B's session.
- The durable relay (`reconcile_waits`, `mcp_manager.py:533`) is **pull-only** — the LLM
  must call it; the ledger (`worker.wait_pending`/`worker.wait_resolved`,
  `db.py:131-152, 2143-2221`) is passive. If the gateway crashes mid-wait, the wait is
  simply lost until the LLM chooses to reconcile.

**The single forcing function in code is `close_case`'s refusal guard**
(`mcp_manager.py:712-717`): it refuses to close while `completion_criteria` are unmet/
unwaived. Everything else is *availability*, not *compulsion*.

---

## 3. Can the Manager inspect / judge / replan / dispatch / rework / recover / resume?

Each capability **is a tool**; nothing **forces** continuity across turns.

| Capability | Tool | Forced? |
|---|---|---|
| inspect results | `wait_for_worker`, `get_case`, `read_session_history` | no |
| judge vs objective | none (verdict is prompt-only); `record_review` only *records* it | no |
| replan / dispatch again | `dispatch_worker`, `open_case` | no |
| request correction | `record_review(rework_requested)` + new `dispatch_worker` | no |
| recover from failure | `reconcile_waits` (pull-only) | no |
| close | `close_case` (refuses until criteria reconciled) | **guard only** |

**Durable case state (resume across sessions/resets):** the *facts* are durable and
reconstructable; the *Manager's cognition* is not.

- **Persisted (DB, survives restart):** objective (`flow_runs.objective_lock`),
  `completion_criteria` (migration 24), `current_stage` (shadow), review verdicts
  (`flow_events`: `review.accepted`/`rework_requested`/`waived`), wait markers
  (`worker.wait_pending`/`resolved`, flag-gated), dispatch lineage
  (`parent_flow_run_id`/`dispatched_by`/`dispatch_file`), session affiliation
  (`sessions.current_case_id`/`case_role`). **A Case == a `flow_runs` row; there is no
  `cases` table.**
- **Reconstruction read path:** `GET /api/work/{id}` (full record + `flow_links` + parent +
  children) **plus** `GET /api/work/{id}/timeline` (full `flow_events`). Note the MCP
  `get_case` tool (`mcp_manager.py:571`) returns objective+criteria+stage **only** — NOT
  dispatched workers or verdicts; the Manager must *also* read the timeline.
- **NOT persisted (in-process only, lost on crash/reset):** the Manager's plan / "what to
  do next" decision (`approved_plan` is an unused RECORD column), the live
  `wait_for_worker` poll, and any reasoning not emitted as a `review.*`/status/link. A
  resumed Manager must **re-derive** next-action from the ledger.

---

## 4. Human-loop diagnosis — where the operator is still the engine

*Within* one bounded invocation the operator is **not** the engine: A41/A42/A43/A44/F1
proved a Manager driving multi-task, review-gated, rework-and-accept sequences to
`close_case` in one persistent session with no per-step prompts. The engine-role is
concentrated at **boundaries**:

1. **Ignition (Rank-0).** PR #37 (`aaf1cb2`) `setting_sources=["user","project"]` reconnects
   the Manager MCP tools but is **merged-not-live**; a fired Manager currently boots
   **tool-less** (verified live, Case `1b59822e…`) and cannot dispatch. Requires the
   operator's gateway restart.
2. **Cross-turn re-wake.** After `task.finished`, nothing injects the next Manager turn —
   only a human message does. Live proof ran "TWO Cases across THREE operator turns"
   (2026-07-17): the operator supplied each continuation.
3. **Closure/merge/deploy.** Branch policy: "Merging to main, deploys, and gateway restarts
   are the operator's decision." Every deliverable PR sits open, op-merge.
4. **Direction.** Node acceptance (Rank-1) is operator-gated/paid; "direction beyond the
   survivability arc" is an explicit operator fork.

---

## 5. Current gap — what blocks continuous case-driving today

In dependency order:
1. **Inert dispatch (Rank-0).** Tool-less Manager until the `aaf1cb2` restart. *Operator-gated.*
2. **No re-entry across turns.** `task.finished` records an event but wakes no one
   (`orchestrator.py:2596-2628`). This absence is the mechanism that makes the operator the
   per-worker continuation engine.
3. **Wait not durable-by-default.** `wait_for_worker` is an in-process poll that dies with
   the subprocess; `DURABLE_RELAY_ENABLED` is **OFF by default** and, when ON, **pull-only**.
4. **Manager cognition not persisted.** No column/event stores the plan or next-move;
   resume re-derives by stitching `/api/work/{id}` + `/api/work/{id}/timeline`.

---

## 6. Roadmap correction — retain / remove / reorder / add

**Retain (correct, load-bearing):** `flow_runs`/`flow_links`/`flow_events` substrate,
`completion_criteria` close-gate, `record_review` emitter, role profiles, carrier-
independent Manager (#18), durable-relay design (#38). This *is* the persistent-objective
layer the SDK should run inside.

**Remove / don't build:**
- Any coded orchestration loop chaining dispatch→wait→judge in Python — it duplicates the
  SDK agent loop. Keep the LLM-drives-via-tools design.
- **De-scope M4's custom "task-DAG inside one Case."** Replace with "Manager invokes SDK
  subagents / a dynamic orchestrator-workers workflow," recording only the *synthesized
  outcome* to the Case ledger. A DAG engine reinvents SDK capability.

**Reorder:** Rank-0 restart before all else (nothing is observable without it); make the
durable relay **default-ON + push** before the paid node acceptance so it exercises crash-
resumable continuity.

**Add (the genuinely missing piece — ours to build):** a **bounded cross-turn re-entry** —
on `task.finished` for a Case with a live/resumable Manager, inject a proactive Manager turn
("worker X finished; result attached; decide next"), governed by M3.3 round/turn/cost caps
and the `close_case` gate. This turns "continuous *within* a turn" into "continuous *across*
a case."

**⚠️ Escalate (operator decision — do not code past it):** the objective "remove the human
from the control loop" conflicts with the written P0 anti-goal (v0.7 §0.2): *"Always-on,
self-directed swarm… No standing autonomous process,"* and the load-bearing rule that a
capability acting *without an operator invocation bounding it* is out of scope. The
reconcilable, recommended target is **"one operator invocation → the Manager drives a whole
case (or a queue of cases) to closure without per-step prompts, surviving crashes"** — in
scope and mostly built. **Fully unattended, self-igniting, always-on** (and the parked O5
manager→manager handoff) is a separate fork requiring explicit operator sign-off before any
build removes the invocation bound.

---

## 7. Next three implementation jobs (each with an acceptance test)

Build with cheap tests (TestClient + real `MeshDB` + fake `claude` backend). **Never** the
paid e2e suite.

**Job 1 — Cross-turn Manager re-entry on worker completion (the human-removal lever).**
Wire `_record_flow_terminal_outcome` so that, for a Case whose owning Manager session is
live/resumable, a bounded proactive turn is injected ("worker `<task_id>` finished; diff at
`<ref>`; decide next"), gated by a new per-case round/cost cap and the `close_case` guard.
Flag-gated, default OFF ⇒ byte-identical.
- **Acceptance:** open Case, `dispatch_worker`, drive fake worker to `task.finished`; assert
  a new Manager turn is injected **with no human message** and calls `record_review`/
  `dispatch_worker`/`close_case`; assert it halts at the round cap and emits
  `flow.interrupted` (bounded, not unbounded). Verify via `/api/work/{id}/timeline`.

**Job 2 — Durable resume brief + auto-reconcile at boot (crash-resumable continuity).**
Add `get_case_brief` (or extend `get_case`) to return, in one call, objective +
`completion_criteria` + dispatched workers (`flow_links`) + latest review verdict per worker
+ open `worker.wait_pending` markers, from DB alone. Flip `DURABLE_RELAY_ENABLED` default ON
and auto-call `reconcile_waits` in role-boot.
- **Acceptance:** dispatch, record `worker.wait_pending`, simulate a mid-wait crash, re-boot
  the Manager session; assert boot auto-reconciles and `get_case_brief` returns the full
  in-flight state (workers + verdicts + open waits) with zero timeline-stitching by the
  caller. Covers the `DURABLE_RELAY_ENABLED=1` path.

**Job 3 — Manager-invoked dynamic workflow for parallel decomposition (instead of a DAG).**
Give the Manager a `dispatch_parallel` capability that fans a decomposable task out via SDK
subagents / an orchestrator-workers workflow, synthesizes results, and records a single
`task_attached` + one `review.*` verdict to the Case ledger — replacing M4's hand-rolled
task-DAG.
- **Acceptance:** a task with N independent subtasks runs **concurrently** (assert
  overlapping execution, not N sequential `dispatch_worker` one-offs), results synthesized,
  and the Case graph shows one decomposition node with a single verdict.

**Start with Job 1** — it is the only one that structurally removes the operator from the
per-worker-finish continuation, and the piece the roadmap never named.

---

## Sources
- Karpathy AutoResearch: https://github.com/karpathy/autoresearch/blob/master/program.md ·
  https://github.com/karpathy/autoresearch
- Claude Agent SDK: https://claude.com/blog/building-agents-with-the-claude-agent-sdk
- Building effective agents: https://www.anthropic.com/engineering/building-effective-agents
- Code execution with MCP: https://www.anthropic.com/engineering/code-execution-with-mcp

## Code anchors (verify before citing — file:line as of 2026-07-25)
- `scripts/mcp_manager.py` — tool surface (`_dispatch` :1049, `_wait_for_worker` :452,
  `get_case` :571, `close_case` guard :712, `reconcile_waits` :533)
- `src/orchestrator.py` — `invoke_manager` :2244, `_record_flow_terminal_outcome` :2596
- `src/backends/claude_driver.py` — proactive turn `_dispatch` :700
- `src/control/task_server.py` — proactive→operator fan-out :981-1023
- `src/control/db.py` — `FLOW_EVENT_TYPES` :131-152, wait markers :2143-2221,
  `flow_runs`/`flow_links`/`flow_events` migrations 21–24
- `docs/harness/roles/manager.md` — continuous-loop prompt text :25-27
