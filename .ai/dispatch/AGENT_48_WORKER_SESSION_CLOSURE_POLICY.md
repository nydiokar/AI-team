# AGENT_48 — Worker-session closure is a Manager decision, not automatic

**Dispatched:** 2026-07-17
**Level:** 3 (behavior/policy correction; flag-safe; build on tests)
**Branch:** `feat/manager-decided-worker-close` (code ⇒ PR at close, do NOT merge)
**Supersedes the premise of:** PR #22 (`_close_worker_session_on_case_close`) — see below.

## Why (operator directive 2026-07-17 — reverses an earlier assumption)
PR #22 auto-closes joined worker sessions when their Case closes. **This is the wrong default.**
Closing a worker session tears down the warm backend process and its cache; if the Manager (or
operator) later wants to re-open a question to that worker, the reopen is a **cold resume = burned
tokens**, and any in-context state is lost. The Manager must be able to **keep a worker warm** for
follow-up and **decide itself** when a worker is truly done and may be closed.

(PR #22 is currently also *inert* for real dispatched workers — they aren't session-linked, A47 — so
this correction is timely before A47 makes that scan non-empty and the auto-close starts firing.)

## Intent (ground before building)
Read `src/orchestrator.py::close_case` + `_close_worker_session_on_case_close` +
`_clear_session_case_affiliation`, and the Manager decision surface (`manager_v1` tools in
`scripts/mcp_manager.py`, role prompt `docs/harness/roles/manager.md`).

## Objective
Closure of a worker session becomes an explicit Manager decision; the default is to leave workers
**warm and reusable**.
1. **Neutralize the auto-close on Case-close** — on `close_case`, still CLEAR the worker session's
   Case affiliation (`current_case_id`→NULL so it isn't dangling on a closed Case), but **do NOT close
   the session process.** The worker becomes a free, warm session. Gate or remove
   `_close_worker_session_on_case_close` so it is never the automatic path.
2. **Add a Manager-driven close** — a `manager_v1` tool/decision (e.g. `release_worker` /
   `close_worker_session`) the Manager calls when it decides a specific worker is done. Instruct it in
   `manager.md`: keep workers warm for possible rework/follow-up; close a worker only when you have
   decided it is finished — never reflexively, never as a side-effect of closing the Case.
3. Preserve the ability to **re-dialogue a warm worker** — the Manager can send a follow-up turn to an
   existing worker session (reuse `session_id`) rather than spawning a fresh one (which re-pays a cold
   boot). Confirm `dispatch_worker(session_id=...)` reuses without re-opening.
4. Tests: `close_case` clears affiliation but does NOT close worker sessions by default; the explicit
   Manager close tool closes exactly the named session; a warm worker can take a second turn.

## Completion criteria (ONE string)
close_case clears a worker session's Case affiliation but no longer closes the session process by default (auto-close neutralized/gated); a Manager-driven close tool/decision closes exactly the named worker session on explicit request; a warm (post-Case) worker session can accept a follow-up turn without a cold re-open; manager.md instructs keep-warm + close-only-by-decision; plain-pytest tests cover default-no-close, explicit-close, and warm-reuse and pass; one feat branch + PR opened (NOT merged).

## Live log
- **2026-07-17 — BUILT via live Manager loop → PR #26 (OPEN, not merged).** Case `62bb3a…`, dispatched
  after A47 accepted; `review.accepted`. `scripts/mcp_manager.py` (+47) adds a `release_worker`
  Manager-driven close tool; `src/orchestrator.py` (+17) neutralizes the auto-close (clear affiliation,
  leave warm); `docs/harness/roles/manager.md` (+9) keep-warm-until-decided instruction; tests in
  `test_case_closure.py`/`test_mcp_manager.py`. **NOT deployed** — the two warm worker sessions from
  this run (`157c1d0eac95`, `8033ec60ecb3`) still carry `current_case_id=62bb3a` (closed) as dangling
  affiliation; #26's clear-on-close lands for future cases after merge + restart.
