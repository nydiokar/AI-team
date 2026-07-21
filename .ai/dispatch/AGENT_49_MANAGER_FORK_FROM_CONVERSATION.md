# AGENT_49 — Fork a prior conversation into a Manager session (role + tools + prior context)

**Dispatched:** 2026-07-21
**Level:** 3 (control-API + orchestrator seam; flag-free but default-None ⇒ byte-identical; build on tests)
**Branch:** `feat/manager-fork-from-conversation` (code ⇒ PR at close, do NOT merge/restart — operator's call)

## Why (operator ask, grounded in code — not memory)
The operator asked: *"if I spawn the Manager into another node, will it have tools to read a
specific session's history and autonomously continue the line of work?"* Grounded against the
code, the honest answer was **no on two axes**, and the second one had to be fixed:

1. **The Manager tool surface has no session-history reader.** `scripts/mcp_manager.py` exposes
   exactly seven tools (`dispatch_worker`, `wait_for_worker`, `get_case`, `open_case`,
   `close_case`, `record_review`, `release_worker`). `get_case` reads Case status/stage/criteria/
   objective; nothing reads a prior session's turns. Continuity was only ever achieved by **warm
   session resume** (same `backend_session_id`), never by seeding a *fresh* session from a prior one.
2. **The Manager boot seam could not be forked at all.** `/api/manager` → `invoke_manager`
   (`src/orchestrator.py`) was the ONLY session-creating entry point that did **not** accept a
   fork seed, even though the two proven seams already existed elsewhere:
   - `create_session(continued_from=…)` — session→session lineage (`session_service.py:105`).
   - the compact-context injector `_maybe_inject_compact_context` — prepends a bounded, fenced,
     reference-only `<prior_context>` block from `continue_inline` (a marked-message digest) or
     `continues:` (a prior task_id) (`src/orchestrator.py`).
   So you could boot a Manager with only a bare `objective` string; you could not fork it from the
   conversation that decided the work needed doing.

## Root cause
Feature-composition gap, not a missing capability: the fork seams (`continued_from` /
`continue_inline` / `continues`) were wired into `/api/sessions` + `/api/instructions` but never
threaded through the Manager boot path. The Manager role prompt + scoped worker-dispatch tools are
set on a **different axis** (`_role_boot` → `ClaudeAgentOptions.system_prompt` + `allowed_tools`,
off `session.case_role`), so seeding prior context on the *prompt* axis composes cleanly with the
role boot — they do not conflict.

## The fix (minimal, reuse-first)
Thread the three existing seams through the Manager boot path:
1. `ManagerInvokeBody` (`src/control/control_api.py`) — add `continued_from`,
   `continue_inline` (`Field(max_length=8000)`, same cap as `/api/instructions`), `continues`.
2. `api_manager` — forward all three to `invoke_manager`.
3. `invoke_manager` (`src/orchestrator.py`) — pass `continued_from` to `create_session`; build the
   first-turn `extra_metadata` (`continue_inline` preferred over `continues`, matching the
   injector's own precedence) and pass it to `submit_instruction`. Absent all three ⇒
   `extra_metadata=None`, `continued_from=None` ⇒ **byte-identical legacy boot.**

Net effect: a Manager booted via `/api/manager` with a fork seed wakes with **(a)** its role prompt,
**(b)** its worker-dispatch MCP tools, and **(c)** the prior line of work fenced in its first turn —
then autonomously decides whether/what to dispatch. No new gateway state, no new flag, no new tool.

### Adversarial-review finding → FIXED (cross-layer inertness on the node path)
An adversarial pass refuted the first cut for the operator's *exact* scenario (a **node-pinned**
Manager). The compact-context injector ran inside `process_task`, which executes **after**
`_mesh_enqueue_task` froze `payload["prompt"]` into the remote row (`_task_worker`:
enqueue at ~L2999, `process_task` at ~L3007; injection was at ~L3244). A node worker executes the
frozen prompt and **never runs the injector itself** → role + tools travelled (PR #18's
`_session_dispatch_payload` carries `case_role`/`role_boot`), but the **prior context was silently
dropped on every remote dispatch.** Fix: run `_maybe_inject_compact_context(task)` **before**
`_mesh_enqueue_task` in `_task_worker`. It is idempotent (once-guard on `task.id`), so the existing
in-`process_task` call is a safe no-op and a no-seed task is untouched (byte-identical). This also
repairs the same latent inertness for any remote `continues:`/`continue_inline` task, not just the
Manager.

## Verification (plain pytest — no paid CLI, per the test-cost guard)
- `tests/test_manager_role.py` — **27 pass** (18 prior + 9 new): thread `continued_from` →
  `create_session`; `continue_inline`/`continues` → first-turn `extra_metadata`; inline-precedence;
  no-seed byte-identical (incl. blank/whitespace); `/api/manager` forwards all three; 8000-char cap
  → 422; end-to-end injector rewrites the boot prompt with `<prior_context>`; and the **remote
  regression** — a node-pinned session's mesh `payload["prompt"]` snapshots the INJECTED prompt.
- No regressions: `test_fork_inline_context` + `test_control_api_fork` + `test_manager_carrier_role`
  (19), `test_manager_loop_integration` (4, faked backend), `test_compact_context_injection` +
  `test_mesh_enqueue_affinity` (14). **All green.**

## Completion criteria (ONE string)
`/api/manager` and `invoke_manager` accept `continued_from`/`continue_inline`/`continues` and thread them so a forked Manager boots with its role prompt + worker-dispatch tools + a bounded fenced `<prior_context>` first turn on BOTH the local and node-pinned paths (injection runs before the mesh payload snapshot); all three default None ⇒ byte-identical legacy boot; plain-pytest covers the thread-through, precedence, byte-identity, the 8000-char cap, the end-to-end prompt rewrite, and the remote-payload regression, and passes; one feat branch + PR opened (NOT merged/restarted).

## Deferred (written trace, per §7 / least-action)
- **Session-history reader tool (not built).** A read-only `get_session_timeline`/history MCP tool
  over the existing `/api/sessions/{id}/timeline` would let a Manager *pull* a prior session's turns
  on demand (vs being seeded at boot). Out of scope for this ask; the seed-at-boot path delivers the
  requested "fork a prior conversation" capability using bounded, proven, fence-hardened seams
  rather than an unbounded history dump (which would also be a context-bloat / DoS surface — see §7).
- **`continues:` artifact path-traversal (pre-existing).** `_ContextLoader._load_artifact` joins
  the task_id into `results/{task_id}.json` unsanitized; a `../..` value is a bounded read primitive
  (must exist, parse to a dict, output clamped by SUMMARY/PROMPT/4000-char caps). Pre-existing on
  the `.task.md` / `submit_instruction` callers; this diff adds one operator-authenticated caller.
  Worth a follow-up sanitize; not a blocker for this change.

## Live log
- **2026-07-21 — BUILT + adversarially reviewed + fixed on `feat/manager-fork-from-conversation`.**
  Diff: `control_api.py` (+13), `orchestrator.py` (+~35 incl. the reorder), `test_manager_role.py`
  (+~170). All targeted suites green (64 tests across the touched surface). **PR to open at close;
  merge + gateway restart are operator-gated (branch policy).** Live paid proof of the full
  `/api/manager` boot (needs `MANAGER_ROLE_ENABLED` + `~/.claude.json` + a gateway restart on this
  code) remains operator-gated exactly like every prior Manager PR — the in-process faked
  integration + the remote-payload regression prove the seam end-to-end without spending tokens or
  touching the live gateway.
