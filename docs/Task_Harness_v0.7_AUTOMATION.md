# Task Harness v0.7 — Automation Build-Out (supersedes v0.6)

**Status:** operator-authorized build spec (2026-07-11). **Supersedes `Task_Harness_v0.6_AUTOMATION.md`**
(kept verbatim as the trace — do not delete it). v0.7 == v0.6 **plus** one correction: it
inserts the **Case Admission & Affiliation foundation (M2.5)** that the
[`workflow_architecture_audit.md`](../.ai/workflow_architecture_audit.md) proved is missing
and is a hard prerequisite for M3. Everything else in v0.6 stands.

> This document does **not** replace `docs/Task_Harness_v0.4.md` (the kernel spec — quality
> loop, roles, §11 state fields). It is the corrected automation roadmap.

---

## 0.7 Changelog vs v0.6 (read this first)

The 2026-07-11 architecture audit verified a foundation defect that v0.6 never named:

> **Case (`flow_run`) is minted per turn, not per objective.**
> `orchestrator._enqueue_task → _record_flow_run_start → db.create_flow_run` mints a fresh
> `uuid4` flow_run for **every task**, plus a fresh `session→worker` link, and the loop
> auto-stamps `execution → impl_review → closure` + auto-closes the Case on the single
> task's success. So `Task finished == Case completed`, which the spec forbids. The Web UI
> then shows one Case per turn.

This is a **writer-policy bug over a sound substrate** — `flow_links` already supports N
tasks + N sessions per Case (`db.py:2551`). M1/M2 remain **valid and shipped**; nothing is
rolled back. The fix is a new foundation milestone, **M2.5**, that must land **before M3.1
(Manager role)** — which silently assumed a durable per-intent Case already exists.

**Corrections applied in v0.7:**
1. New milestone **M2.5 — Case Admission & Affiliation** inserted between M2 and M3 (§3).
2. **M3 renamed the "Manager role" step to M3.1** and marked **blocked on M2.5** (§3).
3. New adversarial findings **F7 (case-per-turn)** and **F8 (auto-closure)** added (§4).
4. M1's per-turn `create_flow_run` is reclassified: **correct as a *record*, wrong as *Case
   identity*** — see M2.5.

Ready-to-dispatch jobs live in the audit doc §E (Jobs 1–9) and as packets `AGENT_36`
(M2.5 Job 1) and `AGENT_37` (M2.5 Job 2). Jobs 3–9 map onto M3.1/M3.2/M3.3/M4 and are pulled
when unblocked.

---

## 0. Why this document exists

### 0.1 The phase changed

v0.4 §13 lists a "multi-agent autonomous company loop" under **Do not build yet**, and §15
states "This is not a multi-agent platform." Those were **correct for the prototype phase**
— run the loop by hand and prove the discipline before spending engineering on a platform.
That phase is **done**: A9H–A19 ran the §14 loop by hand across docs and real code, the
discipline held, and the `flow_runs` record shipped to `main` (A19).

**Operator decision (2026-07-06): build the automation.** The manual loop works; the pain is
that it is manual, opaque (operator blind to spawned workers), and non-durable across
sessions. v0.6/v0.7 authorize closing exactly those gaps.

### 0.2 The guardrail that keeps this inside the anti-goals

v0.7 is **not** a repeal of the anti-goals in `production_vision.md` §6. It builds the bounded
case and only the bounded case:

| Forbidden (anti-goal) | What this roadmap builds instead (allowed) |
|---|---|
| Always-on, self-directed swarm | **Operator-invoked.** One operator intent → one bounded loop. No standing autonomous process. |
| Broad self-directed execution across the machine | **Scope-locked per invocation.** Objective-lock + Level-3 approval gate + bounded cwd/tools. |
| Opaque memory / opaque state | **The state machine is the opposite of opaque** — every stage transition + dispatch edge is a queryable row. |
| Multi-agent planning platform | **One Manager role + workers**; a chain of command with a paper trail, not a swarm. |

**Load-bearing rule:** if a proposed capability would let the system act **without an operator
invocation bounding it**, it is out of scope and belongs to the anti-goal list.

### 0.3 The promotion ladder is retired

`docs/harness/promotion_ladder.md` was the prototype-era instrument for deciding whether to
build the platform at all. Its standing verdict is superseded by this roadmap; read it as
historical evidence only. Do not cite its "deferred/drop" verdicts against this work.

---

## 1. Target end-state (what "automated" means here)

The operator invokes a **Manager** with an intent. The Manager, as a first-class
gateway-spawned role:

1. **Orients** — reads project context + relevant code before touching anything.
2. **Expands** the intent into a correctly-scoped, not-overstated objective lock + plan.
3. For a **feature-sized** intent, first **authors a specification** and runs a
   **score-based review** before decomposing (§M4).
4. **Opens ONE Case** for the objective and **dispatches worker(s)** *into that Case* — the
   parent→child edge and the shared-Case membership are **persisted and traceable**.
   *(v0.7: this is the corrected model — one Case spans the Manager + its workers + reviewer,
   not one Case per turn. See M2.5.)*
5. **Reviews** the worker's committed diff adversarially, **transitions the flow stage on
   real events**, and — as the sole authoritative closer — **closes the Case** when the
   objective is accepted (not when a task ends).

The durable backbone is v0.4 §11's field set, promoted from a per-turn *record* to a
**per-objective, driven state machine** with dispatch lineage and durable Case identity.

---

## 2. Prerequisites already in place (do NOT leave these aside)

- **Gateway spawns + routes worker sessions across the mesh.** The "spawn a worker"
  primitive exists.
- **`flow_runs` + `flow_links` + `flow_events` on `main`** (A19/A21/A25–A30). The substrate
  is complete and **structurally supports one Case ↔ many Tasks/Sessions** — M2.5 wires the
  writer to use it that way; it is not rebuilt.
- **The manual quality loop** (packets, burndown, adversarial review, closure) — proven.
- **The Manager driver prompt** — `docs/harness/manager_invocation.md`.
- **Level-3 admission gate** on `_enqueue_task` (`HARNESS_LEVEL3_GUARD`) — stays authoritative.
- **`load_compact_context(task_id)`** — bounded DB-canonical resume context.
- **Web UI Work surface** — renders Cases faithfully (it shows one-Case-per-turn only because
  the writer feeds it that; fixed by M2.5/Job 2).

---

## 3. The prioritized milestone roadmap

Ordered by prerequisite dependency. Each milestone is flag-guarded so `OFF ⇒ byte-identical
legacy`. New order: **M0 → M1 → M2 → M2.5 → M3 → M4.**

### M0 — Reconcile the base · Level 1–2 — **DONE** (A20)
Make the base clean and honestly-labeled before building on it. (Unchanged from v0.6.)

### M1 — Flow-state machine: record → driven, queryable stages · Level 3 — **LIVE** (A21–A23)
Promote the 5-column record to the full §11 model; write `current_stage` at each step behind
`HARNESS_FLOW_DRIVE` (shadow — nothing reads it to drive execution); read API `/api/flows`.
> **v0.7 reclassification:** M1's `create_flow_run`-per-task is **correct as a telemetry
> record** but was mistaken for **Case identity**. Minting a row per task is fine for a
> record; it is wrong when that row IS the Case. M2.5 separates the two.

### M2 — Dispatch lineage + Work Control Substrate · Level 2–3 — **DONE & merged** (`24dff9b`, A25–A30)
Authoritative `flow_links` + append-only `flow_events` + write path + read model + mobile Work
surface + honest session affiliations. **Valid and shipped — not rolled back.**
> **v0.7 follow-up note:** the A26 write path over-creates Cases (one flow_run + one
> `session→worker` link per turn), and A30's "resolve session to most-recent case" is a
> **cosmetic mask** over the shattering, not a fix. The *schema* scope M2 delivered is
> correct; the *write policy* is corrected in M2.5. See
> [`WORK_CONTROL_SUBSTRATE_MILESTONE.md`](WORK_CONTROL_SUBSTRATE_MILESTONE.md) §follow-up.

### M2.5 — Case Admission & Affiliation (NEW in v0.7) · Level 3 — **NEXT** (A36, A37)

**The missing foundation.** Make a Case mean *one objective*, not *one turn*, and make a
Session's Case membership durable across turns. This is the prerequisite M3.1 assumed existed.

- **M2.5 / Job 1 — Case admission** (packet `AGENT_36`): a turn attaches to the session's
  **open Case if one exists** (`task_attached` link+event), otherwise runs **Case-less**
  (Pattern A: standalone session, many tasks, no Case). Cases are created only by an explicit
  managed entrypoint / Manager `open_case`, never unconditionally in `_enqueue_task`.
  New primitives: `db.find_open_case_for_session`, `db.open_case`, durable
  `session.current_case_id`+`role`. Flag-gated; OFF ⇒ byte-identical.
- **M2.5 / Job 2 — Continuity, honest stages & closure** (packet `AGENT_37`): remove the auto
  `impl_review`/`closure` stamps; a task terminal outcome updates **task** state only, never
  `flow_runs.status`; Case status transitions only via an authoritative closer; pause/resume
  reuse the same `flow_run_id`; retire the most-recent-link mask; read model groups a Case's
  N tasks/sessions and shows all affiliations. **Closure carries a completion contract**
  (`completion_criteria`, salvaged from MAX — `PRIOR_ART_MAX_REUSE.md` Tier B/§8): a Case cannot
  close with unmet, unwaived criteria, so an authoritative close is a *checkable* close, not a
  rubber-stamp. M3.2 later automates a reviewer verifying it.
- **Why before M3:** M3.1 (Manager role) is *defined* as "one intent → one dispatch → one
  review → close, all durable." That sentence is false while every turn mints a new Case.
  Build the durable Case first, then give it a Manager.
- **Acceptance:** 10 turns on a standalone session ⇒ 0 Cases; 10 turns on a Case-attached
  session ⇒ 1 Case with 10 task links + 1 session link; a completed worker task leaves its
  Case `open`; timeline shows no fabricated review/closure. OFF ⇒ byte-identical.

### M3 — Manager-as-invoked-role · Level 3 — **BLOCKED on M2.5**

Two sub-phases (per `M3_MANAGER_INVOCATION_SPEC.md`):
- **M3.1 — Manager role & control surface** (audit Job 5): promote `manager_invocation.md` to
  a spawnable gateway session that `open_case`s, dispatches workers *into that Case*, inspects,
  and is the **sole authoritative closer**. Add the ~14 missing MCP control tools (only
  `dispatch_worker`/`wait_for_worker` exist today). **Prerequisite: M2.5.**
- **M3.0 — F4 dispatch/wait spike** — **code-complete** (A31–A35, gated OFF on `main`).
  Preserves the proven dispatch/wait plumbing; does **not** define Case semantics.
> **v0.7 correction:** M3.0's `dispatch_worker` mints a *separate* child flow_run joined by a
> lineage pointer. Under the corrected model a worker joins the **parent's Case** (shared
> membership) rather than spawning its own Case — reconcile at M3.1 against M2.5.

### M3.2 — Review-from-above + `review.*` emitter (audit Job 6) — **after M3.1**
Real reviewer role emits `review_requested/passed/failed/rework_requested`; the vocabulary A29
deferred "until a reviewer role exists" gets its reviewer. Case cannot close with unresolved
review. Distinct plan-reviewer seat.

### M3.3 — Guardrails, kill path & durable relay (audit Jobs 4, 7) — **after M3.1**
Round/turn/cost caps; kill path → `flow.interrupted` (→ `status=blocked`/resumable); crash
recovery. **Includes the durable, case-aware relay (audit Job 4):** worker completion routed to
the owning Case reaching a live *or resumed* Manager, exactly once — today `wait_for_worker` is
an in-process poller that dies with the Manager. Case blocks (not closes) on unmet
approval/input/child work.

### M4 — Feature-spec authoring + scored review (audit Job 8) · generators early → wiring after M3
Manager authors a specification + rubric-scored adversarial review before decomposing
(`spec_authoring` stage before LOOP 0). `publish_artifact` → `artifact` links+events. Generators
are docs and can land opportunistically; stage-wiring waits for M3. **Decomposer home (post-M2.5):**
an expanded objective is `open_case()` + N `task_attached` links forming a **task-DAG inside one
Case** (edges on `flow_links.metadata_json`), not N orphan flow_runs — MAX salvage re-anchored in
`PRIOR_ART_MAX_REUSE.md` §8. Design M4 against the Case container.

### Optional accelerator — `codebase-memory-mcp` (unchanged from v0.6)
Orientation adapter for M3's "orient" step; off-box trial first. Off the critical path.

### Off critical path — provider adapters/subagents (audit Job 9, handoff §2.6)
Mirror provider-native subagents as first-class workers **only if** pursued. Not a foundation.

---

## 4. Internal adversarial review of this roadmap

P0/P1 from v0.6 (F1–F6) still hold — see `Task_Harness_v0.6_AUTOMATION.md` §4 (base drift,
anti-goal drift, ordering, hidden F4 phase, `current_stage` irreversibility, M4 scope). v0.7
adds two:

- **[F7 · P0 · foundation] Case identity is minted per turn.** Verified: every task mints a
  new `flow_run` + `session→worker` link; the read model masks it with "most-recent case."
  Building M3.1 (which assumes a durable per-intent Case) on this produces a Manager whose
  "Case" evaporates each turn. **Resolution:** M2.5 inserted as a hard prerequisite before
  M3.1; substrate already supports the correct model, so this is wiring, not a rebuild.
  *Contained.*
- **[F8 · P1 · false-closure] Case auto-closes on task success.** `_flow_terminal_outcome`
  sets `status=closed` when the one task ends, and the loop auto-stamps `impl_review`/`closure`
  with no reviewer — violating `Task finished != Case completed` and narrating a quality loop
  that never ran. **Resolution:** M2.5 Job 2 removes auto-stamp/auto-close; closure becomes an
  authoritative-actor decision (M3.1/M3.2). *Contained.*

**Verdict:** order becomes **M0 → M1 → M2 → M2.5 → M3.1 → {M3.2, M3.3} → M4**. The two new
P0/P1 (case-per-turn, false-closure) are structurally contained by M2.5.

---

## 5. Build guardrails & success criteria

Unchanged from v0.6 §5 (Cost Guard binds M3; role-separation/abort/reap authored at M3
spec-time; success = one intent → traceable dispatch → reviewed diff → close → all queryable,
every flag OFF ⇒ byte-identical). v0.7 adds one success clause:

> **M2.5 success:** a reused session accumulates Tasks under **one** Case (not one Case per
> turn); a standalone session creates **no** Case; a worker task completing does **not** close
> the Case. Verified by SQL dump of `flow_runs`/`flow_links` across ≥3 turns.

---

## 6. Cross-references

- **Audit that produced v0.7:** [`.ai/workflow_architecture_audit.md`](../.ai/workflow_architecture_audit.md) (deliverables A–F, Jobs 1–9).
- Prior roadmap (trace): `docs/Task_Harness_v0.6_AUTOMATION.md`.
- Kernel spec: `docs/Task_Harness_v0.4.md`. Shipped v0.5: `docs/Task_harness_workflow.md`.
- M3 spec: `docs/M3_MANAGER_INVOCATION_SPEC.md` (amended 2026-07-11: M3.1 blocked on M2.5).
- Substrate: `docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md` (amended 2026-07-11: write-path follow-up).
- Dispatch packets: `.ai/dispatch/AGENT_36_CASE_ADMISSION.md`, `.ai/dispatch/AGENT_37_CASE_CONTINUITY_CLOSURE.md`.
