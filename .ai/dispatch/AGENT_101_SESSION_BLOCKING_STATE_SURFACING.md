```yaml
job_id: AGENT_101_SESSION_BLOCKING_STATE_SURFACING
created_at: "2026-10-07T18:59:46.815411+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: [".ai/dispatch/A101_O1_FINDINGS_RESUME_DECISION.md","tests/test_a101_block_state.py"]                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-07T20:39:57.401226+00:00"
```

# DISPATCH — AGENT_101_SESSION_BLOCKING_STATE_SURFACING

**Level:** 3 (backend read-model field + frontend; control-plane surface) — operator-flagged live incident.
**Type:** fix (make hidden session-blocking states visible + unblockable in the session window)
**Authored:** 2026-10-07
**Depends on:** diagnosis `A101_SESSION_BLOCKING_STATE_DIAGNOSIS.md` (committed, accepted). Frontend
part extends the A99 Turn Rail (coordinate with A99 so the banner lives in the same session surface).
**Branch:** one `feat/a101-blocking-state` branch + PR (backend + web).

## Why (the live incident)
After a quota stop, an operator message enrolled, showed "1 waiting", and was **silently never
delivered** — held by a **quota pause** (`flow.quota_paused`) whose state is **not in the turn-queue
read-model** (`paused`/`hold` stayed 0/NULL, proven live). The resume approval surfaced **only in the
Work/mesh view**, never in the session window, so the operator had to hunt through dev tools + Work
to resume. Diagnosis found **6 fully-HIDDEN hold states** (quota pause, transient pause, retry-pause,
legacy-draining, backoff/not-before, lineage-pending) that can hold a message with no session-window
signal. Root cause: the frontend cannot show what the API does not return.

## TASK
**(b) Backend — the enabling change (do first).** Add a single `blocked` / `pause_reason`
(+ optional `resume_action`) field to the session turn-queue read-model (the `GET
/api/sessions/{id}/turn-requests` response built in `src/control/...`; the gate logic lives in
`_MANAGED_RETRY_GATE_SQL` `db.py:915` / head-select `db.py:3418` / `_apply_turn_block` `db.py:3533`).
It must report, for the head/active turn, WHY the queue is held (quota | transient | retry | legacy
draining | backoff | lineage | operator-pause | recovery | carrier-offline) and what clears it
(auto vs operator action). Read-only projection over existing state — no scheduling/behavior change.
Apply the §7 service-boundary checklist to the new field.

**(a) Frontend — surface it in the SESSION window (extends A99 Turn Rail).** When the queue is held,
show a first-class, concise banner co-located with the composer: what state, who/what it's waiting
on, and the action (e.g. "Paused — daily quota reached · [Resume]" wired to the existing
`resume_case`/`set_turn_queue_paused` route). The operator must never again have to leave the session
window to discover a held message. Honor invariants #11/#12 (reuse turn-requests read + SSE).

## OPERATOR DIRECTIVES (2026-10-07) — authoritative; this is a SURFACING MOVE, not a behavior change
- **O1 — Investigate first.** Before changing anything, investigate how the CURRENT resume/Case-decision
  flow behaves in the Work/Case view: what exactly it asks, what options it offers (resume in the SAME
  session vs a NEW session, escalate, etc.), how the operator's choice is applied, and how reliable it
  has been. Document it. We replicate it faithfully; we do not redesign the decision.
- **O2 — Move the surfacing into the SESSION, per-session.** The same resume/decision must appear IN the
  session window (co-located with the composer, per the diagnosis), and be **removed from the Work tab**
  (the Work tab is the wrong place for a per-session decision). Same options, same outcome — ONLY the
  location and timing of surfacing change.
- **O3 — Do NOT change anything materially.** No change to the decision logic, the options, or what each
  choice does. If moving it requires touching the decision backend at all, keep behavior byte-for-byte
  equivalent and say exactly what you touched and why.
- **O4 — Push on surface.** When a decision is surfaced, send the operator a PUSH (via the existing
  Telegram/notify path) so they know a choice is required. Investigate the existing notify mechanism;
  reuse it, don't invent one.
- **O5 — NO auto-decide (yet).** Do NOT make the system auto-resume / auto-choose on its own. Keep it a
  manual operator choice. The auto-resume policy (`CASE_QUOTA_RESUME_AUTO`) stays OFF/deferred until we
  understand the current behavior (O1). Surface the policy question; do not flip it.

## ACCEPTANCE — done only when all true
* The turn-queue read-model returns a populated `blocked`/`pause_reason` for EACH of the 6 hidden
  states (unit/integration tests per state, using the real gate SQL — not mocked).
* The session window renders a banner for each held state with the correct reason + action; Vitest
  covers the mapping. No regression to telemetry reads (#11) or live-invalidation (#12).
* Live proof: reproduce a held state (e.g. operator pause, or a quota-paused scratch Case) and show
  the session window surfaces it + the in-window action clears it (zero-paid method where possible).
* PR merged; gateway redeployed (backend read-model change); operator can see a held message in-window.

## RESERVED DECISIONS
Whether a quota/idle resume should be AUTOMATIC (`CASE_QUOTA_RESUME_AUTO`) vs always prompt is an
operator policy call — surface it, default to making it VISIBLE + one-click, do not silently
auto-resume without operator sign-off.

## SCOPE OUT
No scheduler/gate behavior change (projection only); no new admission pathway; no A100/A84 work.

## TRAIL
PR + merge + redeploy; close out here; note the resume-auto policy decision for the operator.

## Milestone checklist
- [ ] Backend `blocked`/`pause_reason` read-model field (+ tests per hidden state)
- [ ] Frontend in-session banner + action (Vitest)
- [ ] Live proof: held state visible + clearable in the session window
- [ ] PR merged + gateway redeployed

## Closure
**2026-10-07 — worker delivered, PR #200 open (NOT merged — Manager owns merge/deploy).**
Surfacing MOVE per O1–O5, behaviour-neutral:
- **O1** findings committed: `.ai/dispatch/A101_O1_FINDINGS_RESUME_DECISION.md` (the exact
  Decline / In place / Fresh Manager decision + how `resume_case` applies it + reliability).
- **Backend** (no scheduler change): factored `_MANAGED_RETRY_GATE_SQL` into reusable
  quota/transient/retry `EXISTS` fragments, recomposed byte-equivalent (gate tests prove it);
  `_session_block_state` mirrors the gate and `GET /api/sessions/{id}/turn-requests` now carries
  `blocked`/`pause_reason`/`resume_case_id` for all 6 hidden holds + operator pause/hold.
- **Frontend** (O2): `SessionDetailScreen` mounts the IDENTICAL `CaseResumePanel` gated by
  `resume_case_id`; `TurnQueuePanel` labels the other holds; removed `PausedCaseInbox` /
  `CaseResumePanel` from the Work tab. Invariants #11/#12 preserved.
- **O4** push: reused `notify_case_resume_proposal`; re-pointed deep link `/work/{case}` →
  `/sessions/{session}`. **O5**: no auto-decide; `CASE_QUOTA_RESUME_AUTO` stays OFF.
- **Tests**: `tests/test_a101_block_state.py` (per-state vs real gate + HTTP integration),
  push deep-link tests, Vitest mapping/render. Full CI `pytest -q` green locally (post-rebase on
  A82 Stage-8b); `pnpm typecheck` + 205 Vitest green. Live zero-paid proof: incident shape
  `{queued:1,paused:false,hold:null}` → `{blocked:true,pause_reason:"quota",resume_case_id:…}`,
  clears to `blocked:false` on resume.
- **Deferred for the operator**: whether quota resume should ever be AUTOMATIC
  (`CASE_QUOTA_RESUME_AUTO`) stays a policy call — surfaced + one-click, not flipped.
