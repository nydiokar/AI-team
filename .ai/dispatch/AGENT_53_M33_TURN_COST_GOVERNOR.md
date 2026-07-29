# DISPATCH — A53 · M3.3 completion: per-invocation turn/cost governor + kill path

**Level:** 3 (backend driver options + orchestrator lifecycle + Case status) · **Type:** code
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, not yet executed)
**Depends on:** — (independent; may run alongside A52 in a different loop). **Unblocks:** safe long
autonomous runs (A55). **Ultimate goal this serves:** `SPEC_COMPLETION_PLAN.md` §0 goal step 7 ("bounded — caps + kill path").

> **Outcome, not a script.** A bounded *autonomous* Case must not be able to run away in turns or cost, and
> the operator must be able to stop it cleanly into a resumable state. The engine owner decides the exact
> config surface; the acceptance is the contract.

## Why (intent)
The M3.3 milestone named "round/turn/cost caps; kill path → `flow.interrupted`" but only the durable-relay
half shipped (A46). Verified gaps: `ClaudeAgentOptions` (`claude_driver.py:537`) passes **no `max_turns``;
the design §1 table states no round/cost governor runs on any Manager/worker. M3.4 added a *round* cap
(continuation generations) but not an intra-session turn/cost cap or a kill path. Without this, enabling
long autonomous continuation (A52+) is unsafe.

## TASK
Give every Manager/worker session an enforceable turn (and, if cheap, cost) ceiling passed to the SDK, plus
a programmatic/operator kill path that lands the Case in a resumable `blocked`/`interrupted` state — reusing
existing cancel/stop plumbing, not a second one.

## TYPE
code. Branch `feat/m33-turn-cost-governor`; PR at close, merge yourself.

## CONTEXT (reuse verbatim)
- SDK options assembly: `claude_driver.py:537` (`ClaudeAgentOptions(...)`). `max_turns` is a first-class SDK
  option — add it here, sourced from config / `completion_criteria`.
- Existing stop/cancel: `orchestrator.stop()` + per-task cancel events (`_task_cancel_events`,
  `_running_exec_tasks`) — reuse for the kill path.
- Event vocab already has `flow.interrupted` + `flow.blocked` (`db.py FLOW_EVENT_TYPES`). Round cap +
  `flow.interrupted` escalation pattern already implemented in `orchestrator._continue_case_once` — mirror
  its escalation shape, do not fork it.

## CHANGES
- **(a) Turn ceiling.** Pass `max_turns` into `ClaudeAgentOptions` for Manager + worker sessions, resolved
  from config (a new `config.system` / `config.mesh` knob) with a per-Case override via `completion_criteria`
  JSON. Surface the effective cap in `/health` (it is currently echoed there but never enforced).
- **(b) Kill path.** A programmatic/operator stop for a Case → cancel its in-flight worker tasks (reuse the
  cancel plumbing) → append `flow.interrupted` → set Case `status=blocked` (resumable, per A37 semantics) →
  escalate via the notifier. Idempotent.
- **(c) Cost cap (only if cheap).** If a per-session token/cost signal already exists (`llm_events` /
  backend usage), enforce a soft ceiling → same `flow.interrupted` escalation. If it needs new plumbing,
  RESERVE it (do not over-build) and ship turns-only.

## ACCEPTANCE (proof, not vibes)
1. e2e (fake backend): a session configured with a low `max_turns` is halted by the SDK and the Case is
   surfaced honestly (not a silent stall); assert the option is actually passed (not just stored).
2. e2e: a kill request on an open Case cancels its worker task(s), writes `flow.interrupted`, leaves the
   Case `blocked` + resumable, and escalates once (idempotent on a second kill).
3. Config/flag default leaves behavior byte-identical (no cap ⇒ no `max_turns` passed ⇒ legacy).
4. Targeted pytest green (`test_claude_driver`, orchestrator lifecycle, case-close tests).

## REALITY CONSTRAINTS
- Do NOT confuse the M3.4 *round* cap (continuation generations, already shipped) with this *turn* cap
  (SDK turns within one session). They are distinct ceilings; both escalate via `flow.interrupted`.
- The kill path must leave the Case **resumable** (`blocked`), never force-`closed` (closure is A37's
  authoritative, criteria-gated op).

## RESERVED DECISIONS
- **R1 — cost cap plumbing.** Ship turns-only if a cost signal isn't already per-session; escalate whether
  to build cost accounting now or defer. Recommendation: defer cost to a follow-up, ship the turn governor +
  kill path.

## SCOPE OUT
The round cap (A52/M3.4); reconstruction/respawn (A54/A55).

## TRAIL / EVIDENCE (fill at close)
- Branch / PR · pytest output · the config knob name + default.

---
## Milestone (burndown)
- [ ] max_turns threaded into ClaudeAgentOptions + config knob + health surfacing
- [ ] kill path → cancel + flow.interrupted + blocked (resumable) + escalate, idempotent
- [ ] cost cap shipped or reserved with a written reason
- [ ] e2e green, default byte-identical
- [ ] PR opened + merged

## Closure (fill on completion)
_(verdict + evidence)_
