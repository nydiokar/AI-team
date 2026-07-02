# Adversarial Review — T1 Web Push, as built

**Reviews:** the shipped T1 code (not the dispatch). Read critically against the
actual implementation on `feat/operator-signal`.
**Date:** 2026-07-03
**Verdict:** Two real bugs (one would break the *second* notification in prod),
one hardening gap, plus minor notes. Fixed inline; re-verified.

---

## Findings

### B1 (BUG — would break the 2nd push) — `vapid_claims` dict reuse

`push_service._send_blocking` passed a freshly-built `{"sub": subject}` each call,
which is correct — BUT the original risk class is real and worth pinning: pywebpush
**mutates** the `vapid_claims` dict it's given (it injects `aud` and an `exp`
expiry). If that dict were ever module- or instance-level (a tempting
"optimization"), the second send would reuse a now-*expired* `exp` and pywebpush
raises `VapidException: "Vapid exp claim already expired"`. This is the same
"works once, breaks on message #2" failure mode as the transcript-overlay bug.

**Action:** keep the dict construction strictly **inside** `_send_blocking` (per
call), and add an explicit `aud`-free comment so no one hoists it. Also set `exp`
ourselves is unnecessary — pywebpush fills it — but the dict must be per-call.
Verified: it is per-call. Hardened the comment. **No functional change needed but
locked against regression.**

### B2 (BUG — memory) — body read before the size cap

`api_push_subscribe` did `raw = await request.body()` and *then* checked
`len(raw) > max_bytes`. `request.body()` buffers the **entire** payload into
memory first, so a hostile multi-MB/GB body is fully read before rejection — the
cap does not protect memory, only DB writes. 

**Action:** pre-check the `Content-Length` header and reject `413` **before**
reading the body. Keep the post-read length check as a belt-and-suspenders guard
for chunked/absent-Content-Length requests. **Fixed.**

### B3 (HARDENING) — poisoned subscription row retries forever

`_send_blocking` indexes `sub["endpoint"]`/`sub["p256dh_key"]`/`sub["auth_key"]`
with `[]`. A malformed row (missing key from a future migration/manual edit)
raises `KeyError`, classified by `_handle_send_error` as a transient error
(status None) → `mark_push_error`, never disabled → retried on every outcome.

**Action:** treat a `KeyError`/`ValueError` in payload assembly as a permanent
(malformed) subscription and disable it, same as a 410. **Fixed.**

### N1 (NOTE — accepted) — `payload_too_large` UI copy

The frontend `apiClient.post` surfaces the backend `reason`. A `413` from an
oversized subscribe is developer-facing only (the SW builds the body; a user can't
trigger it). No user copy needed. Accepted.

### N2 (NOTE — accepted) — no rate limit on subscribe

`/api/push/subscribe` is auth-gated (Bearer DASHBOARD_TOKEN) and idempotent by
endpoint, so a flood only upserts existing rows. Global control-API rate limiting
is a separate, out-of-scope concern. Accepted.

### N3 (NOTE — verified good) — availability gating

`push_available` requires VAPID configured AND `pywebpush` importable AND a DB.
`GET /api/push/status` returns `available:false` + `vapid_public_key:""` when not
configured, and the frontend `PushSetting` renders nothing in that state. No dead
control, no fabricated capability. Verified good.

### N4 (NOTE — verified good) — non-blocking wiring

`_maybe_push_outcome` schedules via `loop.create_task` and returns; the outcome
path never awaits the fan-out. If there's no running loop (sync context) it logs
and skips. The fan-out itself bounds concurrency (Semaphore) and per-send time
(`asyncio.wait_for`). A stuck OS thread from a hung `to_thread` send can delay
*interpreter teardown* but never *task completion* (the loop is long-lived in
prod). Verified good.

---

## Fixes applied

- **B2:** `Content-Length` header pre-check in `api_push_subscribe` (reject before
  buffering); kept the post-read guard.
- **B3:** `_send_blocking` raises a typed `_MalformedSubscription` on missing keys;
  `_handle_send_error` disables on that (permanent), not `mark_push_error`.
- **B1:** locked the per-call `vapid_claims` construction with a regression comment.

## Re-verification

- `tests/test_push_notifications.py` extended: oversized `Content-Length` rejected
  pre-read; malformed-row subscription disabled (not retried). All pass.
- Backend targeted suite green; `cd web && npx tsc -b` clean; vitest green.
