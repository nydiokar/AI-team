```yaml
job_id: AGENT_106_MANAGER_SELF_IDENTITY
created_at: "2026-10-11T10:30:05.368661+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A106             # -> DISPATCH_LOG.md section with the verdict prose
evidence: ["tests/test_mcp_manager.py","tests/test_mcp_jobs.py","tests/test_manager_role.py","tests/test_agent_inbox_delivery.py","scripts/mcp_manager.py"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-11T10:31:18.732266+00:00"
```

# DISPATCH — AGENT_106_MANAGER_SELF_IDENTITY

**Level:** 3 (mesh/agent-facing MCP + gateway route, crosses the scripts↔gateway boundary) ·
**Type:** fix + small feature · **Authored:** 2026-10-11 · **Depends on:** — ·
**Branch:** `feat/manager-self-identity`

> This packet is a **decision trace**: where the change came from, how it was formed, what shipped,
> and the evidence. The work was implemented directly in this session (not dispatched to a worker)
> at the operator's instruction. No separate spec — the trace lives here.

## Read this first
- `scripts/mcp_manager.py` — Manager MCP tools (`open_case`, `close_case`, `dispatch_worker`, …).
- `scripts/mcp_jobs.py` — worker-facing MCP server (`watch_job`).
- `src/control/agent_inbox.py` — the ONE pending source (`pending_for`); guarded by
  `tests/test_a104_single_pending_source.py`.
- `src/control/routes/cases.py`, `src/orchestrator.py` — Case/inbox gateway seams.

## Where this came from (the trigger)
A live Manager session filed UX feedback after mis-opening a Case **on the wrong owner session**.
Step-by-step failure it reported:
1. `open_case` had a **required** `session_id` with no self-resolution — unlike `close_case`, which
   already keys off the caller.
2. Its boot context exposed a Claude-Code **transcript uuid** (`11ca1374-…`); passing it → HTTP 404
   `session_not_found`. The id the agent was *given* at boot is **not** the id the Case API accepts.
3. With no way to discover its real id, it scraped a sibling id (`9325036c38d2`) out of a doc header
   and passed that. `open_case` **accepted it silently** → a correctly-structured Case hanging off the
   **wrong owner session**, invisible until the operator revealed the real id (`e5675771bbd8`).

Three bugs, in priority: (1) no self-resolution [root cause]; (2) the boot-exposed id is a different
namespace from the Case-API id; (3) `open_case` accepts an arbitrary other-session id with no
ownership check. Secondary: validate the success echo; add a `whoami` / "my cases" read; a way to
read the outbox/inbox; make the ownership model consistent across case ops.

## How the decision was formed (grounded investigation)
Mapped the code before changing anything (symbol index + targeted reads; two scoped sub-agent
sweeps). Key finding that shaped the whole fix:

- **The Manager MCP server runs INSIDE the agent's own process.** The carrier/backend exports the
  agent's gateway `session_id` into that process env as `AI_TEAM_SESSION_ID` (`SESSION_ID` fallback),
  and the A102 `_AGENT_IDENTITY_KEYS` filter (`src/backends/claude_driver.py`) guarantees it is **this
  session's own** id, never a stale inherited one. `dispatch_worker` already read it
  (`scripts/mcp_manager.py`, the `requester` line).
- **That env value IS the id `/api/cases` keys on** (`sessions.session_id`, the control-plane PK). The
  `11ca1374-…` the agent saw at boot is the SDK transcript uuid — a different namespace the Case API
  404s on, and which the env does **not** hold.
- ⇒ For an MCP tool, "the authenticated caller" = the env session id. That makes self-resolution,
  arbitrary-id rejection, and a `whoami` all clean and bounded — **no gateway auth-principal is
  needed** (and none exists: the mesh shares one token, so gateway-side ownership truly cannot be
  enforced; the honest place for the guard is the MCP layer where identity actually exists).
- **Outbox = the A104 agent inbox** (`pending_for`), and `tests/test_a104_single_pending_source.py`
  **fails the build** if a second pending pathway appears. So the "subscribe to outbox" affordance had
  to be a *read over the existing single source*, never a new store/reader.
- `release_worker` already models the ownership-guard pattern — reused its shape conceptually.

## TASK (what shipped)
1. **`open_case` self-resolves + rejects arbitrary (root cause).** `session_id` is now **optional**;
   the owner is resolved from the caller's env via a new `_self_session_id()` helper. An explicitly
   passed id that is **not** the caller's own is **REFUSED** (structured message, no Case opened) —
   the silent mis-owned Case is now impossible. Env-absent (tests/manual) falls back to the passed id.
   Success echo now says *"Opened Case … on YOUR session <id> (self-resolved owner)"*.
2. **`whoami` (all agents, caller-only).** New read tool in **both** `mcp_manager.py` (identity +
   node + current Case, filtered to the caller — a sibling's affiliation row is never surfaced) and
   `mcp_jobs.py` (env-only identity, no network — workers reach the task server, not the control API).
   Takes no session argument: it can only ever report the caller. This is how an agent learns its real
   id instead of guessing/scraping.
3. **`check_inbox` (subscribe-to-outbox, bounded).** New read tool over the ONE pending source: a
   session-keyed `GET /api/agent/inbox` → `orchestrator.agent_inbox_view(session_id)` → `db.pending_for`.
   Returns `resolved` (finished completions waiting for you) + `pending` (your dispatched work still
   running), in the same shape as the Case-keyed `reconcile_worker_waits`. Writes nothing; no second
   pending pathway (guard test stays green).
4. **Consistency / DRY.** `dispatch_worker`'s requester read now uses the same `_self_session_id()`
   helper, so every identity-scoped tool resolves identity one way.

## Reserved decisions / scope out (deliberate boundaries)
- **No gateway-side ownership check on `/api/cases`.** It cannot be done honestly (no per-caller
  principal; one shared mesh token). The MCP-layer guard fully covers the reported agent fumble. The
  existing 404-on-unknown-session is unchanged. Documented here rather than faked.
- **SessionStart-exposed transcript uuid left as-is.** Reconciling the namespaces operationally
  (agent never needs the transcript id: self-resolution + `whoami` surface the correct control-plane
  id) rather than changing SDK-side boot seeding (risky, out of scope).
- **"subscribe" = bounded poll read**, not a push/stream. Live cross-process wake signalling is A105's
  job; a new stream would duplicate it and risk a second pending pathway.
- No change to `close_case` (already self-resolves) beyond the shared mental model now matching.

## ACCEPTANCE (proof)
- `open_case` with no `session_id` + env identity → POST carries the env id; echo says self-resolved.
- `open_case` with a non-caller id → refusal, **no** API call.
- `open_case` with a matching id → accepted (back-compat); with neither → guidance, no crash.
- `whoami` returns the caller's own id/node/current Case and never another session's row; no-identity
  → clear message. Worker `whoami` returns env identity with no network.
- `check_inbox` / `GET /api/agent/inbox` return `resolved`+`pending` for the caller; `session_id`
  required (422 without); unknown session clean; empty id refused.
- `tests/test_a104_single_pending_source.py` green (no second pending pathway introduced).

## TRAIL
- Investigation: two scoped Explore sub-agents (MCP server structure + env identity; gateway
  case-open validation + inbox single-source) — findings summarized above.
- Files touched: `scripts/mcp_manager.py`, `scripts/mcp_jobs.py`, `src/orchestrator.py`,
  `src/control/routes/cases.py`, and the four test files below.

## Milestone checklist
- [x] `open_case` self-resolution + arbitrary-id rejection + validated echo
- [x] `whoami` in manager + worker MCP servers (caller-only)
- [x] `check_inbox` + `GET /api/agent/inbox` over the single `pending_for` source
- [x] Targeted tests green incl. the A104 single-source guard
- [ ] Worker-side `whoami` (mcp_jobs) live — activates on the next worker redeploy (gateway-side is live on gateway restart)

## Closure

### Manager session-identity & case-ownership — SHIPPED (2026-10-11)
**What changed:**
- `scripts/mcp_manager.py` — added `_self_session_id()`; `open_case` self-resolves + rejects a
  non-caller id + validated echo; new `whoami` and `check_inbox` tools (registered + schemas);
  `dispatch_worker` requester read reuses the helper.
- `scripts/mcp_jobs.py` — env-only `whoami` tool (available to workers).
- `src/orchestrator.py` — `agent_inbox_view(session_id)` (read over `db.pending_for`).
- `src/control/routes/cases.py` — `GET /api/agent/inbox` (+ `Query` import).
- Tests: `tests/test_mcp_manager.py`, `tests/test_mcp_jobs.py`, `tests/test_manager_role.py`,
  `tests/test_agent_inbox_delivery.py` (new coverage).

**Verification:** `.venv/bin/python -m pytest tests/test_mcp_manager.py tests/test_mcp_jobs.py
tests/test_case_admission.py tests/test_manager_role.py tests/test_agent_inbox_delivery.py
tests/test_a104_single_pending_source.py -q -n 4` → **168 passed**.

**F-tag outcomes:** bug1 (no self-resolution) → fixed; bug2 (namespace mismatch) → fixed
operationally (whoami surfaces the control-plane id; agent never needs the transcript uuid);
bug3 (arbitrary id accepted) → fixed at the MCP layer (gateway-principal unavailable — documented).

**What follows / continuation plan:** gateway-side changes (`open_case` tool, `whoami`/`check_inbox`
reachable, `/api/agent/inbox`, `agent_inbox_view`) go live on a gateway restart. The worker-side
`whoami` in `mcp_jobs.py` lands only on the next **worker** redeploy (Horse) — operator-gated per
project policy; until then workers keep the old `watch_job`-only server.
