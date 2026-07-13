# Overview — You Are Here

A front door for anyone (human or agent) landing in this repo cold. It orients and
**routes** to the doc that owns each topic. It is **not** a source of truth — every fact
that changes lives behind a link below.

## What this is

A session-first gateway for local coding agents, controlled from its own Web UI
(`web/` — React, served by the gateway itself) with Telegram as a secondary
surface over the same backend. Native backend resume (Claude Code / Codex) is the
runtime — the gateway opens a persistent session and continues it through the
backend's own resume. It is **not** a generic autonomous framework (see the
anti-goals in [`.ai/context/production_vision.md`](../.ai/context/production_vision.md)).

On top of that base, the gateway can also **invoke a Manager** — a Claude session
bound to a durable Case that can dispatch worker sessions and authoritatively close
the Case once done. This is invoked, not autonomous-by-default: it runs only when
something calls `/api/manager`, and it's flag-gated (`MANAGER_ROLE_ENABLED`). See
[`docs/ARCHITECTURE.md` §2b](ARCHITECTURE.md#2b-manager--case-surface-m2m3-flag-gated).

## The shape (as it runs)

```text
                      one process: python main.py
   ┌───────────────────────────────────────────────────────────────┐
   │  Control API (:9003) serving the Web UI  +  mesh task server (:9002) │
   │  Manager/Case surface (/api/manager, /api/work, flag-gated)    │
   │  Telegram bot (secondary surface, in-process, optional)        │
   └───────────────────────────────────────────────────────────────┘
                                  │  (assigns tasks / dispatches workers)
                                  ▼
        workers — separate processes on other machines
```

A thumbnail only. For the full process / HTTP map see
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

## Where things live / where to go next

| You want… | Read |
| --- | --- |
| current state + priorities + shipped | [`.ai/CONTEXT.md`](../.ai/CONTEXT.md) |
| state of every dispatched job | [`.ai/dispatch/DISPATCH_LOG.md`](../.ai/dispatch/DISPATCH_LOG.md) |
| which doc owns which info | [`.ai/DOC_MAP.md`](../.ai/DOC_MAP.md) |
| full catalog of every doc in `docs/` | [`docs/INDEX.md`](INDEX.md) |
| full process/HTTP architecture | [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) |
| install + first run | [`docs/QUICK_START.md`](QUICK_START.md) |
| strategic intent + anti-goals | [`.ai/context/production_vision.md`](../.ai/context/production_vision.md) |
| the task-quality harness | [`docs/harness/dispatch_pipeline.md`](harness/dispatch_pipeline.md) |
| completed-work history | [`docs/archive/progress/_archive_PROGRESS_LOG.md`](archive/progress/_archive_PROGRESS_LOG.md) |

## Check it's alive

```bash
curl http://127.0.0.1:9003/health
```

Do **NOT** run `python main.py status` — it acquires the gateway lock and kills the live
PM2 gateway. Use the `curl` health check above.
