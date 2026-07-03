# Roadmap

> **This file is a pointer, not the source of truth.** The live roadmap, current
> priorities, and dispatched-job state moved into the `.ai/` context hub. This page
> exists so anyone landing here from `docs/` is redirected to the right place.

## Where the real state lives

| You want… | Read |
|---|---|
| Current priorities + what's shipped + how it's wired now | [`.ai/CONTEXT.md`](../.ai/CONTEXT.md) |
| State of every dispatched job (the manual state machine) | [`.ai/dispatch/DISPATCH_LOG.md`](../.ai/dispatch/DISPATCH_LOG.md) |
| Strategic product intent + anti-goals | [`.ai/context/production_vision.md`](../.ai/context/production_vision.md) |
| Completed-work history | [`docs/archive/progress/_archive_PROGRESS_LOG.md`](archive/progress/_archive_PROGRESS_LOG.md) |

## The one constraint that never changes

- The product is a **session-first Telegram/Web gateway for local coding agents**.
- Backend-native resume (Claude Code / Codex) stays the primary runtime.
- The gateway must **not** drift into a broad autonomous orchestration framework,
  opaque memory, or PTY-persistence-as-backbone. See `production_vision.md` §6/§7.
