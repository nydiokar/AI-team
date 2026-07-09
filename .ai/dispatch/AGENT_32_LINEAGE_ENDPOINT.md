# A32 — M3 Phase 3.0: lineage endpoint extension (`parent_flow_run_id` on `/api/instructions`)

**Level:** 3 (code) · **Branch:** `feat/m3-phase30-mcp-manager` (continues A31) · **Date:** 2026-07-10
**Reads with:** `AGENT_31_M3_PHASE30_MCP_MANAGER.md` §"NOT done" item 1 (the 🔴 blocker),
`docs/M3_MANAGER_INVOCATION_SPEC.md` §6, `tests/test_dispatch_lineage.py` (the M2 wiring it reuses).

---

## Objective-lock (bounded)

Close the **🔴 lineage-endpoint gap** A31 flagged: `POST /api/instructions` had no HTTP
path to record a Manager→worker edge, so §6's *"child flow visible in `/api/flows` with
lineage"* was unmet. Add an **optional** `parent_flow_run_id` to the endpoint and thread it
to the child's `flow_runs` row through the **existing** M2 stamp/record machinery. No new
schema, no new dispatch path, no new stamping hook.

Out of scope (unchanged from A31): the live paid F4 spike (item 2) and session wiring
(item 3). A33 consumes this from `mcp_manager`.

---

## What shipped (this dispatch)

- **`src/control/control_api.py`** — `InstructionBody` gains `parent_flow_run_id: Optional[str]
  = None`; both `submit_instruction` call sites (session + one-off) thread `body.parent_flow_run_id`.
- **`src/orchestrator.py`**
  - `submit_instruction(...)` gains `parent_flow_run_id: Optional[str] = None`, included in the
    stamp-trigger condition and passed to the stamp helper.
  - `_stamp_child_dispatch_lineage(...)` gains a keyword-only `parent_flow_run_id`. An explicit
    **loose** id (the HTTP seam, where there is no in-process parent `Task`) is honored; when a
    `parent_task` is also given, the explicit id **wins**, else it derives from the parent's
    metadata exactly as before. Everything downstream (`_dispatch_lineage_fields` →
    `create_flow_run` → the authoritative `flow_links(child_flow)` edge on the parent) is A26/A26a
    code — **reused, not duplicated**.
- **Tests** — `tests/test_dispatch_lineage.py` +3 (loose-id records edge & reverse-lookup;
  explicit-wins-over-parent_task; OFF-path no-op). `tests/test_control_api_write.py` +1 threading
  assertion + one-off asserts `None` is threaded when absent. **38 passed** across both files.

Run: `.venv/bin/python -m pytest tests/test_control_api_write.py tests/test_dispatch_lineage.py -q`

## Double-gated safety (why this is byte-identical when unused)

1. **Field absent / None** (every normal Telegram & Web request) ⇒ stamp-trigger is false ⇒
   nothing runs. Locked by `test_instruction_one_off` (`parent_flow_run_ids[-1] is None`).
2. **`HARNESS_FLOW_DRIVE` OFF** ⇒ `_stamp_child_dispatch_lineage` returns before touching the
   child ⇒ metadata untouched. Locked by `test_flag_off_loose_parent_flow_run_id_is_noop`.

RECORD only — nothing reads `parent_flow_run_id` to drive execution.

## Adversarial review (self, pre-commit)

| Challenge | Resolution |
|---|---|
| A Manager passes a **bogus** `parent_flow_run_id` — does it break the child dispatch? | No. `flow_runs.parent_flow_run_id` is a plain `TEXT` column (migration 22 `ALTER TABLE ADD COLUMN`; SQLite attaches no FK that way) ⇒ no FK violation. And `_record_flow_run_start` + `_record_flow_link` are best-effort wrapped, isolated from `_enqueue_task`. Worst case: an orphan convenience-index value + a link on a nonexistent parent — SHADOW noise, the task still runs. |
| Can an authed caller **forge** a false lineage edge? | Only in the SHADOW graph, and only when the flag is ON. The endpoint is already auth-guarded (loopback/tailnet, `DASHBOARD_TOKEN`); such a caller can already dispatch tasks. Nothing reads the edge to drive execution ⇒ low blast. |
| Byte-identical for the live gateway? | Yes — additive optional field, default-off behavior, no signature break (all new params defaulted). Import smoke + 38 tests green; live gateway untouched (not restarted). |
| Idempotency interaction? | Unchanged — the idem guard keys on the `Idempotency-Key` header, not the body; not a regression. |

---

## NOT done (unchanged — for the live-spike agent/operator)

- **🟡 Live F4 spike** (A31 item 2) and **🟡 session wiring** (`~/.claude.json` + `claude_driver`
  allowed-tools, A31 item 3) — still deliberately unrun (paid CLI + global/live change).
- **🟢 Open question** (A31 item 5): does a *plain* worker dispatch transition its
  `flow_runs.status` to a terminal value so `wait_for_worker`'s poll sees "done"? Validate live.

---

## Closure

**Status: `built` (branch `feat/m3-phase30-mcp-manager`).** The 🔴 endpoint blocker is closed:
there is now a flag-gated, additive HTTP path to record a Manager→worker lineage edge, reusing
the A26/A26a substrate. Phase 3.0 acceptance still gates on the live spike (A31 item 2). Next:
**A33** wires `mcp_manager.dispatch_worker` to send it.
