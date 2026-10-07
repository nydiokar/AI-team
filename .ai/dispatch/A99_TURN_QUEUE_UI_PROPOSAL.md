# A99 — Session Turn-Queue Frontend: Ground-Up Redesign Proposal (Phase 1)

**Job:** `AGENT_99_TURN_QUEUE_FRONTEND_OVERHAUL` · **Phase:** 1 (discovery + proposal; Phase 2 is operator-gated)
**Author:** worker `b0a1a898f6ab` · **Date:** 2026-10-07 (UTC)
**Backend depended on:** A82 Stage-8a turn queue — MERGED, live (schema 43), single-admission-authority audited
(`.ai/dispatch/A82_STAGE8A_BACKEND_AUDIT.md`), FIFO proven live (`.ai/dispatch/A82_STAGE8A_VALIDATION.md`).
**Status of this doc:** a *from-scratch* redesign of the queue experience. Not a tweak list.

> **`frontend` skill availability.** The mandated `frontend` skill is **NOT installed in this
> environment** — `Skill(frontend)` returned `Unknown skill: frontend`. Per the brief I state this
> explicitly and apply equivalent front-end rigor throughout: information architecture, a complete
> component tree, an exhaustive state/transition model, per-state low-fi wireframes, interaction and
> micro-state handling, loading/error/empty coverage, and WCAG-oriented accessibility notes.

---

## 0. How this was grounded (so every claim is checkable)

- **Live gateway driven** at `http://127.0.0.1:9003` (token from `.env` via
  `config.mesh.dashboard_token` / `DASHBOARD_TOKEN`). A **disposable scratch session**
  `83d17630a0cf` (`repo_path=/home/cifran/dev/AI-team`, backend `claude`) was created, **enrolled,
  and PAUSED before any turn was submitted** so turns stayed `queued` and **no carrier ever
  activated — zero paid execution**. Session **closed** at end of run. This is the method Phase 2
  should reuse for live verification.
- **Observed queue payloads** (verbatim, trimmed):
  - *Empty, enrolled, paused:* `{"turns":[],"count":0,"queued":0,"active_turn_id":null,"active_status":null,"enrolled":true,"paused":true,"hold":null,"effects_failed":0,"effects_failed_turn_id":null}`
  - *Two queued (FIFO), paused:* two summaries, `queue_sequence` 1 & 2, `queue_position` 1 & 2, `status:"queued"`, `turn_source:"human"`, `turn_kind:"instruction"`, `preview` present, `body` absent (detail only).
  - *Submit:* `POST …/turn-requests` → **202**, `{turn_id,status:"queued",revision:1,queue_sequence,queue_position,idempotent_replay:false,source:"operator"}`.
  - *Edit:* `PATCH /api/turn-requests/{id}` + `If-Match:"1"` → **200**, `revision:2`, preview/body updated.
  - *Stale edit:* same `If-Match:"1"` again → **409** `{reason:"ownership_conflict","message":"stale revision","current":{…revision:2…}}`.
  - *Withdraw:* `POST …/{id}/withdraw` + `If-Match:"1"` → **200**, `status:"withdrawn"`, `queue_position:null`, `preview:""`; the open-queue read then reports `count 1 / queued 1` (withdrawn turn drops out of the queue read).
  - *Idempotent replay:* resubmit same `operation_id` → **202**, `idempotent_replay:true`, same `turn_id`, same `accepted_at`.
  - *Errors:* unknown turn → **404**; empty `body` → **422** (`string_too_short`); no token → **401**.
  - *Read-model separation (invariant #11) proven:* the withdrawn turn `task_6f06ada6` **vanished from the
    `turn-requests` queue read** but **still appears in `/api/turns` telemetry** — two genuinely distinct read models.
- **Code read directly** (not just summarized): `web/src/components/timeline/TurnQueuePanel.tsx`,
  `web/src/lib/turnQueue.ts`; integration facts confirmed in `Composer.tsx`, `SessionDetailScreen.tsx`,
  `useSessionActions.ts`, `liveInvalidation.ts`, `useEventStream.ts`, `apiClient.ts`.
- **Backend contract** from `.ai/dispatch/AGENT_82_SESSION_TURN_QUEUE.md` (§9/§10, invariants #11/#12),
  `src/control/routes/turn_requests.py`, `src/control/control_api.py` DTOs, `src/control/turn_queue.py` status sets.

---

## 1. User-journey walkthrough — what a user actually sees today, step by step

Each step names the **route** and the **component** behind it, and what the user observes *now*.

| # | Step | Route / API | Component (file:line) | What the user sees TODAY |
|---|---|---|---|---|
| 1 | **Create / enroll session** | `POST /api/sessions`; enrollment is **flag-gated** (`TURN_QUEUE_ENROLLMENT_ENABLED`) | — | Session opens in `SessionDetailScreen`. **Observed ambiguity:** the explicit `…/turn-requests/enroll` call returns `{"reason":"enrollment_disabled"}`, yet the queue read reports `"enrolled":true` and accepts submissions. The user is given **no indication** a session is "queue-enrolled" vs not. |
| 2 | **Submit turn-1 (idle session)** | `POST /api/instructions` (NOT the `turn-requests` route) via `useSubmitInstruction` | `Composer.tsx` (hook at :69-70; placeholder `"Send an instruction…"` :264) | Textarea with "Send an instruction…". On send, an optimistic bubble is added to the transcript. No queue surface is involved yet. |
| 3 | **Submit turn-2 while busy** | same `POST /api/instructions`; admitted to the managed queue by the single-admission path | `Composer.tsx` | Placeholder flips to `"Task running…"` and the send button becomes **Stop** — **but there is NO signal the submit will QUEUE behind running work.** The user cannot tell "run now" from "line up behind N". This is the central UX failure. |
| 4 | **Watch FIFO activation** | `GET …/turn-requests` key `["session-turn-queue", id]`; SSE `turn_queue_changed` → invalidate | `TurnQueuePanel.tsx` (:168 render gate; :205 `<ol>`) | A panel titled **"Next up · N waiting"** appears *only if* `turnQueue?.enrolled` (`SessionDetailScreen.tsx:1332`) **and** the panel's own gate passes (`:168`). The active (running) turn and the waiting turns are rendered in **one flat `<ol>`** with only a small colored badge to distinguish `Working` from `Waiting`. There is **no "Now running" vs "Up next" separation** and the `active_turn_id`/`active_status` page fields are **never used to anchor the running turn**. |
| 5 | **Edit a queued turn** | `GET /api/turn-requests/{id}` (load body) → `PATCH` + `If-Match` | `TurnQueuePanel.tsx` `startEdit`/`save` (:101-137) | An **Edit** button appears only for `queued` human instructions (`isEditableTurn`, `turnQueue.ts:77`). Inline textarea; on 409 the draft is kept and a notice shown. Works, but the control is buried in the flat list and there is no undo. |
| 6 | **Pause / resume** | `POST …/turn-requests/pause` \| `/resume` | `TurnQueuePanel.tsx` `toggleQueue` (:159-164); button :180-188 | A single **"Pause queue / Resume queue"** pill in the panel header. Hold reason shown as a one-line warn (`:190-194`). Correct, but disconnected from the Composer and from Stop. |
| 7 | **Stop** | `POST /api/sessions/{id}/stop` via `useStopSession` | **`Composer.tsx`** (NOT the queue panel) | Stop lives in the **Composer**, shown when `session.opState === "running"` (`SessionDetailScreen.tsx:623`). So **three different interventions — Stop (active), Pause (queue), Withdraw (one waiting turn) — live in two different components with no shared mental model.** |
| 8 | **Recovery-required turn** | `POST /api/turn-requests/{id}/resolve-recovery` | `TurnQueuePanel.tsx` resolver (:314-366) | A red **"Resolve…"** expands to a checkbox + **Mark failed / Mark cancelled**. The checkbox copy (`:325`) is the *only* explanation of the consequence, and **the `requeue` decision the backend supports is not offered at all** (`resolve` is typed `"failed" \| "cancelled"`, `:145`; `apiClient.resolveTurnRecovery` :278-285 has no `requeue`). A `claimed`-but-unstarted turn that is *requeue-eligible* has **no UI path**. |
| 9 | **Empty / error / offline-carrier** | — | `TurnQueuePanel.tsx:168` | **Empty enrolled queue renders NOTHING** (`return null` when `cards.length===0 && !held && !effectsFailed`). There is **no loading skeleton** (the panel takes a required `page` prop; while the query is in flight the panel is simply absent). `blocked_reason:"carrier_offline"` is mapped to a label (`turnQueue.ts:111`) but only as a small sub-line on a card; there is no first-class "carrier offline" state for the *active* turn, and **`effects_error` (available on the detail DTO) is never rendered** — the alert just says "check the transcript" (`turnQueue.ts:121`). |

---

## 2. Defect inventory (numbered, grounded)

Severity: **F**=functional (not 100% working) · **UX**=experience/flow · **UI**=hierarchy/affordance/polish · **A11y**.

### Functional (feature is not fully usable)

1. **[F] No `requeue` recovery path.** Backend `resolve-recovery` accepts `failed|cancelled|requeue`
   (`turn_requests.py:39`, `TurnRecoveryResolveBody`), and `requeue` is the *only* valid resolution for a
   `claimed`/unstarted turn. The UI never offers it: `resolve()` is typed `"failed" | "cancelled"`
   (`TurnQueuePanel.tsx:145`) and `api.resolveTurnRecovery` (`apiClient.ts:278-285`) has no `requeue`.
   **Repro:** a recovery turn that is unstarted can only be force-failed/cancelled — the safe "release it
   back to the queue" action is unreachable. *(Frontend-only gap — backend already supports it.)*
2. **[F] Empty-but-enrolled queue is invisible.** `TurnQueuePanel.tsx:168` returns `null` when the queue
   is empty and not held. **Observed:** an enrolled, empty session shows no queue affordance at all, so a
   user cannot discover that this session queues turns, cannot see it's enrolled, and has no entry point.
3. **[F] No loading state.** The panel requires a resolved `page` prop; during the initial
   `useSessionTurnQueue` fetch the panel is simply not rendered (`SessionDetailScreen.tsx:1332`). On a slow
   network the queue flashes in late with no skeleton — looks like "nothing is queued."
4. **[F] `effects_error` is never surfaced.** `effects_failed`/`effects_failed_turn_id` drive a one-line
   banner (`turnQueue.ts:117-123`) that says "check the transcript", but the detail DTO's `effects_error`
   (`control_api.py:451`, confirmed in the live detail payload as a field) is never fetched/shown. A
   post-commit reply-delivery failure gives the operator **no actionable detail and no drill-down**.
5. **[F] Enrollment semantics are undefined in the UI.** Observed: `enroll` route returns
   `enrollment_disabled` while the session still reports `enrolled:true` and accepts turns. The UI gates
   the *entire* experience on `turnQueue?.enrolled` (`SessionDetailScreen.tsx:1332`) without ever
   explaining or controlling enrollment — an un-enrolled session silently has **no queue UI whatsoever**.

### UX (flow, feedback, mental model)

6. **[UX] The Composer gives no "will queue" signal.** `Composer.tsx` only swaps placeholder
   `"Send an instruction…"`→`"Task running…"` and shows Stop (:264, :623). It never tells the user a submit
   will be **appended to a FIFO queue behind N waiting turns**. The whole point of the feature (line up
   multiple turns against a busy session) is invisible at the exact moment the user acts.
7. **[UX] The active turn is not distinguished from waiting turns.** The panel renders one flat `<ol>`
   (`TurnQueuePanel.tsx:205-370`); the `active_turn_id`/`active_status` fields are never used to pin a
   "Now running" card. Users cannot answer "what is the agent doing *right now*?" at a glance.
8. **[UX] Three interventions, two surfaces, no model.** Stop (Composer), Pause (panel header), Withdraw
   (per-card) are scattered. There is no single place that says "running now · paused · N waiting" with the
   matching controls co-located. (Steps 6–7 above.)
9. **[UX] `running` under-reports activity.** `Composer` `running` is `opState==="running"`
   (`SessionDetailScreen.tsx:623`), and `queueOpState` returns `"running"` **only** for `claimed|running`
   (`turnQueue.ts:153-160`) — a `pending` (activated, awaiting carrier) or a fully `queued` backlog reads as
   **idle**. A session with 3 queued turns + a `pending` head looks like nothing is happening.
10. **[UX] Recovery consequence is under-explained.** The failed/cancelled distinction is carried entirely
    by one checkbox sentence (`TurnQueuePanel.tsx:325`); there is no "what happened / what each choice does"
    affordance, no link to the turn's diagnostics.
11. **[UX] No undo / no confirmation symmetry.** Withdraw has an inline confirm (`:282-299`); Pause and
    recovery resolutions do not, and none are reversible within a grace window.

### UI (hierarchy, affordance, polish)

12. **[UI] Flat visual hierarchy.** Status is a single pill (`LABEL_TONE`, `TurnQueuePanel.tsx:36-42`);
    position is `#{queue_position ?? queue_sequence}` (`:210`) which can jump when `queue_position` is
    `null` (observed `null` on a withdrawn/active turn) and silently falls back to the monotonic
    `queue_sequence` — a different number from the run-order position.
13. **[UI] Cramped, scroll-trapped container.** `max-h-[40vh] overflow-y-auto` on the whole section
    (`:173`) means the Pause control scrolls away with the list on small viewports; no sticky header.
14. **[UI] Destructive styling is weak.** Withdraw / Mark failed / Resolve are low-contrast text or thin
    bordered pills (`:288, :332, :360`) — destructive and non-destructive actions read nearly identically.
15. **[UI] No queue-liveness indicator.** SSE invalidation is wired (`useEventStream.ts:129` opens
    `/api/events/stream`; `liveInvalidation.ts:118` invalidates `["session-turn-queue", id]`), but the UI
    gives **no sign whether updates are live or whether the stream dropped** (in which case the user is on
    the 60s safety-net poll `SAFETY_NET_MS`, `turnQueue.ts:21`, without knowing it).

### Accessibility

16. **[A11y] Status transitions are not announced reliably.** The `<ol>` has `aria-live="polite"`
    (`:205`), but a card changing `Waiting→Working` in place is a text/class change inside a list item;
    SR users get weak/absent announcements for the single most important event (a turn starting).
17. **[A11y] No focus management on inline confirms.** Entering withdraw-confirm, edit, or
    recovery-resolve (`:282, :227, :314`) does not move focus to the new controls; keyboard users must hunt.
18. **[A11y] Destructive actions rely on color alone** (red text, `text-bad`) without a non-color cue
    (icon/label), failing WCAG 1.4.1.

---

## 3. The redesign (ground-up)

### 3.1 Design thesis (one paragraph)

Today the queue is an afterthought panel bolted beside a Composer that doesn't know the queue exists.
The redesign treats the enrolled session as **one object with one spine: _what ran · what's running
now · what's up next · how I enqueue more_**. Everything collapses into a single always-present
**Turn Rail** docked directly above the Composer, and the **Composer becomes the rail's enqueue head**
— so the act of typing a turn and the queue it joins are the same surface. The rail answers three
questions at a glance: *Is the agent working right now?* (a single prominent **Now** card with live
status + Stop/Resolve), *What did I line up?* (an ordered **Up next** list with inline edit/withdraw and
true run-order position), and *Is the whole queue healthy?* (a compact **control bar**: live-dot,
queued count, pause/resume, hold/effects banners). It binds to the **existing** `turn-requests` API and
the **existing** `["session-turn-queue", sessionId]` query key with **existing** SSE invalidation — no
new routes, no touching `/api/turns` telemetry.

### 3.2 Information architecture

```
SessionDetailScreen
└── TurnRail  (always rendered for an enrolled session — this is the fix for defects #2,#3)
    ├── QueueControlBar         ← queue-level truth + controls (count · pause/resume · live-dot · banners)
    ├── NowZone                 ← the single active slot (active_turn_id), or a clear "idle/ready" line
    │     └── ActiveTurnCard    ← live status, elapsed, source; Stop (running) / Resolve (recovery)
    ├── UpNextZone              ← FIFO queued turns
    │     └── QueuedTurnList (<ol>)
    │           └── QueuedTurnCard  ← position, source, preview; Edit / Withdraw inline
    └── Composer (enqueue head) ← QueueTargetHint: "Run now" | "Add to queue · behind N"
```

Rationale: a strict top-to-bottom reading order = **past (transcript, above the rail) → now → next →
compose**. The Composer sits at the bottom where the user already types; the rail grows upward above it.
This keeps the historical-exchange read model (transcript) and the queue read model **visually and
structurally distinct** (honoring §10 / invariant #11): the transcript owns *finished exchanges*; the
rail owns *open turns* (`queueOwnedIds` dedup stays exactly as-is, `turnQueue.ts:176`).

### 3.3 Complete state / transition model

**Turn-level (per card)** — mirrors `src/control/turn_queue.py`; labels from `turnCardLabel`
(`turnQueue.ts:68`). The redesign keeps the conservative labeling and adds zone placement:

| Backend status | Card label | Zone | Allowed actions |
|---|---|---|---|
| `queued` | **Waiting** | Up next | Edit, Withdraw (human instruction only) |
| `pending` | **Starting** | Now | Stop (cancels); shows `carrier_offline` if blocked |
| `claimed` | **Starting** | Now | Stop |
| `running` | **Working** | Now | Stop |
| `recovery_required` | **Recovery required** | Now | Resolve → failed \| cancelled \| **requeue** |
| `completed` | (yielded to transcript) | — | — (card drops via `queueOwnedIds`) |
| `failed` / `cancelled` / `failed_node_offline` | **Finished** (transient) | fades out | — |
| `withdrawn` | removed from queue read | — | — (confirmed live: drops from queue) |

**Queue-level (QueueControlBar)** — derived from the page object:

| Queue state | Source | Presentation |
|---|---|---|
| **Loading** | query `isLoading` | skeleton rail (fix #3) |
| **Error** | query `isError` | inline error + Retry (invalidate key) |
| **Empty, ready** | `enrolled && count===0 && !paused` | "Queue is empty — your next turn runs when you send it." (fix #2) |
| **Active** | `active_turn_id != null` | Now card populated |
| **Backlog** | `queued > 0` | Up next list; control bar shows "· N waiting" |
| **Paused / hold** | `paused \|\| hold != null` | amber control bar + `blockedReasonLabel(hold)`; Resume CTA |
| **Carrier offline** | active turn `blocked_reason==="carrier_offline"` | Now card shows "Carrier offline — waiting for it to return" (first-class, fix #9/#4-adjacent) |
| **Effects failed** | `effects_failed > 0` | red banner + **"View error"** → fetches detail, shows `effects_error` (fix #4) |
| **Live / reconnecting** | `useEventStream` connection state | green/amber live-dot (fix #15) |
| **Not enrolled** | `enrolled===false` | rail collapses to a single explanatory line (no silent disappearance, fix #5) |

**Transitions** (what the UI reacts to): every `turn_queue_changed` SSE event invalidates
`["session-turn-queue", id]` (non-terminal) or the full session set (terminal) exactly as
`liveInvalidation.ts` already does — the redesign **changes none of this** (invariant #12 preserved).

### 3.4 Component tree (with responsibilities & data)

```
TurnRail(sessionId)                         // owns useSessionTurnQueue(sessionId); passes page down
  state: none (pure render of query + SSE)
  ├─ QueueControlBar(page, live)            // count, pause/resume (api.pause/resumeTurnRequests), hold/effects banners, live-dot
  ├─ NowZone(active)                         // active = page.turns.find(t => t.id === page.active_turn_id)
  │   └─ ActiveTurnCard(active)
  │       ├─ TurnStatusBadge(status)         // from turnCardLabel
  │       ├─ ElapsedClock(started_at)        // "working 0:12"
  │       ├─ StopControl()                   // api stop (running/starting)
  │       └─ RecoveryResolver(turn)          // failed | cancelled | requeue  (fix #1)
  ├─ UpNextZone(queued[])                     // page.turns.filter(queued & owned)
  │   └─ QueuedTurnList (<ol aria-live>)
  │       └─ QueuedTurnCard(turn)
  │           ├─ positionBadge(queue_position)
  │           ├─ TurnEditor(turn)             // GET detail → PATCH If-Match (unchanged semantics)
  │           └─ WithdrawControl(turn)        // POST withdraw If-Match
  ├─ QueueEmptyState() / QueueLoadingSkeleton() / QueueErrorState()
  └─ (Composer rendered by SessionDetailScreen, fed queue summary)
Composer(sessionId, queueSummary)             // queueSummary = {active, queued} derived from the same query
  └─ QueueTargetHint(queueSummary)            // "Run now" vs "Add to queue · behind N" (fix #6)
```

Pure logic stays in `web/src/lib/turnQueue.ts` (keep every export; **add** a `recoveryOptions(turn)`
helper returning the legal decisions for a given status, and a `queueActivity(page)` helper that reports
`idle | starting | working | recovery | paused` so the Composer hint and the control bar share one
source of truth — fixing the under-reporting in #9 without widening `queueOpState`'s session-status role).

### 3.5 Low-fi wireframes (per state)

**(a) Active + backlog (the common case)**
```
┌─ Turn queue ───────────────────────────── ● live ──┐
│  Agent is working · 2 waiting      [ Pause queue ]  │
├─────────────────────────────────────────────────────┤
│  NOW                                                  │
│  ┌───────────────────────────────────────────────┐  │
│  │ ● Working · 0:14      You        [  Stop  ]     │  │
│  │ "Refactor the admission path and add a test…"   │  │
│  └───────────────────────────────────────────────┘  │
│  UP NEXT                                              │
│  ┌─ #2  You ───────────────────────── Edit  Withdraw┐│
│  │ "Then update the CHANGELOG entry for A82."        ││
│  └───────────────────────────────────────────────── ┘│
│  ┌─ #3  Agent 1f9bce3f ──────────────────────────────┐│
│  │ "Run the targeted vitest suite and report."       ││
│  └───────────────────────────────────────────────── ┘│
└───────────────────────────────────────────────────────┘
┌─ Composer ────────────────────────────────────────────┐
│ [ Add to queue · behind 2 … ]            [ ⤒ Queue ]   │
└───────────────────────────────────────────────────────┘
```

**(b) Idle / empty but enrolled (fixes the invisible-empty defect #2)**
```
┌─ Turn queue ─────────────────────────────── ● live ─┐
│  Idle · queue empty                                   │
│  Your next turn runs as soon as you send it.          │
└───────────────────────────────────────────────────────┘
┌─ Composer ────────────────────────────────────────────┐
│ [ Send an instruction (runs now) … ]      [ ▶ Run ]    │
└───────────────────────────────────────────────────────┘
```

**(c) Recovery required (adds `requeue`, explains consequences — fixes #1,#10)**
```
│  NOW                                                  │
│  ┌───────────────────────────────────────────────┐  │
│  │ ▲ Recovery required        You                  │  │
│  │ "Deploy the gateway and verify /health."        │  │
│  │ Held: outcome uncertain — the agent may or may  │  │
│  │ not have finished. Choose how to resolve:        │  │
│  │  ○ Requeue  — unstarted; run it again (safe)     │  │  ← only if status==claimed/unstarted
│  │  ○ Cancelled — drop it; no result recorded       │  │
│  │  ○ Failed   — mark failed; will not re-run        │  │
│  │  [ ] I confirmed the agent has stopped           │  │
│  │                         [ Resolve ]  [ Not now ] │  │
│  └───────────────────────────────────────────────┘  │
```

**(d) Paused / hold**
```
┌─ Turn queue ──────────────────────────────── ● live ─┐
│  ⏸ Paused — nothing new starts     [ Resume queue ]   │
│  Stopped by operator                                  │   ← blockedReasonLabel(hold)
├───────────────────────────────────────────────────────┤
│  NOW   (empty — nothing activates while paused)        │
│  UP NEXT   #1 #2 #3 …  (editable/withdrawable)          │
└───────────────────────────────────────────────────────┘
```

**(e) Carrier offline (active turn blocked)**
```
│  NOW                                                  │
│  ┌───────────────────────────────────────────────┐  │
│  │ ◌ Starting · carrier offline      You            │  │
│  │ Waiting for the carrier to return. The turn      │  │
│  │ keeps its place; nothing is lost.   [ Stop ]     │  │
│  └───────────────────────────────────────────────┘  │
```

**(f) Effects-failed banner with drill-down (fixes #4)**
```
│  ⚠ 1 finished turn: reply delivery failed   [ View error ]│
│     (expanded) effects_error: "history write timed out…"   │
```

**(g) Loading skeleton / (h) Error**
```
(g)  ░ Turn queue ░░░░░░░░░   (h)  Turn queue — couldn't load.   [ Retry ]
     ░░░ NOW ░░░░░░░░░░░░░░
     ░░░ ░░░ ░░░ ░░░ ░░░
```

**(i) SSE disconnected (stale-risk, fix #15)**
```
┌─ Turn queue ─────────────────── ◐ reconnecting… ─────┐
│  (data may be up to 60s old)                          │
```

### 3.6 Interaction & micro-states

- **Composer hint** recomputes from the same `["session-turn-queue", id]` query: `queued===0 && !active`
  → "Run now" (▶); otherwise "Add to queue · behind N" (⤒). Submitting while busy keeps the optimistic
  bubble logic but the card ownership (`queueOwnedIds`) prevents duplication (unchanged).
- **Stop** confirms inline when a turn is `running` ("Stop the running turn?"), fires immediately when
  merely `starting`/`pending`.
- **Edit** keeps the proven If-Match flow verbatim (observed 200→rev2, 409→keeps draft). On 409 the editor
  shows the live diff hint and an explicit **Reload** button (fixes the dead-end at `TurnQueuePanel.tsx:131-133`).
- **Withdraw** keeps the two-step confirm; on success drops the optimistic bubble (unchanged).
- **Recovery** offers only the *legal* decisions for the status (`recoveryOptions`): unstarted→`requeue`
  (+`cancelled`); started/`recovery_required`→`failed`/`cancelled`. `acknowledge_uncertain` required for
  started turns (matches backend 409 contract).
- **Optimism & dedup** unchanged — the transcript still takes ownership on terminal (`queueOwnedIds`).

### 3.7 Accessibility

- **Now card is an `aria-live="assertive" role="status"` region** so "Working → Recovery required"
  announces immediately (fixes #16). The Up-next list stays `polite`.
- **Focus management:** opening Edit moves focus into the textarea; opening Withdraw-confirm or
  Recovery moves focus to the first action; Esc restores focus to the trigger (fixes #17).
- **Every control is a real `<button>`/`<label>`** with an explicit accessible name
  (`Stop turn #1`, `Resume queue`, `Resolve turn #1`); status conveyed by **icon + text + color**,
  never color alone (fixes #18). Contrast tiers: destructive = filled red; primary = filled accent;
  neutral = outline.
- **Keyboard:** Cmd/Ctrl+Enter submits the Composer; Esc cancels any inline editor/confirm; Tab order
  follows the visual spine Now → Up next → Composer.
- Live-dot has a text equivalent (`title`/visually-hidden "Live" / "Reconnecting — data may be stale").

### 3.8 API & query-key binding (no new backend)

| UI action | Existing route | Query effect |
|---|---|---|
| Read queue | `GET /api/sessions/{id}/turn-requests` | `useSessionTurnQueue` key `["session-turn-queue", id]` (unchanged) |
| Enqueue (Composer) | existing submit path (`POST /api/instructions` enrolled branch → single-admission) | invalidate `["session-turn-queue", id]` (already in `useSubmitInstruction`) |
| Load body for edit | `GET /api/turn-requests/{id}` | — |
| Edit | `PATCH /api/turn-requests/{id}` + `If-Match` | invalidate key |
| Withdraw | `POST /api/turn-requests/{id}/withdraw` + `If-Match` | invalidate key |
| Pause / Resume | `POST …/turn-requests/pause` \| `/resume` | invalidate key |
| Resolve recovery | `POST /api/turn-requests/{id}/resolve-recovery` (`failed\|cancelled\|requeue`) | invalidate key |
| Live refresh | SSE `turn_queue_changed` via `useEventStream` → `liveInvalidation.ts` | unchanged (invariant #12) |
| Telemetry | `GET /api/turns` + `useSessionTurns` | **separate read model — untouched** (invariant #11) |

`/api/turns` stays telemetry; the rail never calls it. The 60s `SAFETY_NET_MS` fallback and SSE
invalidation are preserved exactly — **no regression to 3s polling.**

---

## 4. Backend / DTO dependencies the redesign needs

The redesign needs **no new backend routes** and **no telemetry-DB changes**. Honest list of what it
depends on, separating "frontend-only" from "genuine backend ask":

1. **(Frontend-only, not a backend dep)** Expose `requeue` in `apiClient.resolveTurnRecovery` and the
   recovery UI. Backend already accepts it (`turn_requests.py:39`).
2. **(Frontend-only)** Render `effects_error` from the **existing** `GET /api/turn-requests/{id}` detail
   DTO (`control_api.py:451`) behind the "View error" drill-down. No backend change.
3. **(Frontend-only)** Surface `useEventStream` connection state for the live/reconnecting dot. No backend change.
4. **(Clarification — may need a backend/contract decision, NOT invented here):** **Enrollment
   semantics.** Observed: `…/turn-requests/enroll` returns `enrollment_disabled`
   (`TURN_QUEUE_ENROLLMENT_ENABLED`) while the queue read reports `enrolled:true`. Before building the
   "not enrolled" rail state, the operator should confirm: *is enrollment automatic for new sessions, is
   the flag retiring the manual enroll route, and what should `enrolled:false` mean to a user?* This is a
   **contract question**, surfaced as a dependency — not a route I will invent.
5. **(Explicitly NOT requested):** **No reorder route.** Drag-to-reorder would violate FIFO / the
   single-admission ordering invariant the backend guarantees (`A82_STAGE8A_BACKEND_AUDIT.md`). The
   redesign deliberately keeps strict FIFO and does **not** ask for a reorder endpoint.
6. **(Rely-on, already present):** `blocked_reason` must be populated on the **active** turn summary for
   the first-class "carrier offline" Now state. Observed present on queued summaries; Phase 2 must
   live-verify it is also set on the active turn when a carrier drops (verification item, not a new DTO).

---

## 5. Phase-2 implementation plan (operator-approvable as-is)

**Branch/rollout:** one `feat/a99-turn-queue-ui` branch → PR → self-merge → deploy via
`deploying-the-gateway`. No flag needed (the surface already gates on `enrolled`); ship behind the
existing enrollment gate. Live-verify with the **paused-queue scratch-session method** from §0 (zero paid
tokens). No telemetry or backend code touched.

**Component-by-component order (each step independently shippable & testable):**

1. **`turnQueue.ts` pure helpers** — add `recoveryOptions(status)`, `queueActivity(page)`,
   `composerTarget(page)` (→ `"run_now" | {queueBehind:n}`). *Vitest:* extend `turnQueue.test.ts`
   (table-driven over every status; asserts zone placement, legal recovery decisions, composer hint).
2. **`apiClient.resolveTurnRecovery`** — accept `"failed"|"cancelled"|"requeue"`. *Vitest:* adapter test +
   MSW asserting the request body.
3. **`QueueControlBar`** — count, pause/resume, hold/effects banners, live-dot. *Vitest:* renders each
   queue state (paused, hold, effects_failed, live/reconnecting) from fixture pages.
4. **`ActiveTurnCard` + `NowZone`** — anchored on `active_turn_id`; Stop; `role="status"` live region;
   carrier-offline state. *Vitest:* status→label/zone; Stop calls stop; a11y (name + live region).
5. **`RecoveryResolver`** — legal-decision set incl. `requeue`; acknowledge gating. *Vitest:* option set
   per status; resolve payloads; disabled-until-ack.
6. **`QueuedTurnCard` + `QueuedTurnList` + `TurnEditor`/`WithdrawControl`** — port proven If-Match flows;
   add Reload-on-409 and focus management. *Vitest:* edit 200/409-keeps-draft; withdraw drop; focus moves.
7. **`QueueEmptyState` / `LoadingSkeleton` / `ErrorState`** — always-rendered rail. *Vitest:* empty/loading/error.
8. **`Composer` `QueueTargetHint`** — "Run now" vs "Add to queue · behind N" from the shared query.
   *Vitest:* hint text across idle/active/backlog.
9. **`TurnRail` container + `SessionDetailScreen` wiring** — replace `TurnQueuePanel` mount; keep the
   `queueOwnedIds` dedup and transcript boundary intact. *Vitest:* integration render with a full page fixture.

**Test strategy guardrails:** Vitest + RTL + MSW only (no paid path, no e2e). Reuse/extend the existing
`turnQueue.test.ts` and `liveInvalidation.test` so **invalidation behavior is regression-locked**
(invariant #12). Component tests assert accessibility names and the live region. **Do not run the full/e2e
suite.** Final live check: scratch session, paused, drive submit/edit/withdraw/pause and *visually* verify
the rail; resume only on a throwaway if the operator wants a paid FIFO-activation screenshot.

**Rollout order:** steps 1–2 (pure/adapters, invisible) → 3–8 (new components, not yet mounted) →
9 (swap the mount) in a single PR, or split 1–2 as a pre-PR if the operator prefers smaller diffs.

---

## 6. Hand-back summary

- **Top 3 functional defects:** (1) no `requeue` recovery path though the backend supports it
  (`TurnQueuePanel.tsx:145`, `apiClient.ts:278`); (2) an enrolled-but-empty queue renders nothing
  (`TurnQueuePanel.tsx:168`) so the feature is undiscoverable; (3) no loading state — the panel is simply
  absent until the query resolves (`SessionDetailScreen.tsx:1332`), reading as "nothing queued."
- **Proposed IA (one paragraph):** collapse the Composer + "Next up" panel + orphaned Stop into one
  always-present **Turn Rail** above the Composer with a single spine — a **control bar** (live-dot, count,
  pause/resume, hold/effects banners), a prominent **Now** card for the single active slot (live status +
  Stop/Resolve incl. `requeue`), an ordered **Up next** FIFO list (inline edit/withdraw, true run-order
  position), and the **Composer as the enqueue head** showing "Run now" vs "Add to queue · behind N" —
  all bound to the existing `turn-requests` routes, the `["session-turn-queue", id]` key, and the existing
  SSE invalidation, with `/api/turns` telemetry left untouched.
- **Phase-2 plan headline:** nine independently-shippable steps on `feat/a99-turn-queue-ui` (pure helpers →
  adapter → control bar → Now/recovery → Up-next/editor → empty/loading/error → Composer hint → rail
  swap), each Vitest-covered with MSW, invalidation regression-locked, live-verified via the zero-paid
  paused-scratch-session method — no new backend routes, no telemetry changes.
```
