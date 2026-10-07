# A82 Stage 8a — LIVE session turn-queue validation

**Date:** 2026-10-07 17:11 FLEDT (13:56–14:00 UTC turn window)
**Target:** LIVE deployed gateway, control API `http://100.88.11.88:9003`
**Deployed state (verified via `/health` + `/api/nodes`):** schema 43 / image
`ai-team:prod-cdef7ec`; managed carriers online = `Horse`, `kanebra`, both
advertising `managed_backends=["claude","codex","opencode-server"]`.
**Carrier used:** `kanebra` (pinned; off the local `Horse` box).
**Method:** real scratch session driven through a two-turn sequence against the
live API — NOT a unit test, NOT a mock. Driver: `scripts/_a82_stage8a_validate.py`
(throwaway; raw log `scripts/_a82_run3.log`, capture JSON `scripts/_a82_stage8a_result.json`).
**Auth:** control-API bearer resolved from the worker's own env/`.env` via
`config.mesh.worker_token` (the live container rejects the locally-stale
`dashboard_token`; `_token_accepted` accepts either — probed, used the accepted
one). Token never hardcoded, never printed (redacted in artifacts).

Endpoints derived from the route modules (no guessing):
`POST /api/sessions`, `POST /api/sessions/{id}/turn-requests` (202 receipt),
`GET /api/sessions/{id}/turn-requests`, `GET /api/turn-requests/{task_id}`,
`POST /api/sessions/{id}/close` (`src/control/routes/{sessions,turn_requests}.py`).

Scratch session: **`669862de646e`** (backend `claude`, pin `kanebra`, repo
`/home/cifran/dev/analyzer` — a real path on the carrier).
Turns: **turn1 = `task_e6e6ded3`** (seq 1), **turn2 = `task_bfe493f0`** (seq 2).

---

## Verdict per property

| # | Property | Verdict |
|---|----------|---------|
| 1 | 2nd message to a BUSY session → **HTTP 202**, **distinct** turn id, **queued** (no reject, no clobber of the running turn) | **PASS** |
| 2 | While busy: turn1 holds the active slot (seq 1), turn2 sits **queued behind it** (seq 2) | **PASS** |
| 3 | After turn1 reaches a terminal result, turn2 **activates FIFO** and is delivered **serially to the SAME session** | **PASS** |
| 4 | turn2 resumes the **SAME native backend session** (resume, not create) | **NOT PROVEN** — backend-layer failure on the carrier (see below); queue layer did its part correctly |

**Overall: the A82 born-managed turn queue works end-to-end at the admission /
scheduling / FIFO-delivery layer (properties 1–3 PASS).** Property 4 (native
backend resume) could not be observed because BOTH turns failed inside the
**Claude backend `initialize` on the kanebra carrier** (`Control request
timeout: initialize`), so no native session id was ever established to resume.
This is a carrier/backend failure, NOT an A82 turn-queue defect — the queue
admitted, ordered, FIFO-activated, serially delivered, and committed **honest
`failed` terminals** (no false success).

---

## Captured output (verbatim, redacted)

### Property 1 — 202 + distinct id + queued, no clobber
turn1 submit (session idle):
```json
{"turn_id": "task_e6e6ded3", "task_id": "task_e6e6ded3", "status": "queued", "revision": 1, "queue_sequence": 1, "queue_position": 1, "accepted_at": "2026-10-07T13:56:08.253422+00:00", "idempotent_replay": false, "source": "operator", "sender_session_id": null}
```
`POST /api/sessions/669862de646e/turn-requests -> 202`

turn2 submit **while turn1 is BUSY** (sent ~1s later, turn1 already active):
```json
{"turn_id": "task_bfe493f0", "task_id": "task_bfe493f0", "status": "queued", "revision": 1, "queue_sequence": 2, "queue_position": 2, "accepted_at": "2026-10-07T13:56:13.896228+00:00", "idempotent_replay": false, "source": "operator", "sender_session_id": null}
```
`POST /api/sessions/669862de646e/turn-requests -> 202`

→ **202**, turn id `task_bfe493f0` is **distinct** from turn1 `task_e6e6ded3`,
status **queued**, `idempotent_replay:false`. Turn1 was NOT rejected,
overwritten, or flipped to BUSY/ERROR. ✅

### Property 2 — queue state while busy (turn1 active, turn2 queued behind)
`GET /api/sessions/669862de646e/turn-requests -> 200`:
```json
{"turns": [
  {"id": "task_e6e6ded3", "status": "pending", "queue_sequence": 1, "queue_position": 1, "activated_at": "2026-10-07T13:56:12.855556+00:00", "started_at": null, "turn_source": "human", "turn_kind": "instruction"},
  {"id": "task_bfe493f0", "status": "queued", "queue_sequence": 2, "queue_position": 2, "activated_at": null, "started_at": null, "turn_source": "human", "turn_kind": "instruction"}
]}
```
→ turn1 = `pending` (activated, holds the single active slot, seq 1); turn2 =
`queued` behind it (seq 2). One active slot per session enforced. ✅

### Property 3 — FIFO activation + serial same-session delivery
Lifecycle timestamps (UTC), from `GET /api/turn-requests/{id}` polls:

| turn | seq | created_at | activated_at | started_at | terminal |
|------|-----|-----------|--------------|-----------|----------|
| `task_e6e6ded3` (turn1) | 1 | 13:56:08 | 13:56:12 | 13:56:38 | **failed**, completed_at 13:58:20 |
| `task_bfe493f0` (turn2) | 2 | 13:56:13 | **13:59:23** | **13:59:51** | **failed** |

→ turn2 did not activate (13:59:23) until **after** turn1 was terminal
(completed 13:58:20); turn2 started (13:59:51) strictly after. Both ran on the
**same** gateway session `669862de646e`, serially, in acceptance order (seq 1
then seq 2). FIFO + same-session delivery confirmed. Driver flag
`t2_ever_activated=True`; final poll `both terminal: t1=failed t2=failed`.
Queue drained afterward (`count:0, queued:0, active_turn_id:null`). ✅

### Property 4 — native backend resume (NOT PROVEN — backend failure)
Session record after the sequence (`GET /api/sessions`):
```
status= closed | backend_session_id= '' | last_backend_session_id= '' | last_task_id= task_bfe493f0 | last_summary= 'Control request timeout: initialize'
```
→ `backend_session_id` is EMPTY — the Claude backend on `kanebra` never
completed `initialize` (timeout), so no native session was created and there was
nothing for turn2 to resume. Resume could not be observed. The queue layer still
behaved correctly: turn2 was delivered to the same session only after turn1
terminal (property 3), and both failures were committed as honest `failed`
terminals with `effects_state=done` (no false success).

`last_task_id=task_bfe493f0` confirms turn2 (seq 2) was the last turn delivered
to this session — consistent with FIFO serial delivery.

---

## Cleanup
Scratch session `669862de646e` **closed** (idempotent `POST .../close -> 200`,
`session.status=closed`). Queue drained (no queued/active rows). No test litter
left on the live gateway.

## Caveats / honesty notes
- The live `/api/sessions` list endpoint intermittently **timed out** (status
  -1) while the carrier was busy running turns — a read-path latency issue under
  load, not an admission/queue failure. The lighter per-session
  `/api/sessions/{id}/turn-requests` endpoint stayed responsive throughout and
  carried the authoritative queue state.
- The backend `initialize` timeout on `kanebra` is reproducible across runs
  (run on a nonexistent path failed with a repo error; run on a real path failed
  with `Control request timeout: initialize`). Root cause is on the kanebra
  carrier's Claude backend; **out of scope** for this A82 queue validation and
  explicitly a reserved (no kanebra-side ops) area — surfaced here for the
  operator. A82's queue mechanics are proven independent of it.
- Properties 1–3 are what A82 Stage 8a's "born-managed turn queue" claim is
  about (admission commits a protocol-1 row, 2nd message 202-queued with a
  distinct id, FIFO activation to the same session). Those are PASS on the LIVE
  schema-43 / prod-cdef7ec gateway.
