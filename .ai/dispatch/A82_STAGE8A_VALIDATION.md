# A82 Stage-8a Live End-to-End Validation

**Verdict:** PASS  
**Probe date:** 2026-10-07T15:20–15:21 UTC  
**Carrier node:** `kanebra`  
**Schema version:** 43 (confirmed in image `ai-team:prod-cdef7ec`, main @ `9dade9c`)  
**Gateway:** http://127.0.0.1:9003 (mesh_degraded=False, MESH_ENABLED=True)  
**Executed by:** worker session on kanebra (this session = task_577826f8, action=create_session)

---

## What Was Proved

The session turn-queue **accepts a second turn submitted to a busy/active managed session
and delivers it FIFO after the first turn finishes**, end-to-end, against the live gateway.

Specific assertions from the acceptance spec — all PASS:

| # | Assertion | Result |
|---|-----------|--------|
| 1 | Session born-managed (`enrolled: true`) without explicit `/enroll` call | PASS |
| 2 | Turn 1 accepted: HTTP 202, distinct turn_id | PASS — `task_05f1a9e3`, 202 |
| 3 | Turn 1 reaches ACTIVE slot (`active_turn_id` set) | PASS — activated at 15:21:04.768 |
| 4 | Turn 2 submitted while Turn 1 active: HTTP 202, distinct turn_id | PASS — `task_7a1fb926`, 202 |
| 5 | Turn 2 ID ≠ Turn 1 ID | PASS — `task_05f1a9e3` ≠ `task_7a1fb926` |
| 6 | Turn 1 NOT interrupted after Turn 2 submit | PASS — still active_turn_id=task_05f1a9e3, status=pending |
| 7 | FIFO: Turn 2 activated only AFTER Turn 1 completed | PASS — T1 completed 15:21:13; T2 activated 15:21:16 |
| 8 | Turn 2 ran via `resume_session` (same session, not a new create) | PASS — action=resume_session |
| 9 | Both turns status=completed, effects_state=done | PASS |
| 10 | Session closed (no junk pinned to carrier) | PASS — status=closed |

**What a FAIL would have looked like:**
- Turn 2 returning 409 (BUSY-reject / clobber) instead of 202
- Turn 2 having the same turn_id as Turn 1 (overwrite)
- Turn 2 creating a new backend session (`create_session`) instead of resuming
- Turn 2 activating before Turn 1 completed (FIFO violated)
- Turn 1 status changing to cancelled/stopped after Turn 2 was submitted (interruption)

---

## Exact Commands

```bash
# Environment
TOKEN="${DASHBOARD_TOKEN}"  # 64-char token from .env (in-process env, not printed)

# 1. Create scratch session on kanebra (managed carrier)
curl -s -X POST http://127.0.0.1:9003/api/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"backend":"claude","repo_path":"/home/cifran/dev/AI-team","node_id":"kanebra"}'
# -> HTTP 200, session_id=13c4a3019ba7, status=idle, machine_id=kanebra

# 2. Verify born-managed enrollment
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:9003/api/sessions/13c4a3019ba7/turn-requests"
# -> HTTP 200, enrolled=true (no explicit /enroll needed — born-managed)

# 3. Submit Turn 1
curl -s -X POST http://127.0.0.1:9003/api/sessions/13c4a3019ba7/turn-requests \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body":"Count from 1 to 20, writing each number on its own line. Be thorough.",
       "operation_id":"tq-proof-t1-<uuid>"}'
# -> HTTP 202  (see raw response below)

# 4. Poll until turn 1 appears in active slot (~3s)
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:9003/api/sessions/13c4a3019ba7/turn-requests"
# -> active_turn_id=task_05f1a9e3, turn1.status=pending, activated_at set

# 5. Submit Turn 2 (while Turn 1 holds active slot)
curl -s -X POST http://127.0.0.1:9003/api/sessions/13c4a3019ba7/turn-requests \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body":"Reply with exactly this text and nothing else: FIFO-CONFIRMED-T2",
       "operation_id":"tq-proof-t2-<uuid>"}'
# -> HTTP 202  (see raw response below)

# 6. Verify FIFO completion
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:9003/api/turn-requests/task_05f1a9e3"
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:9003/api/turn-requests/task_7a1fb926"

# 7. Verify resume_session action (not create_session) on task_7a1fb926
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:9003/api/tasks?session_id=13c4a3019ba7" | \
  python3 -c "import sys,json; [print(t['id'],t['action']) for t in json.load(sys.stdin)['tasks'][:3]]"
# -> task_7a1fb926 resume_session
# -> task_05f1a9e3 create_session

# 8. Close scratch session
curl -s -X POST http://127.0.0.1:9003/api/sessions/13c4a3019ba7/close \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'
# -> HTTP 200, status=closed
```

---

## Raw Captured Responses

### 1. Session Create (HTTP 200)
```json
{
  "ok": true,
  "reason": "",
  "session": {
    "session_id": "13c4a3019ba7",
    "backend": "claude",
    "repo_path": "/home/cifran/dev/AI-team",
    "status": "idle",
    "machine_id": "kanebra",
    "backend_session_id": "",
    "model": null,
    "effort": null,
    "default_model": "opus",
    "last_task_id": "",
    "is_active": true,
    "origin_channel": "web",
    "origin_kind": "user",
    "updated_at": "2026-10-07T15:20:23.191195+00:00"
  }
}
```

### 2. Enrollment Check (HTTP 200) — `enrolled: true` without explicit /enroll
```json
{
  "turns": [],
  "count": 0,
  "queued": 0,
  "active_turn_id": null,
  "active_status": null,
  "next_cursor": null,
  "enrolled": true,
  "paused": false,
  "hold": null,
  "effects_failed": 0,
  "effects_failed_turn_id": null
}
```

### 3. Turn 1 Submit — HTTP 202
```json
{
  "turn_id": "task_05f1a9e3",
  "task_id": "task_05f1a9e3",
  "status": "queued",
  "revision": 1,
  "queue_sequence": 1,
  "queue_position": 1,
  "accepted_at": "2026-10-07T15:21:04.734839+00:00",
  "idempotent_replay": false,
  "source": "operator",
  "sender_session_id": null
}
```

### 4. Queue State When Turn 2 Was Submitted (Turn 1 Holds Active Slot)
```json
{
  "turns": [
    {
      "id": "task_05f1a9e3",
      "turn_id": "task_05f1a9e3",
      "session_id": "13c4a3019ba7",
      "status": "pending",
      "revision": 1,
      "queue_sequence": 1,
      "queue_position": 1,
      "turn_source": "human",
      "turn_kind": "instruction",
      "blocked_reason": null,
      "created_at": "2026-10-07T15:21:04.734839+00:00",
      "activated_at": "2026-10-07T15:21:04.768762+00:00",
      "started_at": null,
      "preview": "Count from 1 to 20, writing each number on its own line. Be thorough."
    }
  ],
  "count": 1,
  "queued": 0,
  "active_turn_id": "task_05f1a9e3",
  "active_status": "pending",
  "enrolled": true,
  "paused": false
}
```
Note: `active_turn_id=task_05f1a9e3` — Turn 1 owns the slot; Turn 2 not yet submitted.

### 5. Turn 2 Submit — HTTP 202 (submitted while Turn 1 active)
```json
{
  "turn_id": "task_7a1fb926",
  "task_id": "task_7a1fb926",
  "status": "queued",
  "revision": 1,
  "queue_sequence": 2,
  "queue_position": 2,
  "accepted_at": "2026-10-07T15:21:07.833772+00:00",
  "idempotent_replay": false,
  "source": "operator",
  "sender_session_id": null
}
```

### 6. Queue After Turn 2 Submit (Turn 1 active, Turn 2 enqueued)
```json
{
  "turns": [
    {
      "id": "task_05f1a9e3",
      "status": "pending",
      "queue_sequence": 1,
      "queue_position": 1,
      "activated_at": "2026-10-07T15:21:04.768762+00:00",
      "started_at": null,
      "preview": "Count from 1 to 20, writing each number on its own line. Be thorough."
    },
    {
      "id": "task_7a1fb926",
      "status": "queued",
      "queue_sequence": 2,
      "queue_position": 2,
      "activated_at": null,
      "started_at": null,
      "preview": "Reply with exactly this text and nothing else: FIFO-CONFIRMED-T2"
    }
  ],
  "count": 2,
  "queued": 1,
  "active_turn_id": "task_05f1a9e3",
  "active_status": "pending",
  "enrolled": true
}
```
Turn 1 owns active slot (`active_turn_id`). Turn 2 is queued at position 2. Turn 1 NOT interrupted.

### 7. Turn 1 Final Detail (HTTP 200)
```json
{
  "id": "task_05f1a9e3",
  "turn_id": "task_05f1a9e3",
  "session_id": "13c4a3019ba7",
  "status": "completed",
  "revision": 1,
  "queue_sequence": 1,
  "queue_position": null,
  "turn_source": "human",
  "turn_kind": "instruction",
  "blocked_reason": null,
  "created_at": "2026-10-07T15:21:04.734839+00:00",
  "activated_at": "2026-10-07T15:21:04.768762+00:00",
  "started_at": "2026-10-07T15:21:10.109966+00:00",
  "completed_at": "2026-10-07T15:21:13.454527+00:00",
  "body": "Count from 1 to 20, writing each number on its own line. Be thorough.",
  "effects_state": "done",
  "effects_error": null
}
```

### 8. Turn 2 Final Detail (HTTP 200)
```json
{
  "id": "task_7a1fb926",
  "turn_id": "task_7a1fb926",
  "session_id": "13c4a3019ba7",
  "status": "completed",
  "revision": 1,
  "queue_sequence": 2,
  "queue_position": null,
  "turn_source": "human",
  "turn_kind": "instruction",
  "blocked_reason": null,
  "created_at": "2026-10-07T15:21:07.833772+00:00",
  "activated_at": "2026-10-07T15:21:16.892923+00:00",
  "started_at": "2026-10-07T15:21:17.136564+00:00",
  "completed_at": "2026-10-07T15:21:18.598864+00:00",
  "body": "Reply with exactly this text and nothing else: FIFO-CONFIRMED-T2",
  "effects_state": "done",
  "effects_error": null
}
```

### 9. Task Actions Proving Resume (from /api/tasks?session_id=13c4a3019ba7)
```
task_7a1fb926  action=resume_session  status=completed  started=2026-10-07T15:21:17.136564
task_05f1a9e3  action=create_session  status=completed  started=2026-10-07T15:21:10.109966
```
Turn 1 used `create_session` (first turn, new backend session).  
Turn 2 used `resume_session` (FIFO head-only activation on the SAME session). ✅

### 10. Session Final State After Both Turns Complete
```json
{
  "session_id": "13c4a3019ba7",
  "status": "awaiting_input",
  "machine_id": "kanebra",
  "backend_session_id": "fb64eb31-932a-487f-92d5-896a49e6901e",
  "last_task_id": "task_7a1fb926",
  "last_summary": "FIFO-CONFIRMED-T2",
  "turn_queue": {
    "queued": 0,
    "active_turn_id": null,
    "active_status": null,
    "paused": false,
    "hold": null
  }
}
```
`last_summary: "FIFO-CONFIRMED-T2"` — the session executed Turn 2's instruction.  
`last_task_id: task_7a1fb926` — the last executed turn was Turn 2 (FIFO confirmed).  
`backend_session_id` shared across both turns — same Claude session reused.

---

## FIFO Timeline Summary

```
15:21:04.734  Turn1 created (queue_seq=1)        submitted via POST /api/sessions/.../turn-requests
15:21:04.768  Turn1 activated (active slot held)  scheduler picked up Turn1 in <35ms
15:21:07.833  Turn2 created (queue_seq=2)        submitted WHILE Turn1 holds active slot
              => Turn2 status=queued, position=2; Turn1 NOT interrupted; active_turn_id still=Turn1
15:21:10.109  Turn1 started on carrier (kanebra)  action=create_session
15:21:13.454  Turn1 completed                     ~3.3s execution
15:21:16.892  Turn2 activated                     ONLY AFTER Turn1 completed (+3.4s gap)
15:21:17.136  Turn2 started on carrier (kanebra)  action=resume_session (same session!)
15:21:18.598  Turn2 completed                     ~1.5s execution
```

FIFO invariant: Turn2.activated_at (15:21:16) > Turn1.completed_at (15:21:13) ✅  
Head-only: Turn2 was NOT activated while Turn1 held the active slot ✅  
Resume: Turn2 action=`resume_session`, not `create_session` ✅

---

## Environment State Notes

- `TURN_QUEUE_ENROLLMENT_ENABLED: False` — explicit /enroll API disabled; has no effect on
  born-managed sessions (Stage 8a: every INSERT sets `turn_queue_enrolled=1`).
- `MESH_ENABLED: True` — mesh on; carrier-required check passes.
- `WORKER_MANAGED_TURNS=1` on kanebra worker — kanebra registers `managed_backends=["claude","codex","opencode-server"]`.
- `coverage_ok: false` at time of test — pre-existing unrelated issue: one session
  pinned to `kanebra-worker` (legacy worker, no managed_backends); unrelated to this proof.
- Session `13c4a3019ba7` fully closed at 15:21:47 UTC (status=closed, no carrier slot held).
