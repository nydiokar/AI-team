# Loop Configuration Map — the harness's control surface, made legible

**Status:** the loop's configuration/behavior *contract* (2026-07-03). This is the
operator's "fake node graph": a MAP of every configurable step in the harness loop —
who drives it, which existing file **programs** its behavior, its input/output
contract, and the specific dials that change its output quality (the "temperature").

**What this is NOT:** it is **not machinery**. It configures nothing at runtime — no
`flow_runs`, no stage column, no driver, no gateway state (that is Phase 2, spec §16,
deferred). It is a human/agent-facing document. The deliverable is the tables + prose
below, not a rendered graphic. It changes no stage logic; it only makes the existing
loop debuggable *before* real work is driven through it.

**Why it exists:** today the loop's behavior is documented **per-file** — each
generator describes its own stage. There was no single place that maps, across all
stages, *what dials output quality and who turns it*. So when a real loop produced bad
output, you couldn't localize which knob to turn. This map is that missing view, and
the pre-condition for driving real forward work through the harness.

**Read alongside:** [`dispatch_pipeline.md`](dispatch_pipeline.md) (the runbook, the
seven steps in prose), [`operating_model.md`](operating_model.md) (who the three
participants are), [`level_rubric.md`](level_rubric.md) (the level dial).

> ⚠️ **Every dial named here cites a real line in an existing harness file.** No dial
> is invented. A node with genuinely fixed behavior honestly shows
> `none (fixed behavior)` — an empty dial cell is a correct result, never padding.

---

## The linear stage flow (optional illustration only)

The node graph is the **table below**, not this diagram. This is just orientation:

```mermaid
flowchart LR
  N0["0 · LEVEL-SELECT"] --> N1["1 · DRAFT"] --> N2["2 · REVIEW"]
  N2 --> N3["3 · FIX (≤2 rounds)"] --> N4["4 · DISPATCH"]
  N4 --> N5["5 · EXECUTE"] --> N6["6 · CHECKPOINT"] --> N7["7 · CLOSE"]
  N3 -.re-review.-> N2
  N6 -.iterate.-> N5
```

Which nodes actually run depends on the **level** (node 0's output). Level 0 collapses
to nodes 1+5; Level 3 inserts an operator-approval gate between nodes 3 and 4.

---

## (a) The node table

One row per stage. `Programmed by` is the file whose prose *is* that node's behavior —
edit that file and the node behaves differently. Dials are cited inline.

| Node | Driver | Programmed by (file) | Input contract | Output contract | Quality dials |
|------|--------|----------------------|----------------|-----------------|---------------|
| **0 · LEVEL-SELECT** | Manager | [`level_rubric.md`](level_rubric.md) | operator intent | one number 0–3 (packet `<harness_level>` / `.task.md` `harness_level:`) | **Level triggers** — the 8 Level-3 triggers (`level_rubric.md` "Step 1"); **escalate-when-in-doubt bias** (`level_rubric.md` "When in doubt, escalate one level"). Determines how many downstream nodes run at all. |
| **1 · DRAFT** | Manager (drafting mode; a cheaper route is fine — `draft_packet.md` "a cheaper route is fine for DRAFT") | [`generators/draft_packet.md`](generators/draft_packet.md) + [`packet_template.xml`](packet_template.xml) | intent + level + curated context | filled `packet_template.xml` + initialized `milestone_template.md` (`Current Status: drafting`) | **Objective-lock granularity** — `<real_objective>` vs `<literal_request>` vs `<interpreted_task>` split (`packet_template.xml` L28–40); **non-goal/assumption/drift fill** ("an empty one of these is a drafting failure" — `draft_packet.md` L27); **context injection** — `<context_snippets>` curation (§8) + `continues:`/`load_compact_context` resume (`draft_packet.md` "Memory" L44–56); **memory reuse** — file-memory scars read before drafting (`draft_packet.md` L54–56). |
| **2 · REVIEW** | Manager (Reviewer action, not a separate agent — `operating_model.md` L22) | [`generators/adversarial_review.md`](generators/adversarial_review.md) | the drafted `packet_template.xml` | F-tagged P0/P1 findings (house style); zero findings is valid | **Severity floor** — P0/P1 only, no nits (`adversarial_review.md` L17–24); **scar-targeting bias** — the prioritized failure list (`adversarial_review.md` L38–42). Whether to run at all is set upstream by node 0 (review OFF for Level ≤ 1 — `level_rubric.md` "Cost cap"). |
| **3 · FIX** | Manager | [`generators/adversarial_review.md`](generators/adversarial_review.md) "The FIX loop" (L46–56) + `dispatch_pipeline.md` step 3 | F-tags from node 2 + the packet | revised packet (inline per `[Fn]`); unresolved → `<non_goal>`/logged risk; per-tag outcome recorded | **Round cap** — default 2 rounds, then stop (`adversarial_review.md` L51, spec §3 L174); **unresolved-finding disposition** — spill to `<non_goal>`/risk, never silent-drop (`adversarial_review.md` L53). |
| **4 · DISPATCH** | Manager | [`dispatch_pipeline.md`](dispatch_pipeline.md) step 4 (L64–90) | finalized packet | `.ai/dispatch/<NAME>.md` + `DISPATCH_LOG.md` row (`dispatched`); optional `.task.md` frontmatter | **Auto-pickup boundary** — `.task.md` auto-enqueue allowed for Level ≤ 2; Level 3 needs `approved: true` (`dispatch_pipeline.md` L113–119); **enforcement backstop** — `HARNESS_LEVEL3_GUARD` env flag, OFF by default (`dispatch_pipeline.md` L125, table L127–134). *(This is the ONE runtime-config dial in the whole loop; every other dial is prompt/artifact.)* |
| **5 · EXECUTE** | Executor | [`dispatch_pipeline.md`](dispatch_pipeline.md) step 5 (L92–98) + [`milestone_template.md`](milestone_template.md) | dispatched packet | code/docs change + milestone updated after each step + checkpoint commits | **Milestone-update cadence** — "update the milestone after every meaningful step" kills hallucinated success (`milestone_template.md` L4–8, `dispatch_pipeline.md` L93–95); **Single-Item lane** — one item → verify → log → next for fragile/rote work (spec §6, `dispatch_pipeline.md` L96–97). |
| **6 · CHECKPOINT** | Manager (Reviewer action) reviews the Executor's committed diff | [`dispatch_pipeline.md`](dispatch_pipeline.md) step 6 (L99–103) + `operating_model.md` L95–97 | the **committed** diff | P0/P1 F-tags on real code; iterate or pass | **Skill set** — `/code-review` + `/security-review` on the committed diff (`dispatch_pipeline.md` L100–101); **sequential-not-tailing** — commit *then* review, no live-tail (`dispatch_pipeline.md` L102–103, spec §5 L194–204). Severity floor P0/P1 shared with node 2. |
| **7 · CLOSE** | Manager | [`generators/closure_summary.md`](generators/closure_summary.md) | finished work + milestone + F-tags | closure summary + milestone `closed` + `.ai/CONTEXT.md`/`DISPATCH_LOG.md` update; optional `continues:` handoff | **Honesty floor** — SHIPPED/PARTIAL/BLOCKED stated plainly, skipped steps named (`closure_summary.md` L22, L45–47); **memory write** — durable `<memory_entry>` if a scar was learned (`closure_summary.md` L49–51); **`continues:` handoff** — resume context for the next task (`closure_summary.md` L52–54). The Level-3 wiki is `none (fixed behavior — optional, never a gate)` (`closure_summary.md` L44–46). |

**All 8 nodes mapped cleanly.** No stage resisted mapping. See the friction note at the
end for the two stages where the driver identity needed cross-referencing
(`operating_model.md`, not the generator, is the authority for *who* drives review).

---

## (b) The quality dials — where the "temperature" comes from

Every knob that changes loop output quality, each cited to a real source line, with
its default and the direction that trades cost vs. quality. This is the full list;
nothing outside it is a real dial.

1. **Level selection** — node 0. *Controls:* how many nodes run (review OFF below
   Level 2; operator-approval gate at Level 3). *Default:* rule-driven, no default —
   the triggers decide; **bias is to escalate when in doubt**.
   *Cost↔quality:* higher level = more review passes = more cost, more caught defects.
   *Source:* [`level_rubric.md`](level_rubric.md) Step 1 + "Cost cap".

2. **Review severity floor (P0/P1-only)** — nodes 2 & 6. *Controls:* what a finding
   must clear to be reported. *Default:* P0/P1 only; nits are dropped.
   *Cost↔quality:* lowering the floor (reporting P2 nits) burns fix rounds on style;
   raising it misses real defects. *Source:*
   [`generators/adversarial_review.md`](generators/adversarial_review.md) L17–24; spec
   §5 L207–210.

3. **Fix round cap (≤2 rounds)** — node 3. *Controls:* how long the
   plan↔review↔fix loop spins before it must lock. *Default:* **2**.
   *Cost↔quality:* more rounds = a tighter packet but risks an infinite review spiral;
   the cap says a locked-but-imperfect packet beats the spiral. *Source:*
   [`generators/adversarial_review.md`](generators/adversarial_review.md) L51; spec §3
   L174–175.

4. **Objective-lock granularity** — node 1. *Controls:* how sharply the packet
   separates the real objective from the literal words and the drafter's reading — the
   anti-drift dial. *Default:* all three of `<real_objective>` / `<literal_request>` /
   `<interpreted_task>` filled; non-goals/assumptions/drift-risks non-empty.
   *Cost↔quality:* a vaguer lock is cheaper to write but lets execution drift (the #1
   scar). *Source:* [`packet_template.xml`](packet_template.xml) L28–59;
   [`generators/draft_packet.md`](generators/draft_packet.md) L23–27.

5. **F-tag severity/scope** — nodes 2, 3, 6. *Controls:* what the adversarial pass
   hunts for. *Default:* the prioritized scar list (overbatch+hallucinated success,
   forbidden migration/stage machine, paid-CLI verify, unbounded spiral, drift from
   `<real_objective>`). *Cost↔quality:* narrowing it to the scars finds the defects
   that actually recur; widening it re-introduces nit-spirals. *Source:*
   [`generators/adversarial_review.md`](generators/adversarial_review.md) L38–42.

6. **Context injection** — node 1. *Controls:* what prior/curated context enters the
   prompt. *Default:* `<context_snippets>` are small, source-tagged, relevance-stated,
   non-instruction-overriding (§8); resume context is opt-in via `continues:` →
   `load_compact_context`, hard-capped ~4 KB. *Cost↔quality:* more context can ground
   the work or can bury the instruction / override execution rules — hence "curate,
   never dump." *Source:* [`generators/draft_packet.md`](generators/draft_packet.md)
   L44–56; spec §8 L275–290; spec §14 `continues:` L444–457.

7. **Memory reuse (file-memory)** — nodes 1 & 7. *Controls:* whether durable scars
   inform drafting and whether new scars are written back. *Default:* read
   `MEMORY.md` + `memory/*.md` before drafting; write a `<memory_entry>` at close only
   if something durable was learned. *Cost↔quality:* reusing memory avoids repeating a
   recorded failure; skipping it repeats scars. *Source:*
   [`generators/draft_packet.md`](generators/draft_packet.md) L54–56;
   [`generators/closure_summary.md`](generators/closure_summary.md) L49–51; spec §7.

8. **Auto-pickup boundary + guard flag** — node 4. *Controls:* whether a task
   auto-enqueues without a human. *Default:* Level ≤ 2 auto-pickup allowed; Level 3
   needs `approved: true`; the code backstop `HARNESS_LEVEL3_GUARD` is **OFF by
   default** (convention is the primary control). *Cost↔quality:* the guard trades a
   little friction for a hard stop on unreviewed infra/security/autonomy work.
   *Source:* [`dispatch_pipeline.md`](dispatch_pipeline.md) L113–134;
   [`level_rubric.md`](level_rubric.md) "The one hard boundary".

9. **Milestone-update cadence** — node 5. *Controls:* how often visible progress is
   written; this is the anti-hallucinated-success dial. *Default:* after **every
   meaningful step** ("if it isn't written here, it didn't happen"). *Cost↔quality:*
   coarser updates are faster but re-open the overbatch/false-success scar. *Source:*
   [`milestone_template.md`](milestone_template.md) L4–8;
   [`dispatch_pipeline.md`](dispatch_pipeline.md) L93–95.

10. **Single-Item long-running lane** — node 5. *Controls:* batch size for fragile/
    rote extraction. *Default:* one item → verify → log → next; never batch-and-claim.
    *Cost↔quality:* single-item is slower but stops the overbatch scar cold. *Source:*
    spec §6 L216–227; [`dispatch_pipeline.md`](dispatch_pipeline.md) L96–97.

11. **Closure honesty floor** — node 7. *Controls:* whether a partial/blocked result
    is reported truthfully. *Default:* state SHIPPED/PARTIAL/BLOCKED plainly; name any
    skipped step; honesty over polish. *Cost↔quality:* a hedged "done" hides the
    false-success scar; the honest floor catches it at close. *Source:*
    [`generators/closure_summary.md`](generators/closure_summary.md) L22, L45–47.

**Dials the packet's DoD asked for, reconciled with the source:**
- level selection ✓ (dial 1), ≤2-round review cap ✓ (dial 3),
  objective-lock granularity ✓ (dial 4), F-tag severity floor P0/P1-only ✓
  (dials 2 & 5), context injection ✓ (dial 6), memory reuse ✓ (dial 7). All six
  required dials are present and cited.

**Dial I expected but could NOT find (a real finding — a gap in configurability):**
- **No provider/model "temperature" dial inside the loop.** The loop's "temperature"
  is *entirely* prompt/artifact discipline (dials above), never a sampling-temperature
  or model-route knob per node. The one place the spec discusses low-temperature
  sampling is §9 (provider smoke), which is **explicitly onboarding-only, NOT a
  per-task stage** (spec §9 L294–311). So DRAFT-cheap-route vs REVIEW-strong-route
  (spec §14 L426–428, `draft_packet.md` L4) is a *stated preference*, not a wired
  dial — there is no per-node model-route configuration surface today. This is
  honest: promoting model-route-per-node to a real dial would be Phase-2 machinery,
  out of scope here, and is noted as a future item, not built.

---

## (c) The two behavioral roles, explicitly separated

The harness runs **three participants** (Operator → Manager → Executor), not the
spec's four rotating modes — `operating_model.md` reconciles this (Supervisor is
absorbed into the Manager; Reviewer is a Manager *action*, not an agent). The two
*agent* roles and exactly where each is configured:

### Manager — the driving force (nodes 0, 1, 2, 3, 4, 6, 7)
The senior standing agent. It **owns the loop and the milestone burndown**. Configured
by: [`operating_model.md`](operating_model.md) "The three participants" (the role
definition) + the DRAFT/REVIEW/CLOSE generators (its per-node behavior) + the
Manager-behavior spec below (its driving contract). It never works the burndown itself
— it grounds, locks, reviews, and decides. See the dedicated spec in the next section.

### Executor — the worker (node 5, + fixing bounded findings)
The worker agent. Configured by: [`operating_model.md`](operating_model.md)
"The three participants" (role) + [`dispatch_pipeline.md`](dispatch_pipeline.md) step 5
(behavior) + [`milestone_template.md`](milestone_template.md) (the artifact it must
keep current) + the dispatch packet's `<execution_rules>` (the per-task `do`/`do_not`).
Its contract:
- Pick up **one** dispatch packet with clear behavior + "when to stop" instructions.
- Work the **Burndown**; **update the milestone after every meaningful step** — this,
  not a promise, is what proves progress (`milestone_template.md` L4–8).
- Commit at checkpoints so the Manager reviews a **committed diff**, never a live tree.
- Use the **Single-Item lane** for fragile/rote work (spec §6).
- Report an **honest** closure — a partial reported truthfully beats a hidden skip.

**The seam:** the Manager decides *what* and *whether* (grounding, objective-lock,
level, review gate, iterate/close/derive); the Executor decides *how* within the locked
packet and produces the visible milestone trail. Neither wears the other's hat: the
Executor does not re-open scope, the Manager does not silently do the burndown.

---

## Manager behavior spec — the driving-force contract (the filled gap)

> **Decision (executor's call):** this lives as a **headed section inside
> `loop_config_map.md`**, NOT a separate `manager_behavior.md`. Rationale: the
> Manager's per-node behavior is only legible *next to the node table and dials it
> references* — splitting it into a second file would force a reader to hold two docs
> open to answer one question ("who turns this dial?"). The README already lists seven
> harness files; an eighth would dilute discoverability. If the spec later grows a real
> Manager-driver, promote this to its own file then.

**The gap was real.** Before this: `operating_model.md` gives the loop *shape* and the
Manager's *responsibilities as a list*, but no document stated the Manager's
**per-node driving behavior** — what it must do at each node and in what order. The
generators describe DRAFT/REVIEW/CLOSE as *modes*, never attributing them to the
standing Manager or ordering them into a driving discipline. This section is that
missing contract. It invents **no new role** — the Manager already exists in
`operating_model.md`; this is its behavior spec, not a new participant. (v0.5 dropped
the Supervisor on purpose; no critic/supervisor agent is introduced here.)

The Manager's driving behavior, per node:

1. **Grounding reflex (mandatory, before spending a dispatch).** Before node 0, the
   Manager checks operator intent against the spec/plan-of-record. If they conflict
   (intent asks for something the spec defers or forbids), it **surfaces the conflict
   with a recommendation and waits** — it does not silently build the intent nor
   silently override it with the spec. Ground first, then act. *Configured by:*
   [`operating_model.md`](operating_model.md) "The grounding rule" L46–53. *(This is the
   reflex whose failure caused the node-graph drift; it is now mandatory.)*

2. **Level select (node 0).** Apply `level_rubric.md` by rule, escalate when in doubt.

3. **Objective-lock ownership (node 1).** The Manager owns the scope lock — it protects
   `<real_objective>` when the literal request and the real goal diverge, and fills
   non-goals/assumptions/drift-risks so the Executor can't drift into them.
   *Configured by:* [`generators/draft_packet.md`](generators/draft_packet.md) +
   `packet_template.xml`.

4. **Adversarial review (node 2) + bounded fix (node 3).** The Manager runs the
   adversarial pass on the *drafted packet*, emits P0/P1 F-tags, and caps the fix loop
   at 2 rounds; unresolved findings spill to `<non_goal>`/risk. *Configured by:*
   [`generators/adversarial_review.md`](generators/adversarial_review.md).

5. **Scope containment (throughout).** The Manager keeps the Executor inside the locked
   packet; new scope is a new dispatch, not a mid-loop expansion. *Configured by:*
   [`operating_model.md`](operating_model.md) "The three participants" (Manager owns
   scope containment).

6. **The review GATE (node 6) — the load-bearing one.** After the Executor reaches a
   logical end and commits, the Manager reviews the **committed diff** from the
   *project-wide* perspective (not bounded to task context) and **verifies the
   Executor's claims in git/code — it does not trust the summary.** The mechanism is
   `/code-review` + `/security-review` on the committed diff (§5's checkpoint reviewer,
   run by the Manager). *Configured by:*
   [`operating_model.md`](operating_model.md) L70–73 & L95–97;
   [`dispatch_pipeline.md`](dispatch_pipeline.md) step 6; spec §5 L192–212.

7. **The iterate/close/derive decision (after node 6).** The Manager then decides:
   **iterate** (send the Executor back with bounded findings — stay in the loop),
   **close** (mark done, update `DISPATCH_LOG.md` + milestone), or **derive** (close
   this, open the next dispatch from what was learned). *Configured by:*
   [`operating_model.md`](operating_model.md) "The nested loops" (d) L70–76.

8. **Closure (node 7).** Honest summary, milestone → `closed`, ledger update, optional
   `continues:` handoff and `<memory_entry>` write. *Configured by:*
   [`generators/closure_summary.md`](generators/closure_summary.md).

**The Manager is always on top of both the task AND the milestone burndown** — it
manages the Executor *and* the burndown, and it alone decides when a loop closes vs.
continues (`operating_model.md` L86–87).

---

## (d) The failure-localization table — the payoff

If the loop produced BAD output X → suspect node N → turn dial D. Every named dial in
(b) has ≥1 row here, so the map is load-bearing: every dial traces to a failure it
fixes.

| If the loop produced this BAD output… | Suspect node | Turn this dial (from (b)) |
|---------------------------------------|--------------|---------------------------|
| A risky infra/migration/security change ran with too little review | 0 · LEVEL-SELECT | **Level selection (1)** — it was under-leveled; escalate; a Level-3 trigger was missed. |
| A trading/mesh/autonomy change auto-enqueued with no human sign-off | 0 & 4 | **Auto-pickup boundary + guard flag (8)** — set `HARNESS_LEVEL3_GUARD=on`; require `approved: true`; and re-check it was leveled 3 (dial 1). |
| Review missed a real correctness/security defect | 2 · REVIEW / 6 · CHECKPOINT | **Severity floor (2)** + **F-tag severity/scope (5)** — the adversarial pass wasn't hunting the right class; re-point it at the scar list. |
| Review drowned in style nits; fix rounds burned on cosmetics | 2 · REVIEW | **Severity floor (2)** — raise it back to P0/P1-only; nits are not findings. |
| The packet kept churning; review never converged | 3 · FIX | **Fix round cap (3)** — enforce the 2-round cap; spill the unresolved finding to `<non_goal>` and lock. |
| The Executor built the literal words, not what the operator wanted | 1 · DRAFT | **Objective-lock granularity (4)** — sharpen `<real_objective>` vs `<literal_request>`; fill the drift-risks. |
| The work drifted into out-of-scope territory mid-loop | 1 · DRAFT (+ Manager scope containment) | **Objective-lock granularity (4)** — the non-goals were empty/weak; name the scope the Executor drifted into. |
| The prompt was grounded in stale/irrelevant/overriding context | 1 · DRAFT | **Context injection (6)** — curate the snippets (small, source-tagged, non-overriding); check `continues:` pointed at the right prior task. |
| A previously-recorded failure was repeated | 1 · DRAFT / 7 · CLOSE | **Memory reuse (7)** — the scar wasn't read before drafting, or wasn't written at close; read/write file-memory. |
| The Executor claimed "done" but the work wasn't really there | 5 · EXECUTE | **Milestone-update cadence (9)** — updates were too coarse; require a milestone line per meaningful step. |
| A fragile batch extraction reported success but was wrong | 5 · EXECUTE | **Single-Item lane (10)** — switch to one item → verify → log → next; stop batch-and-claim. |
| A partial/blocked result was closed as if it fully shipped | 7 · CLOSE | **Closure honesty floor (11)** — state PARTIAL/BLOCKED plainly and name the skipped step. |

**Coverage check:** dials 1–11 each appear in ≥1 row above (dial 1: rows 1–2; dial 2:
rows 3–4; dial 3: row 5; dial 4: rows 6–7; dial 5: row 3; dial 6: row 8; dial 7:
row 9; dial 8: row 2; dial 9: row 10; dial 10: row 11; dial 11: row 12). Load-bearing.

---

## What this map deliberately does NOT do

- It adds **no runtime config**. The only dial that touches code is
  `HARNESS_LEVEL3_GUARD` (dial 8), which already exists and is OFF by default; this
  map does not turn it on or wire anything new.
- It builds **no graph UI, no driver, no `flow_runs`**. v0.4's platform stays deferred
  (spec §16); A12's evidenced "Phase 2 = NO" verdict stands.
- It introduces **no new role**. The Manager and Executor already exist in
  `operating_model.md`; this only writes down their per-node behavior.
- **Noted future item (not built):** if model-route-per-node ("cheap DRAFT / strong
  REVIEW") is ever wanted as a real dial rather than a stated preference, that is a
  Phase-2 promotion — out of scope here. **Its promotion is governed by the evidence-gated
  [`promotion_ladder.md`](promotion_ladder.md) (row 2):** it stays COLD — a stated
  preference applied by hand — until the ladder's concrete trigger is *observed* (a bad
  output traced to the wrong model on a node, after manual routing was actually tried and
  recurs). Being named absent here does not promote it.

**Spec:** full doctrine — [`../Task_harness_workflow.md`](../Task_harness_workflow.md)
(v0.5). Deferred end-state — [`../Task_Harness_v0.4.md`](../Task_Harness_v0.4.md).
