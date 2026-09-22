```yaml
job_id: AGENT_83_SESSION_STATE_LEGIBILITY
created_at: "2026-09-22T00:00:00+00:00"
status: ready
owner: ""
depends_on: []
results_ref: null
evidence: []
updated_at: "2026-09-22T00:00:00+00:00"
```

# DISPATCH — A83 · Session state legibility (primary state + secondary reason)

---

**Level:** 2 (read-model / API projection + web tag; no state-machine or schema change) · **Type:** code
**Authored:** 2026-09-22 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — (independent; complements A80 heartbeat, reuses its read primitives)
**Branch:** `feat/session-state-legibility` + PR + self-merge (touches `src/` + `web/`)

> **Read this first — why this packet exists.** The status "pillow" for a waiting session is a
> single blunt value (`AWAITING_INPUT`) that hides *what* the session waits on. Two live incidents
> forced this: a worker running a **foreground** script showed as "running" while effectively just
> waiting (it never detached via `watch_job`), and a **Manager appeared to be waiting while nothing
> was actually pending** in the ledger (looked like a healthy wait; may have been stuck/hallucinated).
> After this job, the operator can read, at a glance, whether a session is working, waiting on
> workers, waiting on a script, paused (quota/retry), idle, or an **open-case-idle** (nothing pending
> — the stuck/hallucination tell). The design is locked in `docs/SESSION_WAIT_STATE_GRANULARITY.md`.

## Why (intent)
Make session state **legible** by attaching a derived, non-authoritative **secondary reason** to
the existing **primary** `SessionStatus`, computed on the read path, surfaced in the UI. It is
purely additive and **UI-only for now** — no control action keys off it in v1. It is deliberately
shaped as *system-derived truth* so a later phase can **triangulate** it against agent belief to
catch stuck/hallucinated sessions; do NOT wire that now. The full design, vocabulary, bounded-read
rules, and a completed adversarial review are in `docs/SESSION_WAIT_STATE_GRANULARITY.md` — **read
it first and treat it as the spec.**

## TASK
1. **Derivation helper.** Add a bounded, batched `SessionReason` derivation (see spec §3–§4):
   `{kind, confidence, detail}`. Compute per session on read; **BUSY/terminal short-circuit to
   empty with zero DB reads.** Verify collection with `pytest -q tests/test_session_reason*.py`.
2. **Vocabulary (spec §4), evaluated in priority order for `AWAITING_INPUT`/`IDLE`:**
   `paused_quota` (`db.case_quota_pause(case_id)` non-None) → `paused_retry`
   (`db.transient_pause(case_id)` non-None) → `waiting_workers` (manager + unresolved wait-group;
   **reuse** the existing bounded scan in `orchestrator._cache_heartbeat_owner_live`
   `case_wait_group` branch — do NOT add a new full-log scan) → `waiting_job` (a `jobs` row for
   this `session_id` is `running`, via `db.list_jobs_for_sessions`) → `open_case_idle` (joined to an
   OPEN Case, none of the above; **confidence medium**; the stuck/hallucinated-wait tell) → `idle`
   (no Case, nothing pending). `BUSY` → empty. `PINNED_NODE_OFFLINE`/`PAUSED_PINNED_NODE_OFFLINE` →
   `node_offline` + `detail=<node_id>`. `ERROR`/`CANCELLED`/`CLOSED` → empty.
3. **Bounded read (spec §5 — safety-critical).** Batch across the listed page:
   ONE `list_jobs_for_sessions(ids)`; role/case straight off the loaded session rows; pause + wait
   reads only for the managers on the page, watermark-gated with `db.max_flow_event_ids`. **No N+1,
   no cross-session materialization, and NEVER move this into a background loop/timer** (that is the
   #145/#147 event-loop stall — repeat that as a code comment). Add a test asserting no-N+1.
4. **Attach to read-model + API.** Add the field to `SessionView` (`src/core/view_models.py`) and
   the timeline item (`src/core/session_timeline.py`); populate on `/api/sessions` and
   `/api/sessions/{id}/timeline` (`src/control/control_api.py`) reusing the existing DB handle.
   `needs_input`/`is_active` formulas stay byte-identical. Verify: `python -c "import
   src.control.control_api"` import smoke + targeted pytest; `curl http://127.0.0.1:9003/health`.
5. **Web tag.** Render the richer pillow ("waiting · on workers / on a script / paused: quota /
   idle", "open case · nothing pending", "held for node <id>"). `tsc -b` / typecheck + `pnpm build`
   clean. **Do NOT deploy** (`web/dist` rebuild + gateway restart are operator-gated — surface it).
6. **Tests.** Full primary×reason truth table with fake `MeshDB`, incl. quota/retry pause,
   `open_case_idle` for BOTH manager and worker, racy/stale (ledger written between reads), BUSY→
   empty, node-offline detail, and the no-N+1 assertion. No paid CLI.

## TYPE
code. Branch `feat/session-state-legibility`; PR at close; self-merge per branch policy. Web
rebuild/deploy is NOT part of the merge — surface it to the operator.

## CONTEXT (reuse verbatim)
- **Spec (authoritative for this job):** `docs/SESSION_WAIT_STATE_GRANULARITY.md`.
- **Verified seams (checked 2026-09-22):** `SessionStatus` `src/core/interfaces.py:149`;
  `Session.case_role`/`current_case_id` `interfaces.py:215-216`; `case_quota_pause`
  `src/control/db.py:3905`, `transient_pause` `:3945` (both `Optional[dict]`, non-None ⇒ paused);
  `list_jobs_for_sessions` `db.py:4710` (N+1-safe, carries `orphaned`); `max_flow_event_ids`
  `db.py:3166`; wait-group scan pattern in `orchestrator._cache_heartbeat_owner_live` `:1296`;
  heartbeat gate keys on `== AWAITING_INPUT` `orchestrator.py:1381`; API `/api/sessions`
  `control_api.py:~1246`; `SessionView` `src/core/view_models.py`.
- **Hard rule:** enum is the authority and is UNTOUCHED. The reason is session-scoped and derived; a
  Case is read only as *evidence of what the session did* — the Case is never an authority (this is
  session-state management, not case-state management).

## ACCEPTANCE (proof, not vibes)
1. Truth-table pytest green (fake backend + real `MeshDB`), covering every row in TASK §6.
2. No-N+1 test: deriving reasons for a page of N sessions issues one `list_jobs_for_sessions` and
   bounded per-manager reads — asserted by call-count/spy, not vibes.
3. `/api/sessions` returns the reason field; `needs_input`/`is_active` unchanged (regression test).
4. Web typecheck + `pnpm build` clean; the pillow renders each kind (screenshot or component test).
5. `curl http://127.0.0.1:9003/health` OK against the running gateway after the backend change.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — Node grace-remaining in the `node_offline` detail.** Surface `node_id` for sure; include
  grace-remaining only if it is a cheap existing read. If not, omit it — do NOT add a new query.
- **R2 — Telegram / cost-view surfacing.** Out of scope for v1 (session tag + timeline only).
  Default: don't build it; leave a follow-up note if the operator wants it.
- **R3 — Foreseen `running · <elapsed>` value on BUSY.** Not v1. The mechanism must ALLOW adding a
  value later without rework, but v1 ships BUSY→empty.

## SCOPE OUT
- **No `SessionStatus` enum change, no split of `AWAITING_INPUT`, no migration, no schema change.**
- **No background loop / timer / scheduler** computing the reason — read-path only.
- **No control action** keyed off the reason (no nudge, no Wake-Dispatcher wiring, no auto-close).
  Triangulation is a designed-for future phase, explicitly NOT built here.
- **No BUSY sub-reason** fabricated from silence (unrecoverable at the state layer — spec §4, §8).
- **No web deploy / gateway restart** as part of this job (operator-gated).

## TRAIL / EVIDENCE (fill at close)
- Branch/PR, test paths (`tests/test_session_reason*.py`), web component/build evidence, health probe.

---
## Milestone (burndown)
- [ ] `SessionReason` derivation helper (bounded, batched, watermark-gated, `confidence`)
- [ ] Vocabulary + priority order per spec §4 (incl. `open_case_idle` manager & worker)
- [ ] Attached to `SessionView` + timeline item; `needs_input`/`is_active` byte-identical
- [ ] Populated on `/api/sessions` + `/api/sessions/{id}/timeline`, batched, no N+1
- [ ] Web pillow renders each kind; typecheck + `pnpm build` clean (NOT deployed)
- [ ] Full truth-table + no-N+1 + regression tests green; health probe OK
- [ ] Vigilance: no enum/schema/loop change; Case never treated as authority (self-audit noted)

## Closure (fill on completion)
_(fill per `docs/harness/generators/closure_summary.md`: per-file changes, verification commands +
results, what follows, and the explicit confirmation that the change is additive/read-only and
wired to no control action.)_
