# DROP — Timezone: one clock, native local time, everywhere (kill the ambiguity for good)

**Raised:** 2026-07-13 (operator, live incident debrief)
**Priority:** HIGH — recurring source of false diagnosis and operator distrust
**Level:** 2 (code fix + data audit; no new architecture)
**Owner:** unassigned (operator will spawn)
**Status:** ✅ **FIXED 2026-07-13 — PR #20** (`feat/native-time-one-clock`). See "Resolution" at bottom.

---

## Operator directive (verbatim intent)

> Straighten up the time. Everything that shows time must be **native (local) time**,
> everywhere, consistently. The operator must NOT have to mentally add +3h. This must be
> resolved **once and for all** and never again be cited as a cause of a bug. Timezone is a
> data-hygiene defect to eliminate — it is **not** an explanation to reach for.

**Hard rule going forward:** "timezone" is banned as a hand-wave root cause. If a time looks
wrong, it is a *writer/render* defect to fix here, not a UTC-offset to explain away.

## Root defect (verified in code + DB, 2026-07-13)

Timestamps are written in **mixed representations** across writers:

- `src/services/session_service.py:57,58,96` → `datetime.now().isoformat()`
  — **naive LOCAL** (EEST/UTC+3), no tz suffix. Writes `sessions.created_at`/`updated_at`.
- `src/control/db.py` task/flow/heartbeat writers → `datetime.now(timezone.utc).isoformat()`
  — **UTC-aware** (`+00:00` suffix).

Observed corruption in live DB:
- Manager session `6cae2407a5ee`: `created_at="2026-07-13T13:15:42"` (naive local) but
  `updated_at="2026-07-13T11:26:34+00:00"` (UTC) → `created` LOOKS 1h49m AFTER `updated`.
- Same boot in `mesh_tasks`: `2026-07-13T10:15:42+00:00` (UTC) — i.e. the session row is +3h.
- Our session `60fb97b9c163`: both fields naive local.

Net: a single entity carries two clocks. Any "time ago" render mixing these fields is wrong.

## What the operator saw (and what it was NOT)

Operator saw a dead manager session render **"1 minute ago"**. That was investigated and is
**NOT** a timezone artifact (a 3h skew shows a ~3h error, not "1 min"). The likely real cause
is a genuine recent write/reattach event (gateway restart at 11:26:33 UTC touched the session).
→ **Do not close the "1 minute ago" question as timezone.** It is tracked separately (see the
restart/reattach finding + DROP_DISPATCH_WORKER_REAL_SESSION.md). This drop is ONLY about making
every clock native and consistent so the display is trustworthy while that is chased.

## Scope of work

1. **Decide the storage convention and enforce ONE.** Recommended: store UTC-aware ISO
   (`datetime.now(timezone.utc)`) in the DB *everywhere* (single source of truth), and convert
   to **native local time at the render boundary only** (API serialization / Web UI). Rationale:
   naive-local storage is what created this mess; UTC storage + local render is the standard fix.
   — If operator prefers native-local storage, that is acceptable ONLY if EVERY writer switches
   together and every reader is tz-aware. No mixed state.
2. **Fix the writers:** replace all naive `datetime.now().isoformat()` in
   `session_service.py` (and audit the whole tree: `grep -rn "datetime.now()" src/`) with the
   chosen convention. Add a single helper (e.g. `now_iso()`) so no call site chooses its own clock.
3. **Fix the readers/render:** the Web UI + any API field that shows a time must render in the
   operator's local zone with an explicit, unambiguous format. No bare UTC shown to the operator.
4. **Backfill / tolerate legacy rows:** existing rows have mixed tz. Parser must treat a naive
   timestamp as... (decide: local, since that is what session_service wrote) and normalize, OR
   run a one-shot migration to rewrite naive rows to UTC-aware. Document the choice.
5. **Regression test:** a test that asserts a round-trip (write → read → render) yields the
   correct local wall-clock, and that `created_at <= updated_at` always holds for a session.

## Acceptance criteria

- [ ] Exactly one clock convention in the DB; `grep -rn "datetime.now()" src/` returns no
      naive-local timestamp writers (or all are behind the single helper).
- [ ] Every operator-facing time renders in native local time, unambiguously.
- [ ] `created_at <= updated_at` holds for all new session rows.
- [ ] A test locks the round-trip so this cannot regress.
- [ ] CONTEXT.md note added: "timezone is standardized; do not cite tz as a root cause."

## Files

- `src/services/session_service.py` (naive writers — primary)
- `src/control/db.py` (already UTC; the convention reference)
- Web UI time render (`web/` — find the "time ago"/timestamp component)
- Control API serializers that emit timestamps

---

## Resolution (2026-07-13, PR #20 `feat/native-time-one-clock`)

**Corrected the drop's file pointer:** the naive-local session writers are in
**`src/services/session_store.py`** (`created_at`/`updated_at`), not
`session_service.py`. Full audit (`grep -rn "datetime.now()" src/`) found ~46 naive
writers: `session_store` (3), `task_parser` (1), `observability` (1),
`orchestrator` (41) — plus latent naive-vs-aware `TypeError`s in the Telegram
`_relative_age`/`_heartbeat_age`/node-age helpers (swallowed to a raw ISO string).

**Fix — one clock behind one helper:**
- New `src/core/timeutil.py`: `now_iso()` (the single UTC-aware writer) and
  `parse_iso()` (returns tz-aware; legacy naive rows read as LOCAL so any
  subtraction against an aware `now` never raises).
- Every naive writer routed through `now_iso()` (storage is UTC-aware everywhere).
- Telegram render/age helpers use `parse_iso` + an aware `now`, and render absolute
  timestamps in the operator's LOCAL zone (`astimezone`), never bare UTC.
- `db.py` already used a UTC-aware `_now()` + tz-normalizing `_parse_dt` and
  intentionally avoids `src.core` imports, so it keeps its own (identical-convention)
  helper — no naive-local writers remain in the tree.

**Legacy tolerance:** old naive-local session rows stay correct — both JS `new Date()`
and `parse_iso()` interpret a naive value as local. No path feeds session `created_at`
into db.py's heartbeat parsers (those touch only always-UTC db-written fields).

### Acceptance criteria status
- [x] Exactly one clock convention; no naive-local timestamp writers (all behind `now_iso()`).
- [x] Every operator-facing time renders native-local (Telegram `astimezone`; web already local).
- [x] `created_at <= updated_at` holds for new session rows (test-locked).
- [x] `tests/test_timeutil_native_time.py` locks the round-trip against regression.
- [x] CONTEXT.md note added ("timezone is standardized; do not cite tz as a root cause").
