# AGENT_42 — F1 live Manager loop (continuous, review-gated, 2 tasks)

**Dispatched:** 2026-07-13
**Level:** 3 (paid, operator-gated, supervised live run)
**Branch (worker):** `feat/f1-context-fill-gauge`
**Flags (verified live):** `MANAGER_ROLE_ENABLED=1`, `MANAGER_TOOLS_ENABLED=1`, `REVIEW_EMITTER_ENABLED=1`, `HARNESS_FLOW_DRIVE=1`; `manager` MCP server in `~/.claude.json`.

## Why
F1 = prove the `review.*` emitter end-to-end live as a byproduct of real work. PR #13 (merged
2026-07-13 08:53Z) granted `record_review` in-loop + instructed the verdict; gateway restarted
09:23Z on that code; live DB holds **zero** `review.*` events. This run makes the first genuine
`review.accepted` land — and demonstrates a **continuous, sequential, review-gated loop** (A41
only dispatched one worker).

## Objective (delivered as the Manager's first assignment turn)
Deliver feature **#41 — the "context-fill gauge"** as **two sequential worker tasks with a real
review gate between them** (one worker per tree; do NOT run both at once).

- **Task 1 (backend):** compute + expose a per-turn/per-session context-fill metric (tokens-in-
  context vs the model's context window) reusing existing usage telemetry (`llm_events` /
  `mesh_tasks` / `backend_usage`), surfaced via an existing read path. Honesty-first: unknown
  window ⇒ `null` + reason, never fabricated. Plain-pytest tests required.
- **Task 2 (frontend, consumes Task 1):** surface context-fill as a small gauge in the Web UI
  (session detail / composer), reading Task 1's field. Read-only display; no new gateway state.

Ground first (git/grep the `is_error` "Prompt is too long" path from FX1 + existing usage
telemetry); if the gap differs from this description or conflicts with spec, surface + wait.
Review each worker's committed diff in git, `record_review` accepted|rework_requested (bounded
findings on rework), only proceed to Task 2 after Task 1 accepted. Close only when both criteria
are reconciled met. One `feat/` branch + PR at close (do NOT merge — escalate merge-to-main).

## Completion criteria
- T1: backend exposes per-turn context-fill via an existing read path, honesty-first, pytest green; reviewed in git + `record_review=accepted`.
- T2: Web UI shows a context-fill gauge consuming T1's field, read-only; reviewed in git + `record_review=accepted`.
- One `feat/f1-context-fill-gauge` branch + PR opened (not merged).

## Bounds / supervision
One Manager + ≤2 sequential workers. Plain `pytest` only (never e2e, never `python main.py status`).
Operator-supervised: dispatcher monitors the Case live via `get_case` / `/api/work` and stops on drift.

## Live log
- **2026-07-13 — F1 loop RAN and PASSED (first genuine `review.accepted` verdicts live).**
- `case_id=d536af369743475bb2b26ad6c7751962` (`case_role=manager`, opened via `/api/manager`).
- **Grounding:** confirmed the gap in git — reactive `is_error` "Prompt is too long"
  (`orchestrator.py:316`/`:4253`, `claude_driver.py:114`) is the only pre-flight signal;
  `telemetry_projection.py` already computes per-turn `context_used_ratio`/`context_window_tokens`/
  `context_remaining_tokens`, but `session_timeline._compact_metrics` stripped them. No spec conflict.
- **Task 1 (backend)** — `task_835909d9`, JOINed the Case. Commit `db4593f`: reuse-first projection
  change (whitelist 3 fields + `context_fill` session summary, honesty-first null+reason), 5/5 pytest.
  Reviewed adversarially in git (verified `turns[0]` == latest via `list_turns ... DESC`).
  → `record_review=accepted`.
- **Task 2 (frontend)** — `task_f076ba59`, JOINed the Case (dispatched only after T1 accepted).
  Commit `0f35efe`: read-only `ContextFillGauge` above the Composer consuming T1's `context_fill`
  (rawApi→adapter→`useSessionActivity`), honesty-first "ctx —" when unknown. 3 vitest + `tsc -b` clean.
  → `record_review=accepted`.
- **Two `review.accepted` events landed on the Case ledger** (M3.2 emitter proven live end-to-end).
- One branch `feat/f1-context-fill-gauge` (8 files, +258/−2). **PR opened, NOT merged**
  (merge-to-main is an operator fork — escalated). Case closed with both criteria reconciled met.
