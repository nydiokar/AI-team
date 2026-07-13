# A43 — Live rework-cycle proof + restart-incident hardening

**Date:** 2026-07-13 · **Level:** 3 · **Status:** merged (PR #17) · **Branch:** `feat/manager-restart-resilience` (merged, deleted)

## Trigger
Operator debrief of a live incident: a user session showed
`CLIConnectionError: Cannot write to terminated process (exit code: 0)` in chat, LLM telemetry
"failed" while cache read/write showed activity, and a dead Manager session rendered as recently
"active." Root-caused to a **gateway restart (11:26:33 UTC, clean `pm2` restart)** killing all
in-process Claude CLI subprocesses; only the session with an in-flight turn surfaced the SDK error.

## What ran (proof)
Operator-driven, bounded, supervised in-gateway Manager via `POST /api/manager` (`node_id` omitted =
`__local__`). Case `e8bb1b92fbcc41d4a2c667134bb799f2`, session `1f9bce3f5a87`. Sequential 2-task loop:

```
flow.created → T1 dispatch(task_c903fca2) → finish → review.REWORK_REQUESTED   ← first live rework
             → rework dispatch(task_e64405b8) → finish → review.ACCEPTED
             → T2 dispatch(task_5fc16122) → finish → review.ACCEPTED
             → flow.CLOSED
```

**Milestone:** the `rework_requested → re-dispatch → accepted` cycle is now proven live for the
first time (A41/A42 only ever produced clean accepts). The manager did NOT rubber-stamp — it rejected
worker 1's first commit and the rework produced a correct second commit it then accepted.

## Deliverable (PR #17, merged)
`feat/manager-restart-resilience`, 3 commits + tests (+137/−1):
- `340eee1` claude_driver: recover from dead-subprocess write after gateway restart.
- `639ade0` orchestrator: make terminated-process failures retry-eligible (the rework caught that
  the driver fix alone wasn't enough — the `fatal` classification also had to change).
- `1e0c49c` task_state_truth: honestly report sessions with a lost driver ("open but can't resume"
  dishonesty flagged in the incident).

## 🔴 Critical finding — Manager role is carrier-coupled
The same invoke with `node_id="kanebra-worker"` (session `dfa521bfb2df`) booted a **bare, role-less,
tool-less Claude session** (*"I'm ready to help. What would you like me to work on?"*) — no role
prompt, no manager MCP tools, no assignment. Role boot lives only on the in-gateway SDK driver path,
not the node worker path. **The Manager cannot run on any node → stuck on the gateway host.**
→ [`DROP_MANAGER_ROLE_CARRIER_INDEPENDENT.md`](DROP_MANAGER_ROLE_CARRIER_INDEPENDENT.md) (next build).

## Also observed (tracked)
- Workers ran as sessionless `run_oneoff` tasks again → [`DROP_DISPATCH_WORKER_REAL_SESSION.md`].
- Mixed-timezone timestamp writers → [`DROP_TIMEZONE_NATIVE_TIME.md`].
- `/api/manager` has **no dry-run** — every call is a real paid boot (learned the hard way via a probe).
