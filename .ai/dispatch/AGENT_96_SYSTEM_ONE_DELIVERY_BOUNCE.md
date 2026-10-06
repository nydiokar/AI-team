```yaml
job_id: AGENT_96_SYSTEM_ONE_DELIVERY_BOUNCE
created_at: "2026-10-05T08:46:35.435874+00:00"        # CANONICAL — set once at dispatch, never derive again
status: blocked              # ready | active | blocked | done | dead
owner: ""
depends_on: AGENT_94_SYSTEM_ONE_CORE_DELIVERY_SCORECARD
results_ref: DISPATCH_LOG.md#A96             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-05T08:46:53.362696+00:00"
```

# DISPATCH — A96 · System-One Move 3 "bounce instead of wake" (BLOCKED on A94 data)

**Level:** 3 (agent-behaviour change: the harness sends a bounded rework instruction to a worker and
withholds a Manager wake; touches the Wake-Dispatcher) · **Type:** code
**Authored:** 2026-10-05 · **Status of this packet:** blocked
**Depends on:** A94 **merged AND its precondition artefact met** (below)
**Branch:** `feat/system-one-bounce` + PR + self-merge.
**Canonical spec:** [`docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md`](../../docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md)
§6.3 (normative), §7 M1/M7.

> **Read this first — why this packet exists.** Move 1 only annotates. The money is in not running the
> fat Manager wake at all for deliveries that are clearly not ready. This is the most consequential S1
> authority, so it is earned: it may be built only after Move 1 proves high precision against the
> Manager's own verdicts.

## Unblock condition (mechanical — check before starting)
- `results/s1/delivery_*.json` from A94 shows **precision ≥ 0.90 at `T_ACT` on ≥ 100 labeled verdicts**,
  AND
- ≥ 2 weeks of live shadow/annotate decision rows whose precision against subsequent verdicts is within
  0.05 of the replay precision.

When both hold: `python scripts/dispatch/dispatch_state.py --set AGENT_96_SYSTEM_ONE_DELIVERY_BOUNCE status ready`.

## Why (intent)
Fewer paid Manager wake turns per accepted delivery (spec §7 M1, target −25%), with a wrong-bounce rate
≤ 5% (M7). No loss or duplication of wakes.

## TASK
1. Add `s1.delivery_bounced` to `FLOW_EVENT_TYPES` (`src/control/db.py:152`).
2. In the Wake-Dispatcher, before delivery: for presented tasks whose cached delivery outcome is `act`
   and which pass all hard limits (spec §6.3), do the following:
   1. record the bounce event (idempotency key = `decision_id`);
   2. submit one bounded rework instruction into the same worker session (`submit_instruction`,
      `join_case_id`, principal `automation`);
   3. re-emit `worker.wait_pending` for the same group with `from_task` swapped for `to_task`;
   4. exclude the task from this wake.
3. On the next wake for `to_task`, render the bounce history line.
4. Flag `S1_DELIVERY_BOUNCE`. It refuses to enable while the A94 precondition artefact is missing
   (checked when the flag is read).
5. Tests over the **real** `compute_continuation_tick`:
   - membership swap (ANY/ALL/NAMED);
   - one-bounce cap;
   - crash between event and swap replays safely;
   - no bounce when paused, on the last round, or with a non-live session;
   - flags OFF byte-identical.

## ACCEPTANCE (proof, not vibes)
1. Targeted pytest green, including the recovery/replay tests.
2. Act arm vs annotate arm over ≥ 100 deliveries per arm: M1 down ≥ 25%; M7 wrong-bounce ≤ 5%.
3. Zero duplicate/lost wakes in the decision log ↔ continuation-token reconciliation over the trial.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — Enabling the act arm.** Operator approval (Level 3). Default: off.
- **R2 — Automatic rollback.** If M7 > 5% in any rolling 2-week window, the flag goes off and a CONTEXT
  note is written. The default is automatic.

## SCOPE OUT
- More than one bounce per task.
- Bouncing on the last round or when paused.
- Any S1 acceptance or closure.
- Bouncing non-Case work.

## TRAIL / EVIDENCE (fill at close)
- `evidence:` → trial report `results/s1/bounce_trial_<date>.json`, tests, PR.
- DISPATCH_LOG A96 closure.

---
## Milestone (burndown)
- [ ] Unblock condition verified (artefact + live agreement)
- [ ] Event type + dispatcher bounce path + limits
- [ ] Membership swap + recovery tests
- [ ] Flag with precondition check
- [ ] Trial ≥ 100/arm → report → decision

## Closure (fill on completion)
