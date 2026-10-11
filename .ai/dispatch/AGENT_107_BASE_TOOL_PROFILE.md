```yaml
job_id: AGENT_107_BASE_TOOL_PROFILE
created_at: "2026-10-11T11:51:57.197935+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: ["AGENT_106_MANAGER_SELF_IDENTITY"]
results_ref: DISPATCH_LOG.md#A107             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["tests/test_claude_driver_manager_tools.py","tests/test_manager_role.py","src/core/roles.py","src/backends/claude_role_adapter.py","src/backends/claude_driver.py"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-11T11:52:37.879249+00:00"
```

# DISPATCH — AGENT_107_BASE_TOOL_PROFILE

**Level:** 2 (internal refactor, behaviour-identical, no new surface) · **Type:** refactor ·
**Authored:** 2026-10-11 · **Depends on:** A106 · **Branch:** `feat/base-tool-profile`

> Decision trace. Follows A106: after adding `whoami`/`check_inbox` we hit the tool-grant
> handshake twice, which exposed an asymmetry — role tools flow through a provider-neutral
> profile, but universal tools (`watch_job`, `whoami`) were hard-coded in the Claude driver.

## Why
Operator is about to add more roles and role subtypes. The grant model had to be made general
so a new role (a) automatically gets the universal tools and (b) cannot accidentally inherit a
privileged surface just by existing. Previously "universal tools" had no neutral home — they were
appended inline in `claude_driver._session_allowed_tools`, bypassing the `MANAGER_TOOL_PROFILE` /
`WORKER_TOOL_PROFILE` abstraction the role tools use.

## TASK (what shipped) — behaviour-identical
1. **`BASE_TOOL_PROFILE` (`base_v1`)** in `src/core/roles.py`: the single provider-neutral home for
   universal tools (currently `mcp__jobs__whoami`, `mcp__jobs__watch_job`), layered under every role.
2. **Role→profile registry** `ROLE_TOOL_PROFILES` + `tool_profile_for_role()` in `roles.py`: the one
   map from role id → its non-base profile. Unknown/unmapped role ⇒ `WORKER_TOOL_PROFILE`
   (least-privilege) — a new role can never inherit the manager surface by default.
3. **`profile_tool_names(profile)` + `base_tool_names()`** in `claude_role_adapter.py`: one resolver;
   `manager_tool_names`/`worker_tool_names` become thin wrappers. The adapter stays the ONLY place
   that knows Claude tool-name strings.
4. **`claude_driver._session_allowed_tools`** now composes `defaults + base (if jobs configured) +
   profile_tool_names(tool_profile_for_role(role))`, with the manager-server-configured guard
   preserved (falls back to least-privilege if the manager server is absent). No inline tool names.

Adding a tool is now ONE edit: append to the right profile list in the adapter (universal → base;
role-specific → that role's profile). Adding a role is ONE `ROLE_TOOL_PROFILES` entry.

## ACCEPTANCE (proof)
- `base_tool_names()` == the jobs server's advertised surface (CI pin); `manager_v1` == the manager
  server's advertised surface (existing pin) — both fail the build on drift.
- Base tools reach manager, worker, scientific_governor, an unmapped future role, and None.
- An unmapped role gets base only, never the manager surface (least-privilege).
- Legacy (MANAGER_ROLE_ENABLED OFF) grant path byte-identical.

## SCOPE OUT (deliberate; see runbook discussion)
- **Restart-free tool changes are NOT delivered here.** The grant still lives in worker Python
  (`claude_driver` runs in the node/worker daemon), so adding a tool still needs a worker restart on
  the carrier node to apply the allowlist. Making it restart-free requires sourcing the profile→tool
  map from a node-readable config / control API read at session boot — a separate Level-3 job
  (see "Restart-free follow-up" below). The Claude SDK (0.2.110) has no server-level/wildcard
  `allowed_tools`, so per-tool enumeration is mandatory for Claude; Codex/OpenCode gate per server
  and are already restart-free for new tools.

## Restart-free follow-up (proposed, not built)
Source `_PROFILE_TOOLS` (or at least the role→profile map) from a dynamic surface read at each
session boot (e.g. `GET /control/tool-profiles`, mirrored like the runtime-flag registry, or a
node-local JSON). Then adding/removing a tool = update the MCP script on the node (live, next
session) + update the profile data (live, next session) — no worker code change, no restart. Keeps
per-role granularity (unlike a blanket server-level grant). Gate behind a flag; fall back to the
static `_PROFILE_TOOLS` if the dynamic source is unreachable (fail-closed to least-privilege).

## Milestone checklist
- [x] BASE_TOOL_PROFILE + role→profile registry + resolver
- [x] driver composes base + role profile generally; least-privilege default
- [x] CI pins for base + manager surfaces; behaviour-identical legacy path
- [x] targeted role/driver/MCP tests green
- [ ] restart-free dynamic profile source (separate job — operator decision)

## Closure

### Base tool profile + role→profile registry — SHIPPED (2026-10-11)
**What changed:**
- `src/core/roles.py` — `BASE_TOOL_PROFILE`, `ROLE_TOOL_PROFILES`, `tool_profile_for_role()`.
- `src/backends/claude_role_adapter.py` — `base_v1` entry, `profile_tool_names()`, `base_tool_names()`;
  `manager_tool_names`/`worker_tool_names` now wrappers.
- `src/backends/claude_driver.py` — `_session_allowed_tools` composes defaults + base + role profile
  via the registry; dropped inline universal-tool names and the unused manager/worker name imports.
- Tests: `tests/test_claude_driver_manager_tools.py` (base pin + reach-every-role + least-privilege),
  `tests/test_manager_role.py` (point helper refs at the adapter).

**Verification:** `pytest tests/test_claude_driver_manager_tools.py tests/test_manager_role.py
tests/test_mcp_manager.py tests/test_mcp_jobs.py tests/test_scientific_governor.py
tests/test_worker_role.py tests/test_manager_carrier_role.py -p no:warnings -q` → all green.

**What follows:** restart-free dynamic profile source (above) is the next step toward "change tools
in one place with no redeploy" — deferred as its own job pending operator go-ahead.
