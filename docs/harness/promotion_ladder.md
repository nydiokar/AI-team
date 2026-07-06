# Harness Promotion Ladder — the evidence-gated roadmap from v0.5 → v0.4 end-state

> ## ⛔ RETIRED (2026-07-06) — superseded by `docs/Task_Harness_v0.6_AUTOMATION.md`.
> This ladder was the **prototype-phase** instrument for deciding whether to build the
> automation platform *at all*. That question is now **closed by operator decision**: the
> automation is authorized and scoped in v0.6. **Do not cite this file's "deferred / drop /
> Phase 2 = NO" verdicts against v0.6 work** — they answered a question that is no longer open.
> Kept for historical evidence only (the concurrency scars in § Evidence consistency remain
> real and worth honoring). Operator may delete it once v0.6 M1 lands.

**Status:** ~~reference doctrine (2026-07-03)~~ **RETIRED** — historical evidence only.
The harness's *prototype-era* decision instrument for its own future.

> ## This document authorizes NOTHING.
>
> Every row below is a capability the fuller [`../Task_Harness_v0.4.md`](../Task_Harness_v0.4.md)
> end-state describes and that shipped-v0.5 ([`../Task_harness_workflow.md`](../Task_harness_workflow.md))
> **deliberately deferred**. This ladder does **not** greenlight building any of them.
> Each row stays deferred until its **promotion trigger** is *observed and recorded in a
> real loop's artifacts* — not until a session feels slow, not because the capability was
> named somewhere, not on a hunch. A future Manager checks "have we tripped the gate yet?"
> against this file instead of re-litigating scope.
>
> **The standing verdict is still NO.** A12 ran the §14 loop by hand and produced the
> evidenced conclusion **"Phase 2 = NO"** (`.ai/dispatch/AGENT_12_HARNESS_SELFTEST.md`,
> HARNESS FRICTION REPORT). A13 and A14 added two more docs loops that held with zero lost
> handoff state. Three real loops, no gate tripped. This ladder must not contradict that —
> see [§ Evidence consistency](#evidence-consistency-a12a13a14).
>
> ### ⚠️ Operator override on Row 1 (2026-07-05) — a partial `flow_runs` record now exists in code, NOT because the gate tripped.
> A19 (`.ai/dispatch/AGENT_19_FLOW_RUNS_RECORD.md`) shipped a **minimal `flow_runs` record**
> — migration 21, a 5-column table (`flow_run_id`, `task_id`, `current_stage`,
> `objective_lock`, `created_at`), `create/update/list` methods, and a best-effort
> orchestrator write hook — under an **explicit operator override** of this row's COLD
> verdict. **The Row 1 trigger was NOT observed** (no ≥2-resume lost-handoff event; A12/A13/
> A14/A18 all ran with zero lost state). What shipped is a **RECORD, not a stage machine /
> driver** — nothing reads `current_stage` to drive behavior; existing task execution is
> untouched. Treat this as an operator-directed **experiment**, not a satisfied gate: the
> record's *existence in code does not promote Row 1*, and the trigger above is still the
> condition that would justify building the *driver* on top of it. A future Manager must not
> cite "the table already exists" as evidence the gate tripped.

## How to read a row

- **Trigger** is the load-bearing column. It is a *falsifiable, observable* condition —
  something you could point at in a loop's git history, milestone files, DISPATCH_LOG, or
  logs and say "yes, that happened N times." "When it feels needed" / "when the team
  decides" is **not** a trigger and is banned here (that is the R2 failure mode).
- If an element genuinely has **no realistic trigger** — you cannot name evidence that
  would ever justify building it — it is recorded as a **drop candidate**, not given a
  mushy trigger to fill the cell. An honest "we'd never need this" is a valid output.
- **Cheaper interim move** is what to do *instead* until the gate trips. Every one needs
  **no new machinery** — it is a prompt, a convention, or a manual action.
- **Identified as a gap ≠ trigger satisfied.** A capability being *named* (e.g. model
  routing was flagged as a missing dial in `loop_config_map.md`) does not move it up the
  ladder. Only observed strain does.

---

## The ladder

Ranked by *how close the trigger is to tripping given real evidence* (hotter = a more
plausible near-term promotion), not by how attractive the feature is.

| Rank | Element | v0.4 § / v0.5 § | What it is | Promotion trigger (concrete, observed evidence) | Cheaper interim move (no machinery) |
|-----:|---------|-----------------|------------|-------------------------------------------------|-------------------------------------|
| **1** | **Gateway-owned flow state** — `flow_runs` record + `current_stage` stage machine | v0.4 **§11** (full data model) + **§13** item 1 (`FlowRun record`) / v0.5 **§11** ("NONE in v1") + **§16** (Phase 2) | The gateway persists `flow_run_id`, `current_stage`, `objective_lock`, `plan_review`, `burn_down_items`, … as queryable rows and drives stage transitions, instead of the file-and-dispatch convention. | **A multi-slice dispatch loses handoff state across ≥2 resumes such that the milestone file + `load_compact_context` alone cannot reconstruct where it was** — i.e. a resumed Executor demonstrably redid or dropped a completed slice because the file state was insufficient. Recorded ≥2 distinct times in dispatch milestone Live Logs. **(Also watch:** ≥1 real need to *query* flow status across many concurrent tasks that DISPATCH_LOG grep cannot answer.) | Keep the milestone file + `continues:`/`load_compact_context` as the resume ledger (v0.5 §2.2, §7). On resume, the Manager re-reads the milestone Live Log + DISPATCH_LOG row — no schema. |
| **2** | **Automatic model routing** — per-node cheap-DRAFT / strong-REVIEW as a wired dial | v0.4 **§13** ("Do not build yet: automatic model routing") / surfaced as the *missing* dial in `loop_config_map.md` §(b) "Dial I expected but could NOT find" | A per-node model-route configuration surface, so DRAFT auto-picks a cheap route and REVIEW a strong one, instead of it being a stated *preference* the operator applies by hand. | **A loop produces a bad output that is traced to the wrong model on a node AND the manual "pick a cheaper route for DRAFT" preference was actually followed** — i.e. hand-routing was tried, is proven insufficient, and the miss recurs across ≥2 loops. **NOT** tripped by the dial merely being named absent (it is named in `loop_config_map.md`; three loops A12/A13/A14 ran fine single-model). | Apply the DRAFT-cheap / REVIEW-strong split **by hand** — the operator/Manager just picks the route per dispatch (v0.5 §14 "text engine is a role, not a system"; `draft_packet.md`). It is already a stated preference; use it manually. |
| **3** | **Wiki / HTML dashboard layer** | v0.4 **§2.3** (wiki page, HTML tables/Mermaid) + **§12** (wiki renderer adapter) / v0.5 **§2.3** ("optional… Do not automate it in v1") + **§12** (Required: no) | An automated human-facing dashboard rendering closures/flows as HTML/Mermaid — the "human command center" layer on top of the Markdown source of truth. | **A human operator demonstrably cannot navigate loop state from the existing Markdown surfaces** (DISPATCH_LOG index + dispatch docs + `.ai/CONTEXT.md`) — evidenced by the operator repeatedly asking for status that those files already contain, ≥2 times, because prose is unreadable at the current job count. | The DISPATCH_LOG lean index **is** the dashboard (one row per job, status vocabulary). A closure summary per dispatch gives the human-readable record. Read those; render nothing. (The separate cockpit web dashboard already covers live *session* state.) |
| **4** | **Live tailing reviewer** — a concurrent reviewer agent tailing the Executor's diffs/logs live | v0.4 **§5** ("run a second reviewer/tailer… Reviewer tails docs/diffs/logs") / v0.5 **§5** (reframed to *sequential checkpoint* — "There is no concurrent-agent primitive here; two live agents on one working tree is a merge/race hazard") | A reviewer agent running **concurrently** with the Executor, catching P0/P1 issues while work is still active, rather than reviewing the committed diff at a checkpoint. | **DROP CANDIDATE — no realistic trigger under the current execution model.** v0.5 §5 didn't defer this for cost; it removed it because it is *structurally unsafe here*: dispatches are sequential single turns and two live agents on one working tree co-mingle git indexes (this actually happened — MEMORY `concurrent-agents-shared-tree`, A12 committed A11's `f434a6a`). The only way this becomes safe is separate worktrees per agent — at which point it is a *different* design, not a promotion of this row. There is no observable strain in the sequential model that "live tailing" (as v0.4 framed it) fixes; the checkpoint reviewer already catches P0/P1 on the committed diff. **If ever wanted, re-derive it from worktree isolation — do not un-defer this v0.4 row.** | Sequential checkpoint review (v0.5 §5): Executor commits at a milestone checkpoint, Manager runs `/code-review` + `/security-review` on the **committed** diff. Already the shipped mechanism. |
| **5** | **Async memory compression** — a cheap-model summarizer service | v0.4 **§7** ("Cheap models can be used for async memory compression") / v0.5 **§7** ("reuse what exists, invent nothing… no required async compression service") | A standing background service that runs a cheap model to compress session history / memory into summaries as a separate memory backend. | **DROP CANDIDATE — no realistic trigger; superseded, not merely deferred.** Two memory systems already exist and are canonical: `load_compact_context(task_id)` (bounded, DB-canonical, migration 17) and file-memory (`MEMORY.md` + `memory/*.md`). A *third* async compression backend would duplicate them (MEMORY `db-self-sufficient-conversation`). The only condition that would justify it — "compact context is too large / too slow at scale" — is a **tuning problem inside `load_compact_context`'s existing field caps**, solved by adjusting that function, not by a new service. No observable strain routes to "build a separate compressor." | `load_compact_context` already returns bounded, compressed prompt/summary/files/usage from the DB ledger (v0.5 §7). If a fact is durable, write a `<memory_entry>` to file-memory by hand at close. Tune the loader's caps if bounds bite; add no service. |
| **6** | **Per-task provider smoke test** — model identity/quality check on every task | v0.4 **§9** (smoke test placed *in the task loop*) / v0.5 **§9** ("onboarding only, NOT a per-task stage" — putting it per-task "invites a paid-CLI call on every task, a direct Test Cost Guard violation") | Running a same-prompt/low-temp identity+quality smoke before *each* task's model call, to detect wrong-model-serving / degraded provider quality per task. | **DROP CANDIDATE — promotion is forbidden by a standing rule, not merely gated.** A *per-task* smoke means a model call (often a paid CLI) on every task — the Test Cost Guard's exact prohibition (v0.5 §9; MEMORY `test-cost-guard`). No volume of strain justifies violating the cost guard on the hot path. The legitimate need (catch a bad provider route) is met by **onboarding-time** smoke + backend `doctor` probes, which are deliberate, not per-task. This row can only ever move as *onboarding* smoke, which already exists — so as a *per-task stage* it stays permanently dropped. | Run the identity/quality smoke **at provider onboarding only** (v0.5 §9), reusing the existing LLM-turn-observability smokes + backend `doctor` probes. Any paid run needs explicit operator approval. |

**Row count:** 6 of 6 v0.4→v0.5 deferred elements grounded and ranked. Every element cites
a real v0.4 section and the real v0.5 section that deferred/reframed it — none invented.

### Three of the six resisted a concrete trigger — that is a finding

Rows **4, 5, 6** are **drop candidates**: no falsifiable trigger exists that would justify
building them *as v0.4 framed them*, because each was deferred for a **structural** reason,
not a cost/effort one:

- **#4 live tailing reviewer** — structurally unsafe (concurrent agents share a git tree);
  the safe version is a different design (worktree isolation), not this row un-deferred.
- **#5 async memory compression** — superseded by two existing memory systems; the only
  real need is loader tuning, not a new service.
- **#6 per-task provider smoke** — forbidden by the Test Cost Guard on the hot path; only
  ever legitimate as onboarding smoke, which already exists.

Only rows **1** (flow state) and **2** (model routing) have a *writable* falsifiable trigger
— a concrete, observable condition that could plausibly be met by future strain. Everything
else is honestly "we'd never build this as specified."

---

## Evidence consistency (A12/A13/A14) {#evidence-consistency-a12a13a14}

Three real docs loops have run. Checking each hot-ish row's trigger against them:

- **A12** (`AGENT_12_HARNESS_SELFTEST.md`) — ran the §14 loop by hand on the pipeline doc.
  Its friction report states the objective-lock held (no drift), the milestone file carried
  the trail, and closure was honest — **zero lost handoff state**. Explicit verdict:
  **"Phase 2 = NO."**
- **A13** (`AGENT_13_LOOP_CONFIG_MAP.md`) and **A14** (`AGENT_14_DOC_STRUCTURE_CONTRACT.md`)
  — two further docs loops, both closed on-branch with milestone + closure in one file; no
  lost handoff, no resume that the milestone couldn't recover.

**Consistency result: no trigger is already-satisfied-but-ignored.**

- **Row 1 (flow state) is correctly still-COLD.** Its trigger requires a multi-slice loss of
  handoff state across ≥2 resumes; three loops ran with **zero** lost state. Ranking flow
  state as trippable-now would directly contradict the recorded evidence (the R3 failure).
  It is **#1 only because it is the *most plausible* future trip**, not because it is close.
- **Row 2 (model routing) is correctly still-COLD.** It was *named* as a missing dial in
  `loop_config_map.md` §(b) — but "identified as a gap ≠ trigger satisfied." A12/A13/A14 all
  ran fine single-model. A named-but-unneeded capability stays cold.

**One honest caveat worth recording (not a tripped trigger).** A12's friction report surfaced
a *real* observation: the file-as-state model has **no concurrency control** — two Executors
on the same branch race on the shared DISPATCH_LOG (A12 had to re-read + re-place its row).
This is a **lock-contention** issue, **not** the lost-*handoff*-state condition Row 1's
trigger names, and it resolved with a re-read at this scale. It is already mitigated by the
standing rule **"one worker per branch/tree at a time"** (MEMORY `concurrent-agents-shared-tree`;
`manager_invocation.md` rule 3). So it does **not** trip Row 1 — but if that concurrency cost
recurs *after* the one-worker rule is honored, that would be new evidence to log here. Flagged
loudly rather than quietly folded into a rank.

---

## Priority reasoning

The ranking is by *trigger proximity given real evidence*, tied to the project's actual
scars, not by feature appeal:

1. **Flow state (#1)** ranks highest among the *writable-trigger* rows because it is the one
   deferral that a genuine future strain (lost multi-resume handoff at higher task volume)
   could plausibly justify — it is the natural escape hatch v0.5 §16 itself names. It is still
   cold, but it is the closest-to-real gate, so a Manager watches it first.
2. **Model routing (#2)** ranks next because it has a writable trigger *and* is already named
   as a gap — but the project's #1 scar is **burned tokens / false-success**, and three loops
   proved single-model is sufficient, so promoting it now would add cost (more model calls,
   more routing schema) against no evidenced defect. Named ≠ needed.
3. **Rows 3–6 rank lowest because they are drop candidates** — building any of them would
   *re-open* a scar the project already closed: a wiki renderer is doc-litter/unread machinery
   (the `context-canonical-layout` cleanup exists precisely to *reduce* surfaces); a live
   tailer re-opens the concurrent-agents-shared-tree scar; async compression duplicates the
   already-canonical memory (`db-self-sufficient-conversation`); per-task smoke violates the
   Test Cost Guard directly. The end-goal is *quality without a platform* (v0.5 §0, §17) — so
   the correct roadmap ranks the platform-shaped elements as things to **not** build until
   observed strain forces it, and three of them as things to likely never build as specified.

---

## Cross-references

- Governance of the model-routing dial's promotion is recorded in
  [`loop_config_map.md`](loop_config_map.md) "What this map deliberately does NOT do".
- Listed as harness reference doctrine in [`README.md`](README.md).
- Standing verdict source: `.ai/dispatch/AGENT_12_HARNESS_SELFTEST.md` (HARNESS FRICTION REPORT).
- End-state spec: [`../Task_Harness_v0.4.md`](../Task_Harness_v0.4.md). Shipped spec:
  [`../Task_harness_workflow.md`](../Task_harness_workflow.md) (esp. §16).
