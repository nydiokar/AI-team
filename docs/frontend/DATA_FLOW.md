# Frontend Data Flow

How data gets from the gateway's Control API into a component, and back out
again for writes. Read [`OVERVIEW.md`](OVERVIEW.md) first for the layer names.

## Read path: poll, not push (mostly)

There is **no websocket for state authority**. `hooks/useLiveData.ts` and
`hooks/useWork.ts` poll `/api/*` via TanStack Query on fixed intervals:

| Tier | Interval | Used for | Why |
|---|---|---|---|
| `POLL_MS` | 3s | sessions, tasks, approvals, artifacts, session messages, session activity, turns, jobs | matches the legacy dashboard's cadence |
| `SLOW_POLL_MS` | 20s | nodes (targets), mesh health | these change on a heartbeat timeout (~90s) or a trend-sample cycle; polling faster just keeps the mobile radio warm for no fresher data |

Each hook: reads the token from `authStore`, calls one `transport/apiClient.ts`
method, pipes the result through the matching `transport/*Adapter.ts`, and
returns the canonical `domain/*` type. **Components never call `apiClient`
directly** — always go through a hook.

Query keys are simple arrays (`["sessions"]`, `["session-activity", sessionId,
limit, cursor]`) — no query-key factory abstraction; this codebase deliberately
keeps that flat (YAGNI at this scale).

Two ergonomic details worth knowing before you add a new hook:
- `placeholderData: (prev) => prev` — keeps the previous page painted during a
  poll refetch so the UI doesn't flash a spinner every 3s.
- `refetchOnReconnect: true` on session-scoped hooks — a persisted query cache
  can paint stale turns after the device was offline; this forces a refetch the
  moment the network returns.

## Live/rolling feed: SSE, separate from state authority

`hooks/useEventStream.ts` opens **one** `EventSource` to
`/api/events/stream?token=...` for the whole app (`EventStreamProvider` in
`hooks/eventStreamContext.tsx` — mounting the hook per-screen would open a
socket each). It:

- adapts every raw event through `transport/eventAdapter.ts` into the dotted
  `GatewayEvent` union (`domain/events.ts`),
- dedupes by a stable raw-event key across reconnects (`transport/eventDedupe.ts`)
  — a reconnect replays the tail from the last offset, so a raw event can
  legitimately arrive twice,
- keeps a bounded rolling log (`MAX_EVENTS = 500`),
- **drops the connection while the tab is hidden** and reconnects on focus —
  a real mobile-battery cost otherwise (the backend pushes a frame ~every 1s).

This feed powers **live activity pills** (`useTaskActivity`, "Using Bash" /
"Thinking…" while a turn runs) and the connection banner. It is explicitly
**not** where session/task state authority lives — that's the polled Query
hooks above. Don't wire a screen's primary data off `useLiveEvents()`.

## Two timelines — don't conflate them

The Session Detail screen renders from **two different sources**, and they
answer different questions:

1. **`useSessionMessages` / `useSessionTimeline`** (hook wraps
   `GET /api/sessions/{id}/messages`) — the actual conversation, reconstructed
   server-side from on-disk artifacts: user instruction → assistant result,
   oldest→newest. This is what makes a Telegram-started session show real
   messages in the Web UI instead of "No activity yet".
2. **`useSessionActivity`** (wraps `GET /api/sessions/{id}/timeline`) — the
   **durable, session-owned execution timeline**: task/job/turn/approval
   facts, each tagged `durability: "durable" | "diagnostic"` and
   `staleness: "fresh" | "stale" | "unknown"`. This is the honesty-first read
   model from the Session State Timeline work
   (`docs/SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md`) — it must not be
   backfilled from the rolling SSE log, which is not durable.

Rolling live events (`useLiveEvents`) are a **third, transient** thing layered
on top for real-time feel (e.g. the current-turn activity pill) — never the
record of what happened.

## Write path: mutations, idempotency, optimistic UI

`hooks/useSessionActions.ts` holds the mutation hooks (submit instruction,
stop, close, restore, compact, set model, inspect). Each one:

1. mints **one `Idempotency-Key` per attempt** (`newIdempotencyKey()` in
   `apiClient.ts`) — a TanStack retry reuses the same key, so a flaky network
   retry can't double-submit; the backend dedupes on it,
2. exposes a `CommandDeliveryState` (`draft → sending → acknowledged | rejected`,
   `domain/status.ts`) so the composer can show progress,
3. invalidates the relevant Query keys on settle so polled state catches up.

Because the backend is whole-turn (no `message.delta`, no
`message.created` event for what you just typed), the composer's optimistic
echo is **client-only state** in `stores/sentStore.ts` — shown immediately,
reconciled once the polled transcript reflects the real turn.

Write failures come back as `{ok:false, reason}` (control API command
envelope) or a FastAPI `{detail}` wrapper; `apiClient.ts`'s `post()` unwraps
both into a single `ApiError` with a human-readable message — components
should render `ApiError.message`, never re-derive copy from a raw reason code.

## Work / Case read model

`hooks/useWork.ts` + `transport/workAdapter.ts` bind the **A27 Work/Case
substrate** (`GET /api/work`, `/api/work/{id}`, `/api/work/{id}/timeline`,
`/api/work/{id}/graph`, `/api/work/affiliations/sessions`) into
`domain/work.ts` types. This is **read-only** — no mutations. Two things to
know before touching it:

- **Honesty-first contract**: every field is derived only from authoritative
  substrate rows (`flow_runs`, `flow_links`, `flow_events`). Missing
  relationships render as empty arrays / `null` — never inferred from
  adjacency (e.g. a session is only "in" a case if `flow_links` says so, not
  because it happened to run a task the case also touched).
- **The substrate only populates when the gateway runs with
  `HARNESS_FLOW_DRIVE` on** (see `docs/ENV_FEATURE_FLAGS.md`). With it off,
  these endpoints return empty lists — which the UI must render as "no work
  tracked yet", not an error state.

## Adapters — what actually happens in translation

Each `transport/*Adapter.ts` is a real translation layer, not a type cast.
Concrete examples worth knowing before extending one:

- `sessionAdapter.ts` splits the backend's one flat `SessionStatus` into the UI's
  two axes (`SessionLifecycle` + `SessionOpState`, `domain/status.ts`) — the A18
  pinned-node-offline states fold into `running` / `failed_attention` rather
  than minting new op-states.
- `eventAdapter.ts` maps ~25 snake_case backend event names onto a much smaller
  dotted `GatewayEvent` union — several backend events collapse into one UI
  event (e.g. `task_received`/`timeout`/`cancelled`/`retry` → one
  `task.state_changed`), and some backend events are swallowed entirely
  (heartbeats).
- `taskAdapter.ts` exposes both `toTasks` (flat list) and `toTaskSections`
  (backend-bucketed `attention/running/queued/recent` — the backend overlays
  each task's owning-session status so e.g. `waiting_for_input` correctly
  lands in `attention`).

If a backend field doesn't cleanly map, the convention is: model the gap
explicitly in the domain type's doc-comment (✅/🟡/❌/⛔ tag + one-line reason),
not to silently coerce or omit it.

## Related

- [`OVERVIEW.md`](OVERVIEW.md) — layer names, directory map, state-kind summary.
- [`SCREENS_AND_COMPONENTS.md`](SCREENS_AND_COMPONENTS.md) — which screen uses
  which hooks.
- [`docs/CONTROL_CONTRACT.md`](../CONTROL_CONTRACT.md) — backend-side contract
  (event envelope, entry points, read model) this layer binds to.
- [`docs/SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md`](../SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md) —
  why the durable timeline / diagnostic-vs-durable split exists.
