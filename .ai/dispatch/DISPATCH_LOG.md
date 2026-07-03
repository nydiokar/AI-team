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
| A10 | M3 Claude Telemetry (#10) + #9 gateway-mesh smoke | `AGENT_10_M3_CLAUDE_TELEMETRY.md` (+ `_REVIEW`) | `feat/m3-claude-telemetry` | **built** | M3 `ClaudeStreamJsonAdapter` + `_maybe_emit_telemetry` at `ClaudeCodeBackend` public boundary (SDK + PrintResume paths); token semantics `includes_cache`; double-count guard; tool-name/category mapping (no input/content stored); coverage `stream_only`; 18 tests. Dispatch review corrected the wiring boundary (R1) before build. | **T1 (#9) still open** — gateway-routed mesh Codex smoke needs a live gateway + live worker to record non-null `gateway_node_id`. Manual op step, not a pytest gate. See "Open gates" below. Not yet merged to `main`. |

---

## Open gates (block declaring milestones shipped)

### #9 — Gateway-routed mesh Codex smoke  → blocks marking M1/M2 formally shipped

M3 (A10) is built and tested but **scheduled ahead of the #9 gate**. #9 is the
one recorded blocker to declaring LLM-Turn-Observability M1/M2 shipped.

- **2026-07-02** (branch `validate/llm-turn-observability-m1m2`): local Codex smoke
  **passed** (session `49c5c6d1157f`, turn `task_99bc7bec`, reply `LOCAL_CODEX_SMOKE_OK`);
  graph/diagnostics/timeline APIs verified; SQLite benchmark #8 passed earlier
  (~16k evt/s ingest, ~85 ms query). Controlled worker/controller mesh smoke
  **passed** on temp ports (task `task_mesh_smoke_20260702`, `MESH_CODEX_SMOKE_OK`).
  Privacy scan clean for all sentinels across `llm_%` tables + spool + API JSON.
- **Why #9 is still open:** that mesh smoke **bypassed the gateway submit path**, so
  `gateway_node_id` was null. A gateway-routed attempt on temp port `9014` failed
  before submission.
- **To close:** run one gateway-routed mesh Codex smoke through the production
  controller/gateway path (kanebra + Horse online), verify non-null `gateway_node_id`
  in `llm_turns`. DB verification query is in `AGENT_10_M3_CLAUDE_TELEMETRY.md` §T1.
  Then mark M1/M2 shipped and merge `feat/m3-claude-telemetry`.

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
