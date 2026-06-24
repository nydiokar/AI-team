# UI-6 — PWA / hardening / a11y (ship the cockpit to the phone)

Scope fence per COCKPIT_REFACTOR_SPEC §13: **only the boxes below are in scope.**
Baseline: branch feat/webui-ui0 @ 7b64f0c (UI-5). The gateway already serves
`web/dist` at `/` (+ `/api/*`) on one tailnet-bound port (U5); the SPA file resolver
(`control_api._mount_web_ui`) serves any real file in `dist` (so a manifest + service
worker placed in `web/public/` ship automatically).

Spec row (§14): *"hardening, a11y, PWA, push"* — the LAST rung, the one that makes
the cockpit installable on the phone.

## Design decision (what ships now — and what is deferred, honestly)
**Installable PWA + offline shell + a11y are the shippable core.** They are pure
frontend over the existing single-port serving — no new backend, no new secret.

**Push notifications are DEFERRED — NOT built here (operator-confirmed 2026-06-24).**
Web Push is a real backend surface: VAPID keypair (a new secret on the gateway) +
a subscription-storage endpoint + a sender wired into the event spine. The operator's
ACTUAL want is **Telegram-like info notifications** — "agent done → I get pinged → I
go look at the output", "task failed → I know" — NOT approval prompts (agents have
full access; nothing gates on the human). So when push IS built it's a small
info-fanout on `task.completed` / `task.failed`, not approval-gating. Still a backend
piece, so still deferred to its own focused box. The PWA we ship is push-*ready*
(installed + SW present — push REQUIRES a service worker, which Box 2 builds anyway),
so push is purely additive later, not a rebuild. Logged in `docs/DEFERRED.md`.

PWA mechanism: a **hand-written minimal manifest + a small service worker** in
`web/public/`, NOT `vite-plugin-pwa`. Rationale: the gateway already serves static
files from dist; a ~40-line SW (offline app-shell cache, network-first for /api,
cache-first for hashed assets) is transparent and dependency-free, vs. a build plugin
that needs Vite 8 / Rolldown compat babysitting. If the SW grows beyond an app-shell,
revisit the plugin — but that's a different box.

NO new domain types, NO new API route, NO push backend. Edit-the-checklist-first
(§13) if a box needs one.

---

## Box 1 — web app manifest + icons ✅ DONE
- [x] `web/public/manifest.webmanifest`: name, short_name "AI-Team", start_url "/",
  display "standalone", background/theme `#0b0e14` (matches index.html theme-color),
  icons (192 + 512, maskable). orientation any.
- [x] Icons in `web/public/` (192/512 png + a maskable). A simple generated mark is
  fine (cockpit cyan on near-black) — committed as real files.
- [x] `index.html`: `<link rel="manifest">` + apple-touch-icon + apple-mobile-web-app
  meta (iOS standalone). Keep the existing viewport-fit=cover / theme-color.

Done = exactly: manifest + icons + the head links. No SW yet.
Do NOT touch: vite.config plugins, the build pipeline.
Revert: delete public/ additions + the head links.

## Box 2 — service worker (offline app-shell) ✅ DONE
- [x] `web/public/sw.js`: precache the app shell on install (index + built assets are
  hashed, so cache-on-fetch); **network-first** for navigations + `/api/*` (never
  serve stale data as if live — falls back to cached shell only when offline);
  **cache-first** for hashed `/assets/*`. Versioned cache name; clean old caches on
  activate. NO precache of /api responses (data must be live or visibly offline).
- [x] Register the SW from the app entry (main.tsx) behind
  `'serviceWorker' in navigator`, production-only (don't fight the dev server).
- [x] Confirm the SPA file resolver serves `/sw.js` + `/manifest.webmanifest` from
  dist (it already serves real files; verify scope `/`).

Done = exactly: SW file + registration; app loads offline (shell), /api shows the
existing reconnecting/offline state when the network is down.
Do NOT touch: useEventStream/poll logic (offline is surfaced by the EXISTING
connection state, not new code).
Revert: delete sw.js + the registration block.

## Box 3 — a11y + hardening pass ✅ DONE
- [x] Bottom-nav links + icon-only buttons get aria-labels; the live status pill
  already carries a text label (no color-only meaning — acceptance #13, keep it).
- [x] Focus-visible rings on interactive elements; the activity feed + lists are
  keyboard-reachable. Respect `prefers-reduced-motion` for the breathing dot.
- [x] `lang` is set (it is, on <html>); confirm tap targets ≥44px (nav already is).
- [x] No console errors on load; remove any dead placeholder copy uncovered.

Done = exactly: an a11y/hardening sweep of the shipped screens (no new feature).
Do NOT touch: data flow, adapters, endpoints.
Revert: per-change.

## Box 4 — install affordance ✅ DONE
- [x] Capture `beforeinstallprompt` → a small "Install" hint in System Settings card,
  triggering the prompt. iOS has no event → show a one-line "Add to Home Screen" note
  on iOS Safari. Skip entirely if it can't stay small.

Done = exactly: a discoverable install path, or skipped with a note.
Do NOT touch: the rest of System screen.
Revert: delete the hint.

## Gate
- [x] Frontend `tsc` + vitest + `vite build` green; manifest + sw.js land in dist.
- [ ] Lighthouse/installability sanity: manifest parses, SW registers, theme-color +
  icons present (manual check acceptable on the phone/desktop).
- [ ] Live (Telegram OFF, MESH OFF, port 9003): the gateway serves
  `/manifest.webmanifest` (200, correct content-type-ish) and `/sw.js` (200);
  the app is installable; with the network cut the shell still loads and the UI
  shows the existing offline/reconnecting state (no white screen).

## Surfaced to the operator (NOT built — §13)
- **Web Push** = a future track: VAPID keypair, a `/api/push/subscribe` endpoint
  storing subscriptions, and a send-on-attention emitter off the event spine. Same
  "deliberate design + secret surface" class as approvals-automation / terminal. The
  shipped PWA is push-ready; this is an additive box when the operator wants it.
