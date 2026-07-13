# Screens & Components Tour

Route-by-route and component-group map. Read [`OVERVIEW.md`](OVERVIEW.md) first
for the layer names this doc assumes (domain / transport / hooks).

## Routes → screens

| Route | Screen file | Shell? | What it is |
|---|---|---|---|
| `/sessions` | `screens/SessionsScreen.tsx` | tab | Session list, grouped/filtered by target, closed sessions collapsed. |
| `/sessions/:id` | `screens/SessionDetailScreen.tsx` | full-screen | The chat/session workspace — see below, the largest screen. |
| `/work` | `screens/WorkScreen.tsx` | tab | Read-only Case inbox, grouped by attention bucket (A28). |
| `/work/:id` | `screens/WorkDetailScreen.tsx` | full-screen | One Case: header, lineage, ledger, audit timeline. |
| `/system` | `screens/SystemScreen.tsx` | tab | Nodes/targets, jobs, backend usage, push settings — infra-focused. |
| `/tasks` | — | redirect | → `/system`. Tasks was folded into Session Detail + System (`.ai/CONTEXT.md` #36). |
| (linked from Files tab in Session Detail) | `screens/FilesScreen.tsx` | — | Artifact/file-change browser (`useArtifacts`/`useArtifact`). |

Full-screen routes (`/sessions/:id`, `/work/:id`) sit **outside**
`MobileAppShell` — no bottom nav, back-stack navigation via a `ChevronLeft`
button, per the mobile back-stack model. Tab routes render inside
`MobileAppShell` (persistent `ConnectionBanner` + `BottomNavigation`).

### SessionDetailScreen — the core workspace

`screens/SessionDetailScreen.tsx` has three tabs (`chat | files | info`):

- **chat** — `components/timeline/SessionTimeline.tsx` (message thread, built
  from `useSessionTimeline` — see `DATA_FLOW.md` "Two timelines") +
  `components/timeline/Composer.tsx` (input, submit/stop, file upload).
- **files** — file changes for this session's artifacts.
- **info** — `components/timeline/SessionTurns.tsx` (LLM turn/telemetry list,
  `useSessionTurns`) + `components/system/JobsPanel.tsx` scoped to the session
  + session metadata (repo, model, affiliation).

Sheets opened from the top bar: `ModelPickerSheet` (parity with Telegram
`/model`), `GitPanelSheet` (parity with `/git_status`, `/commit`,
`/commit_all` — routed through `POST /api/sessions/{id}/inspect` so it runs on
the session's **owning node**, not the gateway host).

## Component groups

```
components/
  shell/     app chrome — always mounted or gate-mounted
  sessions/  session list row + the 3 action sheets (new/model/git)
  timeline/  the chat surface (composer, message thread, turn list)
  system/    infra panels (jobs, backend usage, node detail, push)
  work/      Case/Work read-only views (A28)
  ui/        primitives — Button, StatusChip, SectionHeader
```

### `shell/` — app chrome

| File | Role |
|---|---|
| `MobileAppShell.tsx` | root layout — phone-width column, banner + nav persist, content scrolls. |
| `TokenGate.tsx` | blocks all `/api/*` calls until a `DASHBOARD_TOKEN` is present. |
| `CompactTopBar.tsx` | sticky frosted header — title + optional mono subtitle + right slot. |
| `ConnectionBanner.tsx` | derives connection state from the live poll (auth failure / unreachable / offline-with-cache) — stale data must read as visibly distinct from current. |
| `BottomNavigation.tsx` | the 3 tabs: Work, Sessions, System. |
| `TargetSelector.tsx` | pill-chip row filtering Sessions by target/node; binds live to `useTargets`. |

### `sessions/`

| File | Role |
|---|---|
| `SessionRow.tsx` | one row in the Sessions list. |
| `NewSessionSheet.tsx` | guided wizard mirroring Telegram `/session_new` — backend → node (if remote workers exist) → repo (auto-discovered or free-text path). |
| `ModelPickerSheet.tsx` | parity with `/model` — model catalog for the session's backend. |
| `GitPanelSheet.tsx` | parity with `/git_status` / `/commit` / `/commit_all`. |

### `timeline/`

| File | Role |
|---|---|
| `SessionTimeline.tsx` | renders the chat thread from `useSessionTimeline` (real transcript + optimistic sends + pending approvals + one live "running" indicator). |
| `Composer.tsx` | input box — submit/stop mutation, file upload, draft persistence via `draftStore`. |
| `SessionTurns.tsx` | LLM turn/telemetry list (Feature #37) — token usage per turn. |
| `RichText.tsx` | shared text-rendering helper (`lib/richText.ts`) for message bodies. |

### `system/`

| File | Role |
|---|---|
| `JobsPanel.tsx` | parity with `/jobs` — running + recently-finished watched jobs. Headerless by design (parent owns the one `SectionHeader`). |
| `BackendUsagePanel.tsx` | Backend Account + Usage (#30/#33) — **provable facts only**; unknown limits render literally as "unknown", never fabricated. |
| `NodeDetailSheet.tsx` | parity with `/node <id>` — backends, repos, load, heartbeat, reusing the already-fetched `Target` (no extra call) + `useProjects`. |
| `PushSetting.tsx` | Web Push toggle (#21) — only offers a button when push is genuinely available; otherwise states the honest unavailable-reason. |

### `work/` (A28 — read-only Case surface)

| File | Role |
|---|---|
| `WorkCaseRow.tsx` | one case row in the inbox — title/bucket/stage, no actions. |
| `CaseLineage.tsx` | compact parent/self/children tree from `/api/work/{id}/graph` — navigation only, not an editable DAG. |
| `CaseLedgerView.tsx` | the authoritative case↔entity ledger grouped by type; empty sections render explicit "none linked". |
| `CaseTimelineView.tsx` | append-only audit trail (`flow_events`) — does not duplicate the ledger's evidence links. |
| `ToneBadge.tsx` / `SessionAffiliationLabel.tsx` | bucket/role chips; the latter labels a session's Case role on the Sessions screen. |

### `ui/` — primitives

`Button.tsx`, `SectionHeader.tsx` (collapsible section pattern used across
System/Work), `StatusChip.tsx` (`SessionStatusChip`, `HealthChip`, `StatusDot`
— all derive their color from `domain/status.ts` enums, never ad hoc).

## `lib/` — pure helpers (no React, no fetch)

| File | Role |
|---|---|
| `time.ts` | `relAge()` — coarsest human-readable "how long ago" (never forces the reader to do unit math). |
| `cn.ts` | `clsx` + `tailwind-merge` class combiner (shadcn convention). |
| `richText.ts` | message-body rendering helper. |
| `activityFormat.ts` | formats live SSE activity labels. |
| `sessionActivityPresentation.ts` | maps `SessionActivityItem.kind`/`status` to label + tone for the durable timeline. |
| `workPresentation.ts` | Case bucket/status → label + tone for the Work surface. |
| `jobOwnership.ts` | resolves whether a job belongs to the current session (used to scope `JobsPanel`). |

Each has a co-located `*.test.ts` — these are the highest-value unit tests in
the frontend because the logic is pure and the input/output shapes are exactly
what the domain layer already pins down.

## Related

- [`OVERVIEW.md`](OVERVIEW.md) — stack, layering rule, directory map.
- [`DATA_FLOW.md`](DATA_FLOW.md) — which hooks feed which screens, and why
  chat/activity/timeline are three distinct sources.
- [`DEV_AND_BUILD.md`](DEV_AND_BUILD.md) — running this locally, testing, PWA.
