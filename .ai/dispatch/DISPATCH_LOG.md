# Dispatch Log — Operational State Table

**Purpose:** the source-of-truth index for every dispatched job. This is a
**manual state machine** (documentation step until a real dispatch state machine
exists). A "manager" agent that creates and dispatches jobs must read this file
first, append new rows here, and update status as jobs move through the pipeline.

Referenced from [`.ai/CONTEXT.md`](../CONTEXT.md). Each dispatch has its own
`AGENT_N_*.md` file in this folder (the job packet), usually paired with a
`*_REVIEW.md` (adversarial review of the dispatch) and/or a `*_BUILD_REVIEW.md`
(adversarial review of the shipped code).

## Status vocabulary

`dispatched` → job packet written, not yet built ·
`built` → code shipped on a branch ·
`reviewed` → adversarial build-review done, fixes folded ·
`merged` → on `main` ·
`blocked` → waiting on a gate ·
`deferred` → parked on purpose

---

## Jobs

| # | Job / Track | Packet | Branch | Status | What was done | What's left |
|---|---|---|---|---|---|---|
| A8 | Operator Signal (Web Push #21, Backend Usage #30/#33, unblock M1/M2) | `AGENT_8_OPERATOR_SIGNAL.md` (+ `_REVIEW`, `_T1_BUILD_REVIEW`) | `feat/operator-signal` | **merged** (PR #5) | T1 Web Push (migration 20, `push_service.py`, SW handlers, 15 tests) + T2 Backend Usage (`backend_usage.py`, `/api/backends/usage`, `BackendUsagePanel`, 8 tests), both reviewed & fixed (per-call VAPID, size cap, malformed-sub disable). | **Operator TODO:** set `VAPID_*` env, `pip install -e ".[push]"`, add VAPID vars to `.env.example` (env files weren't editable from build env). |
| A9 | Compact-Context Continuation (#31/#32) | `AGENT_9_COMPACT_CONTEXT.md` (+ `_REVIEW`, `AGENT_9_BUILD_REVIEW`) | `feat/compact-context` | **merged** (PR #6) | Wired dead-but-tested `load_compact_context` into opt-in `continues: <task_id>` path in `process_task`; fence-escape hardened (`_defuse_fence`); instance-local re-injection guard; 13 tests. Docs: `docs/Task_harness_workflow.md` §7/§14. | — (complete) |
| A9H | Task Harness Workflow Kernel (v1) | `AGENT_9_TASK_HARNESS.md` (+ `_REVIEW`) | `feat/task-harness` | **dispatched** | Dispatch packet + adversarial review (v0.4→v0.5, F1 flow-engine contradiction resolved). Spec is `docs/Task_harness_workflow.md`. | **Build not started.** Stand up v0.5 task-quality loop as prompt+artifact discipline (templates, generators, Dispatch Pipeline) with ZERO new gateway state. |
| A10 | M3 Claude Telemetry (#10) | `AGENT_10_M3_CLAUDE_TELEMETRY.md` (+ `_REVIEW`) | commit `c168028` (+ `896990f`) on `main` | **merged** | M3 `ClaudeStreamJsonAdapter` (`src/core/telemetry_adapters/claude_stream_json.py`) + wiring at `ClaudeCodeBackend` public boundary; token semantics `includes_cache`; double-count guard; tool-name/category mapping (no input/content stored); coverage `stream_only`. Went straight to `main`, NOT via a `feat/m3-claude-telemetry` branch (that branch never existed — earlier docs said "ready to merge" in error). | — (complete; live on main) |
| FX1 | Fix: SDK `is_error` stored as successful reply ("Prompt is too long") | `FIX_CLAUDE_ISERROR_PROMPT_TOO_LONG.md` | `feat/compact-context` → main | **merged** (a3f734b) | `ClaudeSDKClientDriver` now inspects `ResultMessage.is_error`/`.subtype` and delivers salvaged work + honest failure instead of copying `"Prompt is too long"` into the reply as `success=True`. Root cause: long-lived `claude` process never exits non-zero, so the SDK yields (not raises) the error result. `task_server.py` + timeline refinements. Tests in `test_claude_driver.py`. | Memory `claude-iserror-prompt-too-long`: **Horse redeploy needed** (this restart covers it); #41 context-fill gauge still open. |

---

## Milestone status

**M1/M2 (LLM Turn Observability) — SHIPPED (operator-confirmed 2026-07-03).** Local
Codex smoke + controlled mesh smoke passed 2026-07-02; SQLite benchmarks passed (#8).
The old "#9 gateway-routed mesh smoke" gate is **retired** — the `gateway_node_id`-null
detail was a nice-to-have validation artifact, not a real product blocker, and is no
longer treated as one. **M3 (A10) is already merged on `main`** (commit `c168028`).

---

## How to add a new job (for a manager/dispatcher agent)

1. Pick the next unblocked item from **Current Priorities** in `.ai/CONTEXT.md`.
2. Write the job packet `AGENT_N_<THEME>.md` in this folder (follow the
   house style in `AGENT_8_OPERATOR_SIGNAL.md`: theme, ranked "why these, in this
   order", per-task real-value + code-grounded scope, F-tag scope guards, test-cost
   guard header).
3. Run an adversarial review of the dispatch → `AGENT_N_<THEME>_REVIEW.md`; fold
   corrections back into the packet before build.
4. Append a row here as `dispatched`.
5. On build, add the build review → `AGENT_N_..._BUILD_REVIEW.md`; move to `built`,
   then `reviewed`, then `merged`. Update "What's left" as it shrinks.
6. When a job clears an open gate, update **Open gates** above and the matching row
   in `.ai/CONTEXT.md`.
