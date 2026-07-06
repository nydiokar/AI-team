# Dispatch Log — the dispatch index

**This is an INDEX.** Full state per job lives in its `AGENT_N_*.md` (packet +
`## Milestone` burndown + `## Closure`, one living file). If a row needs a paragraph,
it's in the wrong file — move the detail into the dispatch doc.

Role boundaries: [`.ai/DOC_MAP.md`](../DOC_MAP.md). Forward priorities + "what's next"
live in [`.ai/CONTEXT.md`](../CONTEXT.md) (Current Focus + Priorities), **not here**.

**Status vocabulary:** `dispatched` (packet written, not built) · `built` (code on a
branch) · `reviewed` (build-review folded) · `merged` (on `main`) · `blocked` (waiting
on a gate) · `deferred` (parked on purpose). Add `— op-merge` when the only thing left
is an operator merge decision.

---

## Index

| # | Dispatch | Date | Level | Status | One-line |
|---|---|---|---|---|---|
| A8 | `AGENT_8_OPERATOR_SIGNAL.md` | 2026-07-03 | 2 | merged (PR #5) | Web Push (#21) + Backend Usage (#30/#33); VAPID env setup done by operator. |
| A9 | `AGENT_9_COMPACT_CONTEXT.md` | 2026-07-03 | 2 | merged (PR #6) | Wired `load_compact_context` into opt-in `continues:` path; fence-hardened. |
| A9H | `AGENT_9_TASK_HARNESS.md` | 2026-07-03 | 3 | merged (PR #8, `fd90a46`) | Harness v1 (`docs/harness/`) + Level-3 admission gate on `_enqueue_task`, flag-guarded, zero new state. |
| A10 | `AGENT_10_M3_CLAUDE_TELEMETRY.md` | 2026-07-03 | 2 | merged (`c168028`) | M3 Claude stream-json telemetry adapter; #9 gateway-routed smoke CLOSED, T2 verified live. |
| A11 | `AGENT_11_MESH_AFFINITY_ROUTING.md` | 2026-07-03 | 2 | merged | Fix: session pin honored at execution — no silent local fallback; `affinity_unrouted` guard; T3 re-validated. |
| A12 | `AGENT_12_HARNESS_SELFTEST.md` | 2026-07-03 | 2 | merged (PR #8) | Ran the §14 loop by hand on the pipeline doc; two-lane banner + copyable example; friction verdict: Phase 2 NOT justified. |
| A13 | `AGENT_13_LOOP_CONFIG_MAP.md` | 2026-07-03 | 2 | merged | `docs/harness/loop_config_map.md`: node table + 11 dials + Manager spec + failure-localization; docs-only. |
| A14 | `AGENT_14_DOC_STRUCTURE_CONTRACT.md` | 2026-07-03 | 2 | merged | Doc-role contract (`DOC_MAP.md`), slimmed this index, restored CONTEXT Current Focus, one-dispatch-one-file rule wired into harness templates. |
| A15 | `AGENT_15_HARNESS_PROMOTION_LADDER.md` | 2026-07-04 | 2 | merged | Evidence-gated v0.5→v0.4 promotion ladder + `manager_invocation.md` driver; 3 of 6 v0.4 elements are drop-candidates. First loop via the driver. |
| A16 | `AGENT_16_HARNESS_BLOCK_SURFACE.md` | 2026-07-04 | 2 | built — op-merge | WebUI-first surfacing of the Level-3 admission block (A9H "Next"): `HarnessAdmissionBlocked` → clean 409 + `mark_idle` session revert + Composer approval-needed copy. Gate untouched, guard OFF ⇒ byte-identical. |
| FX1 | `FIX_CLAUDE_ISERROR_PROMPT_TOO_LONG.md` | 2026-07-03 | — | merged (`a3f734b`) | SDK `is_error` no longer stored as a successful "Prompt is too long" reply; open: #41 context-fill gauge. |
| A18 | `AGENT_18_ORIENTATION_PAGE.md` | 2026-07-04 | 2 | built — op-merge | Static `docs/OVERVIEW.md` newcomer front-door: what-it-is + ASCII shape thumbnail + router table to owning docs. Links, never restates (DOC_MAP anti-overlap). No renderer/mkdocs — v0.4 §2.3 need, not the deferred tooling. |
| A17 | `AGENT_17_WIP_MERGE_RECONCILE.md` | 2026-07-05 | 2 | reviewed | Audited `d1556ad`: 4 A16 + 9 orphan + 4 doc; A16 verified on main; 4 orphan clusters assessed (activity-forwarder live/untested, backend-usage & mesh-fleet fix real bugs, opus-default flip). No P0/P1. Keep-vs-revert per cluster = Level-3 fork (op approval). |
| A17b | `AGENT_17_BACKEND_USAGE_AGGREGATION_TESTS.md` | 2026-07-05 | 2 | merged | First real-*code* harness loop. Regression-locks backend-usage sum-vs-peak token aggregation (the "166M tok" fix); test-only +112; mutation-verified. (Renumbered A17→A17b: number collided with the WIP-reconcile dispatch.) |
| A18b | `AGENT_18_WORKER_AFFINITY_FALLBACK.md` | 2026-07-05 | 2 | merged (`739e3e4`) | Option A shipped behind `MESH_AFFINITY_OFFLINE_GRACE_SEC` (default **0 ⇒ byte-identical A11**): offline pinned node holds in `PAUSED_PINNED_NODE_OFFLINE` + polls liveness, resolves to dispatch if the node returns, else honest resumable `PINNED_NODE_OFFLINE` (not bare ERROR). Two-class rule in CONTEXT; three affinity guards (claim filter + local-loop assert + fail-closed dispatch-site check); restart-recovery of a mid-hold session; 8 tests green. **Feature OFF by default** — operator sets grace>0 to activate + optional T-final + redeploy. (Renumbered A18→A18b in-index: number collided with the A18 orientation-page dispatch.) |
| A19 | `AGENT_19_FLOW_RUNS_RECORD.md` | 2026-07-05 | 3 | merged (`0b6b1ec`) | **First CODE loop.** `flow_runs` record (v0.4 §13 item 1): migration 21 + create/update/list + guarded orchestrator hook + pytest (39 passed). Manager review: 0 P0/P1, F1–F5 held, additive/revertible. **OPERATOR OVERRIDE of COLD ladder Row 1** (trigger NOT observed) — recorded in `promotion_ladder.md`; RECORD not stage-machine. |
| A20 | `AGENT_20_RECONCILE_BASE.md` | 2026-07-06 | 1 | merged | **v0.6 M0.** Docs-only base reconcile: fix stale A19 status, surface the 2 forks (A17 orphan code; quota branch w/ VERIFIED state — 9 ahead/2 behind, additive, NOT "293-file destructive"), reconcile `manager_invocation.md` Rule-2 vs v0.6, add v0.6 pointer. Prerequisite for M1. |
| A21 | `AGENT_21_FLOW_SCHEMA_EXTENSION.md` | 2026-07-06 | 3 | dispatched | **v0.6 M1.** Additive migration: 5-col `flow_runs` → full §11 field set + lineage cols (`parent_flow_run_id`/`dispatched_by`, so M2 is wiring-only). Extend create/update; stage-vocab constant. NULLable, idempotent, byte-identical when unused. Depends on A20. |
| A22 | `AGENT_22_FLOW_STAGE_TRANSITIONS.md` | 2026-07-06 | 3 | dispatched | **v0.6 M1.** Write `current_stage` at each loop transition behind `HARNESS_FLOW_DRIVE` (default OFF ⇒ byte-identical A19). SHADOW only — nothing reads stage to drive execution; writes best-effort/isolated. Depends on A21. |
| A23 | `AGENT_23_FLOW_READ_API.md` | 2026-07-06 | 2 | dispatched | **v0.6 M1.** Read-only `GET /api/flows` + `/api/flows/{id}` on the loopback/tailnet control API — first payoff: query flow state, not grep. No mutation endpoints, no public bind. Depends on A21; can run parallel to A22. |

> **Note — Set A superseded (number collision, 2026-07-06).** An earlier batch reused the
> A20–A22 numbers (`SUPERSEDED_AGENT_20_FLOW_STATE_SCHEMA` / `_21_FLOW_STAGE_INSTRUMENTATION` /
> `_22_FLOW_TRACE_SURFACE`). It was **superseded by the A20–A23 rows above** (the v0.6 M0/M1
> batch) and moved to [`deferred/`](deferred/) to free the numbers. Those files are historical
> only — do not dispatch them; the live v0.6 packets are `AGENT_20_RECONCILE_BASE.md` …
> `AGENT_23_FLOW_READ_API.md`.

---

## How to add a dispatch (manager/dispatcher agent)

1. Pull the next unblocked item from **Current Priorities** in `.ai/CONTEXT.md`.
2. Write the packet `AGENT_N_<THEME>.md` here (house style: `AGENT_8_OPERATOR_SIGNAL.md`).
   Grow ONE file through its life — packet, then a `## Milestone` burndown section, then
   a `## Closure` section. **No `.milestone.md` / `.closure.md` siblings** (see
   [`docs/harness/dispatch_pipeline.md`](../../docs/harness/dispatch_pipeline.md)).
   Materially-important reference artifacts (maps, specs) go in `docs/`, never here.
3. Append a one-line row above as `dispatched`; advance it through the status vocabulary.
4. Keep the row to ONE line — all detail stays in the dispatch doc.
5. When a job clears a gate or ships, update the matching entry in `.ai/CONTEXT.md`
   (Current Focus / Priorities / Shipped Ledger).
