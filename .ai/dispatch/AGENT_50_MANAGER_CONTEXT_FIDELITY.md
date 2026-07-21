# AGENT_50 — Manager context fidelity: generous carry + read-history tool

**Dispatched:** 2026-07-21
**Level:** 3 (control-API + orchestrator + MCP tool + web; default-safe; build on tests)
**Branch:** `feat/manager-context-fidelity` → PR, merged, deployed (operator directive: ship it).

## Why
Two operator-surfaced gaps after the fork→Manager delivery fixes (#31/#32):
1. **Truncation was reckless AND backwards.** The fork prior-context digest was hard-sliced at
   4000 chars (~700 words) **keeping the head** (`slice(0, N)`) — for "continue the work" the
   *latest* turns matter, so it dropped exactly the useful end. A bound must exist (unbounded paste
   overflows the window + re-costs tokens), but 4000 was far too small.
2. **No way to read the full prior session.** The Manager had no tool to pull a specific session's
   history — so if the boot excerpt was truncated it could not familiarize itself further. This was
   the deferred item from AGENT_49.

## What shipped
- **Generous, tail-keeping clamp.** `_COMPACT_PREFIX_MAX_CHARS` 4000 → **48000** (~12k tokens),
  env-tunable `AI_TEAM_COMPACT_PREFIX_MAX_CHARS`. New `_clamp_keep_tail` keeps the **most recent**
  content and marks the dropped front (`…(earlier context truncated)…`), in both server prefix
  builders and web `buildForkDigest`. API `continue_inline` cap raised to match (`_CONTINUE_INLINE_MAX
  = 48000`, shared by InstructionBody + ManagerInvokeBody; web `FORK_DIGEST_MAX_CHARS = 48000`).
- **`read_session_history` MCP tool** (`scripts/mcp_manager.py`) over the auth-guarded
  `GET /api/sessions/{id}/messages` — returns a bounded You:/Agent: transcript, `limit`-paged, output
  hard-capped (60k) keeping the tail. Granted in the manager profile (`claude_role_adapter.py`).
- **Boot pointer.** `invoke_manager` appends `read_session_history(session_id='<continued_from>')`
  to the first assignment when the Manager was forked — so it knows which session to read for the
  FULL prior line of work beyond the bounded excerpt.

## Verification
- Backend: 119 targeted tests green (manager_role, mcp_manager, fork_inline, compact_injection,
  control_api_fork, driver_manager_tools, setup_mcp_manager, mesh_enqueue_affinity) incl. new
  read_session_history + boot-pointer + tail-keeping truncation tests.
- Web: tsc clean, 98 vitest (incl. rewritten tail-keeping digest test), production build.
- Adversarial review: **SHIPS** — truncation math exact, fence-defuse applied before clamp (tail-cut
  cannot leak a fence fragment), tool id url-quoted+bounded over an auth-guarded traversal-safe
  endpoint, byte-identical when no fork / MANAGER_ROLE_ENABLED OFF.

## Caveat (honest)
`read_session_history` runs via the manager MCP server; on a **remote node** (Horse) it works only
if that node's `~/.claude.json` has the manager server configured and can reach the gateway API
(the standing "remote-node MCP reachability = on-box only" caveat). Works immediately for `__local__`
Managers; unverified on Horse from here.

## Status
BUILT → adversarially reviewed → merged → gateway restarted + frontend rebuilt (operator directive).
