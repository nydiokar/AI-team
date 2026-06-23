# AI-Team Gateway — Web UI

Mobile-first (360px portrait) web control surface for the AI-team coding gateway.
Built per `.ai/context/mobile_coding_gateway_product_ui_spec_v0.2.md`, reconciled
against backend reality in `docs/FRONTEND_BACKEND_GAP.md`, on the milestone ladder
in `docs/COCKPIT_REFACTOR_SPEC.md §14`.

## Scope (this branch): UI-0 + UI-1 only

- **UI-0** — frontend domain + contract: canonical TypeScript types
  (`src/domain`), the snake→dotted transport adapters (`src/transport`), state
  transition tables, and fixtures (`src/fixtures`). ⛔-dropped concepts
  (tool executions, `task.progress`, `archived`, per-session
  `connection_unknown`, token streaming) are **omitted by design**.
- **UI-1** — mobile shell + Sessions + System bound to **live** read APIs;
  Tasks + Timeline render from **fixtures**.

UI-2+ is **not** started — it depends on backend Move F (write + WS/SSE) and
Move I (event adapter), which aren't built. Do not add write paths
(send/stop/retry/approve) here.

## Architecture

```
backend (read-only dashboard.py)
  → src/transport/rawApi.ts      raw snake_case payloads (exact shapes)
  → src/transport/*Adapter.ts    translate raw → canonical (NOT pass-through)
  → src/domain/*                 canonical types components bind to
  → src/hooks/useLiveData.ts     TanStack Query (poll 3s; no WS until Move F)
  → src/screens, src/components  presentation (mobile, 360px)
```

Backend payloads never leak into components (spec §11.1). The event adapter
(`src/transport/eventAdapter.ts`) is a real three-bucket translation:
rename · collapse scattered transitions · operational "job" events →
`SystemNotice` (the replacement for the dropped tool-level events).

## Live data

Sessions bind to `GET /api/sessions`; System binds to `GET /api/nodes` (using the
derived `live` flag + `heartbeat_age_sec`, never the stale `status` column). Auth
is `Authorization: Bearer DASHBOARD_TOKEN` — entered in the in-app token gate.

## Run

```bash
npm install
# start the read-only dashboard separately, e.g.:
#   uvicorn src.control.dashboard:app --host 127.0.0.1 --port 9003
npm run dev          # http://localhost:5180  (/api proxied to :9003)
```

Point at a different dashboard with `VITE_API_TARGET=http://host:port npm run dev`.

## Checks

```bash
npm run typecheck    # tsc -b --noEmit
npm test             # vitest — adapter translation tests
npm run build        # tsc + vite production build
```

## Stack (latest, June 2026)

- **React 19** + **Vite 8** (`@vitejs/plugin-react-swc` — the SWC plugin; the
  babel/Rolldown `plugin-react@6` injects `__BUNDLED_DEV__` and breaks plain-Vite
  dev, so we use SWC).
- **Tailwind CSS v4** — CSS-first via `@tailwindcss/vite` + `@theme` in
  `src/index.css`. There is **no `tailwind.config.ts`** (v4 configures in CSS).
- **react-router v7**, **TanStack Query v5** (server state), **Zustand v5** (local
  UI state) — the split in spec §11.3.
- **shadcn-style primitives** (Radix Slot + CVA + tailwind-merge), **lucide-react**
  icons, **framer-motion** for the connection banner / list reveals.
- **TypeScript 5.9**, split project refs (`tsconfig.app.json` / `.node.json`),
  strict + `verbatimModuleSyntax` + `erasableSyntaxOnly`.

## Design system — "Cockpit"

Operational control surface, not a marketing app. Status is the loudest thing on
screen. Near-black base + two charcoal elevations, electric-cyan accent, **Geist**
+ **Geist Mono** type. The signature element is the live status system: the status
pill with a breathing dot, and the amber edge-glow on attention cards. Tokens live
in `src/index.css` `@theme`. Honors `prefers-reduced-motion` and safe-area insets.
