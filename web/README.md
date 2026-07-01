# AI-Team Gateway - Web UI

Mobile-first (360px portrait) web control surface for the AI-team coding gateway.
Built per `.ai/context/mobile_coding_gateway_product_ui_spec_v0.2.md`, reconciled
against backend reality in the cockpit specs, and served by the gateway in
production.

## Scope

- UI-0: frontend domain + contract with canonical TypeScript types
  (`src/domain`), snake-to-canonical transport adapters (`src/transport`), state
  transition tables, and fixtures (`src/fixtures`). Dropped concepts such as
  tool executions, `task.progress`, `archived`, per-session
  `connection_unknown`, and token streaming remain omitted by design.
- UI-1 through UI-6 are shipped: live read/write sessions, SSE activity, tasks,
  approvals UI, files/artifacts, logs/health, PWA shell, and install affordance.
  Deferred work is tracked in `docs/DEFERRED.md`.

## Architecture

```
gateway embedded Control API (src/control/control_api.py)
  -> src/transport/rawApi.ts      raw snake_case payloads (exact shapes)
  -> src/transport/*Adapter.ts    translate raw -> canonical (NOT pass-through)
  -> src/domain/*                 canonical types components bind to
  -> src/hooks/useLiveData.ts     TanStack Query + SSE event invalidation
  -> src/screens, src/components  presentation (mobile, 360px)
```

Backend payloads never leak into components (spec section 11.1). The event
adapter (`src/transport/eventAdapter.ts`) is a real three-bucket translation:
rename, collapse scattered transitions, and map operational job events to
`SystemNotice`.

## Live Data

Sessions bind to `GET /api/sessions`; System binds to `GET /api/nodes` (using the
derived `live` flag + `heartbeat_age_sec`, never the stale `status` column). Auth
is `Authorization: Bearer DASHBOARD_TOKEN` / `WORKER_TOKEN`, entered in the
in-app token gate.

## Run

```bash
npm install
# start the gateway separately:
#   python main.py
npm run dev          # http://localhost:5180  (/api proxied to gateway :9003)
```

Point at a different gateway with `VITE_API_TARGET=http://host:port npm run dev`.

## Checks

```bash
npm run typecheck    # tsc -b --noEmit
npm test             # vitest adapter translation tests
npm run build        # tsc + vite production build
```

## Stack

- React 19 + Vite 8 (`@vitejs/plugin-react-swc`).
- Tailwind CSS v4 via `@tailwindcss/vite` + `@theme` in `src/index.css`. There is
  no `tailwind.config.ts`.
- react-router v7, TanStack Query v5, and Zustand v5.
- shadcn-style primitives, lucide-react icons, and framer-motion.
- TypeScript 5.9, split project refs (`tsconfig.app.json` / `.node.json`),
  strict + `verbatimModuleSyntax` + `erasableSyntaxOnly`.

## Design System

Operational control surface, not a marketing app. Status is the loudest thing on
screen. Near-black base + two charcoal elevations, electric-cyan accent, Geist +
Geist Mono type. The signature element is the live status system: the status pill
with a breathing dot, and the amber edge-glow on attention cards. Tokens live in
`src/index.css` `@theme`. Honors `prefers-reduced-motion` and safe-area insets.
