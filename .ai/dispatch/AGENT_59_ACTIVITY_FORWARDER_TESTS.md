# DISPATCH — A59 · A17 orphan drift: `_ActivityForwarder` keep-with-tests

**Level:** 3 (live remote-worker path) · **Type:** code (test-first)
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, not yet executed)
**Depends on:** — (independent). **Unblocks:** clears the last live, untested A17 orphan cluster.
**Ultimate goal this serves:** correctness/observability hygiene — untested live code on the worker path.

> **Outcome, not a script.** `_ActivityForwarder` is **live** on the remote-worker path with **zero tests**.
> It came in through the undispatched A17 WIP snapshot (`d1556ad`), never reviewed. Decision already taken:
> **keep-with-tests, not revert** — it is live working code; ripping it out is the riskier move. This job
> makes it honest by locking its behavior with tests.

## Why (intent)
The A17 audit (`AGENT_17_WIP_MERGE_RECONCILE.md`) flagged four orphan clusters. Three are resolved:
backend-usage aggregation (A17b, merged), mesh-fleet count/tz (folded), and the opus-default flip
(**FIXED** 2026-07-30, PR #46 — reverted the catalog default to sonnet). The remaining live cluster is
`_ActivityForwarder` — instantiated in `src/worker/agent.py` on the remote-worker path, forwarding worker
activity to the gateway, with no test coverage at all.

## TASK
Characterize `_ActivityForwarder`'s real behavior (what it forwards, when, and how it fails), then lock it
with tests: happy-path forward, transport failure is swallowed (a forwarding hiccup must never crash or
stall the worker), and the §7 service-boundary questions (unbounded activity, backpressure, forwarder
death) are answered with either a test or a written deferral. If characterization reveals a real defect,
fix it minimally under the same branch.

## TYPE
code, test-first. Branch `feat/activity-forwarder-tests`; PR at close, merge yourself.

## CONTEXT (reuse verbatim)
- `src/worker/agent.py`: `class _ActivityForwarder` (~line 844); constructed at ~line 1010
  (`self._activity_forwarder = _ActivityForwarder(self._http, self.cfg.node_id)`); field declared ~938.
- It uses the worker's HTTP client to POST activity to the gateway — assert failures are isolated
  (no exception escapes into the worker's execution loop).

## ACCEPTANCE (proof, not vibes)
1. Tests cover: activity is forwarded on the happy path; a transport error is swallowed (worker unaffected);
   the forwarder's lifecycle (start/stop/None-http) is safe.
2. §7 boundary answered: is forwarded activity bounded/backpressured? If not, a written deferral in this packet.
3. Targeted pytest green; no change to live behavior unless a real defect is found (then minimal fix + note).

## REALITY CONSTRAINTS
- **Keep, do not revert** — this is live remote-worker code. Do not delete it to "clean up."
- Tests must not hit a real gateway or spawn a worker — fake the HTTP client.

## SCOPE OUT
The other (already-resolved) A17 clusters; any worker-protocol redesign.

## TRAIL / EVIDENCE (fill at close)
- Branch / PR · pytest output · §7 boundary answer.

---
## Milestone (burndown)
- [ ] `_ActivityForwarder` behavior characterized
- [ ] happy-path + failure-isolation + lifecycle tests green
- [ ] §7 boundary (bound/backpressure/death) answered or deferred in writing
- [ ] PR opened + merged

## Closure (fill on completion)
_(verdict + evidence)_
