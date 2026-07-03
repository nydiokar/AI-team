# Operating Model — how we actually run the harness

**Status:** the real, in-use operating model (2026-07-03). Reconciles what we *do* with
`Task_harness_workflow.md` §4/§5. Where they differ, **this file wins for how we run**;
the spec still governs the *discipline inside a loop* (objective-lock, F-tags, bounded
fix rounds, closure).

---

## Why this diverges from spec §4 (and why the divergence is correct)

Spec §4 defines four roles as **modes one model rotates through per single turn**
(Manager / Supervisor / Executor / Reviewer). That assumes a one-model, one-turn topology.

**We run a different topology: two standing agents in a multi-turn conversation, plus a
human operator.** In that topology:

- **Supervisor is dropped as a separate role.** It existed in the spec only so plan-review
  wouldn't get skipped when one model wears every hat. We have a permanent senior Manager
  who already reviews adversarially with project-wide context — the **Manager absorbs the
  Supervisor**. A third agent to supervise one task is friction, not safety.
- **Reviewer is kept, but as a Manager action, not an agent** — the post-work adversarial
  review (spec §5, review the committed diff). No extra agent.
- **The Operator (human) is a first-class seam** the spec never modeled.

Net: **three participants, not four roles.** Operator → Manager → Executor.

---

## The three participants

- **Operator (human).** Supplies intent and terse nudges. Decides genuine forks (merge,
  approve Level-3, strategic direction). Not required to be precise — turning vague intent
  into grounded work is the Manager's job.
- **Manager (senior standing agent — me).** The smarter, higher-perspective seat. Owns:
  objective-lock, scope containment, grounding intent **against the spec/plan BEFORE
  spending a dispatch**, writing the dispatch packet, adversarially reviewing the
  Executor's work with **project-wide** context (not just task context), and deciding
  after each loop: iterate, close, or derive the next dispatch. Always on top of both the
  task AND the milestone.
- **Executor (worker agent).** Picks up one dispatch packet with clear behavior +
  "when to stop" instructions, implements, updates the milestone, runs checks, reports.

---

## The grounding rule (the one reflex that failed this session — now mandatory)

Before the Manager spends a dispatch on operator intent, it **checks that intent against
the spec/plan-of-record.** If they conflict (e.g. intent asks for something the spec
defers or forbids), the Manager **surfaces the conflict with a recommendation and waits**
— it does not silently build the intent, and does not silently override it with the spec.
Ground first, then act. (This is what a good senior does; skipping it is how the
node-graph drift happened.)

---

## The nested loops (the actual process)

```
LOOP 0 — milestone framing (Operator + Manager, once per milestone)
  idea → spec/milestone → adversarial review → FIRST dispatch packet + DISPATCH_LOG row

LOOP N — one dispatch, worked to done (repeats until milestone exhausted)
  (a) DISPATCH   Manager writes packet (objective-lock, scope, behavior, when-to-stop)
                 + appends DISPATCH_LOG row (status: dispatched)
  (b) EXECUTE    Executor picks it up, works. 2–5 Manager↔Executor turns are NORMAL —
                 they may genuinely need to converge, clarify, or iterate a fix.
  (c) LOGICAL END  Executor reaches the dispatch goal OR simply stops. Manager nudges:
                 "adversarial review of what you did."
  (d) MANAGER REVIEW  Manager reviews the committed diff critically, from the HIGHER
                 (project-wide) perspective — not bounded to task context. Verifies
                 claims in code (don't trust the Executor's summary). Then DECIDES:
                   → iterate  (send Executor back with bounded findings) — stay in LOOP N
                   → close    (mark done, update DISPATCH_LOG + milestone)
                   → derive   (close this, open LOOP N+1's packet from what was learned)

LOOP N+1, N+2, … — same shape, each next set of tasks (from prior task or the spec)

FINAL LOOP — after the milestone is exhausted
  Manager does one project-wide review and dispatches a FINAL cleanup job:
  fix latent/visible bugs, inconsistencies, perf (memory/logic), pipelines that diverge
  or run parallel to the project structure, and violations of modern dev/research
  practice — judged against the overall goal, not any single task.
```

**The Manager is always on top of each loop AND the milestone** — it manages the Executor
*and* the burndown, and it's the one that decides when a loop closes vs. continues.

---

## Mapping to existing artifacts (nothing new invented)

- **LOOP 0 / packets** → `.ai/dispatch/AGENT_N_*.md` (+ `_REVIEW.md`), per `dispatch_pipeline.md`.
- **Loop state / status** → `.ai/dispatch/DISPATCH_LOG.md` — already the loop tracker
  (`dispatched → built → reviewed → merged`, plus `blocked`/`deferred`). Manager updates it.
- **MANAGER REVIEW (d)** → `/code-review` + `/security-review` on the committed diff,
  house F-tag style; this IS spec §5's checkpoint reviewer, run by the Manager.
- **Milestone burndown** → `docs/harness/milestone_template.md`.
- **Closure / derive** → `generators/closure_summary.md` + `.ai/CONTEXT.md` ledger update.

---

## What the spec still owns (unchanged)

Inside any loop, the discipline is the spec's: level selection (§3), objective-lock +
XML packet (§2.1), F-tagged adversarial review capped at ~2 rounds (§3 cost rule),
memory-reuse (§7), no paid CLI to "verify" (§9), ZERO new gateway state in v1 (§0/§11).
This file changes *who does what and how loops nest* — not the quality bar within a loop.
```
