# DISPATCH — A52 · M3.4 Job 1 adoption + activation (make the Wake-Dispatcher actually used)

**Level:** 3 (role prompt + MCP tool + control route + db seam) · **Type:** code + docs
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, not yet executed)
**Depends on:** — (M3.4 Job 1 engine merged, PR #45). **Unblocks:** A54 (Job 2), A56 wiring (M4).
**Ultimate goal this serves:** `docs/SPEC_COMPLETION_PLAN.md` §0, goal step 4 ("arm and return, never block").

> **Outcome, not a script.** The Wake-Dispatcher engine exists and is proven; it is **inert** because
> nothing tells the Manager to use it. The load-bearing result: a live Manager, after fanning out
> workers, **arms a wait-group and hands control back** — free to talk to the operator — and is
> re-entered on each completion instead of block-polling `wait_for_worker`. You own how to phrase the
> role guidance; the acceptance below is the contract.

## Why (intent)
`wait_for_worker` is an in-turn BLOCKING poll (`mcp_manager.py:142`, bounded ≤600s). M3.4 Job 1 shipped
the event-driven replacement (`arm_wait_group` → coalesced wake on satisfaction, skips a BUSY session so
it never interrupts a live operator turn). But `docs/harness/roles/manager.md:138` still names M3.4 as a
*future* thing and steers the Manager to bounded-block-poll, and `CASE_CONTINUATION_ENABLED` is OFF. Until
the role adopts arm-and-return, the fix is dead code.

## TASK
Make the Manager adopt the continuation loop as its default waiting posture, give it an ergonomic way to
set the round cap, and hand the operator a clean activation runbook. Do NOT change the engine.

## TYPE
code + docs. Branch `feat/m34-job1-adoption`; PR at close, merge yourself per branch policy.

## CONTEXT (reuse verbatim)
- Engine seam already on `main`: `db.arm_wait_group` / `compute_continuation_tick` / orchestrator
  `_continue_case_once` / the `arm_wait_group` MCP tool + `POST /api/cases/{id}/wait-group`. Do not
  duplicate any of it.
- Round cap already read from `completion_criteria` JSON `{"round_cap": N}` (`db.case_round_cap`).
- Role prompt: `docs/harness/roles/manager.md`. Dispatch hints: `scripts/mcp_manager.py::_dispatch_worker`
  ("Next:" lines) + `_wait_for_worker`.

## CHANGES
- **(a) Role posture.** Rewrite the "Waiting on a batch" section of `manager.md`: default = after a
  fan-out, `arm_wait_group` (ANY for "wake me on each completion", ALL/named for "wake me when the batch is
  done") and RETURN control; the harness re-enters you. `wait_for_worker` demoted to a last-resort single
  synchronous wait. State plainly that a wake never interrupts a live operator turn (BUSY-skip).
- **(b) Dispatch hints.** Update the `_dispatch_worker` "Next:" text to point at `arm_wait_group`, not a
  serial `wait_for_worker` chain.
- **(c) Round-cap ergonomics.** Thread an optional `round_cap` through the `open_case` MCP tool → the
  `/api/cases` open route → `db.open_case`, encoding it into the `completion_criteria` JSON the engine
  already reads. Keep `completion_criteria` human criteria working alongside it (JSON object OR plain text).
- **(d) Activation runbook.** A short section (in `manager.md` or `SPEC_COMPLETION_PLAN.md`): set
  `CASE_CONTINUATION_ENABLED=1` + confirm sibling flags + `pm2 restart ai-team-gateway`.

## ACCEPTANCE (proof, not vibes)
1. Unit: `open_case(round_cap=2)` (tool → route → db) round-trips so `db.case_round_cap(case)==2`, and a
   plain-text `completion_criteria` still parses (no regression to A37 close-gate).
2. The role/hint docs no longer instruct serial block-polling as the default; a grep for the old
   "long-poll each" guidance is gone, replaced by arm-and-return.
3. Targeted pytest green (`test_mcp_manager`, `test_control_api_*`, case/close tests). Flag OFF ⇒
   byte-identical.
4. **Live (operator-gated, records in Closure):** flag on + restart; a Manager fans out ≥2 workers, arms an
   ANY group, returns, exchanges ≥1 message with the operator, and is re-entered with a coalesced review
   turn per completion — no blocking poll observed on the Case timeline.

## REALITY CONSTRAINTS
- `completion_criteria` is dual-purpose now (human criteria for A37's close-gate AND `{"round_cap"}` for
  M3.4). Do not break `_parse_completion_criteria` — support both shapes.
- Do not flip the flag in code or `.env` as part of the merge; activation is the operator's call (it turns
  on new autonomous behavior).

## RESERVED DECISIONS
- Default `round_cap` when the Manager omits it (engine default is 50). Confirm 50 or set a lower live-safe
  default in the role guidance.

## SCOPE OUT
Engine changes; reconstruction/respawn (A54/A55); the turn/cost governor (A53).

## TRAIL / EVIDENCE (fill at close)
- Branch / PR · targeted pytest output · the live re-entry timeline (Case id + `case_continuation_delivered`
  events) if activation is run.

---
## Milestone (burndown)
- [ ] role posture rewritten (arm-and-return default)
- [ ] dispatch hints updated
- [ ] round_cap threaded tool→route→db, dual-shape criteria safe
- [ ] activation runbook written
- [ ] targeted pytest green, flag OFF byte-identical
- [ ] PR opened + merged
- [ ] (operator) live re-entry proof captured

## Closure (fill on completion)
_(verdict + evidence)_
