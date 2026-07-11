# A34 — M3 Phase 3.0/3.1: Manager-tool session wiring (double-gated, reversible)

**Level:** 3 (code) · **Branch:** `feat/m3-phase30-mcp-manager` (continues A31–A33) · **Date:** 2026-07-10
**Reads with:** `AGENT_31_M3_PHASE30_MCP_MANAGER.md` §"NOT done" item 3 (the wiring),
`docs/M3_MANAGER_INVOCATION_SPEC.md` §2.2 item 2 / §6 Phase 3.1, `scripts/mcp_jobs.py` +
`scripts/setup_mcp.py` (the precedent this mirrors).

---

## Objective-lock (bounded)

Resolve A31 item 3 — *the Manager tools were never wired into a session* — **in code, without
touching the live global config or restarting the production gateway**. Make
`mcp__manager__dispatch_worker` / `wait_for_worker` grantable to a Claude session via the
same MCP-tool-to-session pattern `mcp_jobs` uses, but **double-gated** so the default is
byte-identical. Ship the operator's one-command registration path + docs. This is the last
*code* step before the live F4 spike (A35, operator-gated).

Out of scope: the live paid spike itself (A35), a dedicated manager-role session type +
role-prompt boot (Phase 3.1 proper — noted below), the `SESSION_ID` env enrichment (3.1).

---

## What shipped (this dispatch)

- **`src/backends/claude_driver.py`**
  - `_mcp_manager_configured()` — mirrors `_mcp_jobs_configured()`; True iff a `manager`
    server exists in `~/.claude.json`.
  - `_manager_tools_enabled()` — a **second, independent** gate reading the
    `MANAGER_TOOLS_ENABLED` env flag (default OFF). `dispatch_worker` is a *dispatch*
    primitive (materially more powerful than jobs' read-only `watch_job`), so its grant gets
    an operator-controlled kill switch in the gateway env, not just the global config file.
  - `_session_allowed_tools()` — extracted the (previously duplicated) tool-assembly into one
    pure, unit-testable helper: defaults + jobs (unchanged) + manager (double-gated). Both call
    sites now use it: the SDK driver (`_SDKSession._async_run`) and the `print_resume` fallback
    (`_build_cmd`). **The env-flag check is ordered FIRST**, so the default path reads
    `~/.claude.json` exactly once (jobs only) — zero added I/O when the flag is off.
- **`scripts/setup_mcp.py`** — new opt-in `--with-manager` flag registers the `manager` server
  in `~/.claude.json` (Claude Code only; merges, never clobbers `jobs`/other keys). **Default
  invocation is byte-identical** (no manager registration). Prints the "also set
  MANAGER_TOOLS_ENABLED=1" reminder.
- **`docs/ENV_FEATURE_FLAGS.md`** — `MANAGER_TOOLS_ENABLED` row in §A (behaviour gates) + the
  "should-be-managed" table (it's an unmanaged `os.environ.get`, per the maintenance rule).
- **Tests** — `tests/test_claude_driver_manager_tools.py` (16: gate predicates + the full
  double-gate truth table + jobs-independence) and `tests/test_setup_mcp_manager.py` (4:
  opt-in only, merge-preserves-jobs, default-main-doesn't-register). **20 green**; the full
  claude-driver suite (111) still passes ⇒ jobs path unregressed.

Run: `.venv/bin/python -m pytest tests/test_claude_driver_manager_tools.py tests/test_setup_mcp_manager.py -q`

## Triple-gated activation (why nothing changes until the operator opts in)

To grant the manager tools ALL of these must hold — miss any one ⇒ byte-identical:
1. `MANAGER_TOOLS_ENABLED=1` in the gateway env (default absent).
2. A `manager` server in `~/.claude.json` (`setup_mcp.py --with-manager`; default absent).
3. The gateway restarted onto this branch's code (default: running old `main`).

## Adversarial review (self, pre-commit)

| Challenge | Resolution |
|---|---|
| Did extracting `_session_allowed_tools()` change the jobs grant? | No — same `list(_DEFAULT_TOOLS)` + `watch_job` logic, now shared by both call sites. 111 existing claude-driver tests pass unchanged. |
| Broad grant: EVERY session gets `dispatch_worker` when active. | Bounded: (a) triple-gated opt-in above; (b) the Level-3 admission gate still guards `_enqueue_task`, the shared dispatch choke point; (c) the control API is loopback-only on the gateway host, so the tool is inert from remote worker boxes. **Correct hardening = scope the grant to a manager-role session — that is Phase 3.1's job** (no session-role concept exists yet), flagged NOT done. |
| Could a config-read throw and break session boot? | No — both `_mcp_*_configured()` swallow all errors → False. And the env-flag short-circuit means the file isn't even read on the default path. |
| Per-boot I/O cost? | Default path: one `~/.claude.json` read (jobs), same as before — the manager read is skipped when the flag is off (flag checked first). |
| Blast on the live gateway? | None at import; helpers run only at session boot; gateway not restarted (health `ok` before/after). Fully reversible: unset flag / drop the `manager` server / revert the commit. |

---

## NOT done — for A35 (operator-gated) and Phase 3.1

- **🟡 The live F4 spike (A35).** Register the server, set the flag, restart the gateway, spawn
  a manager session, have it `dispatch_worker` → `wait_for_worker`, and confirm parent→child
  lineage in `/api/flows` with **no task-slot starvation**. Paid CLI + live restart + global
  config edit ⇒ operator go/no-go. Exact steps + rollback are in `AGENT_35_LIVE_F4_SPIKE.md`.
- **🟢 Phase 3.1 proper** — a spawnable *manager-role* session type that boots with the
  `manager_invocation.md` role prompt + grounding, and scopes the manager tools to THAT session
  (replacing the current all-sessions grant), plus `SESSION_ID` env so the manager can pass its
  own `parent_flow_run_id` automatically.

---

## Closure

**Status: `built` (branch `feat/m3-phase30-mcp-manager`).** The Manager tools are now wired to
sessions behind a reversible double gate — the last code step before Phase 3.0's live proof.
Everything up to the paid/live boundary is done; A35 packages that boundary for the operator.
