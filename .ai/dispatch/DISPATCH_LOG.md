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
| A9H | Task Harness Workflow Kernel (v1) | `AGENT_9_TASK_HARNESS.md` (+ `_REVIEW`, `_BUILD_REVIEW`) | `feat/task-harness` | **built (backend-only), awaiting operator merge** | v0.5 loop as prompt+artifact discipline: `docs/harness/` templates + DRAFT/REVIEW/CLOSE generators + `dispatch_pipeline.md` runbook. **Level-3 admission gate on the HOT path** — `_harness_level3_allows_autopickup` runs in `_enqueue_task` (choke point every lane shares: Telegram/Web `submit_instruction`, `.task.md`, internal). Blocked ⇒ raises `HarnessAdmissionBlocked` (no faked task_id, no side effect). **T5 (commit 5):** surface catches STRIPPED to backend-only — the T4 control-API 409 + Telegram approval replies were out-of-scope (UX wiring on Telegram while migrating to WebUI); reverted `interface.py`+`control_api.py` to pre-T4. Backend raises the typed signal; surface presentation is a later **WebUI-first** task. Flag-guarded `HARNESS_LEVEL3_GUARD` OFF by default, byte-identical legacy. ZERO new gateway state. 24 guard tests green. B2 corrected: guard is NEW (git-verified), not pre-existing. | **OPERATOR: merge decision only.** B1 fixed (gate on real ingestion path); surface scope corrected (T5). Nothing merged; review commit 5 diff, then `feat/task-harness` → `main` if satisfied. **Next:** WebUI-first surfacing of the blocked signal. Optional: `HARNESS_LEVEL3_GUARD=1` to arm. |
| A10 | M3 Claude Telemetry (#10) | `AGENT_10_M3_CLAUDE_TELEMETRY.md` (+ `_REVIEW`) | commit `c168028` (+ `896990f`) on `main` | **merged** | M3 `ClaudeStreamJsonAdapter` (`src/core/telemetry_adapters/claude_stream_json.py`) + wiring at `ClaudeCodeBackend` public boundary; token semantics `includes_cache`; double-count guard; tool-name/category mapping (no input/content stored); coverage `stream_only`. Went straight to `main`, NOT via a `feat/m3-claude-telemetry` branch (that branch never existed — earlier docs said "ready to merge" in error). | — (complete; live on main) |
| A11 | Fix: mesh affinity routing — session pin ignored at execution (silent local fallback) | `AGENT_11_MESH_AFFINITY_ROUTING.md` | `fix/mesh-affinity-routing` (to cut) | **dispatched — T1 diag pending on kanebra** | Surfaced by the A10 §T1 gateway smoke (`task_6fffd05d`): session created with `machine_id:Horse` (persisted — F4 echoed it) but the turn executed **locally on kanebra** (`llm_invocations.node_id=kanebra`, `execution_node_id` empty) → §T1 gate stays FAILED. Root cause is one of H1 (`MESH_ENABLED` false in gateway process) or H2 (`machine_id` not durably persisted to the DB row `process_task` re-reads); routing was never attempted (no `mesh_routing_failed`). Diagnosis = DB/config/log read on kanebra, **no paid turn**. | **T1 (kanebra, no cost):** read `mesh_tasks`/sessions `machine_id` for the smoke + gateway `MESH_ENABLED` + hostname + log grep → report back. **T2 (build):** fix per H1/H2 + kill the silent fallback (explicit `affinity_unrouted` event). **T3:** re-run §T1 (one turn) → mark A10 §T1 PASSED. |
| FX1 | Fix: SDK `is_error` stored as successful reply ("Prompt is too long") | `FIX_CLAUDE_ISERROR_PROMPT_TOO_LONG.md` | `feat/compact-context` → main | **merged** (a3f734b) | `ClaudeSDKClientDriver` now inspects `ResultMessage.is_error`/`.subtype` and delivers salvaged work + honest failure instead of copying `"Prompt is too long"` into the reply as `success=True`. Root cause: long-lived `claude` process never exits non-zero, so the SDK yields (not raises) the error result. `task_server.py` + timeline refinements. Tests in `test_claude_driver.py`. | Memory `claude-iserror-prompt-too-long`: **Horse redeploy needed** (this restart covers it); #41 context-fill gauge still open. |

---

## Milestone status

**M1/M2 (LLM Turn Observability) — SHIPPED (operator-confirmed 2026-07-03).** Local
Codex smoke + controlled mesh smoke passed 2026-07-02; SQLite benchmarks passed (#8).
The old "#9 gateway-routed mesh smoke" gate is **retired** — the `gateway_node_id`-null
detail was a nice-to-have validation artifact, not a real product blocker, and is no
longer treated as one. **M3 (A10) is already merged on `main`** (commit `c168028`).

**A10 T1 re-attempt (2026-07-03, from Horse) — BLOCKED, gate NOT passed.** The optional
"prove `gateway_node_id` non-null" smoke (packet §T1) was re-attempted from the Horse
worker box. Prereqs confirmed live (worker `online`+registered to kanebra; task-server
`/health` shows `nodes_online=2`, mesh not degraded). DB read confirms the gate is still
open: `llm_turns` has **no** row with a distinct non-null `(gateway_node_id,
execution_node_id)` pair — only `(None,'Horse',146)`, `(None,'smoke-mesh-20260702',1)`,
and `('DESKTOP-3PGTBMF','DESKTOP-3PGTBMF',33)` (same-host, not distinct). Could not close
it: the production submit path (control API `:9003` / Telegram) lives on kanebra and is
**not reachable from Horse** (9003 refuses on the tailnet; only the worker-facing task
server `:9002` is exposed, which per [F1] must not be used). To close, run §T1 steps from
a shell on kanebra (or expose `:9003` on the tailnet). Full detail + handoff in
`AGENT_10_M3_CLAUDE_TELEMETRY.md` Implementation log → T1. This is a validation artifact
only; M3/A10 itself remains merged and unaffected.

**Update (2026-07-03, ran §T1 from kanebra):** the smoke executed (`task_6fffd05d`,
`success`) but **still FAILED the gate** — and surfaced a real routing bug. Session was
created with `machine_id:Horse` (persisted) yet the turn ran **locally on kanebra**
(`llm_invocations.node_id=kanebra`, `execution_node_id` empty). Session-level affinity is
honored but execution routing ignored it → silent local fallback. Tracked as **A11
(`fix/mesh-affinity-routing`)**; §T1 remains open until A11 lands and re-validates. See
`AGENT_11_MESH_AFFINITY_ROUTING.md`.

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
