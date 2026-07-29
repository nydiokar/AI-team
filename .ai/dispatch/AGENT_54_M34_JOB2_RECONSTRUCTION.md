# DISPATCH — A54 · M3.4 Job 2: durable Case reconstruction (`get_case_brief`) + auto-reconcile at boot

**Level:** 3 (db read model + MCP tool + role-boot hook) · **Type:** code
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, not yet executed)
**Depends on:** A52 (Job 1 adoption). **Unblocks:** A55 (crash-respawn). **Ultimate goal this serves:**
`SPEC_COMPLETION_PLAN.md` §0 goal step 6 ("survive context resets — resume from the DB alone").

> **Outcome, not a script.** A Manager that lost its context (compaction, restart) must be able to pick a
> Case back up knowing everything that matters from the DB in ONE call, and automatically re-establish its
> outstanding waits/groups. Design the brief's shape to serve that; acceptance is the contract.

## Why (intent)
Design `docs/AUTONOMOUS_CASE_CONTINUATION_DESIGN.md` §7 job 2. Today `get_case` returns
objective + criteria + stage only (`mcp_manager.py`), so a resumed Manager is half-blind and its
in-flight waits live in lost in-process memory. This is the prerequisite that makes crash-respawn (A55)
*safe* — you cannot respawn a Manager onto a Case it can't fully reconstruct.

## TASK
Add a single DB-only `get_case_brief(case_id)` returning the full working state of a Case, and make a
booting Manager auto-reconcile its waits and re-arm its live wait-groups from the ledger.

## TYPE
code. Branch `feat/m34-job2-reconstruction`; PR at close, merge yourself.

## CONTEXT (reuse verbatim)
- Read sources already present: `flow_runs` (objective/criteria/status), `flow_links` (dispatched workers +
  worker sessions), `flow_events` (`review.*` verdicts, `worker.wait_pending/resolved`, the M3.4 wait-group
  markers with `entity_type='wait_group'`), the M3.4 `continuation_watermark` (rounds used) + `case_round_cap`.
- Existing seams to reuse, not duplicate: `db.reconcile_worker_waits` (A46), `db.arm_wait_group` +
  `compute_continuation_tick` (M3.4), the role-boot path `orchestrator._invoke_manager`/`_role_boot`.

## CHANGES
- **(a) `db.get_case_brief(case_id)`** — one call returning: objective + `completion_criteria` + round cap +
  turn/cost cap (if A53 landed) + rounds-used + dispatched workers (from `flow_links`) + latest `review.*`
  verdict per worker + open/ready waits + **armed wait-groups and their satisfaction state** (reuse
  `compute_continuation_tick`). Read-only; no new table.
- **(b) `get_case_brief` MCP tool** — the Manager's single "where am I on this Case" read.
- **(c) Boot reconcile hook** — at Manager role-boot on an existing Case, auto-run `reconcile_waits` AND
  re-arm any live wait-groups (idempotent — A46/M3.4 idempotency already guarantees no duplicate markers), so
  the resumed Manager wakes with its full obligation set.

## ACCEPTANCE (proof, not vibes)
1. e2e (fake backend): build a Case with ≥2 dispatched workers (one finished + reviewed, one in-flight) and
   an armed ANY group; `get_case_brief` returns ALL of it from the DB alone (no in-process state) — assert
   every field against the seeded ledger.
2. e2e: boot a Manager onto that Case → assert waits reconciled + groups re-armed idempotently (running the
   boot hook twice writes no duplicate markers).
3. Targeted pytest green (case/flow/relay/continuation suites). Flag OFF ⇒ byte-identical (brief is
   read-only; boot hook no-ops when continuation/relay flags are OFF).

## REALITY CONSTRAINTS
- `get_case_brief` must be a bounded single query set (no N+1 per worker — CLAUDE.md §8); reuse the existing
  JOIN'd read helpers (`list_flow_links`, `list_flow_events`) rather than per-entity fanout.
- Re-arm must be idempotent against the existing markers (do not create a second group on re-boot).

## RESERVED DECISIONS
- Whether the boot reconcile hook is always-on when the flags are ON, or gated behind an explicit
  "resume" signal. Recommendation: always-on when continuation is enabled (a boot onto an open Case IS a
  resume).

## SCOPE OUT
Respawning a dead session (A55); M4.

## TRAIL / EVIDENCE (fill at close)
- Branch / PR · pytest output · the brief's field list.

---
## Milestone (burndown)
- [ ] get_case_brief db read (single bounded query set)
- [ ] get_case_brief MCP tool
- [ ] boot reconcile + re-arm hook, idempotent
- [ ] e2e green, byte-identical when flags OFF
- [ ] PR opened + merged

## Closure (fill on completion)
_(verdict + evidence)_
