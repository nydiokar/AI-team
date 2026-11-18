```yaml
job_id: AGENT_117_TELEMETRY_CONTEXT_ECONOMICS
created_at: "2026-11-17T09:37:34.631774+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A117             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-17T09:37:36.047561+00:00"
```


# DISPATCH — A117 · Finish trustworthy context, compaction and Manager recovery economics

**Level:** 3 (worker/SDK boundary, telemetry/pricing, recovery policy, >5 files).
**Type:** finish existing implementation, verify, ship and measure production acceptance.
**Authored:** 2026-11-17, Europe/Vilnius. **State:** ready to execute; implementation checkpoint exists, delivery is not complete.
**Depends on:** none for engineering. Coordinate overlapping SDK/authority changes with A102/A114; do not invent a second queue or budget authority.
**Branch/PR:** resume [draft PR #293](https://github.com/nydiokar/AI-team/pull/293), `feat/telemetry-context-economics`, checkpoint `4f1c61c`; local worktree `/tmp/ai-team-issue144`. Fetch the remote branch if that worktree is unavailable. Keep one feature PR; no rewrite from scratch.

## Read this first

The operator's [issue #144](https://github.com/nydiokar/AI-team/issues/144) combines two failures:
telemetry did not explain the actual cost of restarting a Manager after quota exhaustion,
and did not offer measurable, context-based compaction advice. The operator explicitly
requested root-cause verification, a branch, tests, measurable proof, and a final adversarial
review. A follow-up on 2026-11-17 requested an anchor an agent can execute autonomously.

**Authorization already exists for this engineering work.** The original instruction was:
“verify the claims and cut a branch to carry out the testing and fix of the underlying/root
causes.” Do not ask the operator to re-explain the issue, locate code, approve routine fixes,
or approve engineering again. Repo policy delegates commit/push/PR/self-merge and gateway
rebuild. This does not authorize destructive operations, worker restarts or paid proof turns.

Read, in order: `.ai/CONTEXT.md`, `DISPATCH_LOG.md`, `.ai/DOC_MAP.md`, this packet,
`git status/log/diff`, and the checkpoint's `docs/cost_monitoring_audit.md` §9.
Use symbol lookup before reading named definitions. Verify documentation against code and git.

## Why (objective lock)

An operator must be able to make and later audit a recovery/compaction decision using
actual session token usage, measured context and its fraction of an observed window,
explicit monetary assumptions, durable proposals and attributable outcomes.

Success requires both features to work through the real worker→telemetry→SQLite→API→UI
path. “Green helper tests,” a draft PR, a screenshot, a current-context calculation, or an
invented estimate alone is insufficient. Measured capacity reclaimed, provider-reported
API cost, counterfactual API-price scenarios, and a subscription invoice are different
quantities and must stay different in storage, labels and acceptance evidence.

## Current state — observed 2026-11-17

- Checkpoint `4f1c61c` is pushed on `origin/feat/telemetry-context-economics`. It contains the
  existing implementation, tests, sanitized native trace and audit notes. It is unmerged
  and unactivated. The remote checkpoint, not a `/tmp` log or this conversation, is the
  recoverable starting point.
- The checkout at `/home/cifran/dev/AI-team` belongs to unrelated work on
  `feat/codex-quota-observation`; do not switch it, stage its files, or carry its changes.
  Local `main` has unpublished divergence; use fresh `origin/main` when reconciling history,
  preserve unrelated commits, never reset/rebase/force-push to “clean up.”
- Historical runs reported 205 targeted Python tests passed in 36.09s; later API/outcome
  seam checks reported 80 passed in 18.32s; 26 related frontend checks, typecheck and a
  production build passed. These are prior-run results, not proof of the final checkpoint:
  the last outcome/display regressions and documentation must receive fresh verification.
- Independent review found and drove fixes for aggregate/context confusion, child-agent
  contamination, stale cache assumptions, incorrect model selection, incomplete cost
  coverage, ambiguous compaction attribution, unsafe payload display and query scans.
  It reached conditional acceptance, but its turn ended at quota before a final all-clear.
  Do not represent that as an unconditional final review of this checkpoint.
- Natural sanitized replay: `claude-opus-5-5`, boundary dated 2026-11-04,
  **206,931→57,016 tokens**, **149,915 reclaimed (72.4469%)**. The source has no window or
  isolated compaction price. It proves capacity reduction, not adequate threshold calibration
  or realized net monetary savings.
- No worker restart, paid verification turn, production history repair or migration was
  performed. Re-read current deployment/workload; old health/build observations are stale.

## Scope/map and verified root causes

| Seam | Read/verify before changing it |
|---|---|
| Native SDK reader/result | `src/backends/claude_driver.py`: parent usage, `compact_boundary`, `model_usage`, `total_cost_usd`, result/ownership semantics |
| Adapter and projection | `src/core/telemetry.py`, `src/core/telemetry_adapters/claude_stream_json.py`, `src/core/telemetry_projection.py`: allowlists, deduplication, latest/entry context versus aggregate work/cost |
| Durable reads | `src/control/db.py`, `src/control/telemetry_store.py`, `src/control/session_timeline.py`: retained summaries, session identities, producer-token linkage, bounded/indexed reads |
| Recovery decision | `src/orchestrator.py`: `estimate_case_resume_cost`, `_recommended_resume_mode`, approval writers and `_admit_managed_recovery_turn` |
| Pricing and read boundary | `src/services/context_economics.py`, `src/services/pricing.py`, `src/control/routes/sessions.py` |
| User-visible contract | `web/src/transport/{rawApi,sessionTimelineAdapter}.ts`, `web/src/domain/models.ts`, `ContextEconomicsPanel`, `CaseResumePanel`, `SessionDetailScreen` |

The original Claude adapter mislabeled uncached input as inclusive-cache; invocation sums
cannot establish the current request context. Largest historical cache-write estimates can
miss accumulated context or stay stale after compaction. Pause age never proved an hour-long
cache hit. SDK reported cost/window/model were discarded. Recovery proposals and replacement
session receipts were not joined into an auditable deal. The gauge lacked advice/outcomes.
Re-derive each claim from the base/changed code; fix defects that remain rather than copying
these descriptions into a completion claim.

## TASK — staged execution and reviewable outputs

### P0 — recover and freeze the starting point

1. Inspect the existing worktree, checkpoint, remote PR and current `origin/main`. Confirm
   staged/untracked state and who owns each change. Resume this feature branch or fetch it
   into an isolated worktree. Preserve the full existing diff and test fixture.
2. Set this job owner and `status active` using `scripts/dispatch/dispatch_state.py --set`.
   Reconcile current upstream changes without including another job's unmerged work. Preserve
   this packet/ledger when merging the feature's older CONTEXT notes.
3. Record the exact revision under review and a short writer→consumer map in this packet's
   Milestone section. No new summary-document tree or parallel implementation.

### P1 — finish correctness and historical compatibility

4. Keep provider-exclusive cache buckets for billing; entry/latest parent context for fill;
   aggregate token work for cumulative work. A repeated snapshot/final result/retry must not
   double-count. An unknown final invocation cannot inherit an earlier invocation's context.
   Preserve zero versus unknown, partial coverage, truncation, malformed counters, the 4096
   identity bound and observed model identity after live-alias→final-model replacement.
5. Verify both real SDK paths: final result with normal usage and a cost-only `/compact`
   result (including explicit zero). Validate finite nonnegative reported USD. Prefer the
   provider receipt for observed cost; label table-derived fallbacks and coverage. Do not
   apply Claude's 5-minute-write premium to another provider or assume a cache TTL.
6. Recovery proposals must retain a bounded snapshot of token basis, observed context/window
   fraction, timestamp/model, price and excluded costs. Execution-time snapshots must join the
   canonical producer token/managed turn/provider receipt and be visible from old and new
   sessions. Distinguish proposal-time from execution-time estimates. An unpriced fresh boot
   cannot pass a positive automatic cost ceiling using the old-context alternative's price.
7. Compaction advice/outcomes must remain coherent after restart, replay, a failed compaction,
   no reduction, unknown next context, intervening compactions and native-session replacement.
   Deduplicate one manual/native intervention. Unknown buckets must never become displayed
   zeroes; unavailable reads must never look like an empty recovery history.
8. Preserve historical spend/retention/withdrawal invariants. Do not broaden the existing
   lifecycle repair's `<3` scan into a write-heavy historical v4 replay. Do not rewrite live
   history to manufacture missing windows or context. Reuse DB snapshots/admission/queues.

### P2 — targeted verification, measurements and adversarial review

9. Read `running-targeted-tests` and run the exact module set below, using a verified Python
   3.12+ environment. The existing host `.venv` was Python 3.11; do not “fix” tests by running
   an incompatible interpreter or replacing a live worker's environment. Existing local
   Docker images can verify offline with `--network none`; if unavailable, follow the
   repository's declared dependency workflow and constraints, not ad-hoc package installs.
10. Add only meaningful missing regressions, then repeat affected checks. Re-run related
    frontend tests, typecheck and production build after the final UI change. Include an
    API→transport→display reconciliation for proposal tokens/%/cost, recovery actuals and
    unknown/error states; verify that no DTO drops the proof fields.
11. Replay the committed natural trace and report exact counts with provenance. Measure the
    bounded read/projection at maximum advertised cardinality; inspect SQL query plans,
    memory and timings. Report warm/cold distinctions and exclusions. Keep a reproducible
    command/harness or test for each acceptance number; `/tmp` artifacts alone are not proof.
12. Explicitly run the service-boundary checklist (§7 in AGENTS.md): concurrency at N=100,
    memory, payload/path limits, deadlines, malformed input and backing-store failure.
    Check permit retention through actual serialization/error/cancellation and document real
    deferrals in CONTEXT, not a vague “later.” Retain the existing native IO/socket/history-byte
    qualification; incremental Python memory is not absolute process RSS.
13. Perform a fresh adversarial review of the final diff and execution path. Ask whether it
    violates a principle, copies a flawed pattern, makes an unverified assumption, measures
    the wrong thing, or leaves a cross-layer implementation inert. Fix every blocker, retest
    affected seams and record findings plus resolution. Use no paid CLI to verify this work.

### P3 — ship and prove activation honestly

14. Follow `shipping-prs`: commit only owned files, push, finish the existing draft PR, inspect
    CI, resolve integration failures and self-merge when green. Do not close issue #144 at
    merge if live acceptance remains open. Do not run the full/e2e pytest suite locally.
15. Follow `deploying-the-gateway` for both gateway/task-server, with preflight, rollback and
    read-only API/UI verification. Preserve other checkouts and their unmerged work. If a
    documented preflight cannot pass, surface its concrete blocker; never bypass it silently.
16. Worker activation is a distinct decision. Read `scripts/worker_context.py` for each exact
    node, including this agent's ancestry, running task IDs, loaded build, pooled sessions and
    logins. Reuse any actual session authorization; absent authorization, present that concrete
    impact and request the one required worker rollout decision. Idle alone is not approval.
    Complete all independent engineering/shipping work before making this the last gate.
17. Observe naturally occurring post-activation receipts for both features. Reconcile native
    usage/window/cost→stored events→projection→session API→display. Record loaded worker build,
    gateway build, session/task/approval identities and timestamps. Observe the recovery estimate
    versus its linked actual receipt and a compaction's capacity/cost outcome. No deliberate
    quota exhaustion, forced respawn, worker crash, historical rewrite or paid proof turn.
18. Evaluate advice adequacy using records with an actually reported window: threshold crossing,
    warning before a native limit/compaction, false/missing advice and available benefit evidence.
    Eighty percent is the checkpoint's explicit advisory policy, not an empirically optimal
    result. A trace without a window cannot certify it. If representative natural evidence is
    absent, leave this production/calibration gate open with a named observation procedure;
    finish all executable work, then report the precise external gate rather than claiming done.

## Verification commands (Linux; run `pwd` separately first)

Run from the feature checkout. The Docker image names are previously observed local tooling,
not dependencies to fetch blindly. Verify their availability and Python/Node versions first.

```bash
docker run --rm --network none --entrypoint python -e PYTHONPATH=/workspace -v "$PWD:/workspace" -w /workspace ai-team:dependency-closure-tests -m pytest tests/test_context_economics.py tests/test_claude_telemetry_adapter.py tests/test_session_timeline.py tests/test_case_quota_resume.py tests/test_telemetry_projection.py tests/test_case_respawn.py tests/test_control_api.py tests/test_sdk_driver_proactive.py tests/test_pricing.py tests/test_watched_job_consumption.py tests/test_turn_queue_respawn_crash_safety.py tests/test_turn_queue_respawn_revalidation.py --tb=short
```

Frontend, in a verified supported Node environment with repo-managed dependencies:

```bash
pnpm --dir web exec vitest related --run src/transport/sessionTimelineAdapter.ts src/components/work/CaseResumePanel.tsx src/components/timeline/ContextEconomicsPanel.tsx src/screens/SessionDetailScreen.tsx
pnpm --dir web typecheck
pnpm --dir web build
```

Native fixture reproduction: `pytest tests/test_claude_telemetry_adapter.py -k natural_compaction --tb=short`
in that same offline test environment. Select additional modules only for changed seams or
specific failures. Use `--tb=short`; no full suite, e2e, faulthandler timeout or real agent CLI.

## ACCEPTANCE — all required for full closure

- [ ] Final checkout/PR/revision and exact scope are recorded; all owned code is durable.
- [ ] Native SDK→adapter→projection→SQLite→API→UI proves distinct context, token work and spend.
- [ ] Observed model, exclusive cache accounting, provider USD and full-session priced coverage
      survive live/final deduplication, partial receipts and process/replay boundaries.
- [ ] Proposed recovery tokens, context percentage and cost assumptions persist; actual recovery
      tokens/cost link the correct proposal/execution snapshot, old/new sessions and exact turn.
- [ ] Fresh-boot unknowns cannot silently pass a positive auto ceiling; no stale warm-cache claim.
- [ ] Threshold boundary/unknown-window regressions and failure/ambiguity/duplicate compaction
      cases pass; reclaimed tokens and amortization scenarios are reproducible and correctly labelled.
- [ ] Required targeted Python/frontend checks, typecheck/build and service-boundary checks pass
      after final fixes; actual commands, revision and results are in this packet.
- [ ] Fresh adversarial review has no unresolved blockers; remaining limits are explicit.
- [ ] PR merged and gateway/task-server build verified; authorized worker build activated and checked.
- [ ] Natural new recovery and compaction receipts reconcile end to end; adequacy evaluation uses
      observed windows and distinguishes absent evidence from a successful result.
- [ ] Independent reviewable before/after report states capacity, context %, provider cost, priced
      coverage and scenario savings without claiming unsupported realized net savings.
- [ ] Only then mark `done`, update the ledger/CONTEXT and close issue #144.

## RESERVED DECISIONS — only real external gates

- **R1 — worker rollout:** operator authorization under AGENTS.md. No implicit permission from
  this packet, a gateway restart or an idle snapshot. Ask only with fresh workload/ancestry evidence.
- **R2 — paid/forced acceptance:** no paid synthetic verification, deliberate quota exhaustion or
  disruptive test without separate explicit authorization. Existing fixtures/read-only traces and
  natural activity are the default. Missing natural activity is a pending acceptance gate, not a
  reason to spend autonomously or mislabel synthetic results as production.
- **R3 — automatic compaction or broader policy:** advisory only. Optimizing/calibrating the warning
  with evidence is in scope; turning the warning into an automatic destructive/context-discarding
  action or enabling unrelated autonomous flags requires a separately explained operator decision.

## SCOPE OUT

No second telemetry DB, scheduler, queue, generic pricing/authority framework or broad refactor.
No subscription-invoice inference from API equivalents. No backfilled fabricated context/window,
paid test suite, worker restart by reflex, unscoped pricing refresh or unrelated A111/A114 work.
Scope only further price corrections actually required by observed models; verify primary sources.

## Architecture end state

The obsolete pause-age cache-warm shortcut is retired; both features use existing session,
producer-token, telemetry and read-boundary owners. This narrow mechanical check does not replace
the behavioral/consumer proofs above.

```arch-contract
scope: [src/orchestrator.py, src/core/telemetry_adapters/claude_stream_json.py, tests/test_case_quota_resume.py]
retired: [RESUME_IN_PLACE_CACHE_WARM_SEC]
```

## TRAIL / EVIDENCE

- Intent: [issue #144](https://github.com/nydiokar/AI-team/issues/144) and operator instructions
  quoted above; 2026-11-17 request to create an autonomously executable anchor.
- Durable implementation: [draft PR #293](https://github.com/nydiokar/AI-team/pull/293), `origin/feat/telemetry-context-economics` at `4f1c61c`.
- Narrow retirement contract checked against checkpoint `4f1c61c`: 3 Python files parsed, contract met, zero waivers. This is a static retirement check, not final behavioral acceptance.
- Dispatch registry audit on 2026-11-17 validates A117; its only reported gap is the pre-existing unrelated A67 missing-proof entry. Do not expand this job to repair A67.
- Audit on that branch: `docs/cost_monitoring_audit.md` §9. Fixture:
  `tests/fixtures/context_economics/natural_compaction.ndjson`.
- Prior-run reports: 205 targeted Python / 80 later seam checks / 26 related web checks;
  typecheck/build passed before the last changes. Refresh, do not inherit a green verdict.
- Review-measured query plan used session/continued-from indexes plus primary-key joins;
  ten warm reads median 3.49ms, first cold read 575.337ms. Warm incremental economics for
  500 turns+500 compactions measured 80.2ms, 311,696 peak traced Python bytes and 9,207 JSON bytes.
  These exclude imports/existing history allocations and require a reproducible recheck.
- On completion put exact commands/results/revisions, review findings and deployment/receipt
  evidence here. Separate necessary artifacts belong only under `evidence/A117_*`, linked back
  to this packet. Set machine `evidence` to paths present on merged main, not local `/tmp` logs.

## Milestone (burndown)

- [x] Issue intent, scope, authority and known limits anchored in this packet.
- [x] Existing implementation preserved as remote checkpoint `4f1c61c`.
- [ ] P0: starting revision, current ownership and writer→consumer map verified.
- [ ] P1: final correctness/compatibility gaps closed.
- [ ] P2: final targeted checks, reproducible measurements and adversarial review pass.
- [ ] P3: merge and gateway deployment complete.
- [ ] R1: authorized worker activation complete.
- [ ] Natural receipts and advice-adequacy evidence complete.
- [ ] Full closure recorded consistently in packet, ledger, CONTEXT and issue.

## Closure

**Not complete.** This packet is ready for an executor to resume the existing code and carry
it through verification and shipping. Production worker activation and natural acceptance
remain explicit external gates. Replace this paragraph with the evidence-bound closure only
when every required acceptance item above is satisfied.
