# DROP — Manager Telemetry & Work Cockpit

**Date:** 2026-07-22 · **Branch:** `feat/manager-telemetry-cockpit`

## The incident that motivated this (root-caused, evidence-backed)

A live Manager (session `c301e89d0e49`, Case `00d317e8…`) reported dispatching "workers"
**via `mcp__jobs__watch_job → claude -p --model …`**, not via `mcp__manager__dispatch_worker`.
Verified from the session transcript and `get_case` (Case open, only a `manager` link, **zero
worker `flow_links`**).

**Root cause — a capability gap, not a lie:**
- `mcp__manager__dispatch_worker` has **no `model` parameter** (verified against its live schema);
  a worker inherits the gateway default model.
- The operator's objective explicitly demanded **per-job model tiering** ("not all of them with the
  same opus model … spawn the agent with the model necessary to handle the complexity").
- `watch_job` runs an **arbitrary command**, so `claude -p --model X` is the *only* instrument the
  Manager had that lets it choose a model per job. It reached for it rationally.

**Consequence (why the operator is blind):** those `claude -p` processes are rows in the `jobs`
table (`pid/pgid/status/log_path/session_id`) — **not** sessions, Cases, or `flow_links`. So they
carry no worker profile, no token telemetry, no Case linkage, and are invisible to the Work/Cases
UI. Meanwhile `WorkDetailScreen` is a static ledger of IDs (header + lineage + ledger + append-only
timeline) — it never answers *"who is doing what right now, on what, for how many tokens, and are
any scripts still running / orphaned."*

## Goals

- **G1 — Right instrument.** `dispatch_worker` gains per-worker **model** selection → removes the
  reason to misuse `watch_job` as an agent-spawner.
- **G2 — Legible misuse.** A `watch_job` whose command invokes an agent CLI (`claude …`) is flagged
  `is_agent_spawn` in the read model, so even a fallback to the old pattern is *visible*, never hidden.
- **G3 — The cockpit.** Per Case, an operational view: the **live roster** (manager + worker
  sessions with role, model, cumulative tokens, turn count, status, last activity) **and running
  scripts** (jobs: command summary, duration, status, orphaned/lost/agent-spawn flags), on top of the
  Case **activity spine** (the existing `flow_events` timeline) so the operator sees *sequence +
  what's live now*.

## Non-goals (explicit)

- Manual Case open/close from the UI — operator said this is **not** the point. Deferred.
- Building our own sub-agent framework.
- `effort` per-worker tiering — no create-time entry point exists (`CreateSessionBody` has no
  `effort`); **deferred to a follow-up**, `model` is the operative lever.
- Quota-kill as a distinct signal — **not a detectable state** (a quota-killed `claude -p` surfaces
  as a normal `failed` with a nonzero exit + log tail). We surface `failed`/`lost`/`orphaned`
  honestly and do **not** fabricate a "quota" flag.

## Corrections folded in from adversarial review

1. **Model routes through the session-create seam, NOT `/api/instructions`.** `CreateSessionBody.model`
   already exists and flows `create_session → session model → ClaudeAgentOptions(model=…)`.
   `/api/instructions` has no model field and would drop it.
2. **Reused warm workers pin their boot model** (the SDK client is cached and only rebooted on an
   `effort` change, not a `model` change). Per-worker model tiering therefore applies to **newly
   opened** worker sessions only; a reused `session_id` keeps its boot model. Documented in the tool
   description + `manager.md`, not silently.
3. **Orphan detection reads worker-maintained `jobs.status`** (`lost`/`failed`) + the existing
   `orphaned` flag from `list_jobs`. The T3 process probe is `os.kill`-based and worker-side only —
   the control read path must never shell out or block on a probe (§7 timeout).
4. **Token aggregate must join `llm_model_requests → llm_turns` with `is_duplicate = 0`** (authoritative
   per-request token columns live one table deeper than `llm_turns`, and dupes double-count). One
   batched `GROUP BY session_id … WHERE session_id IN (…)` query — no N+1.
5. **Case→jobs join goes through the sessions.** Jobs carry the `SESSION_ID` env of the process that
   registered them; the Manager's `claude -p` jobs are stamped with the **manager** session_id. Roster
   join: `case → flow_links(entity_type='session') → {manager, worker session_ids} → jobs by those
   session_ids`. **Known blind spots (stated, not hidden):** legacy `task`-linked one-off workers and
   `unowned` jobs (no resolvable session) do not appear in a per-case roster.
6. **UX:** the Case `flow_events` timeline is the **spine**; the live roster is its "now" head —
   not a flat roster burying a demoted timeline.

## Plan — two coherent PRs on this branch

### PR-1 — Root-cause dispatch fix (backend + docs)  ← build first, independently shippable
- `scripts/mcp_manager.py`: add optional `model` to the `dispatch_worker` input schema + put it in
  the `POST /api/sessions` body (`sess_body`). Tool description gains the reused-session caveat.
- `docs/harness/roles/manager.md`: `dispatch_worker` now tiers models per job; **`watch_job` is for
  long-running non-agent scripts only — never to spawn agents.**
- `docs/ENV_FEATURE_FLAGS.md`: note the model-tiering capability.
- Tests: plain `pytest` on `mcp_manager` dispatch body + `control_api` create-session model pass-through.

### PR-2 — The cockpit (backend read-model + frontend)
- `src/control/db.py`: `get_session_token_totals(session_ids) -> {session_id: {input,output,cache,…}}`
  (batched, `is_duplicate = 0`).
- `src/control/work_read_model.py`: `build_case_roster(...)` assembling sessions[] (role, status,
  model, tokens, turn_count, last_activity) + jobs[] (command_summary, status, started_epoch→duration,
  orphaned, is_agent_spawn).
- `src/control/control_api.py`: `GET /api/work/{case_id}/roster` — **auth-guarded**, registered
  **before** the `/api/work/{flow_run_id}` catch-all.
- Frontend: `api.workRoster` + `useWorkRoster` + adapter; `WorkDetailScreen` gains a **Live** section
  (roster + running scripts) above the ledger, timeline kept as the prominent spine.
- Tests: `pytest` on the token query + roster builder + endpoint; `pnpm` typecheck/build.

## Verification & seams

- Backend: plain `pytest` on touched modules only (no full/e2e suite — TEST COST GUARD).
- Cross-layer trace (model): `dispatch_worker(model) → sess_body → CreateSessionBody.model →
  create_session → session row model → ClaudeAgentOptions(model)`.
- Cross-layer trace (roster): `case → flow_links(session) → get_session_token_totals + list_jobs`.
- **Live proof is operator-gated** — a running Manager only picks up the new MCP schema after its
  session/MCP restarts; gateway restart + merge are the operator's call. This DROP ships the code +
  green targeted tests; it does not restart infrastructure.
