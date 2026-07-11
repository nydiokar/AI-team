# MAX Salvage Map — mining the retired orchestrator into the Task Harness

**Status:** advisory salvage map (2026-07-07). **Not a new roadmap, not a new milestone.**
This document binds reusable ideas from the retired **MAX** project (`~/dev/MAX`) onto the
**existing** harness specs. It adds **no** new build surface: everything below lands as work
*already authorized* under `docs/Task_Harness_v0.6_AUTOMATION.md` (M3/M4) or fills the
*already-reserved* optional-adapter slot in `docs/Task_Harness_v0.4.md` §12. If a line here
cannot be traced to an existing milestone or the §12 slot, it is out of scope — delete it.

**Reading order:** kernel = `Task_Harness_v0.4.md`; automation roadmap = `Task_Harness_v0.6_AUTOMATION.md`;
this file = "what, if anything, MAX lets us skip re-inventing." Cross-ref, don't restate (`.ai/DOC_MAP.md`).

---

## 0. What MAX actually is, in one paragraph

MAX is a Python re-implementation ("butchered" for personal use) of **AWS's Multi-Agent
Orchestrator** (now "agent-squad" / "Agent Squad"). Shape: `user query → Classifier → picks one
agent → agent replies → storage`. Single-process, conversational routing. ~20k LOC, last real
commit 2025-03, **never launched, killed for being too complex.** Heavy deps: `torch`+
`transformers`+`roberta` (the KPU), ChromaDB, vendored mem0, MongoDB, Ollama, Discord.

**Verdict up front:** MAX is a **parts donor, never a platform to adopt.** It is precisely the
"multi-agent autonomous company loop" that v0.4 §13 lists under *Do not build yet* and v0.6
§0.2 forbids as an anti-goal. We salvage schema and patterns; we do **not** import the stack.

---

## 1. The load-bearing guardrail (why this cannot blow scope)

Three hard boundaries, each inherited — not invented here:

1. **The kernel must run without any of this.** v0.4 §12 already classifies an external
   task-graph / orchestrator backend as an **optional adapter, "Required: no."** Every salvage
   in Tier B/C below lands *behind that boundary*. Turn it all off ⇒ the harness is byte-identical.
2. **No new ingestion lane. No always-on process.** Anything MAX contributes emits work through
   the **one existing choke point** — `orchestrator.py::_enqueue_task` — under the
   **Level-3 admission guard** (`HARNESS_LEVEL3_GUARD`, `dispatch_pipeline.md` §"auto-pickup
   safety boundary"). MAX ideas never open a side door and never spawn a standing loop
   (v0.6 §0.2 load-bearing rule).
3. **"Two things, not one."** This is the operator's own architectural instinct, and MAX
   *validates* it: MAX's single win is the separation `Orchestrator (routing) ≠ Agents
   (execution) ≠ Storage`. The salvaged planning/decomposition logic therefore belongs in the
   **Manager role / optional adapter**, **not embedded in the gateway**. Gateway stays
   transport + routing; the automation/planning layer is a *producer of work* that rides the
   same dispatch/Control-API path a worker or the Web UI uses.

If a proposed use of MAX violates any of the three, it is rejected at review — same bar as any
v0.6 dispatch.

---

## 2. Where MAX touches the harness specs (the map)

| MAX asset | Nature | Lands as | Spec anchor | Risk |
|---|---|---|---|---|
| `Task`/`SubTask` models (`storage/utils/types.py`, `types/collaboration_types.py`): `dependencies`, `completion_criteria`, `priority_score`, `estimated_hours`, `BLOCKED`, `ExecutionHistoryEntry` | schema fields | extra planning fields on the flow/packet decomposition output | v0.4 §11 field set / §2.1 packet · v0.6 **M4** | Low |
| `TaskExpertAgent` (`agents/task_expert/`): `request → LLM → structured JSON task list` | **pattern** (re-implement, don't copy) | the **DRAFT/expand** step of an invoked Manager: intent → decomposed, dependency-aware packet(s) | v0.6 **M3** step "Expands", **M4** spec-authoring | Med |
| `SupervisorAgent` + `agent_registry` (delegation, status tracking, fallback/retry, escalation) | **pattern** + **cautionary tale** (see §3 Tier A) | informs Manager delegation + worker-status/fallback in the dispatch tree | v0.6 **M2** (lineage) + **M3** | Med |
| `ChainAgent` (sequential pipeline, output→input) | **pattern** | multi-step decomposition where task N's output feeds N+1 → maps to `continues:` / `parent_flow_run_id` chains | v0.6 **M2/M3**; `.task.md` `continues:` | Med |
| `TaskStorage` ABC (`storage/abstract_storage/task_storage.py`) | interface design | reference shape **only** — behind the §12 "task-orchestrator MCP" slot if ever wired | v0.4 **§12** (optional) | Low |
| `component_guide.md`, `Phase 1 Core Infrastructure.md` | ideation | vocabulary/checklist to sanity-check M2–M4 design | v0.6 §3 | None |

---

## 3. Salvage tiers (value ÷ risk)

### Tier A — Ideation only (take now, zero code, zero risk)
- Read `MAX/Phase 1 Core Infrastructure.md` and `MAX/component_guide.md` as a **checklist**
  against v0.6 M2–M4: agent registry, supervisor delegation, fallback/retry, escalation,
  confidence thresholds, classifier fallback. Your instinct "Phase 1 says exactly what I'm
  doing now" is correct — mine it as a spec to converge against, **not** code to import.
- **Cautionary value (read before M3).** MAX's `agents/supervisor_agent.py` — the multi-agent
  **coordination** layer, the exact thing v0.6 M3 builds — opens with a self-written banner:
  `## DO NOT TRUST THIS CODE ... WORK IN PROGRESS ... FOCUS ON SINGLE AGENT IDENTIFICATION ...
  NOT ON TEAM MANAGEMENT`. **The prior project stalled precisely at the coordination layer we
  are now building.** Lesson for M3: keep the Manager a *single role driving a chain of command*
  (v0.6 §0.2), not a team-of-agents platform. MAX is direct evidence for the narrower scope, not
  a coincidence. Keep it visible as "the thing v0.6 §0.2 refuses to become."

### Tier B — Schema cherry-picks (fold into M4, no MAX import)
AI-team's task lifecycle is execution-shaped (`pending/claimed/running/completed/failed/
cancelled` + claim/lease/incarnation). It has **no decomposition/planning fields.** MAX's
`Task`/`SubTask` models have exactly those, and they slot onto v0.4 §11 / §2.1 / the M4
decomposition output:
- `dependencies: Set[task_id]` — a task **DAG** (not a flat list); already half-expressed by
  `.task.md`'s `continues:` and v0.6's `parent_flow_run_id`. Adopt the **concept**, reuse the
  existing columns.
- `completion_criteria` — a **machine-checkable "done"** per subtask; maps onto the packet's
  `<definition_of_done>` (v0.4 §2.1). The single most useful field to steal — it fights the
  #1 scar (hallucinated success) by forcing a checkable close condition.
- `priority_score`, `estimated_hours` — planning hints on a decomposed packet (non-authoritative).
- `BLOCKED` status + `ExecutionHistoryEntry` (append-only change log) — the execution-history
  idea maps onto the milestone Live Log; don't add a second store (v0.4 §7: memory is not truth).
Reimplement as fields on the **existing** model/packet — never `from MAX.storage import ...`.

### Tier C — Pattern cherry-picks (inform M2/M3, re-implement natively)
- **TaskExpert decomposition** = the single most relevant piece: it *is* the missing
  automation-intake layer (intent → dependency-aware task list). Re-implement as the Manager's
  "Expands" step (v0.6 M3) / a `spec_authoring` generator (M4). Output must be a v0.4 XML
  packet + `.task.md` frontmatter, routed through `_enqueue_task` + Level-3 guard. Its strict-JSON
  contract and `human_task` vs `agent_task` split are the reusable bits; our Manager expands into
  a *reviewed* objective-lock + packet, which is richer than MAX's one-shot JSON dump.
- **Supervisor delegation + fallback/retry + status tracking** informs M3 Manager behavior and
  M2 dispatch-lineage (which worker, what stage, what result). AI-team already has
  `src/control/node_registry.py` and mesh health — the MAX registry is a *design reference*,
  not a replacement.

### Tier D — DO NOT PORT (traps)
KPU (`torch`/`transformers`/roberta — a non-starter on the Pi's memory budget), vendored mem0,
ChromaDB, MongoDB, the Discord adapter, and the whole `orchestrator.py` classifier-routing loop
(fights the claim/lease mesh — different paradigm). All redundant with or hostile to shipped work.
MAX's KB-retriever/KPU only *confirms the concept* of an "orient before you act" adapter — the
sanctioned path is the off-box `codebase-memory-mcp` trial (v0.4 §12 / v0.6 Optional accelerator),
never MAX's torch stack.

---

## 4. Precise plug-in points into the existing dispatch jobs

The dispatch machinery already exists (`docs/harness/dispatch_pipeline.md`; live jobs
`.ai/dispatch/AGENT_20..23` = v0.6 M0/M1). MAX plugs in at **one** stage and nowhere else:

- **Stage 1 DRAFT is the only injection site.** A TaskExpert-style decomposer becomes an
  *optional* front-half of DRAFT: intent → N dependency-linked packets, each a normal
  `packet_template.xml` + `.task.md`. It produces **the same artifacts the loop already
  consumes** — it does not add a lane.
- **It must pass through the choke point.** Decomposed tasks enqueue via
  `file_watcher → _handle_new_task_file → parse_task_file → _enqueue_task`, hitting the
  Level-3 guard like any task. A `harness_level: 3` fragment still needs `approved: true`.
  **A decomposer must never emit `approved: true` autonomously** — that would remove the
  operator-invocation bound (v0.6 §0.2). Approval stays a human gate.
- **Dependencies ride existing columns.** Task-DAG edges map onto `continues:` (resume lineage)
  and `parent_flow_run_id`/`dispatched_by` (the M1-added lineage columns, wired in M2) — **no
  new schema.**
- **Sequencing = the M2 dispatch tree.** ChainAgent's output→input is just a parent flow with
  ordered child dispatches, already the M2/M3 model. Nothing new to build to express it.

**Ordering rule (inherited from v0.6 F3):** do not build the decomposer (a thing that *produces*
dispatches) before M2 makes those dispatches *observable*. Salvage waits behind M2, same as M3.

---

## 5. Recommended landing sequence (no new milestones — annotations on existing ones)

1. **Now (Tier A):** keep this map next to v0.6 as the "parts list + anti-goal exemplar." No code.
2. **With M4 generators (already Level-2, can land opportunistically):** add the Tier-B
   decomposition fields to the M4 spec-authoring output schema; write the TaskExpert-style
   **decomposer prompt** as a docs generator (like `draft_packet.md`) — usable by hand immediately.
   *(Dispatched as `AGENT_24_DECOMPOSER_GENERATOR.md`.)*
3. **After M2 (lineage observable):** wire the decomposer into DRAFT behind a flag
   (`OFF ⇒ byte-identical`), enqueuing through the existing choke point. This is M3-class work.
4. **§12 slot stays "Required: no" indefinitely.** If a real external task-orchestrator MCP is
   ever wanted, MAX's `TaskStorage` ABC is the interface reference — but the kernel must keep
   running without it.

---

## 6. Internal adversarial pass (v0.4 §5 discipline, on this map)

- **[F1 · P0 · scope] "Salvage" becomes a Trojan horse for the anti-goal platform.**
  *Resolution:* §1 three-boundary rule + Tier D hard exclusion + "no new lane / no always-on"
  binding to `_enqueue_task`. Any salvage that needs a standing process is rejected. *Held.*
- **[F2 · P1 · autonomy bound] A decomposer could self-approve Level-3 fragments.**
  *Resolution:* §4 — a decomposer must never emit `approved: true`; the operator gate is
  load-bearing (v0.6 §0.2). *Held.*
- **[F3 · P1 · ordering] Building the decomposer before dispatch lineage recreates the opacity scar.**
  *Resolution:* §5 sequences it behind M2, mirroring v0.6 F3. (The A24 *by-hand generator* is
  exempt — it wires nothing; only its automation is gated behind M2.) *Held.*
- **[F4 · P1 · duplicate state] Importing MAX's Task/History models spawns a second task store.**
  *Resolution:* Tier B reuses existing columns + the milestone Live Log; fields, not a store.
  v0.4 §7 (memory is not truth) holds. *Held.*
- **[F5 · P1 · resource] KPU/torch/mem0/Chroma on the Pi.** *Resolution:* Tier D — do not port.
  (Consistent with v0.6's `codebase-memory-mcp` "off-box trial first" caution.) *Held.*

**Verdict:** MAX contributes **one pattern (decomposition), one field set (task-DAG + machine-
checkable `completion_criteria` + planning hints), and a pile of ideation** — all landing inside
already-authorized M2/M3/M4 work behind flags and the Level-3 gate. It adds **no** new milestone
and **no** new state model. Converges, does not diverge.

---

## 8. Re-anchor after the 2026-07-11 architecture audit (v0.7 / M2.5)

This map predates the [workflow architecture audit](../.ai/workflow_architecture_audit.md) and
the **M2.5 Case Admission** milestone (`Task_Harness_v0.7_AUTOMATION.md`). The audit changes
*where two salvages land* — the salvages themselves stand, they just anchor better now:

- **`completion_criteria` is PROMOTED from an M4 packet field to the M2.5 Case-closure
  contract.** The audit proved Cases currently **auto-close on task-end** and that even the
  M2.5 fix (authoritative-actor-only closure, `AGENT_37`) is a rubber-stamp without a checkable
  done-condition. Tier B already called `completion_criteria` "the single most useful field to
  steal (fights the #1 scar)" — it is now wired into `open_case` (`AGENT_36`) + `close_case`
  (`AGENT_37`, item 3b): a Case cannot reach `closed` with unmet, unwaived criteria. **M3.2**
  later automates a *reviewer verifying* those criteria. This is the salvage's highest-value
  landing and it was invisible until a durable Case existed to attach a done-condition to.
- **The decomposer/TaskExpert finally has a concrete home.** §4 said MAX "plugs in at Stage 1
  DRAFT" and sequencing is "a parent flow with ordered child dispatches." With M2.5 that is now
  literal: an expanded objective is **`open_case()` + N `task_attached` links forming a DAG**
  (dependency edges ride `flow_links.metadata_json` / `parent_flow_run_id`) — **one Case
  containing the task-DAG, NOT N orphan flow_runs.** No new table; the seam exists. Design M4
  against this container, not against per-turn rows.
- **SupervisorAgent cautionary tale binds M3.1, not M3-generic.** The "stalled at the
  coordination layer" evidence (Tier A) is now the explicit guardrail for **M3.1 Manager role**
  (`M3_MANAGER_INVOCATION_SPEC.md`): one role driving a chain of command, sole authoritative
  Case closer — not a team-of-agents platform.
- **Everything else is unchanged:** Tier D stays discarded; `BLOCKED`→`status=blocked` and
  `ExecutionHistoryEntry`→`flow_events` are already shipped; `human_task`/`agent_task` ≈
  approvals + the audit's "request user input" gap (M3.1). No new milestone; converges on
  M2.5/M3/M4.

## 7. Cross-references
- Kernel: `docs/Task_Harness_v0.4.md` (§2.1 packet, §5 review, §11 fields, §12 optional adapters, §13 build order, §7 memory rule).
- Automation roadmap: `docs/Task_Harness_v0.6_AUTOMATION.md` (§0.2 anti-goal bound, M2 lineage, M3 Manager, M4 spec-authoring).
- Dispatch machinery: `docs/harness/dispatch_pipeline.md` (7 steps, the `_enqueue_task` choke point, Level-3 guard, `.task.md` contract).
- First salvage dispatch: `.ai/dispatch/AGENT_24_DECOMPOSER_GENERATOR.md` (M4 generators-early split — by-hand decomposer prompt).
- Manager driver: `docs/harness/manager_invocation.md`. Topology: `docs/ARCHITECTURE.md`.
- Source project (parts donor, do not build on): `~/dev/MAX` — `Phase 1 Core Infrastructure.md`, `component_guide.md`, `MAX/agents/task_expert/`, `MAX/agents/supervisor_agent.py`, `MAX/storage/utils/types.py`, `MAX/types/collaboration_types.py`.
</content>
