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
