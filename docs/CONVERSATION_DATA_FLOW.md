# Conversation Data Flow — Current State & Migration Path

> Written 2026-06-29 after a full audit of the live system.
> **Updated 2026-06-30: the DB is now the self-sufficient source. See §0.**
> Purpose: canonical reference so anyone opening this repo understands exactly
> where every piece of data comes from, why it is messy, and what to do next.

---

## 0. RESOLVED (2026-06-30) — mesh.db is now self-sufficient

The conversation + artifact data is now canonical in **`mesh_tasks`**, not the files.
The chat is a projection of the task ledger (session-first, task-first — the convo is
a read-model over tasks, not a separate entity).

**What changed:**
- `mesh_tasks` gained artifact-complete columns (migration 17): `prompt`, `reply_text`
  (FULL untruncated assistant reply), `parsed_output_json`, `file_changes_json`,
  `files_modified_json`, `usage_json`, `error_class`, `return_code`.
- `orchestrator._mesh_complete_task` writes them DB-first at completion via
  `db.enrich_task(...)`. The old `result.output[:2000]` cap is gone for the reply.
- `transcript.get_transcript` now reads the DB first (`_turns_from_db`), projecting
  `mesh_tasks` → turns. Falls back to the file-stitching path only for sessions with
  no enriched rows (old/un-backfilled). No file I/O on the hot path.
- `scripts/backfill_conversation_turns.py` backfilled all historical turns from the
  artifact files. **Parity verified: 786 tasks enriched, 509 turns checked, 0 mismatches.**
- `raw_stdout` (the 264 MB / 87% of artifact bytes — pure debug NDJSON, nothing
  product-facing reads it back) is NOT copied into the DB. With `system.slim_artifacts=true`
  it's gzipped to `results/raw/<id>.ndjson.gz` (~10×) and dropped from the JSON.

**Remaining (safe, optional):** flip `slim_artifacts` on, then archive/delete the fat
`results/task_*.json` after a grace window. The app already survives without them for
all enriched sessions.

The §7/§8 standalone-`conversation_turns`-table plan below was **superseded** — we made
the task ledger itself complete instead of adding a parallel turns table (one writer,
one row per turn, no dual-store drift), per the session-first design.

---

## 1. The Problem in One Sentence

There is no single table that stores a conversation. Instead, four separate files/stores
are stitched together at read time every time the web UI asks for messages.

---

## 2. Current Data Sources (all of them)

```
disk/
├── state/
│   ├── sessions/<id>.json          ← PRIMARY session record
│   │   ├── task_history[]          ← ordered list of turns (user+result, truncated*)
│   │   ├── last_result_summary     ← last 400 chars of last result (preview only)
│   │   ├── last_user_message       ← last user instruction (full)
│   │   └── status / backend / ...  ← live session state
│   │
│   └── summaries/<id>.md           ← DEAD. Telegram-era single-turn snapshot.
│                                      Still on disk, no longer read by transcript.py
│
└── results/
    └── task_<id>.json              ← artifact per task (one per turn)
        ├── raw_stdout              ← NDJSON stream from claude-code (THE FULL TEXT)
        │   └── {"type":"result","result":"<full assistant reply>"}  ← canonical
        ├── parsed_output           ← structured summary (backend-dependent schema)
        │   ├── [claude-code]  assistant_text, usage, session_id, ...
        │   └── [codex]        git_diff_stat, tokens, cost, ...  (NO text)
        ├── task.prompt             ← full user instruction (written post-fix)
        ├── task.title              ← truncated display label ("Task: first 50 chars...")
        └── session.session_id      ← links artifact → session

sqlite/
└── mesh.db
    ├── mesh_sessions               ← live session state (mirrors sessions/<id>.json)
    ├── mesh_tasks                  ← task queue (pending/claimed/completed/failed)
    ├── approvals                   ← pending approval requests
    ├── llm_turns                   ← telemetry: token counts, timing (NOT message text)
    └── mesh_jobs                   ← watched background jobs
```

`*` `task_history[].result_summary` was written as `out[-400:]` until 2026-06-29.
    Fixed in orchestrator.py — new turns now store full text.
    Old turns on disk still have 400-char summaries; the artifact is the recovery path.

---

## 3. The Read Path: What the Frontend Actually Gets

```
GET /api/sessions/:id/messages
        │
        ▼
src/control/transcript.py  get_transcript()
        │
        ├─ 1. Read state/sessions/<id>.json → task_history[]
        │       Each entry has: task_id, timestamp, user_message, result_summary
        │
        ├─ 2. Load ALL results/task_<id>.json artifacts for this session
        │       (always, not lazily — needed for full-text recovery of old turns)
        │
        └─ 3. For each history entry → _turn_from_history()
                │
                ├─ instruction = task_history.user_message  (full, always)
                │
                └─ result: pick the LONGER of:
                    a) task_history.result_summary          (may be 400-char truncated)
                    b) artifact raw_stdout NDJSON result line  (full text, priority 1)
                       └─ {"type":"result","result":"<complete reply>"}
                    c) artifact parsed_output via extract_text_from_payload  (fallback)
                    d) "(task failed — no output)" if nothing found
```

**Current state: STABLE for all sessions that have artifact files on disk.**  
Sessions where the artifact file is missing (e.g. worker crashed before writing it)
will show the 400-char summary if they were created before 2026-06-29, or the full
text if created after (new orchestrator writes full text to task_history).

---

## 4. What Each Frontend Screen Uses

| Screen | Data source | Hook | Stable? |
|--------|-------------|------|---------|
| Sessions list | `/api/sessions` → `mesh_sessions` DB | `useSessions()` | ✓ |
| Chat bubbles | `/api/sessions/:id/messages` → transcript.py | `useSessionMessages()` | ✓ (see §3) |
| Optimistic sent msg | `sentStore` (in-memory, lost on reload) | Zustand store | ✓ |
| Approval cards | `/api/approvals` → `approvals` DB table | `useApprovals()` | ✓ |
| Files tab (diffs) | `/api/artifacts` → `results/task_*.json` | `useArtifacts()` | ✓ |
| Info tab (turns) | `/api/turns` → `llm_turns` DB table | `useSessionTurns()` | ✓ |
| System log | `/api/events` → in-memory ring buffer | SSE/poll | ✓ |
| Tasks screen | `/api/tasks?sectioned` → `mesh_tasks` DB | `useTaskSections()` | ✓ |

---

## 5. Why It Is Messy (honest post-mortem)

| Problem | Root cause |
|---------|-----------|
| `result_summary` was 400 chars | Orchestrator sliced `out[-400:]` before saving — designed for Telegram previews, accidentally became the only conversation store |
| Full text buried in raw_stdout | The NDJSON result line was never promoted to a first-class field; you have to parse it out of a 94KB stream |
| Summaries dir abandoned | `state/summaries/<id>.md` was the Telegram-era "last turn" snapshot; the rewrite deprecated it but left 100+ files on disk |
| Two sources for same thing | `mesh_sessions.last_result_summary` (DB) mirrors `sessions/<id>.json` (file) — same data, two stores, either can be stale |
| Artifact-per-turn schema varies | claude-code artifacts have `raw_stdout`+`parsed_output.assistant_text`; codex artifacts have `parsed_output.git_diff_stat` and NO text — different parsers needed |

---

## 6. Is It Fixed Now?

**Yes, for practical purposes:**

- New turns (post 2026-06-29): full text in `task_history.result_summary` AND in artifact. Both paths work.
- Old turns with artifacts on disk: full text recovered from `raw_stdout` NDJSON result line.
- Old turns without artifact files (rare — worker crash before write): 400-char summary shown. No recovery possible without the artifact.

The stitching logic in `transcript.py` is now deterministic and tested. It will not regress silently — the priority order is explicit: raw_stdout result line → parsed_output → task_history summary.

**However, it is still fragile by design.** See §7.

---

## 7. The Right Fix: A `conversation_turns` Table

The stitching should be replaced with a proper append-only table written by the
orchestrator at turn completion. This is straightforward and eliminates all the
fallback chains.

### Proposed schema

```sql
CREATE TABLE conversation_turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,
    task_id       TEXT    NOT NULL UNIQUE,   -- one turn per task
    turn_index    INTEGER NOT NULL,          -- 0-based order within session
    role          TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    text          TEXT    NOT NULL,          -- full untruncated content
    success       BOOLEAN NOT NULL DEFAULT 1,
    timestamp     TEXT    NOT NULL,          -- ISO8601
    files_changed INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_conv_turns_session ON conversation_turns(session_id, turn_index);
```

Two rows per task: one `role='user'` (instruction), one `role='assistant'` (result).

### Write path (orchestrator, on task completion)

```python
# Instead of: session.last_result_summary = out[-400:]
# Do:
db.insert_turn(session_id, task_id, 'user',      instruction, timestamp)
db.insert_turn(session_id, task_id, 'assistant', full_output, timestamp)
```

### Read path (replaces transcript.py entirely)

```sql
SELECT role, text, timestamp, task_id
FROM conversation_turns
WHERE session_id = ?
ORDER BY turn_index ASC
LIMIT ?
```

No file I/O. No NDJSON parsing. No fallback chains. One query.

---

## 8. Handoff Prompt for New Session

> Use this verbatim to start the database migration session.

---

**HANDOFF PROMPT — conversation_turns DB migration**

Context: We have a running multi-agent gateway (Python/FastAPI backend + React PWA frontend).
The codebase is at `C:\Users\Cicada38\Projects\AI-team`.

**The problem:** The chat conversation (user instructions + agent replies) is currently
stored across 3 sources stitched together at read time:
1. `state/sessions/<id>.json` → `task_history[].result_summary` (was 400-char truncated, fixed 2026-06-29)
2. `results/task_<id>.json` → `raw_stdout` NDJSON → `{"type":"result","result":"..."}` (full text, primary recovery)
3. `results/task_<id>.json` → `parsed_output` (fallback, backend-dependent schema)

Read logic lives in `src/control/transcript.py` `get_transcript()`.
API endpoint: `GET /api/sessions/:id/messages` in `src/control/control_api.py`.
Frontend hook: `useSessionMessages()` in `web/src/hooks/useLiveData.ts`.
Frontend rendering: `useSessionTimeline()` in `web/src/hooks/useSessionTimeline.ts`.

**The task:** Add a `conversation_turns` table to `mesh.db` (SQLite, managed by
`src/control/db.py`) and wire it as the canonical conversation store.

Schema in `docs/CONVERSATION_DATA_FLOW.md §7`.

**Steps:**
1. Add `conversation_turns` table to `src/control/db.py` (schema migration, safe to add if not exists).
2. Add `insert_turn(session_id, task_id, role, text, timestamp, files_changed)` and
   `get_turns(session_id, limit)` to `db.py`.
3. In `src/orchestrator.py` around line 1571 (where `task_history.append` happens),
   also write two rows: user turn + assistant turn.
4. Update `src/control/transcript.py` `get_transcript()` to query `conversation_turns`
   first; fall back to the file-stitching logic only when the table has no rows for
   this session (backwards compat for old sessions).
5. Update `GET /api/sessions/:id/messages` in `control_api.py` — no signature change needed,
   the transcript layer handles it transparently.
6. No frontend changes needed — the API contract (`RawTranscriptTurn`) stays identical.

**Do NOT break:** existing sessions that have no rows in `conversation_turns` yet —
they must continue to work via the file fallback in `transcript.py`.

Read `docs/CONVERSATION_DATA_FLOW.md` for the full picture before starting.

---
