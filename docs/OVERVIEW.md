# Overview — You Are Here

A front door for anyone (human or agent) landing in this repo cold. It orients and
**routes** to the doc that owns each topic. It is **not** a source of truth — every fact
that changes lives behind a link below.

## What this is

A session-first Telegram/Web gateway for local coding agents. Native backend resume
(Claude Code / Codex) is the runtime — the gateway opens a persistent session and
continues it through the backend's own resume. It is **not** a generic autonomous
framework (see the anti-goals in [`.ai/context/production_vision.md`](../.ai/context/production_vision.md)).

## The shape (as it runs)

```text
                      one process: python main.py
   ┌──────────────────────────────────────────────────────────────┐
   │  Telegram bot   +   Control API (:9003, also serves Web UI)   │
   │                 +   mesh task server (:9002)                   │
   └──────────────────────────────────────────────────────────────┘
                                  │  (assigns tasks)
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
