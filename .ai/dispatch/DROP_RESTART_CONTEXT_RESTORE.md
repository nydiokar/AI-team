# DROP — Auto-inject session context on worker-restart recovery

**Raised:** 2026-07-23 (operator, live incident debrief)
**Priority:** MEDIUM — UX/continuity; no data loss, no correctness defect
**Level:** 2 (additive enhancement; flag-gated; no new schema)
**Owner:** unassigned
**Status:** ✅ **BUILT 2026-07-23** — see Resolution below

---

## What happened (the live incident)

Worker restarted at 10:30:43Z mid-task on session `eabd930f0de9`. DB-backed conversation
history was intact (UI showed it correctly). The next task (`task_7bcab4c7`) did
`action=create_session` — fresh SDK subprocess, no context. Claude started blind.

## Root cause

When a worker restarts with a new incarnation, all SDK sessions on that node get
`driver_status = 'lost'` (via `db.mark_driver_sessions_lost_for_node`). The next task for those
sessions cannot resume the prior subprocess and starts fresh via `create_session`. The session's
conversation history lives in `mesh_tasks` (DB-canonical since migration 17) but nothing injects
it into the new Claude Code process.

## Scope of work

Add `_maybe_inject_restart_recovery_context(task, session)` in `orchestrator.py`. When
`session.driver_status == 'lost'` and the session has prior completed turns, prepend a bounded
`<prior_context source="restart-recovery">` block (last N turns, hard char-capped) to the task
prompt before `_mesh_enqueue_task` freezes it into the remote payload.

**Trigger condition (all must hold):**
- `session.driver_status == 'lost'` + `session.backend_session_id` non-empty
- ≥1 completed turn in `mesh_tasks` for this session (`reply_text IS NOT NULL, status='completed'`)
- Most recent completed turn ≤24h old (stale dormant sessions: skip)
- `RESTART_CONTEXT_RESTORE_ENABLED=true` (default OFF → byte-identical)

**Caps (non-negotiable — a single Claude turn can be 50k chars):**
- Max 3 turn pairs
- Per-turn reply truncated at 1 500 chars
- Total block hard-capped at 4 000 chars

**DB helper needed:** `get_session_turns_tail(session_id, limit)` — `ORDER BY created_at DESC
LIMIT ?` then reversed. The existing `get_session_turns` orders ASC from the front; using it
would give the *oldest* turns, not the most recent.

**TODO(A50-tier2) in `_maybe_inject_restart_recovery_context`:** desired future state is to
spawn a Haiku-class agent to summarize the full session history (last ~10 turns → ≤500 word
prose summary) instead of raw turn injection — more token-efficient for long sessions, more
semantically dense. Not built now; comment in the code keeps it discoverable.

## Acceptance criteria

- [ ] Flag OFF (default): zero behavior change on any existing path
- [ ] Flag ON + `driver_status='lost'` + prior turns: `event=restart_context_injected` in logs;
      task prompt contains `<prior_context source="restart-recovery">` before the instruction
- [ ] Normal session (driver='live'): no injection, no log entry
- [ ] Char caps respected: reply truncated at 1 500, total block at 4 000
- [ ] Age gate: session with most recent turn >24h old → no injection
- [ ] Idempotent: double-call on same task.id → single injection
- [ ] Targeted pytest green (no full suite)

## Files

| File | Change |
|---|---|
| `config/settings.py` | + `RESTART_CONTEXT_RESTORE_ENABLED: bool = False` |
| `docs/ENV_FEATURE_FLAGS.md` | + flag entry |
| `src/control/db.py` | + `get_session_turns_tail(session_id, limit)` |
| `src/orchestrator.py` | + `_maybe_inject_restart_recovery_context` + call site |
| `tests/test_restart_context_restore.py` | hermetic unit tests |

---

## Resolution (2026-07-23, branch `feat/restart-context-restore`, PR #39)

**Built and tested 2026-07-23.**

- `src/control/db.py` — `get_session_turns_tail(session_id, limit)`: DESC + reverse, filters
  `status='completed' AND reply_text IS NOT NULL`; excludes the interrupted partial turn.
- `src/orchestrator.py` — `_maybe_inject_restart_recovery_context(task)` + `_db_get_session_turns_tail`
  sync wrapper; call site added before `_maybe_inject_compact_context` in `_task_worker`. Class
  constants `_RESTART_CTX_TURN_LIMIT=3`, `_RESTART_CTX_PER_TURN_CHARS=1_500`,
  `_RESTART_CTX_TOTAL_CHARS=4_000`, `_RESTART_CTX_MAX_AGE_HOURS=24`. `TODO(A50-tier2)` comment
  documents the Haiku-summariser upgrade path in place.
- `docs/ENV_FEATURE_FLAGS.md` — flag entry added.
- `tests/test_restart_context_restore.py` — 16 hermetic tests: all passing.
- Compact-context + driver regression suites: 74/74 clean.

**Live activation:** set `RESTART_CONTEXT_RESTORE_ENABLED=true` in `.env` + gateway/worker restart.
Operator merge + restart gated per branch policy.
