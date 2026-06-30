# UI-5 — Live activity / logs (off the event stream)

Scope fence per COCKPIT_REFACTOR_SPEC §13: **only the boxes below are in scope.**
Baseline: branch feat/webui-ui0 @ 330a90e (UI-4). Backend deps in system python.

Spec row (§14): *"logs / health / terminal"*. Health is ALREADY live (System screen
→ /api/nodes targets). Terminal is a remote-exec surface = OUT (no backend, security
surface — a future deliberate track, like approvals/H). UI-5's shippable slice is the
**live activity log**: a readable, filterable feed of the operational event spine.

## Design decision (what this is — and is NOT)
The live event stream **already flows app-wide** (UI-2): `useEventStreamContext()`
gives a bounded (500), de-duplicated, canonical `StampedEvent[]` (dotted
`GatewayEvent`s incl. the `system.notice` operational channel) + a `connection`
state, fed by the SSE `/api/events/stream`. UI-5 is therefore a **presentation layer
over data that already exists** — NO new backend for the live feed.

- We render the operational events as a reverse-chronological log with a one-line
  human summary + severity color + timestamp + correlation (session/task id).
- We do NOT surface raw stdout/stderr log LINES — the backend has no line stream;
  `events.ndjson` is operational events, not process output. "Logs" here = the
  operational activity feed, named honestly. (Raw process output stays in the
  artifact's triage/stdout, not exposed — a separate future concern.)
- We do NOT add a 5th bottom tab (spec §5 IA is 4 tabs). The feed lands as a
  **"Live activity" section on the System screen**, replacing the diagnostics
  placeholder copy — logs/health are the same operational surface.
- Optional cold-history: the existing `/api/events?since=0` returns the tail; if the
  live buffer is empty on first open we MAY seed from it. Only if cheap; else skip.

NO new domain types, NO new event types, NO backend route. If a box needs one, the
rule is edit-the-checklist-first (§13), not silent drift.

---

## Box 1 — event → log-line projection (pure, testable)
- [ ] `web/src/transport/eventLog.ts`: pure `toLogLine(StampedEvent) -> LogLine`
  where `LogLine = {id, at, severity, kind, text, sessionId?, taskId?}`. Reuses the
  existing SystemNotice severity for `system.notice`; maps the other dotted
  GatewayEvent variants to a stable one-liner + severity (task.state_changed →
  info/'s state, run.cancelled → warning, target.disconnected → warning, etc.).
- [ ] `eventLog.test.ts`: a representative event of each rendered variant → expected
  severity + text; unknown/again-dropped variants handled (no throw).

Done = exactly: the projector + test. No hook, no screen.
Do NOT touch: eventAdapter, events.ts domain union.
Revert: delete eventLog.ts + test.

## Box 2 — a selector hook over the existing stream
- [ ] `web/src/hooks/useActivityLog.ts`: reads `useEventStreamContext()`, projects
  via toLogLine, returns `{lines: LogLine[] (newest-first), connection}` with an
  optional `{ sessionId?, severity? }` filter. NO new EventSource (reuses the app
  one — a 2nd would double-connect, the very thing eventStreamContext prevents).
- [ ] (Optional) seed from `api.events(token,0)` ONCE if the live buffer is empty,
  behind the same adapter; skip if it complicates the box.

Done = exactly: the hook (+ optional seed). 
Do NOT touch: useEventStream internals (consume its context only).
Revert: delete the hook.

## Box 3 — System screen "Live activity" section
- [ ] SystemScreen: add a "Live activity" SectionHeader + a compact log list bound
  to useActivityLog — severity dot, kind, text, relative time; session/task id links
  where present. Cap the rendered rows (e.g. 100) for the phone.
- [ ] A small filter affordance (All / Attention) is OPTIONAL — only if it stays a
  one-liner; otherwise show All. Replace the "diagnostics arrive in later phases"
  placeholder copy.
- [ ] Empty ("no activity yet") + a reconnecting hint reusing `connection`.
- [ ] tsc + vitest + vite build green.

Done = exactly: System screen shows the live operational feed from the phone.
Do NOT touch: Targets section logic, Files/Tasks/Sessions screens, BottomNavigation.
Revert: restore the placeholder card.

## Gate
- [x] Frontend `tsc` clean + vitest 29 (+6 eventLog) + `vite build` green.
- [x] Live (2026-06-24, Telegram OFF, MESH OFF, port 9003): SSE
  `/api/events/stream` emits real frames (quota.observed, approval.requested/
  granted, mesh_*) and `/api/events?limit` history works — the exact source the
  feed renders. Traced real events through adaptEvent→toLogLine: quota.observed→
  info, approval.requested→"approval requested", mesh_routing_failed→ERROR,
  heartbeat→swallowed (no blank row). Reconnect dedupe is the existing UI-2
  mechanism (unchanged), so it holds.

## Design notes / scope honesty
- Decided AGAINST a 5th bottom tab — spec §5 IA is 4 tabs; the feed is a "Live
  activity" section on the System screen (logs + health = one operational surface).
- "Logs" = the operational EVENT feed, named honestly. Raw process stdout/stderr is
  NOT a line stream the backend exposes; it lives in the artifact triage (UI-4) and
  is not surfaced here. Terminal/remote-exec is OUT (security surface, future track).
- The optional cold-history seed (Box 2) was SKIPPED — the live buffer fills within
  one poll/stream cycle and seeding added a code path for little gain; the empty
  state covers the cold-open second.

ALL BOXES DONE 2026-06-24. System screen shows the live operational feed with
correct severity from the phone. NEXT RUNG = UI-6 (PWA / hardening / push) to ship.
