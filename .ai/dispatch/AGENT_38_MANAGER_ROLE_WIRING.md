# A38 — M3 Phase 3.1: Manager role wiring (canonical architecture + minimal vertical slice)

**Level:** 3 (code) · **Branch:** `feat/m3-phase31-manager-role` · **Date:** 2026-07-12
**Reads with:** `docs/M3_MANAGER_INVOCATION_SPEC.md` §6 Phase 3.1 (+2026-07-11 amendment),
`docs/harness/manager_invocation.md` (the paste-driver this promotes), `AGENT_34_MANAGER_SESSION_WIRING.md`
(the double-gated tool grant it scopes per-session), `AGENT_36`/`AGENT_37` (the Case admission +
authoritative `close_case` substrate this consumes). Prereq **M2.5 is MERGED** (`33b3f76`, PR #9).

> **Operator directive (2026-07-12):** establish the *canonical Manager architecture* now —
> do NOT hard-wire "Manager = Claude system prompt" and re-derive the separation later.
> Keep the minimal vertical slice, but lay the layered seam. Full directive in the loop notes.

---

## Objective (the proof, §6 Phase 3.1)

One thin end-to-end path, flag-gated (`MANAGER_ROLE_ENABLED`, default OFF ⇒ byte-identical):

```
operator objective
  → orchestrator.open_case (ONE Case, session.case_role="manager")
  → Manager Session boots via the Claude adapter (stable role instructions + manager_v1 tools + case_id + objective)
  → Manager dispatches ONE worker into the SAME Case (not a child Case)
  → Manager reads the worker result (Case stays OPEN — A37)
  → Manager sends one bounded follow-up OR closes via authoritative close_case
```

## Canonical layer separation (what M3.1 builds vs. records-for-later)

| Layer | Home | M3.1 |
|---|---|---|
| **1 Role profile** (stable identity) | `docs/harness/roles/manager.md` (NEW, provider-neutral) | Extract stable identity/authority/boundaries/obligations/decision-vocab/honesty from `manager_invocation.md`. **No** objective/Case/branch/date/provider config. `manager_invocation.md` stays as manual compat wrapper. |
| **2 Skills** (reusable procedures) | `docs/harness/skills/<skill>/SKILL.md` (future) | **Seam + boundaries recorded only.** No generic loader. First loop's procedure inlined in the role. Boundaries: ground-and-frame · open/decompose-case · dispatch-worker · supervise/redirect · review-delivery · bounded-rework · close-or-derive. |
| **3 Tools** (operational interface) | `scripts/mcp_manager.py` | Reuse `dispatch_worker`/`wait_for_worker`; add `get_case` (read) + `close_case` (Decision). Worker joins Manager's Case via the new `case_id` JOIN signal (admission branch J). `manager_v1` = 4 tools; wider surface (Task/Timeline/Evidence) recorded for later. |
| **4 Gateway workflow/state** (durable progression) | `orchestrator` / `db` | Unchanged. `open_case` (`orchestrator.py:1895`), `close_case` (`:1927`), admission (`:1612`). Manager owns judgment; gateway owns durable progression + gates. |
| **5 Policy & schemas** | `db.CaseCloseBlocked` (`db.py:61`); new Pydantic in `src/core/roles.py` | Policy already machine-enforced (Level-3, open-child, criteria) — relied on, not re-encoded in the prompt. New schemas: `ManagerInvocation`, `ManagerDecision`. Dynamic data travels here / as first user turn, never the system role. |
| **6 Provider seam + Claude adapter** | `src/core/roles.py` (neutral) + `src/backends/claude_role_adapter.py` | `AgentRoleDefinition{role_id, system_instructions, declared_skills, tool_profile, output_contract}` — **no Claude imports.** ONE Claude adapter → `system_prompt={"type":"preset","preset":"claude_code","append":<manager.md>}` (verified `SystemPromptPreset`, SDK types.py:36) **+** manager tools. **Preset preserved, instructions appended.** Thin seam only — no registry, no Codex adapter (decided 2026-07-12). |

## Boot trigger (decided 2026-07-12: new control-API path)

`POST /api/manager` → orchestrator `invoke_manager(objective, …)`: create Session → `open_case`
(stamps `case_role="manager"`) → submit the objective as the first assignment to that Session.
The driver's `_get_or_create` (`claude_driver.py:802`) reads `session.case_role`; when
`MANAGER_ROLE_ENABLED` **and** `case_role=="manager"`, it threads the adapter's `system_prompt`
+ per-session manager tools into `_SDKSession` (`:821`, options build `:429`). Worker completion
returns to the same Case-owning Manager Session (M3.3 makes that relay crash-durable).

## Steps (incremental, each verified with plain pytest — no live CLI/restart)

1. Extract `docs/harness/roles/manager.md`; `manager_invocation.md` → compat wrapper header.
2. `src/core/roles.py`: `AgentRoleDefinition` + loader `load_manager_role()` + `ManagerInvocation`/`ManagerDecision` (Pydantic, provider-neutral).
3. `src/backends/claude_role_adapter.py`: definition → `system_prompt` preset+append + tool list.
4. `claude_driver.py`: `_manager_role_enabled()`; thread role/system_prompt into `_SDKSession`; `_session_allowed_tools(role)` per-session scoping (legacy A34 process-wide preserved when flag OFF).
5. `control_api.py` `POST /api/manager` + orchestrator `invoke_manager` seam.
6. `mcp_manager.py` `get_case`; confirm dispatch-into-same-Case.
7. `docs/ENV_FEATURE_FLAGS.md` row; acceptance tests (all assertions below); this packet's `## Milestone`.

## Acceptance
Exactly one Case per Manager objective · one persistent Manager Session with `case_role="manager"` ·
stable role loaded via the Claude adapter · dynamic data outside the system role · one worker joins
the same Case · worker completion leaves the Case OPEN · same Manager Session can inspect/direct the
worker · Manager closes via A37 `close_case` · architecture records role/skill/tool/policy/adapter
separation · ordinary sessions and flag-OFF behavior byte-identical.

**Out of scope:** generic skill loader, full tool surface, reviewer role (3.2), durable relay +
cost caps (3.3), multi-worker. Live proof deferred to the combined A35+3.1 acceptance run.

---

## Milestone (burndown)

**Built 2026-07-12 on `feat/m3-phase31-manager-role`.** All 7 steps landed:

1. ✅ `docs/harness/roles/manager.md` (canonical, provider-neutral stable identity);
   `manager_invocation.md` → compatibility-wrapper header.
2. ✅ `src/core/roles.py` — `AgentRoleDefinition` + `load_manager_role()` +
   `ManagerInvocation`/`ManagerDecision` (Pydantic, no provider imports) + `render_first_assignment`.
3. ✅ `src/backends/claude_role_adapter.py` — `claude_system_prompt` (preset+append) +
   `profile_tools`/`manager_tool_names` (`manager_v1` = dispatch_worker/wait_for_worker/get_case).
4. ✅ `claude_driver.py` — `_manager_role_enabled()`; `_role_boot(session)`; `_SDKSession`
   carries `system_prompt`/`allowed_tools`; `_session_allowed_tools(role)` per-session scoping
   (legacy A34 process-wide grant preserved when flag OFF).
5. ✅ `orchestrator.invoke_manager()` + `POST /api/manager` (`ManagerInvokeBody`) — create
   session → `open_case` (`case_role="manager"`) → deliver objective as first assignment.
6. ✅ `mcp_manager.py` — `get_case` + `close_case` tools + `case_id` (JOIN) arg on `dispatch_worker`;
   admission branch (J): a `join_case_id` attaches the worker task to the Manager's open Case (task
   link + `task.attached`), verified-open, stashed under `_CASE_ID_META_KEY` ⇒ completion leaves the
   Case OPEN. No child Case.
7. ✅ `docs/ENV_FEATURE_FLAGS.md` row (`MANAGER_ROLE_ENABLED`); `tests/test_manager_role.py`;
   `test_mcp_manager` tool-set assertion updated.

## Adversarial-review fixes (2026-07-12, post-build)

Traced the loop end-to-end against intent; fixed three real gaps that would have broken the loop:

- **`wait_for_worker` couldn't observe a JOINED worker.** A joined worker owns NO flow_run, so
  `wait_for_worker(task_id)` (which resolves a flow_run *owned* by the task) returned None and
  looped to timeout — and `dispatch_worker`'s own "Next" hint recommended exactly that broken form.
  Fix: when dispatched with `case_id`, the hint + tool desc now direct
  `wait_for_worker(task_id, flow_run_id=<case_id>)`, which watches the Case timeline filtered by
  the worker's `task.finished` (`_terminal_task_event` already filters by `task_id`).
- **Manager had no way to CLOSE its Case** (acceptance said it must). `orchestrator.close_case`
  had no endpoint/tool. Fix: `POST /api/cases/{id}/close` (returns the structured
  `{ok,closed,reason}` at 200 — a blocked close is a decision signal, not an HTTP error) + a
  `close_case` MCP tool (now in `manager_v1`).
- **`MANAGER_ROLE_ENABLED` silently half-works without `HARNESS_FLOW_DRIVE`.** invoke_manager now
  logs a warning; the flag doc records the dependency.

**Verification:** full suite green (2 pre-existing env fails unrelated: push VAPID, mcp_jobs
Windows-sleep). Flag OFF ⇒ byte-identical (A34 manager-tools + full admission/closure/lineage suites
green). **Live proof (Claude preset+append boot, per-session tools, real dispatch/close) deferred to
the operator-gated combined A35+3.1 acceptance run.**
