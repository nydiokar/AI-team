# DISPATCH — A61 · Quota Window Coordinator: honest audit + finalization to a working observer

**Level:** 3 (real provider telemetry on a paid, shared account) · **Type:** audit + code
**Authored:** 2026-07-31 · **Status of this packet:** ready (authored, not executed)
**Depends on:** N1 salvage (**MERGED** PR #47, `3d9af82`) — the *scaffolding* is on `main` behind `QUOTA_COORDINATOR_ENABLED`.
**Supersedes the premise of:** A58 (which said "just flip the flag + observe one cycle"). **A58 is premature** —
see the gap table: with the merged code, flipping the flag observes *nothing real* (all adapters are `Unsupported`
placeholders). Do **not** run A58's activation step until this job lands a real adapter.

> **Read this first — why this packet exists.** PR #47 was merged as if the quota coordinator were done. It is
> **not.** It landed the skeleton (models + store + observe loop) with **placeholder adapters that never read a
> real quota**, no classification, and a read endpoint whose existence the two spec docs *disagree on*. Turning it
> on today writes rows that all say "unsupported." This job is the honest reconciliation: **compare merged code to
> the spec, state exactly what's missing, and finalize it into something that actually observes the Claude quota
> window** — plus a **separate** operator-facing Telegram digest so Nyd can watch it for a few days. Ground every
> claim in the spec (`docs/SESSION_WINDOW_WARMING_SPEC.md`) and the merged code before writing anything.

---

## Why (intent)
The single recurring failure that halts live Manager+worker runs is the shared-account **session/quota limit**
("resets 4:40pm"). The coordinator's whole reason to exist is to **observe** that window honestly (and, only much
later and only if a bucket is *proven* first-use-anchored, optionally warm it). The merged code cannot do the
first thing yet. This job makes the **observe** product real and gives the operator eyes on it.

## Spec-vs-reality gap (VERIFY each row against the tree before trusting it)

| Spec (`SESSION_WINDOW_WARMING_SPEC.md`) | Merged reality (PR #47) | Status |
|---|---|---|
| §8 real provider `QuotaAdapter` for Claude (observe via status-line rate-limit JSON) | `build_default_quota_adapters()` returns **`UnsupportedQuotaAdapter("claude", …not_validated_phase1)`** — reads nothing | ❌ **MISSING — the core sensor** |
| §17 step 5 "implement observe-only Claude adapter using status-line/CLI telemetry" | Not done; placeholder only | ❌ MISSING |
| §17 step 4 observe-only Codex adapter | Placeholder `Unsupported("codex")` | ❌ MISSING (Codex telemetry unknowns unresolved — spec §6 + Phase-1 "Unresolved Codex" list) |
| §15 / Phase-1 doc: read-only `GET /api/quota-windows` | Phase-1 doc claims it's wired via `src/control/dashboard.py`; the audit did **not** find it in `control_api.py`. **The two docs disagree.** | ⚠️ **VERIFY — may be absent or half-wired** |
| §7 window classification protocol (3-cycle anchored/fixed/sliding test) | Excluded (Phase-1 says so intentionally) | ⛔ deliberately deferred (fine for now) |
| §3 MANUAL_ACTIVATE / AUTO_ACTIVATE + §12 isolated activation env | Excluded intentionally | ⛔ deferred (do NOT build here) |
| §9/§9A `principal_hash`, cross-node shared lock (mesh DB) | Store has `principals` table; adapters produce no real principal (all unsupported) | ❌ effectively absent until a real adapter runs |
| Observe cadence | Fixed `QUOTA_OBSERVE_INTERVAL_SEC=300`, polls every 5 min unconditionally | ⚠️ **operator flagged this inline in `QUOTA_WINDOW_COORDINATOR_PHASE1.md:45`** — must stop/slow polling once a window is confirmed; don't spam providers |
| §15 "must not write Telegram" | Correctly does not | ✅ (keep it that way — see notifier design below) |

**Bottom line:** what's real = a sqlite store + an observe loop + an event envelope. What's missing = **anything that
reads an actual quota.** The subsystem is ~1 layer of 4 short of doing its job.

## Desired end state (what it should actually do and look like)
Per spec §0 / §3 / §18, the baseline product is **observation**, not warming:
1. A **real Claude observe-only adapter** that reads the Claude Code **status-line rate-limit JSON**
   (`rate_limits.five_hour.{used_percentage,resets_at}`, `rate_limits.seven_day.*`) **without spending a token**
   (the status-line read does not consume API tokens — spec §6 "Claude Code With A Claude Subscription"). It must
   produce a stable `principal_hash` (spec §9A priority order; operator-configured key like `claude-max-nyd` is an
   acceptable fallback) and honestly report `telemetry_quality` (AUTHORITATIVE only when the fields are actually
   present; UNSUPPORTED/UNAVAILABLE otherwise — never fake it).
2. **Honest window rows** in `state/quota_windows.db`: real `used_percent` + `reset_at` for the live account, with
   `window_semantics=UNKNOWN` (classification is NOT this job — do not assert ANCHORED).
3. A **verified** read surface `GET /api/quota-windows` (reconcile the doc contradiction: find where it is or wire
   it once, bearer-protected read-only, matching the Phase-1 response shape).
4. **Adaptive cadence** (address the operator's inline note): once a window's `reset_at` is known and stable, back
   off polling to near the reset boundary instead of every 5 min; resume tight polling only around/after reset or
   on limit-reached. Reduce redundant provider calls; this is not mission-critical telemetry.
5. A **temporary operator Telegram digest** (see below) so Nyd can watch it for several days.

## Telegram digest — design (keep the coordinator clean)
Spec §15 is explicit: **the coordinator must not write Telegram**. So do **not** embed Telegram in
`quota_window_coordinator.py`. Instead: the coordinator already emits structured events via
`src.core.observability.emit_event()` (`quota.observed`, `quota.adapter_unavailable`, `window.reset_detected`,
`limit.reached`, …). Wire a **separate, bounded, flag-gated digest subscriber** (own flag, e.g.
`QUOTA_DIGEST_TELEGRAM_ENABLED`, default OFF) that:
- consumes those events and periodically (aggregate, e.g. hourly or on state-change) sends **one** Telegram
  message via the existing `NotificationService` → `TelegramInterface.notify_*` seam (needs `TELEGRAM_BOT_TOKEN`);
- reports: provider · bucket · `used_percent` · `reset_at` · telemetry quality · **and honestly "0 tokens spent"**;
- is explicitly **temporary/removable** (operator wants it "just for several days"): flag-gated, no schema, easy off.

This satisfies the operator's "notify me whenever the coordinator does something, how often, how much it spent"
without breaching the spec's coordinator/notification boundary. **Reality to state in the digest:** until the real
adapter lands, every message will read "adapter unsupported" — that *is* the signal that the sensor isn't built yet.

## TASK
1. **Audit** — walk the gap table above against the actual tree; correct any row that's wrong; produce a short
   written "merged-vs-spec" verdict (this is the artifact the operator asked for). Confirm the read-endpoint truth.
2. **Build the real Claude observe-only adapter** (spec §8 contract + §6 Claude stance): status-line telemetry,
   token-free, honest quality, stable `principal_hash` with operator-key fallback. Tests with a fake status-line
   fixture (no live CLI in tests — TEST COST GUARD).
3. **Verify/wire** `GET /api/quota-windows` (bearer read-only); reconcile the doc contradiction so ONE doc is true.
4. **Adaptive cadence** — stop 5-min spam once a window is confirmed; back off to the reset boundary. Configurable,
   safe defaults, idempotent.
5. **Telegram digest subscriber** — separate module, own default-OFF flag, event-driven, bounded, removable.
6. Update `docs/QUOTA_WINDOW_COORDINATOR_PHASE1.md` + `SESSION_WINDOW_WARMING_SPEC.md` §17 to reflect true state;
   reframe/close A58 against this reality.

## TYPE
audit + code. Branch `feat/quota-coordinator-finalize`; PR at close; merge per branch policy. Flags default OFF ⇒
byte-identical until the operator activates.

## CONTEXT (reuse verbatim)
- Coordinator + store + adapters: `src/services/quota_window_coordinator.py`
  (`QuotaWindowCoordinator`, `QuotaWindowStore`, `build_default_quota_adapters`, `UnsupportedQuotaAdapter`,
  `build_quota_coordinator_from_config`, `_observe_loop`/`observe_once`, event emit ~L720).
- Flag/config: `config/settings.py` `QuotaConfig` (`enabled`/`db_path`/`observe_interval_sec`) + env overrides ~L600.
- Lifecycle: `src/orchestrator.py` (build-only-when-enabled ~L250; `start()/stop()` in lifespan).
- Read endpoint truth: check BOTH `src/control/dashboard.py` and `src/control/control_api.py` for `/api/quota-windows`.
- Notification seam: `src/services/notification_service.py` (`notify_error`/`notify_heartbeat`) →
  `src/telegram/interface.py` (`notify_completion` ~L2870). Event envelope: `src/core/observability.py`.
- Spec: `docs/SESSION_WINDOW_WARMING_SPEC.md` (§0/§3/§6/§7/§8/§9A/§15/§17/§18) +
  `docs/QUOTA_WINDOW_COORDINATOR_PHASE1.md` (note the operator's inline cadence complaint at L45).

## ACCEPTANCE (proof, not vibes)
1. Written merged-vs-spec audit verdict exists and every gap-table row is confirmed or corrected.
2. Real Claude adapter, with the flag ON, writes an **honest** window row for the live account (real `used_percent`
   + `reset_at`, correct quality) — **and provably spends 0 Claude tokens** (status-line read only; show the path).
3. `GET /api/quota-windows` returns the live store's rows (bearer-protected); exactly one doc describes it truly.
4. Cadence: once a window is confirmed, polling demonstrably backs off (no unconditional 5-min provider hits).
5. Telegram digest: with its flag ON, one aggregate message lands reporting bucket/used%/reset/quality/0-tokens;
   with the flag OFF, no Telegram traffic. Coordinator itself contains zero Telegram code (spec §15 held).
6. All flags OFF ⇒ byte-identical (no DB file, no loop, no Telegram) — re-verify.
7. Targeted `pytest` only, fake fixtures, **no live CLI in tests** (TEST COST GUARD).

## REALITY CONSTRAINTS
- Observe-only. **No activation, no warming, no classification probes** in this job (spec §5/§12 non-goals) — those
  are later, gated, and only after a bucket is *proven* anchored across 3 cycles.
- Never circumvent provider limits; official telemetry surfaces only (spec §6A). Do not treat a visible `reset_at`
  as proof of anchoring.
- Runs against the **paid, shared** account: activation of the flag + gateway restart is the **operator's** call.
- Persist only sanitized fields (spec §9 "Do not persist" list): no raw creds/prompts/account-ids/repo-paths.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — Codex adapter.** The Codex telemetry surface (`account/rateLimits/read`, schema fields) is **unverified**
  (spec §6 + Phase-1 "Unresolved Codex"). Do NOT fabricate it. Either validate against the installed Codex version
  or leave Codex `Unsupported` and say so. Claude is the one that matters for the live pain — do it first.
- **R2 — retire-vs-keep.** If, after a real observe cycle, the Claude window data proves un-actionable, the honest
  outcome may be "keep as thin telemetry" or "retire." Decide with data; do not leave it half-on (mirrors A58 R1).
- **R3 — cadence design** is the operator's stated concern; propose the back-off policy explicitly before coding.

## SCOPE OUT
Window warming / synthetic activation (all of spec §7/§11/§12/§13 activation machinery); wake-dispatch (M3.4/A52);
turn/cost governor (A53). This job ends at **honest observation + operator visibility.**

## TRAIL / EVIDENCE (fill at close)
- Audit verdict · real Claude window row sample (used%/reset/quality) · 0-token proof · `/api/quota-windows`
  response · cadence back-off evidence · one Telegram digest sample · flags-OFF byte-identical re-verify.

---
## Milestone (burndown)
- [x] Merged-vs-spec audit verdict written; gap table confirmed/corrected; read-endpoint truth established
- [x] Real Claude observe-only adapter (status-line, token-free, honest quality, principal_hash) + tests
- [x] `GET /api/quota-windows` verified/wired; docs reconciled to one truth
- [x] Adaptive cadence (confirm → back off) implemented + tested
- [x] Telegram digest subscriber (separate module, own default-OFF flag, event-driven, removable) + test
- [x] All flags OFF ⇒ byte-identical re-verified; A58 reframed/closed against this reality

## Closure (fill on completion)
Merged-vs-spec verdict: the PR #47 scaffold was not a working observer. A61 makes the Claude observe path real by
reading captured Claude Code status-line JSON (`rate_limits.five_hour` and `rate_limits.seven_day`) without starting
a model turn. Codex remains explicitly unsupported because its telemetry surface is still unverified; OpenCode remains
unsupported as a quota owner. The read endpoint truth is now singular: `GET /api/quota-windows` lives in
`src/control/control_api.py`; no `src/control/dashboard.py` route exists in this tree.

Evidence:
- Claude row shape tested from fake status-line JSON: `five_hour used_percent=42.5 reset_at=2026-07-29T09:00:00Z quality=authoritative`; adapter `model_invocations == 0`.
- 0-token proof path: `scripts/claude_statusline_capture.py` runs as a Claude Code statusLine command, receives JSON on stdin, stores only sanitized rate-limit fields, and the adapter reads that file. It does not invoke `claude`, `claude -p`, the SDK, or any model request.
- `/api/quota-windows` returns disabled observe-only shape when the coordinator is not constructed and returns the live coordinator read model when present.
- Cadence proof: known reset at 13:00Z with 15-minute probe lead from 08:00Z yields `next_observe_delay_sec() == 17100`, not unconditional 300 seconds; limit-reached falls back to 300 seconds.
- Web UI proof: `SystemScreen` renders `QuotaWindowPanel` after backend usage. The frontend calls `GET /api/quota-windows` through typed transport + `useQuotaWindows`, and shows observer-off, no-data, unavailable, and observed bucket states without activation controls.
- Telegram digest proof: `QuotaTelegramDigestSubscriber` remains separate and default-off; it aggregates quota events and calls `NotificationService.notify_quota_digest` only when `QUOTA_DIGEST_TELEGRAM_ENABLED` is enabled. Telegram is optional, not the primary operator surface.
- Flags-off byte-identical path: `TaskOrchestrator` still constructs no coordinator unless `QUOTA_COORDINATOR_ENABLED` is true, so no quota DB, no observe loop, and no digest subscriber are created by default.
- Targeted verification: `.venv/bin/pytest tests/test_quota_window_coordinator.py tests/test_control_api.py -q` -> 54 passed; `pnpm --dir web typecheck` passed; `pnpm --dir web build` passed; `.venv/bin/python -m py_compile ...` passed.
