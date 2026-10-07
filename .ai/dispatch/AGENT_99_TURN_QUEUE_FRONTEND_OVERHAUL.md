```yaml
job_id: AGENT_99_TURN_QUEUE_FRONTEND_OVERHAUL
created_at: "2026-10-07T15:59:39.560284+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: 
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: [".ai/dispatch/A99_TURN_QUEUE_UI_PROPOSAL.md"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-07T16:37:18.332251+00:00"
```

# DISPATCH — AGENT_99_TURN_QUEUE_FRONTEND_OVERHAUL

**Level:** 3 (UX/UI redesign crossing many web/ files, operator-flagged) — operator has
authorized the work; Phase 1 (proposal) is the approval artifact for Phase 2 (build).
**Type:** design-first overhaul (discovery + proposal → implementation)
**Authored:** 2026-10-07
**Depends on:** A82 Stage-8a (the turn-queue backend) is MERGED + live (schema 43, FIFO proven
in `.ai/dispatch/A82_STAGE8A_VALIDATION.md`). This job is the FRONTEND for that backend.
**Branch:** Phase 1 docs-only → `main`. Phase 2 → one `feat/a99-turn-queue-ui` branch + PR.

## Read this first
- `.ai/dispatch/AGENT_82_SESSION_TURN_QUEUE.md` — the backend contract. Especially design §9/§10
  (route table, read-model split) and invariant **#11** (new resources use `turn-requests` names,
  query key `["session-turn-queue", sessionId]`, hook `useSessionTurnQueue`; `/api/turns` +
  `useSessionTurns` are TELEMETRY, a *separate read model* from the queue — do not merge them).
- Current frontend surface (grounding, NOT a prescription — rediscover it yourself):
  `web/src/components/timeline/TurnQueuePanel.tsx` (+ test), `Composer.tsx`, `SessionTurns.tsx`,
  `web/src/lib/turnQueue.ts` (+ test), `web/src/hooks/useSessionActions.ts`, `useLiveData.ts`,
  `web/src/screens/SessionDetailScreen.tsx`, `web/src/transport/apiClient.ts` / `rawApi.ts`.
- The live API shape is already proven (see validation doc): `POST /api/sessions/{id}/turn-requests`
  (submit → 202), `GET …/turn-requests` (queue read: `turns[]`, `active_turn_id`, `queued`,
  `enrolled`, `paused`, `hold`), `GET /api/turn-requests/{id}` (one turn), pause/resume + recovery
  resolution routes, and `effects_state`. Use the running gateway to see real payloads.

## Why
The turn-queue backend is correct and proven live, but the **frontend is awful** — per the
operator: not 100% functioning, poor UX, poor UI. The feature lets a user queue multiple turns to
a busy session and watch them run FIFO (plus pause/resume, recovery-required, edit-before-run,
stop). None of that is currently legible or reliable in the UI. This is NOT a tweak pass (no
"make the text smaller"): redesign the queue experience **as if it does not exist yet**, driven by
the real user journey, with genuine logic and aesthetic judgment.

## TASK

**Phase 1 — Discovery + ground-up redesign proposal (THIS dispatch; deliverable = a doc).**
1. **Invoke the `frontend` skill** and apply it throughout — design sense, component/IA judgment,
   accessibility, and interaction states are part of the deliverable, not an afterthought.
2. **Walk the real user journey end-to-end** against the running gateway (create/enroll session →
   submit turn-1 → submit turn-2 while busy → watch FIFO activation → edit a queued turn → pause/
   resume → stop → a recovery-required turn → errors/empty/offline-carrier states). Record what a
   user actually sees and does at each step, with the current component/route behind it.
3. **Inventory what is broken / missing / confusing** — functional gaps ("not 100% functioning":
   name the exact broken behaviors with repro), UX gaps (dead ends, ambiguous state, no feedback,
   polling vs live-invalidation per invariant #12), and UI gaps (hierarchy, affordances, the
   queue-card read model vs the historical-exchange read model per §10 — they must stay distinct).
4. **Propose the complete redesign** — information architecture, the full set of states and
   transitions (queued / starting / working / recovery-required / paused / failed / done / empty /
   offline-carrier), the component tree, interaction + micro-states, loading/error/empty handling,
   accessibility, and how it binds to the existing `turn-requests` API and query keys (NO new
   backend routes, NO turning `/api/turns` telemetry into an admission path — reuse the existing
   API layer; this mirrors the backend's single-admission-authority rule). Include low-fidelity
   wireframe descriptions (ASCII/markdown) for each key screen/state and the rationale for every
   decision. Flag anything that needs a backend/DTO affordance that does not exist yet as an
   explicit dependency (do not silently invent one).

**Phase 2 — Implementation (SEPARATE, gated on operator approval of the Phase-1 proposal).**
Build the approved redesign on `feat/a99-turn-queue-ui`: real components, Vitest coverage for the
state machine + adapters, no regression to telemetry reads, live-verified against the gateway.
Do NOT start Phase 2 in this dispatch — stop after the proposal and hand back for review.

## ACCEPTANCE — Phase 1 done only when all are true (proof, not vibes)
* A committed proposal doc `.ai/dispatch/A99_TURN_QUEUE_UI_PROPOSAL.md` containing: (a) the
  step-by-step user-journey walkthrough with the current component/route behind each step;
  (b) a numbered inventory of functional / UX / UI defects, each with concrete repro or file:line;
  (c) the full redesign — IA, complete state/transition set, component tree, wireframe sketches per
  state, interaction + a11y notes, and the API/query-key binding (reusing existing `turn-requests`
  resources); (d) an explicit list of any backend/DTO dependencies the redesign needs.
* The proposal is grounded: every "it's broken" claim backed by an observed behavior against the
  running gateway or a file:line, not assertion.
* A short Phase-2 implementation plan (component-by-component, test strategy, rollout) the operator
  can approve as-is.
* It is a from-scratch redesign, not an incremental tweak list — the operator will reject a
  "small changes" deliverable.

## REALITY CONSTRAINTS
Use the REAL running gateway (`http://127.0.0.1:9003`, token from `.env`) to observe real queue
payloads and drive the journey; a small disposable scratch session is fine (close it after). Honor
invariant #11 (separate read models; `turn-requests` names) and #12 (live-invalidation, not 3s
polling regressions). Do NOT run the paid full/e2e suite. Convert relative dates to absolute.

## RESERVED DECISIONS
Phase-2 build authorization is the operator's (after reviewing the Phase-1 proposal). Any new
backend route / DTO / schema change is out of this job's authority — propose it as a dependency.
No merge of UI code in Phase 1.

## SCOPE OUT
No backend/admission changes (A82 is done); no telemetry-DB changes; no new API pathways; no
Phase-2 implementation in this dispatch; no unrelated web refactors beyond the queue experience.

## TRAIL
Commit `.ai/dispatch/A99_TURN_QUEUE_UI_PROPOSAL.md` to `main` (docs-only). Report: the top 3
functional defects found, the proposed IA in one paragraph, and the Phase-2 plan headline.

## Phase 2 — OPERATOR DESIGN DIRECTIVES (authoritative; approved to build 2026-10-07)
These are the operator's own words, distilled. They **override** the Phase-1 proposal wherever they
conflict (notably: the proposal's "always-present rail" is REPLACED by the collapse-when-empty +
thin-indicator model below). Build to THESE.

**D1 — Queue shows ONLY waiting turns; passed/active turns live in the CHAT.** Today a message that
already went through (active, "working") still shows in the queue AND does **not** appear in the
chat/transcript — the user never sees the actual message. **Root-cause this** (trace the
`queueOwnedIds` dedup boundary in `turnQueue.ts` + whether the A84 managed-completion effects
populate the transcript read model) and FIX: an active/started/finished turn renders in the
transcript; the queue component shows only turns that are genuinely *still waiting*.

**D2 — Hide the queue entirely when zero waiting.** If there are no waiting turns, render **nothing**
(no empty panel).

**D3 — Collapsed = a super-thin, barely-visible indicator.** The default must NOT be today's
~quarter-screen panel. It is a slim line showing *how many are waiting* + *who sent them*, ideally
tucked near/into the composer. Minimal footprint. Click to expand.

**D4 — Expand = a compact box (NOT full-screen; if full-screen, done properly).** Lists the waiting
turns. Each row is super-thin: **sender + a 1–2 word preview**; clicking a row opens the **full
message in a plain, readable view**. Reference well-regarded compact "received-messages" patterns
(e.g. Claude Code's) for density and polish.

**D5 — Sender identity is REQUIRED on every item.** Show WHO sent each turn (operator / manager /
agent / which session) and what it is — never a bare "system"/"continuation". The user must see
where each message comes from. (Backend fields: `turn_source`, `source`, `sender_session_id`,
`turn_kind` — surface them meaningfully.)

**D6 — Count semantics.** "1 message" means **1 waiting** message shown — do not also count/show the
active one.

**D7 — Edit is BROKEN — fix it.** Clicking Edit currently does not open an editable input (no
keyboard/focus) and the editor is too small. Edit must focus an input and **expand** to a comfortable
size.

**D8 — Keep the Withdraw confirmation** (operator likes it).

**D9 — Relocate Pause Queue.** Its current placement is awkward. Once the component shrinks, give
Pause a sensible home so it is not in the way. It is the "stop a message that shouldn't go through"
control — keep that capability obvious (alongside per-item withdraw).

**D10 — Real design quality.** Use the `frontend`/design skill and apply critical UX + aesthetic
judgment (convenience AND beauty; must not block content). If a specific *trusted* external design
skill would materially help, SURFACE it to the Manager for approval — do NOT install unvetted skills.

**D11 — Must be viewable.** The deliverable has to be something the operator can open in the live UI
and judge — so Phase 2 ends in a merged + deployed build (Manager owns merge/deploy), then operator
approve/iterate.

## Milestone checklist
- [x] `frontend` skill invoked and applied
- [x] User journey walked against the live gateway, per-step current behavior recorded
- [x] Functional / UX / UI defect inventory (grounded, with repro/file:line)
- [x] Full ground-up redesign (IA, states, component tree, wireframes, a11y, API binding)
- [x] Backend/DTO dependencies listed explicitly
- [x] Phase-2 implementation plan the operator can approve
- [x] Proposal doc committed to `main`
- [ ] **Phase 2 — build the approved redesign** (operator-gated; NOT started)

## Closure

### Phase 1 — SHIPPED (2026-10-07)
**What changed:** `.ai/dispatch/A99_TURN_QUEUE_UI_PROPOSAL.md` (new, `main @ 9b5520f`) — a grounded,
ground-up redesign proposal (user-journey walkthrough, 18-item functional/UX/UI/a11y defect inventory
with live-observed behavior + file:line, full "Turn Rail" IA + state model + component tree + per-state
wireframes + API binding reusing the existing `turn-requests` routes, backend/DTO dependency list, and a
9-step Phase-2 plan).
**Verification:** Manager review 12/12, Gate 0 pass; defect F1 spot-checked against code
(`turn_requests.py:39` accepts `requeue`; `apiClient.ts:279` omits it — confirmed). Grounded on a live
PAUSED scratch session (zero paid execution).
**What follows / continuation plan:** Phase 2 (build on `feat/a99-turn-queue-ui`) is **gated on operator
approval of the proposal**. On approval, dispatch the Phase-2 build worker against the 9-step plan in the
proposal §5. Status set `blocked` (awaiting operator Phase-2 go/no-go), not `done`.
