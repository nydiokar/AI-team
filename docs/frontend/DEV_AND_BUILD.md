# Frontend Dev, Test & Build

## Running locally

```
cd web
npm install
npm run dev        # vite, http://localhost:5180
```

`vite.config.ts` proxies `/api` and `/health` to the gateway's embedded
Control API — default `http://127.0.0.1:9003`. **The gateway must already be
running** (see `docs/RUNBOOKS/OPERATIONS_PM2.md`) for the dev server to show
live data; point elsewhere with:

```
VITE_API_TARGET=http://<tailscale-ip>:9003 npm run dev
```

You'll hit `TokenGate` on first load — paste the gateway's `DASHBOARD_TOKEN`.
It persists to `localStorage`, so subsequent loads skip the gate.

## Testing

```
npm run test        # vitest run
npm run typecheck   # tsc -b, no emit
```

- `vitest.config.ts` is **deliberately separate** from `vite.config.ts` — Vite
  8's `UserConfig` dropped the `test` key, and tests don't need the
  React/Tailwind plugins loaded.
- Test environment is `"node"` with `globals: true` — most tests here are
  **pure-function tests** (adapters, `lib/*`), not component/DOM tests. Look at
  `src/lib/*.test.ts` and `src/transport/*.test.ts` for the pattern before
  adding a new one; there's no React Testing Library setup in this config, so a
  component-render test would need its own environment override.
- High-value test targets: anything in `transport/*Adapter.ts` (raw→domain
  translation is exactly the kind of logic that silently drifts) and
  `lib/*Presentation.ts` (status/label derivation).

No Playwright config is wired to a script yet, despite `@playwright/test`
being a devDependency — check before assuming e2e tests run in CI.

## Building

```
npm run build       # tsc -b && vite build → web/dist/
npm run preview      # serve the built dist/ locally
```

Two things `vite.config.ts` does that matter if a build looks wrong:

1. **Cache-busting build identity** (`__BUILD_VERSION__`) — the short git
   commit hash (or a timestamp fallback outside a git repo), injected at build
   time. This exists to bust the **persisted TanStack Query cache** on every
   deploy so a stale shape never silently loads from `localStorage`.
2. **Manual vendor chunking** — `@tanstack/*` → `query-vendor`,
   `react`/`react-dom`/`react-router*` → `react-vendor`, kept as separate
   chunks with stable hashes across deploys. The app chunk changes (and is
   cache-busted) every deploy; a returning visitor re-downloads only that, not
   the framework.
3. **Build target is `es2022`** — this UI only ever runs as an installed PWA
   or a modern-browser tab on the operator's own device. Don't add a
   legacy-browser polyfill/transpile target without a real reason.

## How the gateway serves the build

In production there is **no separate frontend server**. The gateway
(`src/control/control_api.py`, `_web_ui` route family) serves `web/dist/`
directly at `/`, mounts `web/dist/assets` as static files, and **injects the
`DASHBOARD_TOKEN` into the served `index.html`** so a trusted device (on the
tailnet) skips `TokenGate` entirely (picked up via
`window.__DASHBOARD_TOKEN__` in `authStore.ts`). If `web/dist` is absent
(dev), the route family no-ops and you're expected to be running `vite`
instead.

**After any frontend change meant to ship:** run `npm run build` and restart
the gateway (or redeploy per `docs/RUNBOOKS/OPERATIONS_PM2.md`) — the gateway
does not watch/rebuild `web/` itself.

## PWA / offline shell

- `public/manifest.webmanifest` — installable app metadata (standalone
  display, portrait, dark theme colors, 3 icon sizes incl. maskable).
- `public/sw.js` — a minimal shell-cache service worker (`CACHE =
  "ai-team-shell-v4"`): caches `/` and `/index.html` on install, cache-first
  for `/assets/*` (hashed, safe to cache-first), and purges old cache
  versions on activate. **Bump the `CACHE` version string when you need to
  force clients to drop a stale shell** — there's no other invalidation path
  for the service worker itself (the `__BUILD_VERSION__` busting only covers
  the Query cache, not the SW's own cache).
- This is a shell cache, not an offline-data story — `/api/*` is not
  intercepted or cached by the service worker; offline just means the last
  painted screen stays visible until the poll/SSE reconnects.

## Related

- [`OVERVIEW.md`](OVERVIEW.md) — stack + layering.
- [`docs/RUNBOOKS/OPERATIONS_PM2.md`](../RUNBOOKS/OPERATIONS_PM2.md) — running
  the gateway process this UI depends on.
- [`docs/RUNBOOKS/CONTROL_SURFACE_DEPLOY_RUNBOOK.md`](../RUNBOOKS/CONTROL_SURFACE_DEPLOY_RUNBOOK.md) —
  deploying the unified gateway (Web UI + Telegram on one process).
