# Adversarial Review — Task Harness Workflow (v0.4 → v0.5)

**Reviews:** `docs/Task_harness_workflow.md` as written (v0.4), against the actual
codebase (`orchestrator.submit_instruction`, the single-turn task model,
`load_compact_context`, the `mesh_tasks` ledger, `file_watcher`/`task_parser`
auto-pickup, and the existing `.ai/dispatch/AGENT_8_*` convention).
**Date:** 2026-07-03
**Verdict:** The doc is sound *doctrine* but **not implementable as written** —
one load-bearing contradiction (F1) would send an executor building a flow engine
the spec elsewhere forbids. Six findings; all resolved in v0.5. Fixes are inline
in the rewritten spec, keyed by tag.

---

## Findings

### F1 (BLOCKER — self-contradiction) — "not a platform" vs. gateway flow state

§15 locks "this is not a multi-agent platform … a small gateway-attached kernel,"
but §11/§13 specify gateway-owned flow state (`flow_run_id`, `current_stage`,
`plan_review`, a stage machine, "FlowRun record"). **In this codebase a task is a
single dispatch unit** — `submit_instruction → _make_task → _enqueue_task` → one
backend turn → one `mesh_tasks` row (`orchestrator.py:1598`). There is no
multi-turn flow object, no `current_stage`, nothing that reads a stage column.

**Failure scenario:** an executor takes §11/§13 literally, adds a `flow_runs`
migration + stage machine to the orchestrator, collides with the single-turn model
(and likely with migration numbering — the project already collided on 13), and
ships a half-built platform the spec's own §15 forbids. Or it freezes, unable to
reconcile §15 with §11.

**Resolution (v0.5 §0, §11, §16):** decision of record — **v1 adds ZERO gateway
state.** The XML packet + milestone file + dispatch convention ARE the state. The
`flow_runs` model is demoted to a deferred **Phase 2 (§16)**, built only if the
discipline proves insufficient. **Fixed.**

### F2 (BUG — duplicate infra) — memory rule reinvents two existing systems

§7 invents a fresh `<memory_entry>` store and "cheap models for async memory
compression," referencing neither `load_compact_context(task_id)` (already returns
bounded prompt/summary/files/usage/errors/constraints from the DB-canonical
`mesh_tasks` ledger — tasks #31/#32) nor the file-memory (`MEMORY.md` +
`memory/*.md`).

**Failure scenario:** an executor builds a third memory store that competes with
the DB ledger for "truth," or stalls trying to reconcile three overlapping
systems; the `<memory_entry>` async-compression service is an unbuilt dependency
masquerading as a rule, so any code that assumes it exists is dead on arrival.

**Resolution (v0.5 §7):** memory = `load_compact_context` (DB-canonical) +
file-memory. `<memory_entry>` is redefined as a **write format for file-memory
only**, not a store. The async-compression requirement is downgraded to
best-effort, never source-of-truth. **Fixed.**

### F3 (BUG — cost-guard violation) — provider smoke test inside the task loop

§9 places a "same prompt, multiple responses, low temperature" model smoke inside
the per-task flow.

**Failure scenario:** an autonomous executor runs a provider smoke on *every*
task; against the paid Claude/Codex CLI that is exactly the token-burn the Test
Cost Guard exists to prevent (and previously cost millions of tokens).

**Resolution (v0.5 §9):** moved out of the per-task loop into **provider
onboarding only**, stamped with the Test Cost Guard, told to reuse the existing
LLM-turn-observability smokes + backend `doctor` probes, and gated behind operator
approval for any paid run. **Fixed.**

### F4 (BUG — no mechanism) — "tailing reviewer" implies concurrent agents on one tree

§5 says the reviewer "tails docs/diffs/logs while the executor is still active."
There is no concurrent-agent primitive here — dispatches are sequential single
turns — and two live agents on one working tree is a merge/race hazard.

**Failure scenario:** an executor tries to run a reviewer concurrently against a
mutating working tree; the reviewer reviews half-written files, or the two race on
the same paths.

**Resolution (v0.5 §5):** reframed as a **sequential checkpoint reviewer** — the
executor commits at a milestone checkpoint, the reviewer reviews the *committed
diff* via the existing `/code-review` + `/security-review` skills. **Fixed.**

### F5 (HARDENING — non-deterministic) — level thresholds are subjective

§3's "tiny / medium / high" have no rule, so an autonomous agent can't select a
level deterministically and will under-escalate risky work (the expensive
direction).

**Failure scenario:** an agent classifies a DB-migration or mesh change as
"small," skips adversarial review, and ships an infra defect unreviewed.

**Resolution (v0.5 §3):** added concrete **Level-3 triggers** (DB migration;
security/secrets; mesh/worker; trading; agent-behavior/autonomy; destructive op;
> ~5 files / service-boundary) with "when in doubt, escalate," plus a mandatory
**cost cap** (review off for Level ≤ 1, plan↔review loop capped at 2 rounds).
**Fixed.**

### F6 (NOTE — phantom consumer) — XML packet / wiki have no parser and dual status

§2.1 reads like a schema; nothing parses it. §2.3 wiki is "optional" in §12 but a
Level-3 *output* in §3.

**Failure scenario:** an executor writes a validator for a format nothing enforces
(wasted work), or treats wiki generation as a shipping gate and blocks closure on
an optional artifact.

**Resolution (v0.5 §2.1/§2.3/§13):** stated explicitly — the packet is
model-facing prose, **not** a parsed contract, and **no validator should be
built**; the wiki is optional, Markdown is source of truth, never a gate, not
automated in v1. **Fixed.**

---

## Meta-process added (operator request)

The `read → adversarial review → fix → dispatch → auto-pickup` loop the operator
described is now a **first-class section (v0.5 §14 — The Dispatch Pipeline)**,
grounded in the real auto-pickup primitive (`file_watcher.py →
_handle_new_task_file → task_parser` consuming `.task.md` YAML-frontmatter files)
and the house F-tag convention. Auto-pickup is bounded: **Level 3 requires the
operator-approval stage before dispatch** — no autonomous pickup of
destructive/infra/security/agent-behavior work. "Text engine" is clarified as a
*drafting role* any model route can play, **not** a new service to build.

---

## Kept as-is (genuinely good in v0.4)

- XML for model-facing packets (drift reduction).
- Milestone burndown as the resumable progress ledger.
- Single-item long-running lane (§6) — directly targets the recorded
  overbatch/hallucinate-success failure (`false-success-intent-only`).
- Honesty framing and feature-flag scope control.

## Re-verification

- v0.5 spec re-read: §0/§11/§16 no longer contradict §15 (F1); §7 names the two
  real memory systems (F2); §9 is onboarding-only + cost-guarded (F3); §5 is
  sequential (F4); §3 has deterministic triggers + cost cap (F5); §2/§13 forbid a
  validator and de-gate the wiki (F6).
- No code changed by this review — v1 is prompt-and-artifact discipline; the build
  work is the dispatch in `AGENT_9_TASK_HARNESS.md`.
