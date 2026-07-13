# Frontend Overview

**What:** `web/` is the mobile-first Web UI for the AI-team gateway — React 19 +
Vite + Tailwind v4 + TanStack Query + Zustand + React Router 7. It is our own
primary UI, running over the **same gateway process** that Telegram (a secondary,
optional surface) also uses — not a separate backend (see
[`.ai/CONTEXT.md`](../../.ai/CONTEXT.md) "What this project is").

**Where it runs:** the gateway serves the built UI in-process at `/` + `/api/*`
on one tailnet-bound port (Control API — `src/control/control_api.py`, default
`DASHBOARD_PORT=9003`). In dev, `vite` proxies `/api` and `/health` to that same
port (`VITE_API_TARGET` env override — `web/vite.config.ts`).

**Stack:**

| Concern | Library | Notes |
|---|---|---|
| Rendering | React 19 | function components only |
| Routing | React Router 7 | `web/src/App.tsx` |
| Server state | TanStack Query v5 | polling, not a generic fetch cache — see [`DATA_FLOW.md`](DATA_FLOW.md) |
| Client/local state | Zustand | auth token, drafts, UI toggles — see below |
| Styling | Tailwind v4 | `@tailwindcss/vite` plugin, no separate config file |
| Icons | lucide-react | |
| Motion | framer-motion | sparingly, sheets/transitions |
| Build | Vite 8, `tsc -b` | `es2022` target — PWA-only, no legacy transpile |

## Why this shape (read before changing structure)

This UI is not built directly against backend payloads. It sits behind a
**canonical domain contract**:

```
backend JSON (snake_case, backend-shaped)
        ↓
web/src/transport/*Adapter.ts   — translates raw → canonical, one adapter per resource
        ↓
web/src/domain/*.ts             — the ONLY shapes components are allowed to bind to
        ↓
web/src/hooks/*.ts              — TanStack Query hooks; call the adapter, expose the domain type
        ↓
components / screens
```

**Rule: backend-specific payloads must never leak into components.** If you're
tempted to read a raw field in a screen, the fix is a new/extended adapter, not
an inline cast. This exists because the backend's shape and the UI's target
shape genuinely disagree in places (flat `SessionStatus` conflating lifecycle +
operational state, snake_case operational events vs. a dotted `GatewayEvent`
union, etc.) — see [`DATA_FLOW.md`](DATA_FLOW.md) for the specifics and why each
gap exists.

Every domain type and event member in `src/domain/` is inline-tagged with its
backend-parity status (✅ present / 🟡 partial / ❌ missing / ⛔ deliberately
dropped) and a short reason. Read those comments in place — they are the
up-to-date contract; the historical gap analysis that produced them is archived
at [`docs/archive/frontend-backend-gap/FRONTEND_BACKEND_GAP.md`](../archive/frontend-backend-gap/FRONTEND_BACKEND_GAP.md)
(superseded, don't build against it).

## Directory map

```
web/src/
  domain/         canonical types — models.ts, status.ts, events.ts, work.ts, transitions.ts
  transport/       apiClient.ts (fetch), rawApi.ts (backend payload types), *Adapter.ts (raw→domain)
  hooks/           TanStack Query read hooks (useLiveData.ts, useWork.ts) + write hooks
                    (useSessionActions.ts) + the SSE event stream (useEventStream.ts,
                    eventStreamContext.tsx)
  stores/          Zustand — client-only state, never server data (see below)
  screens/         one file per route (Sessions, SessionDetail, System, Work, WorkDetail, Files)
  components/      shell/ (nav, top bar, gates) · sessions/ · timeline/ (chat) · system/ · work/ · ui/ (primitives)
  lib/             pure helpers — time formatting, class merging, activity/status presentation
  fixtures/        static sample data — dev-only preview surface, never imported by live code paths
```

## State model (three kinds — don't mix them)

1. **Server state** — TanStack Query, via `hooks/useLiveData.ts` / `hooks/useWork.ts`.
   Source of truth is the backend; the hook polls and the component always
   renders what the last successful fetch returned. See [`DATA_FLOW.md`](DATA_FLOW.md).
2. **Live/rolling state** — the SSE event log (`hooks/useEventStream.ts`), held
   in `EventStreamProvider` as two separate React contexts (events vs.
   connection) so a connection-only consumer doesn't re-render per frame. This
   is a **display feed**, not state authority — see `DATA_FLOW.md` "Two timelines".
3. **Client/local state** — Zustand stores in `src/stores/`, each with a single
   narrow reason to exist:
   - `authStore.ts` — the Bearer `DASHBOARD_TOKEN`, persisted to `localStorage`.
     Injected as `window.__DASHBOARD_TOKEN__` when the gateway serves the UI
     itself (skips the token-entry gate for a trusted device).
   - `draftStore.ts` — per-session composer text, so navigating away doesn't
     lose a half-typed instruction.
   - `sentStore.ts` — optimistic just-sent messages, shown before the server
     round-trip completes (the backend has no `message.created` event).
   - `dismissedStore.ts` — per-viewer "hide this failed task", explicitly NOT
     synced to the backend (hiding on my phone shouldn't rewrite the record).
   - `uiStore.ts` — presentation-only toggles (target filter, collapsed sections).

   None of these hold data the backend also owns as the source of truth — if a
   store starts caching something the backend returns, that's a sign it should
   be a Query hook instead.

## Routing

`App.tsx` is gated on `useAuthStore().hasToken` — no token, no `/api/*` calls,
just `TokenGate`. Once authed:

- `/sessions/:id` and `/work/:id` are **full-screen detail routes**, outside
  the bottom-nav shell (back-stack model, not a tab).
- Root tabs (`/sessions`, `/work`, `/system`) render inside `MobileAppShell`
  (connection banner + bottom nav persist; the routed screen scrolls).
- `/tasks` redirects to `/system` — Tasks was folded into Session Detail +
  System (`.ai/CONTEXT.md` Web UI ladder, #36).
- Screens are `React.lazy`-loaded behind one `Suspense` boundary — the initial
  bundle carries only the shell + first screen.

See [`SCREENS_AND_COMPONENTS.md`](SCREENS_AND_COMPONENTS.md) for what each
screen/component actually does.

## Related

- [`DATA_FLOW.md`](DATA_FLOW.md) — the transport/adapter/hook pipeline, polling
  vs. SSE, the Work/Case read model, write mutations + idempotency.
- [`SCREENS_AND_COMPONENTS.md`](SCREENS_AND_COMPONENTS.md) — route-by-route and
  component-group tour.
- [`DEV_AND_BUILD.md`](DEV_AND_BUILD.md) — running, testing, building, PWA.
- [`docs/CONTROL_CONTRACT.md`](../CONTROL_CONTRACT.md) — the backend-side half
  of this contract (event envelope, entry points, backend registry).
