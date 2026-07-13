# DROP — Manager role must boot on ANY carrier (gateway embedded worker OR node agent worker)

**Raised:** 2026-07-13 (operator, after the A43 live run)
**Priority:** 🔴 HIGHEST — blocks survivable automation. The Manager currently runs ONLY on the
gateway host, where a gateway restart kills it (exactly today's incident).
**Level:** 3 (architectural; execution-path parity between in-gateway SDK driver and node worker)
**Owner:** unassigned (operator will spawn)

---

## Operator directive (verbatim intent)

> The Manager is a ROLE. It must work **independent of who the carrier is.** Whether the process
> that runs it is the gateway's built-in worker or a node agent-worker process, the role must boot
> the same way. A **one-time global machine setup** (global `~/.claude.json` / MCP config) is
> perfectly fine. But the actual spawn/driver must apply the role no matter which worker carries it.
> The fact that it FAILED on the node tells us more than every green test so far.

## Verified finding (A43, 2026-07-13)

`POST /api/manager` with `node_id="kanebra-worker"` (a node agent-worker process on this server):

- Session pinned correctly (`machine_id=kanebra-worker`, driver live).
- BUT the boot turn returned a **bare, generic Claude session**: *"I'm ready to help. What would
  you like me to work on?"* — citing the plain CLAUDE.md workflow, NOT the Manager role.
- **No role prompt, no first-assignment delivery, no manager MCP tools** (dispatch_worker /
  record_review / close_case). Finished in 9s, 102 output tokens, `awaiting_input`, dispatched nothing.

Compare: the identical invoke with `node_id` omitted (`__local__`, in-gateway) booted the full
Manager role and ran the whole review-gated loop (A43 in-gateway = PASS).

## Root cause (to confirm during build)

The Manager-role wiring lives on the **in-gateway SDK driver path only**:
- `_role_boot` (role prompt preset+append + scoped tools) in the claude SDK driver
  (`src/backends/claude_driver.py` / `claude_role_adapter.py`).
- First-assignment rendering + delivery (`render_first_assignment`, `src/core/roles.py`) is applied
  by `invoke_manager` (`src/orchestrator.py:2092`) via `submit_instruction` on the in-gateway path.
- Scoped `manager_v1` tool profile.

When the session is pinned to a node (`machine_id=<node>`), the turn is dispatched to the node's
**worker daemon** (`src/worker/agent.py`), which runs its own claude backend WITHOUT any of the
above: it does not carry `case_role`-driven role boot, does not deliver the manager assignment, and
does not attach the scoped manager tools. The manager MCP *tools themselves* are reachable (they
come from `~/.claude.json`, shared per-user, and the control API is loopback-reachable on-box) — but
nothing on the node path **applies the role** or **hands the session those tools + prompt**.

## Scope of work

1. **Make role boot carrier-agnostic.** The role definition (prompt + scoped tools + assignment) is
   already provider-neutral (`AgentRoleDefinition` + Claude adapter, A38). Ensure the **node worker
   execution path** consumes it: when a task/session carries `case_role="manager"` (or a role field),
   the worker daemon must apply `_role_boot` (system_prompt preset+append) + the scoped tool profile
   + deliver the first assignment — identical to the in-gateway path. Single shared code path preferred.
2. **Carry the role across the dispatch seam.** The task payload dispatched to a node must include
   what the node needs to reconstruct the role boot (role id, case id, assignment, tool profile) so
   the node isn't guessing. Verify `mesh_tasks.payload` / dispatch envelope carries it.
3. **Confirm MCP tool reachability from the node execution context** (documented as green on-box for
   `kanebra-worker`; verify for a truly remote node like `Horse` over tailnet, or scope the manager
   to on-box nodes until remote MCP reachability is solved).
4. **Re-run the A43 loop with `node_id="kanebra-worker"`** and assert it behaves identically to the
   in-gateway run (role prompt present, tools present, dispatch→review→rework→accept→close).

## Acceptance criteria

- [ ] `POST /api/manager` with a node `node_id` boots a session with the Manager role prompt +
      scoped manager tools + the first assignment — identical to in-gateway.
- [ ] That node-carried Manager can `dispatch_worker` / `record_review` / `close_case` and drive a
      full review-gated loop.
- [ ] A single shared role-boot code path serves both carriers (no in-gateway-only branch).
- [ ] Automation survives a gateway restart (runs as the node process's child, not the gateway's).

## Cross-refs

- `src/orchestrator.py:2092` `invoke_manager` (in-gateway assignment delivery)
- `src/backends/claude_driver.py`, `src/backends/claude_role_adapter.py` (`_role_boot`)
- `src/core/roles.py` (`render_first_assignment`, `AgentRoleDefinition`)
- `src/worker/agent.py` (the node execution path that lacks the role wiring — the fix site)
- `docs/harness/roles/manager.md`, `manager_v1` tool profile
- Related: `DROP_DISPATCH_WORKER_REAL_SESSION.md` (node survivability + observable workers)
- `docs/M3_MANAGER_INVOCATION_SPEC.md` (scope against it)
