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
| A10 | M3 Claude Telemetry (#10) | `AGENT_10_M3_CLAUDE_TELEMETRY.md` (+ `_REVIEW`) | commit `c168028` (+ `896990f`) on `main` | **merged, T1 gate #9 closed, T2 verified live** | M3 `ClaudeStreamJsonAdapter` merged on `main`. **T1 (#9 gateway-routed smoke): CLOSED 2026-07-03** — `task_35655be9`, `gateway_node_id=kanebra`/`execution_node_id=Horse` distinct+non-null, privacy scan clean. **T2: verified live** on the real worker-agent/SDK-driver path (`task_bfe8c90b`, `task_f89edffb`) — real turns now produce `model.request.usage`. Unit suite had been vacuously green (gitignored fixtures never committed, tests asserted a nonexistent `send_batch` method); fixed, 18/18 genuinely pass. | — (complete) |
| A11 | Fix: mesh affinity routing — session pin ignored at execution (silent local fallback) | `AGENT_11_MESH_AFFINITY_ROUTING.md` | `feat/task-harness` (operator: no new branch) | **merged, T3 re-validated** | Fix (2 parts): (1) `SessionStore.create()` accepts `machine_id` → atomic pin, no local-host window; (2) hard affinity guard in `process_task` — refuse local execution of a remote-pinned session, emit `event=affinity_unrouted`, force remote when mesh on / honest-fail when mesh off. 54 tests passed. **T3 re-validation: PASSED 2026-07-03** post-redeploy — `task_35655be9`, `gateway_node_id=kanebra`/`execution_node_id=Horse` distinct, no `affinity_unrouted` in gateway logs. | — (complete) |
| A12 | Harness self-test — run one real task through the loop | `AGENT_12_HARNESS_SELFTEST.md` | `feat/task-harness` | **built — awaiting operator read of friction report** | Ran the §14 loop by hand on `docs/harness/dispatch_pipeline.md`: sharpened the two-lane scope banner (`.task.md` batch vs live `submit_instruction`) and replaced the terse worked example with a copyable all-7-stage example (real filled packet + closed milestone + 2 F-tags + closure). Docs-only; all cross-refs verified. HARNESS FRICTION REPORT appended to the packet with the Phase-2 Y/N verdict. | **OPERATOR: read the friction report** in `AGENT_12_HARNESS_SELFTEST.md`, then decide merge. No code, no gateway state. |
| A13 | Loop Configuration Map — make the harness control surface legible ("fake node graph") | `AGENT_13_LOOP_CONFIG_MAP.md` | `feat/harness-config-map` | **reviewed — awaiting operator merge** | Shipped `docs/harness/loop_config_map.md`: 8-row node table (0=level-select..7=close, driver/programmed-by/IO-contract/quality-dials each cited), 11 enumerated "temperature" dials (all cited to real source lines, cost↔quality direction each), Manager-vs-Executor separation + **Manager behavior spec** as a headed in-file section (gap confirmed real — Manager per-node driving behavior was undocumented), and a 12-row failure→node→dial localization table (≥1 row per dial). README cross-linked; all 10 cross-refs resolve. **Finding:** no provider/model "temperature" dial exists inside the loop — the loop's temperature is entirely prompt/artifact discipline; §9 sampling-temp is onboarding-only; cheap-DRAFT/strong-REVIEW is a stated preference, not a wired dial (Phase-2 promotion, not built). Docs-only; ZERO machinery. Dogfooded as a Level-2 run (milestone + closure produced). | **Manager review DONE (verdict: accept):** verified in git — docs-only diff `ff7b6f3`, no `src/` touched; `Task_Harness_v0.4.md` confirmed genuinely-untracked-on-main (not smuggled); dials spot-checked against real source lines (not fabricated); one small Mermaid illustration only, zero machinery/runtime-config; all 10 cross-refs resolve; failure-localization table ≥1 row/dial verified. **OPERATOR: merge decision** — `feat/harness-config-map` → `main` if satisfied. HOLD on branch (no push done). |
| A15 | Harness promotion ladder — evidence-gated v0.5→v0.4 roadmap (first loop via `manager_invocation.md`) | `AGENT_15_HARNESS_PROMOTION_LADDER.md` | `feat/harness-ladder` | **reviewed** | Shipped `docs/harness/promotion_ladder.md`: 6-row evidence-gated table (one row per v0.4→v0.5 deferred element, each citing real v0.4 §/v0.5 §), each with a falsifiable observed-evidence promotion trigger + cheaper interim move. **Finding:** only 2 of 6 have a writable trigger (flow-state #1, model-routing #2) and both stay COLD — three loops A12/A13/A14 ran with zero lost handoff state; the other 3 (live-tailing reviewer, async memory compression, per-task provider smoke) are **drop candidates — no realistic trigger** (structurally unsafe / superseded / cost-guard-forbidden). Consistency-checked vs A12/A13/A14; A12's concurrency caveat flagged loudly as NOT a Row-1 trip. README + loop_config_map cross-linked (model-routing promotion governed by ladder); all cross-refs resolve. Docs-only, ZERO machinery. One-file rule dogfooded (packet+milestone+closure in one file). | **OPERATOR: merge decision** — `feat/harness-ladder` → `main` if satisfied. HOLD on branch (no push). Also carries `docs/harness/manager_invocation.md` (the driver this loop was the first to run). |
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

**Final update (2026-07-03, post-A11 redeploy): §T1 / #9 CLOSED.** Re-ran the smoke from
kanebra — `task_35655be9`, `gateway_node_id=kanebra`, `execution_node_id=Horse`, distinct
and non-null, `llm_invocations.node_id=[kanebra,Horse]`, no `affinity_unrouted`, privacy
scan clean. A11 fix confirmed working. M3 (A10 T2) also verified live on the real
worker-agent path, not just fixtures — see A10 row above.

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
