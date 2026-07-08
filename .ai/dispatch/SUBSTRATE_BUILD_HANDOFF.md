# Work Control Substrate — Build Handoff (read this, then continue)

**Date:** 2026-07-08 · **Branch:** `feat/work-control-substrate` · **Author:** builder agent
**For:** the next agent (continue A28 → A29) and the operator (merge + flag decision).

This is the "cold-boot and continue" doc. It captures exactly what is built, how to
verify it, the one live-demo decision that needs the operator, and what remains.

---

## 1. What this branch contains (in order)

Cut from `main` (`bf98d72` + `4b9a483`, the reconciled Work Control Substrate plan), linear:

| Commit | Job | Summary |
|---|---|---|
| `3caf03a` | **A26a** | M2 direct lineage: `flow_runs.parent_flow_run_id/dispatched_by/dispatch_file` wiring + `_stamp_child_dispatch_lineage` supplier + `list_child_flow_runs`. Flag-gated, 19 tests. (Was `feat/m2-dispatch-lineage-wiring`; folded here so it lands ONCE — see `4b9a483`.) |
| `e546690` | **A25** | Migration 23: `flow_links` (authoritative case↔entity ledger, idempotent) + `flow_events` (append-only audit) + defensive `mesh_tasks/approvals.flow_run_id` columns + DB helpers + vocab constants. 9 tests. |
| `5faf0b9` | **A26** | Write path: `flow.created`+`root_task` link at flow creation; `child_flow` link + `task.dispatched` event on the PARENT (consumes A26a's edge, no second stamping hook); `flow.stage_changed` events. Best-effort/isolated. 5 tests + regressions. |
| `0e6f22a` | **A27** | Read-only Work API: pure `work_read_model.py` builder + `GET /api/work`, `/api/work/{id}`, `/api/work/{id}/timeline`, `/api/work/{id}/graph`. 20 tests. |

**Design invariants held across all four (verify before extending):**
- **ADDITIVE + REVERSIBLE**, no destructive migration (migration 23 is idempotent).
- **Default-OFF byte-identical:** every substrate WRITE is gated behind `HARNESS_FLOW_DRIVE`
  (default OFF). OFF ⇒ zero flow_links/flow_events writes, identical to A19/A22.
- **SHADOW:** nothing reads the new columns/tables to DRIVE execution (grep-verified).
- **Best-effort/isolated writes:** `_record_flow_link`/`_record_flow_event` mirror
  `_record_flow_stage` — a DB failure logs and returns, NEVER raises into task execution.

---

## 2. How to verify (no paid CLI, no gateway restart)

```bash
# venv pytest lives in the main tree; run against this worktree:
env -C /tmp/wcs-wt /home/cifran/dev/AI-team/.venv/bin/python -m pytest \
  tests/test_dispatch_lineage.py tests/test_flow_links_events.py \
  tests/test_flow_link_write_path.py tests/test_work_read_model.py \
  tests/test_control_api_work.py tests/test_flow_runs.py \
  tests/test_flow_schema_extension.py tests/test_flow_stage_transitions.py \
  tests/test_control_api_flows.py tests/test_control_api.py -q
# → 146 passed (last run 2026-07-08)
```
Live gateway check (NEVER `python main.py status` — it kills the live PM2 gateway):
`curl -s http://127.0.0.1:9003/health` → `{"status":"ok"}`.

One pre-existing, UNRELATED failure exists on `main` too:
`test_push_notifications.py::test_status_reports_unavailable_without_vapid` (this box has
VAPID configured → the test's "no VAPID" assumption is false). Not caused by this work.

---

## 3. ⚠️ Operator decision — the ONE thing needed for a LIVE demo

The substrate only POPULATES when **`HARNESS_FLOW_DRIVE`** is ON (default OFF). The read
API (`/api/work`) works either way but returns **empty** until data exists. So:

- **To see real Work/Case data live**, set `HARNESS_FLOW_DRIVE=on` in the gateway env and
  **restart the gateway** (PM2). This activates A22 stage writes + A26a lineage + A26
  links/events together — all shadow, none drive execution.
- I did **not** flip the default or touch env vars (mobile operator; a restart drops the
  active session). This is your call. All behavior is proven in tests with the flag both
  ON (monkeypatched) and OFF, so the code is correct regardless.
- **Restart caveat:** restarting the gateway to pick up the flag (or to serve `/api/work`
  for A28) will END the current agent session. Prepare/commit first, then restart, then
  cold-boot a fresh agent pointed at THIS doc.

No other blockers. Nothing else needs a decision to proceed with A28/A29.

---

## 4. Merge guidance

- **Merge `feat/work-control-substrate`** (contains A26a+A25+A26+A27). Then **retire
  `feat/m2-dispatch-lineage-wiring`** — it is SUBSUMED here (its code is `3caf03a`); do
  NOT merge it separately or A26a lands twice (per `main` commit `4b9a483`).
- Docs on `main` are owned by the manager agent; this branch only updated the per-job
  packet `## Milestone`/`## Closure` sections (A25/A26/A27). The manager advances
  `DISPATCH_LOG`/`CONTEXT` at merge.

---

## 5. What remains — A28, then A29

### A28 — Mobile Work surface (`.ai/dispatch/AGENT_28_MOBILE_WORK_SURFACE.md`)
Read-only Work tab in `web/` driven ONLY by the A27 API. Operations-inbox, not an editor.
- Consume `/api/work` (buckets: needs_decision/blocked/review/active/closed/unknown),
  `/api/work/{id}` (ledger + parent/children + coverage), `/api/work/{id}/timeline`,
  `/api/work/{id}/graph` (compact vertical lineage tree).
- Session affiliation labels (Standalone / Manager|Worker|Reviewer|Evidence for Case).
- No creation, no workflow editor, no mutation. Missing data renders as `unknown`/`unlinked`.
- **Needs real browser/device verification + the gateway serving `/api/work`** (restart, §3).

### A29 — Hardening & closure (`.ai/dispatch/AGENT_29_WORK_SUBSTRATE_HARDENING.md`)
Integrated adversarial review, stale/conflict/no-heuristic fixtures, docs, acceptance that
the UI infers nothing from prose. Also fold in the **A26 deferred seams** (see A26 Closure):
approval links/events, session-role links, and terminal-task OUTCOME events (`review.*`,
`flow.closed`) — all additive using the same `_record_flow_link/_record_flow_event` helpers.

---

## 6. Key files touched
- `src/control/db.py` — migration 23, `_ensure_substrate_columns`, flow_links/flow_events
  helpers + vocab constants; (A26a) `list_child_flow_runs`.
- `src/orchestrator.py` — (A26a) lineage supplier/columns; (A26) `_record_flow_link`,
  `_record_flow_event`, wiring in `_record_flow_run_start` + `_flow_stage_transition`.
- `src/control/work_read_model.py` — NEW pure builder.
- `src/control/control_api.py` — 4 read-only `/api/work*` routes.
- `tests/test_{dispatch_lineage,flow_links_events,flow_link_write_path,work_read_model,control_api_work}.py`
  — new; version bumps in `test_flow_runs.py` / `test_flow_schema_extension.py` (22→23).
