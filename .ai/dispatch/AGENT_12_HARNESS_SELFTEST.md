# AGENT_12 — Harness Self-Test: run one real task through the loop

**Dispatch created:** 2026-07-03
**Level:** 2 (Standard) — this dispatch is itself a Level-2 run of the harness
**Branch:** stay on `feat/task-harness` (do NOT cut a new branch)
**Spec:** `docs/Task_harness_workflow.md` v0.5 — §14 pipeline, §15 success criteria, §3 levels

---

## Why this, not the WebUI

The v1 harness (A9) is built and spec-complete: templates, generators, dispatch
pipeline, Level-3 gate. What has NEVER happened is **using it on a real task.** The spec
is explicit that the next move is not a UI or a driver — those are §12-optional / §16-
deferred, built *only if the file-and-dispatch discipline proves insufficient* (§16).
That trigger is untested. So the correct next action is to **exercise the loop once,
for real, and report where it works and where it hurts.** This is the evidence that
decides whether Phase 2 (or the parked node-graph) is ever justified.

You are both the harness OPERATOR and its first honest test subject.

---

## The task to run through the loop (ASSIGNED — do not pick your own)

**Level 2. Objective:** Add a **worked, copyable end-to-end example** to
`docs/harness/dispatch_pipeline.md` — a fully filled packet + milestone + F-tag + closure
for one tiny fictitious task, so a fresh executor can see the whole loop concretely instead
of reading abstract stage descriptions. Also add the one-line scope banner distinguishing
the `.task.md` batch lane from the live `submit_instruction` lane (A9 owed this).

**Done-condition (testable):** `dispatch_pipeline.md` contains (a) the scope banner near
the top, and (b) a complete worked example exercising all 7 stages with real filled
artifacts, not placeholders. `docs/harness/` remains internally consistent (no broken
cross-refs). No code touched, so no `pytest` needed beyond a docs-link sanity check.

This task is small ON PURPOSE. **The task is the vehicle; the LOOP is the subject** —
you are proving `operating_model.md` + the `docs/harness/` generators actually work by
using them on real work, not by picking something impressive.

---

## What to actually do — run §14 by hand, produce the artifacts

Execute the pipeline stages for real, generating each artifact into the templates A9
built. Do NOT invent a new process; follow `docs/harness/` as written and note any place
the docs are unclear or wrong.

1. **DRAFT** — pick the level via `level_rubric.md`; fill `packet_template.xml` for the
   chosen task; create a milestone file from `milestone_template.md`.
2. **REVIEW** — run `generators/adversarial_review.md` against your own packet. Produce
   real F-tags with file:line evidence. Do not rubber-stamp; find at least the genuine
   weaknesses or record "none, and here's why" with reasoning.
3. **FIX** — resolve F-tags inline, ≤2 rounds, then lock. Update the milestone burndown.
4. **DISPATCH → INGEST** — this is the same-branch, hand-run case; no `.task.md` needed
   for a Level ≤ 2. Execute the actual change.
5. **GATE** — N/A for Level ≤ 2 (note that it correctly does nothing here).
6. **CLOSE** — run `generators/closure_summary.md`; update `.ai/CONTEXT.md` ledger + the
   `DISPATCH_LOG.md` row.

Test cost guard (READ): plain `pytest` only. Never invoke the paid Claude/Codex CLI to
"verify". Never `python main.py status`. Check a live gateway with
`curl http://127.0.0.1:9003/health`. (A9 dispatch, verbatim.)

---

## The deliverable that actually matters — the friction report

Alongside the completed task + artifacts, write a short **HARNESS FRICTION REPORT** at
the end of this dispatch file. This is the point of the whole exercise. Answer, with
evidence from the run you just did:

- **Did the operating loop hold?** Per `operating_model.md`: did the Operator→Manager→
  Executor loop work, or did a gate get skipped? Where did a template/generator/doc do
  real work vs. get in the way?
- **§15 success criteria** — walk each and say met / not-met / N/A with a one-line reason.
- **The §16 question, answered from evidence, not vibes:** did anything get lost in the
  hand-off? Did you ever wish for queryable cross-task flow status? Would a driver or a
  visual graph have *actually* saved you here, or is the file/dispatch discipline enough
  for a task this size? Give a direct recommendation: **Phase 2 justified yet? Y/N + why.**
  This verdict directly governs whether `deferred/AGENT_11_WEBUI_HARNESS_NODEGRAPH.md`
  ever un-defers.
- **Doc bugs found** — anything in `docs/harness/` that was unclear, wrong, or missing.
  Fix the trivial ones inline as part of this run; list the rest.

---

## Scope — DO NOT
- No new branch. No orchestrator/DB change. No new gateway state (§0/§11).
- Do NOT build the node-graph or any UI/driver. If the friction report concludes Phase 2
  is justified, that is a FINDING that triggers a *separate future dispatch* — not work
  you do here.
- Do not pick a Level-3 task (this run must not block on a human gate).

## Definition of done
- One real task taken end-to-end through the loop, its change committed on `feat/task-harness`.
- All five artifacts produced (packet, milestone, F-tags, closure summary, ledger/log update).
- `pytest` green for any code touched; no changes to orchestrator hot path or DB.
- The HARNESS FRICTION REPORT appended here, ending in the explicit Phase-2 Y/N verdict.
- Report back; hold on branch; do not merge — operator reads the friction report first.
