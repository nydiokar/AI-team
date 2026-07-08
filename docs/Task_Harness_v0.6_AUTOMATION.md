# Task Harness v0.6 — Automation Build-Out (companion to v0.4)

**Status:** operator-authorized build spec (2026-07-06). Supersedes the *posture* of
`docs/harness/promotion_ladder.md` (retired — see §0.3). Does **not** replace
`docs/Task_Harness_v0.4.md`: v0.4 remains the **kernel spec** (the quality loop, the
artifacts, the roles, the gateway-state field list §11). This document is the **roadmap
that turns the proven manual kernel into a gateway-driven, traceable state machine.**

---

## 0. Why this document exists

### 0.1 The phase changed

v0.4 §13 lists a "multi-agent autonomous company loop" under **Do not build yet**, and
§15 states "This is not a multi-agent platform." Those were **correct for the prototype
phase** — the phase whose job was to run the loop *by hand* and prove the discipline before
spending engineering on a platform. That phase is **done**: A9H–A19 ran the §14 loop by
hand across docs and real code, the file-and-dispatch discipline held, and the `flow_runs`
record (§11's `flow_run_id`/`current_stage`/`objective_lock`) already shipped to `main`
(A19, `0b6b1ec`).

**Operator decision (2026-07-06): build the automation.** The manual loop works; the pain
is now that it is manual, opaque (operator is blind to spawned workers), and non-durable
across sessions. v0.6 authorizes closing exactly those gaps.

### 0.2 The guardrail that keeps this inside the anti-goals

v0.6 is **not** a repeal of the anti-goals in `production_vision.md` §6. It builds the
bounded case and only the bounded case:

| Forbidden (anti-goal) | What v0.6 builds instead (allowed) |
|---|---|
| Always-on, self-directed swarm | **Operator-invoked.** One operator intent → one bounded loop. No standing autonomous process. |
| Broad self-directed execution across the machine | **Scope-locked per invocation.** Objective-lock + Level-3 approval gate (already on the choke point) + bounded cwd/tools (production_vision safety model). |
| Opaque memory / opaque state | **The state machine is the opposite of opaque** — every stage transition + every dispatch edge is a queryable row. Durability *is* the feature. |
| Multi-agent planning platform | **One Manager role + workers**, mirroring `operating_model.md`'s three participants. Not a swarm; a chain of command with a paper trail. |

**Load-bearing rule:** if a proposed capability would let the system act **without an
operator invocation bounding it**, it is out of scope for v0.6 and belongs to the anti-goal
list. Automation here means *the operator invokes a Manager and can then chase everything it
does* — not *the system runs itself*.

### 0.3 The promotion ladder is retired

`docs/harness/promotion_ladder.md` was the prototype-era instrument for deciding whether to
build the platform *at all*. Its standing verdict ("Phase 2 = NO", drop-candidates) answered
the prototype question. **That question is now closed by operator decision.** The ladder is
superseded by this roadmap and should be read as historical evidence only (it still holds
real scars worth keeping — the concurrent-agents-shared-tree lesson, the lock-contention
note). Do not cite its "deferred/drop" verdicts against v0.6 work.

---

## 1. Target end-state (what "automated" means here)

The operator invokes a **Manager** with an intent ("info panel shows broken data; fix it").
The Manager, as a first-class gateway-spawned role:

1. **Orients** — reads project context + the relevant code (optionally via
   `codebase-memory-mcp`, v0.4 §12 optional adapter) so it understands *what already exists*
   before touching anything. (This is the fix for the scar where a worker duplicated a
   component and half-reimplemented existing, well-built code because it never oriented.)
2. **Expands** the intent into a professional, correctly-scoped, **not-overstated** objective
   lock + plan (v0.4 §2.1) — the redundancy of "a professional briefing a professional."
3. For a **feature-sized** intent, first **authors a specification** and runs a
   **score-based review** to improve it before decomposing (v0.6 §M4).
4. **Dispatches worker(s)** through the existing gateway session/dispatch path — and the
   parent→child edge is **persisted and traceable** (you can chase what the worker is doing).
5. **Reviews** the worker's committed diff adversarially, then **transitions the flow stage**
   and **closes** — all as durable, queryable state.

The durable backbone is v0.4 §11's field set, promoted from the current 5-column *record* to
a **driven, queryable state machine** with dispatch lineage.

---

## 2. Prerequisites already in place (do NOT leave these aside)

The roadmap builds **on** shipped work, not beside it:

- **Gateway spawns + routes worker sessions across the mesh** (production_vision core;
  `orchestrator.py`, `session_service.py`, `db.py`). The "spawn a worker" primitive exists.
- **`flow_runs` record on `main`** (A19, migration in `db.py`): `flow_run_id`, `task_id`,
  `current_stage`, `objective_lock`, `created_at` + `create_flow_run`/`update_flow_stage` +
  a best-effort write hook in `_enqueue_task`. **This is the seed of the state machine — M1
  extends it, it is not rebuilt.**
- **The manual quality loop** (packets, milestone burndown, adversarial review, closure) —
  proven; `docs/harness/` + `dispatch_pipeline.md`.
- **The Manager driver prompt** — `docs/harness/manager_invocation.md`.
- **Level-3 admission gate** on the shared choke point `_enqueue_task`
  (`HARNESS_LEVEL3_GUARD`) — the bound that keeps automation from running dangerous work
  unapproved. Stays authoritative.
- **`load_compact_context(task_id)`** — bounded, DB-canonical resume context (migration 17).
- **Web UI cockpit** — live *session* state surface to extend for *flow* state.

---

## 3. The prioritized milestone roadmap

Ordered by **prerequisite dependency**, not appeal. Each milestone is flag-guarded so
`OFF ⇒ byte-identical legacy` (the v0.4 §13/A9H discipline). Nothing here is irreversible
until a flag is deliberately turned on.

### M0 — Reconcile the base (prerequisite housekeeping) · Level 1–2

Make the base clean and honestly-labeled before building on it.
- Fix stale docs: A19 **is merged** (`0b6b1ec`); CONTEXT.md/DISPATCH_LOG saying "awaiting
  op-merge" is wrong. Correct them.
- Surface the two open drift forks as explicit operator decisions (not silent): the **A17
  orphan-code drift** (`AGENT_17_WIP_MERGE_RECONCILE.md`) and the unmerged
  `phase1-quota-window-coordinator` branch. **Verified 2026-07-07:** that branch is a stale
  *additive* feature branch — **9 ahead / 2 behind `main`**, **+1773 / −0 across 6 files** vs
  its merge-base; the only "deletions" that show up are files `main` added while it sat
  behind. It is **salvageable (rebase-to-current), not destructive** — the earlier
  "~293-file-deleting" characterization was wrong. Still keep it a **separate fork, never
  entangled with M1 work**.
- **Why first:** M1 writes migrations onto `main`; a lying or drifting base corrupts every
  downstream stage-transition assumption.

### M1 — Flow-state machine: record → driven, queryable stages · Level 3 (migration)

Promote the 5-column record to the full §11 model and make `current_stage` **real** (written
at each harness step), while nothing yet *depends* on it (shadow).
- **Schema (additive migration):** add `approved_plan`, `plan_review`, `burn_down_items`,
  `execution_result`, `implementation_review`, `waived_findings`, `closure_summary`,
  `role_assignments`, `artifact_links`, `status`, `updated_at` — **and** `parent_flow_run_id`
  + `dispatched_by` columns now (cheap; M2 only wires them, no second migration).
- **Stage vocabulary:** a defined enum matching v0.4 §1 (`intent → objective_lock → plan →
  plan_review → execution → impl_review → closure`), written as the loop advances.
- **Flag-guarded** `HARNESS_FLOW_DRIVE` (default OFF ⇒ current best-effort record behavior,
  byte-identical). ON ⇒ stages are written at transitions. **Still shadow** — no execution
  path reads `current_stage` to decide what runs (that dependency is deliberately deferred to
  a later hardening pass, so a stage-write bug can never stall a real task in M1).
- **Read API:** `/api/flows` (list) + `/api/flows/{id}` (detail) so the operator can query
  "what flows exist and at what stage."
- **Why #1:** everything else (lineage, invoked Manager, spec layer) needs durable, queryable
  flow rows to hang off. This is the backbone.

### M2 — Dispatch lineage: Manager → Worker traceability · Level 2–3

Kill the "I'm blind to the subagent" pain. When a Manager flow dispatches a worker
task/session, populate `parent_flow_run_id`/`dispatched_by` (columns added in M1) so the
**dispatch tree is queryable**: this Manager spawned these workers, each at this stage, each
with this result. Extend the read API + a minimal cockpit view to render the tree.
- **Why after M1:** you can only link rows once the rows are real and queryable. Building the
  autonomous dispatcher (M3) *before* this substrate would recreate the exact opacity pain.
- **2026-07-08 scope refinement:** a code-grounded read-model review found that `flow_runs`
  + `mesh_tasks` + sessions + approvals are not enough for a truthful mobile Work UI without
  fragile heuristics. M2 therefore expands into the **Work Control Substrate** milestone:
  authoritative `flow_links`, append-only `flow_events`, write-path population, a read-only
  Work/Case read model, and a mobile read-only Work surface before M3. See
  [`WORK_CONTROL_SUBSTRATE_MILESTONE.md`](WORK_CONTROL_SUBSTRATE_MILESTONE.md).

### M3 — Manager-as-invoked-role: operator invokes → expand → dispatch through the machine · Level 3

Wire `manager_invocation.md` as an actual gateway-invokable session type. Operator sends
intent → a Manager session spawns → orients (optionally `codebase-memory-mcp`) → expands into
objective-lock + packet (writes a flow row) → dispatches worker session(s) via the existing
path (lineage from M2) → reviews the committed diff → transitions stages → closes. Bounded:
one invocation → one loop; Level-3 gate intact; relay-to-operator at each worker stop.
- **Prerequisite spike inside M3:** confirm a gateway-spawned session can itself act as an
  orchestrator (spawn/track sub-sessions) with the current backend surface; if not, that
  capability is the first job of M3.
- **Why after M2:** never build a thing that *produces* dispatches autonomously before you can
  *observe* those dispatches.

### M4 — Feature-spec authoring + scored review layer · Level 2 (generators) → Level 3 (wiring)

The layer you don't want to lose: for feature-sized intent, the Manager **authors a
specification document** and runs a **rubric-scored adversarial review** to improve it before
decomposing into dispatches (a `spec_authoring` stage before LOOP 0).
- **Split for cheap early value:** the *generators* (spec-draft + scored-review, prompt
  artifacts like the existing `draft_packet.md`/`adversarial_review.md`) are **docs** and can
  ship + be used by hand at any time, decoupled from automation. The *wiring into the flow
  machine as a stage* waits for M3.
- **Why last:** it depends on the Manager role (M3) to have an author, but its manual
  generators can land opportunistically earlier.

### Optional accelerator — `codebase-memory-mcp` (v0.4 §12, "Required: no")

Not a milestone; an **orientation adapter** for M3's "orient" step (repo symbol/call graph so
the Manager grounds without grepping N files). **Off-box trial first** — confirm an ARM64
binary + acceptable RSS against the Pi's memory-pressure notes before it touches the gateway
host. If it fails the resource check, keep it as a dev-machine tool. It indexes *code*, not
dispatch prose — it does nothing for file growth (that is the archival lifecycle, tracked
separately).

---

## 4. Internal adversarial review of this roadmap

Applying v0.4 §5 discipline to the plan itself. P0/P1 only.

- **[F1 · P0 · prerequisite] M1 migrates onto a base with known drift.** The A17 orphan code
  (9 undispatched files, some live e.g. `_ActivityForwarder`) and the stale
  `phase1-quota-window-coordinator` branch (9 ahead / 2 behind, additive) sit around `main`.
  Building migrations on top without resolving them risks entangling M1 with unreviewed code.
  **Resolution:** M0 exists and is ordered first precisely to force these into explicit
  operator forks before M1 (with the branch's *true* state, not the stale "293-file" scare).
  *Held.*
- **[F2 · P0 · anti-goal drift] The roadmap could slide into the forbidden "autonomous company
  loop."** M3 spawns workers automatically; without a hard bound that is the anti-goal.
  **Resolution:** §0.2 load-bearing rule — every capability must be bounded by an operator
  invocation; Level-3 gate stays on the choke point; no always-on process is authorized. Any
  job that removes the invocation bound is rejected at review. *Held.*
- **[F3 · P1 · ordering] Should lineage (M2) precede the invoked Manager (M3)?** Yes — building
  an autonomous dispatcher before the trace substrate recreates the opacity scar. Order is
  correct. To de-risk further, M1 already lands the lineage *columns*, so the column-level wiring
  is cheap. *Held.*
  > **⚠️ Clarified 2026-07-08 (was "M2 is wiring-only" — that phrasing spawned a duplicate lane).**
  > M2 has **two coordinated halves, not one**, and they must not both stamp the same parent→child
  > edge independently:
  > 1. **A26a — flow_runs lineage wiring** (the literal "wiring-only" piece): populate the mig-22
  >    columns `parent_flow_run_id`/`dispatched_by`/`dispatch_file` at the child-dispatch seam
  >    behind `HARNESS_FLOW_DRIVE`, and expose the `_stamp_child_dispatch_lineage` **supplier** +
  >    `list_child_flow_runs` reverse-lookup. This is the convenience-column + stamping half.
  > 2. **A25–A29 — Work Control Substrate** (the scope refinement below): the authoritative
  >    `flow_links`/`flow_events` ledger + write path + read model + Work UI.
  >
  > **Authority rule:** `flow_links(role=child_flow)` is authoritative; the `flow_runs` column is a
  > convenience index (milestone §"Optional Direct Columns"). **A26 consumes A26a's stamped
  > `parent_flow_run_id` — it does NOT add a second stamping hook** (that would trip the milestone's
  > own F4 "duplicate ledger"). One seam, one stamper (A26a), two stores with a clear order.
- **[F4 · P1 · hidden phase] M3 assumes a spawned session can orchestrate sub-sessions.** That
  capability is unproven against the current backend surface and could be a whole phase.
  **Resolution:** M3 opens with a capability spike; if orchestration isn't supported, that
  becomes M3's first (and possibly separate) job rather than a silent assumption. *Flagged,
  bounded.*
- **[F5 · P1 · irreversibility] Making `current_stage` authoritative is the real risk** — A19
  shipped it explicitly as "nothing reads it; execution untouched." If execution starts
  depending on stage writes, a transition bug could stall real tasks. **Resolution:** M1 keeps
  stage-writing **shadow + flag-guarded**; execution does not read `current_stage` in M1. The
  read-dependency is a deliberately later, separately-gated hardening pass. *Held.*
- **[F6 · P1 · scope] M4's generators are being tied to M3 unnecessarily.** The spec-authoring
  and scored-review *prompts* are docs and deliver value by hand today. **Resolution:** M4 is
  split — generators can ship early; only the stage-wiring waits. *Fixed inline (see M4).*

**Verdict:** order stands (M0 → M1 → M2 → M3, M4 generators opportunistic). Two P0s
(base drift, anti-goal drift) are structurally contained by M0 and §0.2 respectively — they
are the two things a reviewer must keep checking every dispatch.

---

## 5. Build guardrails & success criteria (carried from v0.4 §14 / v0.5)

Not new machinery — the spend bound and the acceptance test the automation must respect.
Import, don't reinvent. These bind **M3** (the automation); M0–M2 are substrate and only
need the flag discipline already stated in §3.

### 5.1 Cost Guard (import v0.5 §3 / §9 — do not restate) — binds M3
The automated loop multiplies model calls (Manager + plan-review + worker + diff-review +
iterate). M3 carries v0.5's existing guard verbatim, not just §0.2 scope-bounding — this is a
**cross-reference to `docs/Task_harness_workflow.md` §3 (cost discipline) / §9 (per-task smoke
is onboarding-only)**, restating only the load-bearing lines:
- Cap the plan↔review↔fix loop at a stated number of rounds (**default 2**); unresolved items
  become explicit `waived_findings`, not another round. (v0.5 §3.)
- **Level ≤ 1** work: review off. (v0.5 §3.)
- **Never a paid CLI to "verify"** (plain `pytest`; live check is `curl .../health`; never
  `python main.py status`) — the project's #1 scar is burned tokens + false-success. (v0.5 §9,
  Test Cost Guard.)
- **Plan review keeps a distinct reviewer seat** (cross-model or a separate cheap pass), not
  Manager self-review — v0.4 §4's adversarial separation is the quality thesis. (The diff
  review is already worker≠Manager; this restores it for the *plan*.)

### 5.2 Role-separation + abort / reap / crash-recovery — AUTHORED AT M3 SPEC-TIME (deferred)
M3 spawns workers, so "know how to undo it" (CLAUDE.md) applies — but the concrete guardrails
are **NOT written now.** They are **authored when M3 is spec'd, after the F4 capability spike**
(§4 F4: "can a gateway-spawned session orchestrate sub-sessions?"), because their exact shape
depends on what that spike proves. Placeholder for the M3 spec to fill: an operator **kill
path** for a running flow (→ `status = blocked`/resumable), **orphan-worker reap** (the
incarnation-ID reaper scar, MEMORY.md), **Manager-crash-mid-flow** recoverability, and the
**Manager≠Worker role separation** the automation must preserve. Deliberately a stub here so
M0–M2 substrate work is not blocked on unwritten M3 policy.

### 5.3 Success criteria (v0.4 §14, for the automation) — the acceptance test
The automation succeeds when: **one operator intent → a Manager flow row is created →
dispatched workers are traceable via `/api/flows` (parent→child) → the committed diff is
reviewed → the flow closes → all of it is queryable**, and with **every flag OFF the system is
byte-identical to today**.

---

## 6. Cross-references

- Kernel spec: `docs/Task_Harness_v0.4.md` (§1 loop, §11 state fields, §13 build order).
- Shipped v0.5: `docs/Task_harness_workflow.md`. Operating model: `docs/harness/operating_model.md`.
- Retired instrument: `docs/harness/promotion_ladder.md` (historical evidence only — see §0.3).
- Manager driver: `docs/harness/manager_invocation.md`. Pipeline: `docs/harness/dispatch_pipeline.md`.
- First-milestone dispatch jobs: `.ai/dispatch/AGENT_20_*` … `AGENT_23_*` (M1).
- Prior-art salvage: the retired-MAX audit (see the salvage map in `docs/`, linked from v0.4
  §12 + `.ai/CONTEXT.md`) — *idea-only* inputs to **M4** (decomposition/schema) and **M2/M3**
  (delegation pattern); bounded by §0.2, imports no MAX code, off the in-flight A20–A23 jobs.
