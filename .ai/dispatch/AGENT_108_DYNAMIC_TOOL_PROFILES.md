```yaml
job_id: AGENT_108_DYNAMIC_TOOL_PROFILES
created_at: "2026-10-11T12:40:58.310051+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: ["AGENT_107_BASE_TOOL_PROFILE"]
results_ref: DISPATCH_LOG.md#A108             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["tests/test_tool_profile_source.py","tests/test_claude_driver_manager_tools.py","src/backends/tool_profile_source.py","src/control/db.py","docs/backend/ENV_FEATURE_FLAGS.md"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-11T12:42:29.425003+00:00"
```

# DISPATCH — AGENT_108_DYNAMIC_TOOL_PROFILES

**Level:** 3 (worker-boot grant path, agent autonomy, controller config authority) · **Type:**
feature · **Authored:** 2026-10-11 · **Depends on:** A107 · **Operator approval:** granted ·
**Branch:** `feat/dynamic-tool-profiles`

> Decision trace + holistic map. Operator approved the restart-free tool-profile mechanism and
> required it be handled as part of the whole system, not an isolated component.

## Why
Operator cannot redeploy/restart the worker on demand, yet needs to add/remove agent tools. Today
the tool allowlist lives in worker Python (`claude_driver`), so a tool change needs a worker
restart on the carrier node. Root reason (verified, PR #227/#229 investigation): `_session_allowed_tools`
runs in the WORKER daemon; the Claude SDK (0.2.110) has no server-level/wildcard `allowed_tools`, so
per-tool enumeration is mandatory for Claude. Codex/OpenCode gate per server and are already
restart-free for new tools on a configured server.

## Design (reuses existing rails — no parallel config system)
Make the profile data **controller-authoritative** and read it at session boot via the EXISTING
runtime-flag registry + A88 controller snapshot:
- `TOOL_PROFILES_DYNAMIC` (gate, default OFF) + `TOOL_PROFILES_OVERLAY` (JSON) added to
  `RUNTIME_FLAG_DEFINITIONS` — registry-writable, served to workers by `/control/runtime-flags`,
  refreshed into the worker's in-memory snapshot (no session-boot I/O).
- `src.backends.tool_profile_source` layers a validated overlay over the static seed map
  (`claude_role_adapter.static_profile_tools`), exposing `base_tool_names`/`profile_tool_names`/
  `tool_profile_for_role` with the SAME signatures the driver already used.
- `claude_driver._session_allowed_tools` now imports those from the source module; its body is
  unchanged. Gate OFF ⇒ static map (byte-identical to A107).
- `db.runtime_flag_value()` added — the raw-string sibling of `runtime_flag_enabled`, same
  worker-safe seam (controller snapshot on a worker, local DB on the controller).

Effect: adding/removing a tool (or remapping a role) = a controller registry write
(`PUT /api/flags/TOOL_PROFILES_OVERLAY`) → the next session on any node picks it up. **No worker
restart, no redeploy.** One-time cost: the worker must run this merged code once (a single future
worker deploy installs the mechanism); everything after is restart-free.

**Fail-closed, least-privilege:** gate OFF / absent / malformed / oversized / bad tool name / role →
unknown profile ⇒ the whole overlay is rejected and the static seed is used. An unmapped role always
resolves to `worker_v1`, so an overlay can never make an unknown role privileged.

## TASK (what shipped)
1. `src/control/db.py` — two flag defs + `runtime_flag_value()` raw reader.
2. `src/backends/tool_profile_source.py` (new) — validated, bounded overlay resolver.
3. `src/backends/claude_role_adapter.py` — `static_profile_tools()` accessor (keeps `_PROFILE_TOOLS`
   private; adapter stays the static seed guarded by CI pins).
4. `src/backends/claude_driver.py` — resolve tool names through the source module.
5. `config/settings.py` — managed env keys; `docs/backend/ENV_FEATURE_FLAGS.md` — documented.

## ACCEPTANCE (proof)
- Gate OFF ⇒ resolved == static, and driver grant byte-identical (tests).
- Valid overlay: add a manager tool / define a new profile + remap a role / extend base — all reach
  the session grant through the driver.
- Every rejection case (invalid JSON, not-object, unknown top key, bad tool list/name, role→unknown
  profile, overlong name, oversized, too-many) falls back to static.
- Least-privilege: an unmapped role is `worker_v1` even under an overlay.
- Real worker seam: an installed A88 controller-state snapshot drives the overlay (not just
  monkeypatched readers); uninstall ⇒ back to static.
- Registry inventory / flag API / DB-authority suites still green with the two new flags.

## Holistic map — related moving parts (inspected; see TRAIL)
The tool-grant path was traced across the whole system. Accounted for here:
- **Deployment shapes:** `runtime_flag_value`/`_enabled` resolve transparently on the single-process
  controller (local DB) and the split worker (A88 snapshot) — the overlay works in both.
- **Session boot is sync but I/O-free:** flag reads hit the pre-fetched snapshot; no new network call
  on the boot path.
- **CI pins preserved:** the static seed (base/manager) is still asserted against the MCP servers;
  the overlay is additive and cannot silently drift the seed.

## SCOPE OUT — deliberate boundaries, surfaced not buried (candidate A109)
These are adjacent to the grant path and were MAPPED, but are a distinct "role-extensibility"
initiative, not part of making tool profiles dynamic:
- **Codex/OpenCode role→server selection is hard-coded.** They expose ALL tools of every configured
  server (no per-tool allowlist), and server *selection* is not role-aware (`codex_native.py:787`,
  `opencode.py` config check). Adding a NEW role that needs a NEW server requires `setup_mcp.py`
  registration on the node — the dynamic overlay (a Claude allowlist concept) does not cover server
  registration. Documented; no change needed for tools on already-configured servers.
- **Hard-coded role-id branches** exist beyond the grant: `claude_driver._role_boot`
  (`case_role==MANAGER_ROLE_ID` / `role_boot==WORKER_ROLE_ID`), orchestrator admission guards (literal
  `"worker"`), `db.py` sender authority (`"manager"`), `cost_read_model.py` (two-role assumption). A
  genuinely new role must be wired through these. A real **role registry/factory** (enumerate roles,
  per-role authority metadata) would remove the literal-string branches — recommended as A109.
- **Overlay write-time validation:** validation is on READ (fail-closed). A write-time schema check on
  `PUT /api/flags/TOOL_PROFILES_OVERLAY` would improve operator UX; deferred (cheap follow-up).
- **Phase 2 — profile DATA changes are already no-redeploy** (registry write). What is NOT removed:
  changing the *static seed* still needs a gateway deploy; and the one-time mechanism install needs a
  single worker deploy.

## TRAIL
- Two scoped investigations (runtime-flag/controller-read rails; full tool-grant + role dependency
  graph incl. Codex/OpenCode + hard-coded role branches) — findings folded into the design + Scope Out.
- Files: `src/control/db.py`, `src/backends/tool_profile_source.py` (new),
  `src/backends/claude_role_adapter.py`, `src/backends/claude_driver.py`, `config/settings.py`,
  `docs/backend/ENV_FEATURE_FLAGS.md`, tests below.

## Milestone checklist
- [x] registry flags + `runtime_flag_value`
- [x] overlay resolver (validated, bounded, fail-closed, least-privilege)
- [x] driver resolves through the source; byte-identical when OFF
- [x] worker-snapshot seam proven; registry/flag/DB-authority suites green
- [x] docs + managed env keys
- [ ] A109 role-extensibility (role registry + Codex/OpenCode role→server + hard-coded branches) — operator decision
- [ ] mechanism live — needs ONE worker deploy on Horse to install; thereafter restart-free

## Closure

### Controller-authoritative dynamic tool profiles — SHIPPED (2026-10-11)
**What changed:**
- `src/control/db.py` — `TOOL_PROFILES_DYNAMIC`/`TOOL_PROFILES_OVERLAY` defs + `runtime_flag_value()`.
- `src/backends/tool_profile_source.py` (new) — overlay resolver (gate, parse, validate, bound,
  fail-closed, least-privilege).
- `src/backends/claude_role_adapter.py` — `static_profile_tools()` accessor.
- `src/backends/claude_driver.py` — tool names resolve through the source module.
- `config/settings.py` managed env keys; `docs/backend/ENV_FEATURE_FLAGS.md` rows.
- Tests: `tests/test_tool_profile_source.py` (new; gate/valid/rejection/least-priv/worker-seam),
  `tests/test_claude_driver_manager_tools.py` (driver end-to-end overlay on/off).

**Verification:** `pytest tests/test_tool_profile_source.py tests/test_claude_driver_manager_tools.py
tests/test_manager_role.py tests/test_worker_role.py tests/test_scientific_governor.py
tests/test_mcp_manager.py tests/test_mcp_jobs.py tests/test_control_api.py
tests/test_database_authority.py -p no:warnings -q` → all green.

**What follows:** A109 role-extensibility (surfaced above); one worker deploy on Horse installs the
mechanism, after which tool/role profile changes are a controller registry write with no worker restart.
