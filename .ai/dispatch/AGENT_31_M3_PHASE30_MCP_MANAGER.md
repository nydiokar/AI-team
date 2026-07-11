# A31 — M3 Phase 3.0: `mcp_manager` tool surface (F4 spike, part 1)

**Level:** 3 (code) · **Branch:** `feat/m3-phase30-mcp-manager` · **Date:** 2026-07-09
**Reads with:** `docs/M3_MANAGER_INVOCATION_SPEC.md` (§2.2 build surface, §6 phasing),
`scripts/mcp_jobs.py` (the pattern this copies), `.ai/CONTEXT.md` Current Focus.

---

## Objective-lock (bounded — not overstated)

Build **the one genuinely new piece** M3 Phase 3.0 needs: a Manager MCP tool surface
(`scripts/mcp_manager.py`) exposing the two minimal tools from spec §6 —
`dispatch_worker` + `wait_for_worker` — so a gateway-spawned Manager session can start a
worker task and block on its outcome **without holding a worker task slot**. Ship it with
hermetic unit tests. **Explicitly out of scope for this dispatch** (see "Not done"): the
live paid F4 spike, the session wiring, and the lineage-endpoint extension.

This dispatch does **not** claim Phase 3.0 is *proven* — it delivers the code artifact the
proof runs against, and pins down exactly what remains.

---

## What shipped (this dispatch)

- **`scripts/mcp_manager.py`** — stdio JSON-RPC MCP server, modeled byte-for-pattern on
  `scripts/mcp_jobs.py` (`.env` bootstrap, bearer-token urllib, identical `_dispatch`/
  `main` protocol shape). Two tools:
  - `dispatch_worker(objective, session_id?, cwd?, files?, parent_flow_run_id?)` →
    `POST /api/instructions`. Returns the worker `task_id`. Thin wrapper over the existing
    auth-guarded, Level-3-gated endpoint — **no new dispatch path**.
  - `wait_for_worker(task_id? | flow_run_id?, timeout?, poll_interval?)` → resolves
    task→flow via `GET /api/flows?task_id=`, long-polls `GET /api/flows/{id}`, returns when
    the flow hits a **done** (`closed/completed/failed/cancelled/…`) or **attention**
    (`blocked/review/needs_decision/…`) status, or on timeout. **Read-only poll ⇒ holds no
    task slot** (the §6 anti-starvation property, at the tool level).
  - Talks to the **control API** (`127.0.0.1:9003`, `DASHBOARD_TOKEN`→`WORKER_TOKEN`
    fallback), *not* the `:9002` task server `mcp_jobs` uses. `DASHBOARD_URL` overridable.
- **`tests/test_mcp_manager.py`** — 19 hermetic tests (no network / no paid CLI; single
  HTTP choke point monkeypatched, `.env` bootstrap neutralised). Covers validation,
  terminal-status classification, POST payload shape, the parent-lineage-not-sent
  invariant, the timeout/no-busy-loop path, and the full MCP JSON-RPC surface
  (initialize / tools/list / tools/call success / soft-error / unknown-tool). **19 passed.**

Run: `.venv/bin/python -m pytest tests/test_mcp_manager.py -q` (plain pytest — cost-guard clean).

## Adversarial review (self, pre-commit)

| Challenge | Resolution |
|---|---|
| Would `parent_flow_run_id` silently no-op and fake lineage? | **Yes it would** — `InstructionBody` is strict and drops unknown fields. So it is deliberately NOT sent; surfaced in the reply as "NOT yet persisted." Test locks this. |
| Busy-loop / CPU peg on a never-terminating flow? | Deadline-bounded; sleeps `min(poll_interval, remaining)`; terminates on the deadline check. Test asserts sleeps occur + TIMEOUT returned. |
| Token leak in tool replies / logs? | Token only in the `Authorization` header; never printed. Error strings carry host:port + server body, not the token. |
| Import reads real `.env` in tests? | Tests set `AI_TEAM_ENV_FILE` to a nonexistent path before import ⇒ no secret load. Config is read at *request* time, not import. |
| Blast on the live gateway? | None — `mcp_manager.py` is a standalone script, not imported by the app; the gateway is untouched until it is wired (see below). |

---

## NOT done — the next agent MUST pick these up (nothing here is silently skipped)

1. **🔴 LINEAGE ENDPOINT GAP (blocks the §6 success criterion).** `POST /api/instructions`
   (`InstructionBody` @ `src/control/control_api.py:88`, handler `:883`) does **not** accept
   or forward `parent_flow_run_id` / `dispatched_by` / `dispatch_file`. `submit_instruction`
   (`src/orchestrator.py:2049`) stamps lineage from a **`parent_task` object**, not loose ids,
   so there is no HTTP path today to record a Manager→worker edge. Until this is extended, a
   child dispatched via `dispatch_worker` will **not** appear with a parent edge in
   `/api/flows` — so the §6 clause *"parent flow → child flow visible in /api/flows with
   lineage"* is **not yet met**. Minimal fix: add optional `parent_flow_run_id` to
   `InstructionBody`, thread it to `submit_instruction`, and stamp it onto the child
   `flow_runs` row behind `HARNESS_FLOW_DRIVE` (reuse the existing lineage-stamp supplier).
   Keep it flag-gated/additive.

2. **🟡 LIVE F4 SPIKE NOT RUN (cost guard — operator/next-agent, on a live gateway).** The
   actual proof — a gateway-spawned Claude session, given `mcp_manager`, dispatches a *child*
   worker session and receives its terminal result **without starving the task loop** — was
   deliberately NOT executed here (it spawns paid Claude CLI sessions; project scar #1). To
   run it: (a) wire the tool (item 3), (b) spawn a manager session, (c) have it call
   `dispatch_worker` then `wait_for_worker`, (d) confirm the child runs in its own slot while
   the parent waits (watch `/api/work` / `curl :9003 …/health` — **never** `python main.py status`).

3. **🟡 TOOL NOT WIRED INTO SESSIONS (global-config + live-gateway change).** Following the
   `mcp_jobs` precedent this needs: (a) add a `"manager"` server to `~/.claude.json`
   `mcpServers`; (b) add an `_mcp_manager_configured()` gate + append `mcp__manager__dispatch_worker`
   / `mcp__manager__wait_for_worker` to `allowed_tools` in `src/backends/claude_driver.py`
   (mirrors `_mcp_jobs_configured()` @ `:310`, tool-append @ `:392`/`:974`); (c) ensure the
   spawned session's env carries `DASHBOARD_TOKEN` + `SESSION_ID`. Left undone because it
   mutates a global file and the live backend path — belongs to the Phase 3.1 role-wiring
   dispatch (or an operator-approved wiring step), not this tool-build.

4. **🟢 `wait_for_worker` returns status, not the child's diff/output.** By design it points
   the Manager at git (review the committed diff, don't trust a self-report). A
   `get_worker_result` / `get_case` tool (spec §2.2) is Phase 3.1+.

5. **🟢 OPEN QUESTION for the spike:** does a *plain* worker dispatch (not itself a harness
   loop) actually transition its `flow_runs.status` to a terminal value on completion, with
   `HARNESS_FLOW_DRIVE` on? If not, `wait_for_worker`'s flow-status polling won't see "done"
   and should fall back to a task-level signal (`/api/work/{id}` or task status). Validate
   during the live spike; adjust terminal detection if needed.

6. **🟢 Minor:** `wait_for_worker` aborts the wait on an HTTP error mid-poll (fail-fast)
   rather than tolerating a transient gateway blip. Fine for a spike; add retry tolerance if
   long waits prove flaky.

---

## Closure

**Status: `built` (branch `feat/m3-phase30-mcp-manager`).** Delivers the Phase 3.0 tool
surface + tests; the milestone is **not closed** — items 1 (lineage endpoint) and 2 (live
spike) gate acceptance. Per branch policy this is a code loop → open a PR at close
(`gh pr create`) once items above are triaged. Recommended next dispatch: **A32** = the
lineage-endpoint extension (item 1, smallest unblock), then the live spike (item 2).
