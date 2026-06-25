# Deferred — Web UI / Cockpit track

Items deliberately **not** built in the UI-0 → UI-6 ladder (which is now complete).
Each is additive, has a real design or secret-surface cost, and is parked here on
purpose rather than half-built. Referenced from `docs/UI6_CHECKLIST.md` and the
spec ladder (`docs/COCKPIT_REFACTOR_SPEC.md` §14).

---

## Web Push notifications
**Status:** deferred (operator-confirmed 2026-06-24). The shipped PWA is push-*ready*
(installable + service worker present from UI-6 Box 2), so this is purely additive —
not a rebuild.

**What it needs (its own focused box):**
- VAPID keypair — a **new secret** on the gateway.
- `POST /api/push/subscribe` — store browser push subscriptions.
- A send-on-event emitter wired into the existing event spine.

**Shape when built:** the operator's actual want is **Telegram-like info pings** —
"agent done → I get pinged → I go look", "task failed → I know" — an info-fanout on
`task.completed` / `task.failed`. **Not** approval-gating (agents have full access;
nothing gates on the human). Keep it small.

## Assistant streaming / token deltas (`message.delta`)
**Status:** dropped for v1 (⛔ in `docs/FRONTEND_BACKEND_GAP.md`). The timeline renders
the per-turn **summary** as the whole assistant message. Token streaming is a post-v1
nice-to-have, not a shipping blocker.

## Diff hunks / file-content preview (UI-4)
**Status:** deferred — no backend source. `FilesScreen` shows per-file change rows
(added/modified/deleted + ±line counts) from the artifact reader; rendering actual
diff hunks or file contents needs a new backend surface and is out of scope for the
core "what did the agent change?" review loop.

## Terminal / raw stdout-stderr line stream (UI-5)
**Status:** out (security + future). UI-5 ships an operational **event** feed off the
SSE stream; a live terminal is a separate, deliberately-designed surface.

## Approvals automation (Move H / UI-3 extension)
**Status:** built but intentionally **inert** — do not extend here. The durable
approval gate (H) + approval card (UI-3) exist and are tested, but nothing in a hot
path auto-emits approvals. Auto-emitting approvals from real risky actions, plus
review/handoff workflows, belong to a future **workflow-automation track** that "needs
to be thought out better" (operator, 2026-06-24). Design deliberately later; don't
bolt more onto H now.

---

These are also the ⛔-DROP / future rows in `docs/FRONTEND_BACKEND_GAP.md`. The
distinction: **dropped** = not coming (fights the architecture); **deferred** = a
real future box, just not now.
