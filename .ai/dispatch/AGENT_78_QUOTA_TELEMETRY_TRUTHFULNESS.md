```yaml
job_id: AGENT_78_QUOTA_TELEMETRY_TRUTHFULNESS
created_at: "2026-08-17T21:10:00+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null
evidence: []
updated_at: "2026-08-17T21:10:00+00:00"
```

# DISPATCH — A78 · Quota telemetry that is actually trustworthy (windows, exhaustion, restore)

**Level:** 2 (observability correctness + bounded reads; NO paid provider calls beyond the
existing `claude get_usage` shell-out the coordinator already performs) · **Type:** fix + verify
**Authored:** 2026-08-17 · **Status of this packet:** ready (authored, not executed)
**Depends on:** — (A61 built the coordinator, A63 audits it; this fixes what the quota-resume work
found while USING it)

> **Read this first — why this packet exists.** The Case quota-resume feature (branch
> `feat/case-respawn-approval-gate`, merged 2026-08-17) makes a real decision off this telemetry:
> it PAUSES a Manager Case when a turn dies on `usage_limit` and proposes a resume when the window
> reopens. Building it surfaced concrete defects in the telemetry layer itself, each verified
> against the live store on this host. The resume feature is deliberately written to degrade
> honestly around them (it falls back to the reset instant the provider attaches to its own 429,
> and treats missing telemetry as "ask the operator" rather than "wait forever"). This job removes
> the need for those fallbacks.

## Why (intent)
"When does the window reopen, and is it open now?" is now load-bearing: it decides when a paused
Case is offered back to the operator. Today that question is answered by an observer that is
asleep for hours at a time, a readiness flag that can never be true, and a history table that grows
without bound. None of that is dishonest by accident — it was built observe-only — but it is no
longer only observed.

## FINDINGS TO FIX (each re-derive independently; do not trust this list at face value)

1. **`automation_ready` is structurally unreachable.**
   `_window_state_for` (`src/services/quota_window_coordinator.py`) sets
   `active_session_state = "unknown"` as a LOCAL LITERAL, then adds the blocker
   `active_session_state_unknown` and requires `active_session_state == "false"` for
   `automation_ready`. The adapter DOES detect it — `_observe_adapter` computes `active_label` from
   `detect_active_user_session()` — but only writes it into the event payload, never into the
   snapshot/window state. So `automation_ready` is False for every bucket, forever, and any future
   automation gated on it is dead code. Decide and implement: thread the observed value through
   (snapshot column or bucket field), or delete the flag and its blocker as unimplementable.
   Verified live 2026-08-17: all five buckets report `automation_ready: false` with
   `active_session_state_unknown` present.

2. **The observer sleeps through most of the window, so `telemetry_state` is usually `stale`.**
   `next_observe_delay_sec` returns "seconds until 15 min before the next reset" (capped at
   `observe_max_interval_sec`, default 6h) whenever the last reading was under the limit. Measured
   live 2026-08-17: last observation `14:20:41Z`, queried at `17:11Z` — every claude bucket
   `telemetry_state: stale`, `used_percent` nearly 3 hours old. Consequence: any consumer that
   (reasonably) requires `telemetry_state == "current"` sees a blind instrument most of the time.
   The cadence is intentional (do not spam the provider) — the fix is not "poll more", it is to make
   staleness *legible and bounded*: state explicitly which decisions may use a stale reading (a
   spent reading is still meaningful until its own `reset_at` passes; a healthy one is not evidence
   of anything an hour later), and expose `age_seconds` so consumers can decide.

3. **A 429 is the most accurate quota signal we get, and it is thrown away.**
   Every refusal carries `rate_limit_event.rate_limit_info.resetsAt` (+ `rateLimitType`), i.e. this
   account's exact window boundary, at the exact moment it was hit — no polling, no cost. Nothing
   writes it into the quota store. Feed it in (a snapshot with `limit_reached=1`, quality labelled
   as event-derived rather than observed) so the store learns the true boundary the instant it is
   crossed. `TaskOrchestrator._rate_limit_reset_iso` already parses it for the Case pause record —
   reuse, do not re-implement. This also gives `_reset_history` real anchored evidence instead of
   inferring boundaries from polls.

4. **Retention: the snapshot table grows forever.**
   Live count 2026-08-17: 53,540 rows (13,383 per polled bucket) and rising, with `_reset_history`
   doing a FULL per-bucket scan on every `status()` call and returning the entire
   `reset_boundary_evidence` list in the API response. Add retention (keep boundary-changing rows +
   a bounded recent window) and/or bound what the endpoint returns. See #5 for why this matters.

5. **(Already fixed here — verify it stayed fixed, then build on it.)**
   `QuotaWindowStore.status()` selected the latest snapshot per bucket with a correlated subquery
   that had no usable index, re-scanning the whole table per row: ~2.8 BILLION row reads at 53k
   rows, measured at **>120 s** on the gateway host. Replaced by `latest_snapshots()` (one indexed
   `LIMIT 1` per key): **0.02 s**, with `status()` now 0.11 s. The per-tick Case quota gate uses
   `latest_snapshots()` deliberately. Add a regression guard (a seeded-volume timing or query-plan
   assertion) so this cannot silently return.

6. **Ground-truth check: does `limit_reached` actually fire?**
   The resume trigger treats `limit_reached` OR `used_percent >= 100` as "spent". Nobody has
   confirmed against a REAL exhaustion that `claude get_usage` reports `limit_reached=1` (rather
   than, say, 98% with a refusal already happening). Capture the next real 429 — the gateway now
   logs `event=case_quota_paused` with the reset time, and the pause record carries the provider's
   own `resetsAt` — and compare it against what the store said in that same minute. Write the
   comparison down; if they disagree, the trigger keys on the wrong field.

## Anti-goals
- Do NOT increase polling frequency to "fix" staleness (§2) — the cadence back-off exists on
  purpose and the provider must not be spammed.
- Do NOT make the Case quota-resume path depend on `automation_ready` — it is a readiness flag for
  a different (future) automation, and today it is unreachable (§1).
- Do NOT delete history that is the only evidence of a window boundary (§4) — retention must keep
  boundary-changing rows.
- No new provider calls beyond the existing `claude get_usage` shell-out.

## Definition of done
- Each finding above either FIXED or explicitly refuted with the re-derived evidence.
- `/api/quota-windows` answers in well under a second on a full-size store, with a regression guard.
- The honest answer to "is the window spent, and when does it reopen?" is available from ONE place
  (`latest_snapshots` + the 429-derived rows), with its age and provenance attached.
- Targeted `pytest tests/test_quota_window_coordinator.py tests/test_case_quota_resume.py` green.
- A short note in `.ai/CONTEXT.md` shift notes stating what became trustworthy and what did not.

## Evidence to attach on closure
- Before/after timings for `status()` on a full-size store.
- The live comparison from §6 (real 429 vs what the store reported that minute).
- The decision on §1 (threaded through vs deleted) with the reasoning.
