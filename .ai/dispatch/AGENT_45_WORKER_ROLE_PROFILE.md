# AGENT_45 — Worker role profile + loader + tier selector

**Dispatched:** 2026-07-17
**Level:** 3 (paid, operator-gated, supervised live run — built BY a live Manager loop)
**Branch (worker):** `feat/worker-role-profile` (Manager cuts it; code change ⇒ PR at close, do NOT merge)
**Flags (expected live):** `MANAGER_ROLE_ENABLED=1`, `MANAGER_TOOLS_ENABLED=1`, `REVIEW_EMITTER_ENABLED=1`, `HARNESS_FLOW_DRIVE=1`; `manager` MCP server in `~/.claude.json`.

## Why
The Manager has a stable, canonical role identity (`docs/harness/roles/manager.md`) loaded via
`src/core/roles.py::load_manager_role` + the Claude adapter (`src/backends/claude_role_adapter.py`,
preset+append). **The Worker has none.** A dispatched worker today is prompted *only* by the
dispatch-packet prose that rides in as its first user turn — there is no stable "who a Worker is,"
no evidence/honesty contract, no declared skills, no output contract. That means every worker
re-derives its discipline from scratch, and there is no artifact to *tune* when we start
calibrating worker behavior.

This is the creation step that must precede any worker-behavior tuning: **create the thing first,
then it can be reviewed and improved by a second pair of eyes.**

## Intent (ground this in code/git before building — do NOT trust this prose)
Read, in order, and confirm the gap is real and matches this description; if it differs or
conflicts with spec, surface + wait:
- `docs/harness/roles/manager.md` — the shape a role profile takes (identity / ownership /
  boundaries / decision vocabulary / evidence+honesty). The Worker profile MIRRORS this structure
  but for a Worker's authority (narrower: owns ONE task on ONE tree, returns to the Manager).
- `src/core/roles.py` — `AgentRoleDefinition`, `load_manager_role()`, `ManagerInvocation`,
  `render_first_assignment()`, `MANAGER_SKILLS`, `MANAGER_TOOL_PROFILE`. The loader pattern to copy.
- `src/backends/claude_role_adapter.py` — `claude_system_prompt()` (preset+append) + the static
  tool-profile helper. How a role becomes a Claude `system_prompt` + scoped tools.
- `scripts/mcp_manager.py::_dispatch_worker` + `src/orchestrator.py` dispatch seam
  (`render_first_assignment` call site, ~`orchestrator.py:2158-2194`) — where a worker is spawned
  and where a role/tier would be selected.

## Objective (delivered as the Manager's first assignment turn)
Create the Worker role layer so a worker can boot with a stable identity, **while preserving a
cheaper role-less tier for mechanical one-off jobs** — do not force a full behavior contract onto
throwaway work.

1. **`docs/harness/roles/worker.md`** — canonical, provider-neutral Worker identity. NO transient
   state, NO current task, NO branch/date (those arrive per-dispatch as the assignment turn, exactly
   as the Manager's do). Cover: who you are (a focused builder who owns one task on one tree);
   what you own (the task + its tree + honest reporting); boundaries (one tree at a time; TDD /
   plain-pytest only, never paid e2e, never `python main.py status`; ground in code/git before
   changing; minimal-diff / principle of least action; commit your own work); evidence & honesty
   (report what was actually done/skipped/failed with evidence; a green test on your layer does NOT
   prove the goal crosses layer boundaries — the A43 lesson); output contract (what you return to
   the Manager for review).
2. **`load_worker_role()` in `src/core/roles.py`** — mirror `load_manager_role()`: a
   `WORKER_ROLE_ID`, `WORKER_SKILLS` (may start empty/minimal — declared, honest), a
   `WORKER_TOOL_PROFILE`, load `worker.md`, raise clearly if missing/empty.
3. **Adapter wiring** — confirm `claude_system_prompt()` already works for any
   `AgentRoleDefinition` (it should — it is role-agnostic); add a worker tool-profile resolver if the
   Manager's static helper is manager-specific. No Claude-specific logic in `roles.py`.
4. **Tier selector at the dispatch seam** — introduce an explicit, opt-in way to dispatch a
   **role-ful worker** (boots with `worker.md`) vs the existing **role-less one-off job** (tier-0,
   current default, unchanged). Smallest honest surface: e.g. an optional `role`/`tier` signal on
   `dispatch_worker` that, when set to `worker`, boots the Worker role; absent ⇒ byte-identical
   legacy one-off. Default OFF/absent ⇒ byte-identical. Do NOT auto-promote every job to role-ful.
5. **Tests (plain pytest)** — `load_worker_role()` loads + raises-on-missing; adapter produces a
   preset+append system prompt for the worker; the tier selector boots role-ful only when asked and
   is byte-identical otherwise.

## Completion criteria (ONE reconciled string — the Manager verifies each clause in git)
A canonical `docs/harness/roles/worker.md` exists mirroring the Manager profile's structure; `load_worker_role()` in `src/core/roles.py` loads it and raises clearly when missing/empty; the Claude adapter yields a preset+append `system_prompt` for the Worker role; the dispatch seam gained an explicit opt-in tier selector that boots a role-ful worker ONLY when asked and is byte-identical (role-less one-off) by default; plain-pytest tests cover the loader, the adapter, and the default-byte-identical tier behavior and pass; one `feat/worker-role-profile` branch + PR opened (NOT merged — merge-to-main escalated to operator).

## Bounds / supervision
One Manager + 1–3 sequential workers (Manager decides). Plain `pytest` ONLY (never e2e, never
`python main.py status`; live gateway check = `curl http://127.0.0.1:9003/health`). One worker per
tree. Operator-supervised: dispatcher monitors the Case live via `get_case` / `/api/work` and stops
on drift. The Manager must **genuinely review** each worker delivery adversarially (git diff, not
the worker's summary), `record_review` accepted|rework_requested with bounded findings, and iterate
until the profile is real quality — not accept the first draft reflexively.

## Live log
- *(to be filled by the Manager loop)*
