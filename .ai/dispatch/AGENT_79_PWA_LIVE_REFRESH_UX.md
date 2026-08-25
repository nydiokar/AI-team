```yaml
job_id: AGENT_79_PWA_LIVE_REFRESH_UX
created_at: "2026-08-24T17:00:00+03:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: "codex"
depends_on: []
results_ref: ".ai/dispatch/AGENT_79_PWA_LIVE_REFRESH_UX.md#closure"
evidence:
  - "web/public/sw.js"
  - "web/src/App.tsx"
  - "web/src/hooks/useEventStream.ts"
  - "web/src/lib/liveInvalidation.ts"
  - "web/src/lib/liveInvalidation.test.ts"
  - "web/src/screens/SessionDetailScreen.tsx"
  - "web/src/screens/WorkDetailScreen.tsx"
updated_at: "2026-08-24T17:07:00+03:00"
```

# A79 — PWA Notification + Live Refresh UX

**Date:** 2026-08-24
**Level:** 2
**Status:** done
**Branch:** `feat/pwa-live-refresh-ux`
**Depends on:** —

## Task

Fix two web/PWA user-facing pain points without changing the agent/session execution logic:

1. Push-notification taps should feel like opening the same installed app, landing on the intended
   session/case without losing normal app context.
2. Session/case screens should stop feeling stale or frozen while waiting for polling, and the PWA
   should have an explicit refresh control because native browser pull-to-refresh is suppressed by
   the app layout.

## Current Behavior

- `web/public/sw.js` handles `notificationclick` by taking the first same-origin window returned by
  `clients.matchAll()`, navigating it to the notification URL, and focusing it. It does not prefer a
  currently focused/visible app client or communicate the click to the running React app.
- `/sessions/:id` and `/work/:id` are full-screen detail routes outside `MobileAppShell`, so direct
  notification entry omits root-level app signals like connection/system banners and bottom nav.
- Session conversation/status data is authoritative through TanStack Query polling. SSE is present
  but only feeds the rolling event log/live activity label; it does not currently invalidate session
  or case read-model queries.
- Native refresh is intentionally unavailable in the PWA because the document body is fixed and the
  app scrolls inside internal containers.

## Root Cause

The app already has a durable read API and a live event stream, but they are not connected at the
state-refresh boundary. A notification can bring an existing PWA window forward before the next
poll, and direct detail routes provide fewer freshness/navigation signals than root routes. That
combination makes the app look stale or like a different entry mode even though the same bundle and
same backend are being used.

## Minimal Design

1. **Notification click handoff**
   - Prefer handing the target URL to an existing same-origin client by `postMessage` before falling
     back to `navigate()`/`openWindow()`.
   - React listens for the service-worker message, navigates inside the current SPA, focuses the app,
     and triggers immediate read-model invalidation.

2. **Detail route app context**
   - Keep detail routes full-screen enough for chat ergonomics, but add the same connection/system
     signal surface used by the shell so notification entry does not feel like a separate app.
   - Preserve existing session behavior: closed sessions still require resume before replying.

3. **SSE-driven invalidation**
   - Use incoming SSE events as invalidation hints, not as state authority.
   - Invalidate targeted session queries when an event carries `sessionId`.
   - Invalidate targeted work/case queries when an event carries `caseId`/`flowRunId`.
   - Keep polling as a fallback; do not replace read APIs.

4. **Manual refresh**
   - Add a compact manual refresh affordance on session and case detail headers.
   - It refetches the visible detail queries immediately and gives lightweight feedback.

## Out of Scope

- No backend execution/session logic changes.
- No fully streamed replacement for session messages.
- No change to closed-session semantics.
- No new endpoint unless an existing query cannot be invalidated/refetched from the frontend.

## Verification Plan

- Add/adjust focused web tests where the behavior is deterministic.
- Run web typecheck and production build.
- Run an adversarial review against:
  - notification target routing,
  - stale cache/refetch behavior,
  - offline/error states,
  - interaction with existing uncommitted UI changes,
  - no accidental backend contract expansion.

## Milestone

- [x] Dispatch registered.
- [x] Notification click handoff implemented.
- [x] Detail route app signals added.
- [x] SSE invalidation implemented.
- [x] Manual refresh implemented.
- [x] Tests/build green.
- [x] Adversarial review complete and findings fixed.

## Closure

Implemented on `feat/pwa-live-refresh-ux`.

Changes:

- Push notification clicks now message an existing controlled app client with
  `ai-team:notification-click`; the React app navigates internally and immediately invalidates the
  target route's read-model queries. If no controlled app client exists, the service worker opens the
  target URL normally.
- Session and Work detail screens now include the same connection/system alert banners as shell
  screens, so deep-link entry carries freshness/outage signals.
- Session and Work detail headers now expose explicit refresh buttons. They refetch the visible
  route data immediately while preserving the existing closed-session behavior.
- SSE events now invalidate affected session/case read-model queries after reconnect dedupe. Polling
  remains the fallback and API reads remain authoritative.
- Added focused tests for the invalidation target extraction and route-target invalidation.

Verification:

- `pnpm test -- liveInvalidation` from `web/`: 16 files / 134 tests passed.
- `pnpm typecheck` from `web/`: passed.
- `pnpm build` from `web/`: passed and rebuilt the production web app.

Adversarial review:

- **Finding 1 fixed:** SSE invalidation initially ran before reconnect dedupe, which could have
  refetched on replayed tail events. It now invalidates only novel events.
- **Finding 2 fixed:** notification click handoff initially included uncontrolled clients, which may
  not have the new SPA listener. It now targets controlled clients only; otherwise it opens the URL.

Verdict: accept. The change is web-facing only, keeps backend state authority unchanged, preserves
closed-session semantics, and adds manual recovery plus event-driven freshness without replacing the
existing polling fallback.
