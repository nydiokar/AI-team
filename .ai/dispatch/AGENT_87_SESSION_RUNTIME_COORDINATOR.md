```yaml
job_id: AGENT_87_SESSION_RUNTIME_COORDINATOR
created_at: "2026-09-24T18:00:20+00:00"
status: active
owner: mgr-a2a819ff
depends_on: []
results_ref: DISPATCH_LOG.md#A87
evidence: []
updated_at: "2026-09-25T08:26:14.805928+00:00"
```

# DISPATCH — A87 · Manager review and integration control for session reliability and runtime delivery

**Level:** 3 (cross-cutting behavior/architecture review and release gating) · **Type:** manager / audit
**Authored:** 2026-09-24 · **Status:** ready. **Manages:** A82, A83, A84, A85, and A86.
**Branch:** `main` for packet/review-only changes; feature branch + PR only if a corrective code change is required. No merge/deploy/restart authority beyond existing branch policy and explicit operator approval.

> These jobs change adjacent truths: turn ordering (A82), what an operator is told about waiting (A83), how a Manager is notified of worker completion (A84), and when a worker image may disrupt a node (A85/A86). A document-compliant patch can still make these truths contradict each other. This manager owns the behavioral contract and reviews workers against the repository and operational evidence, not against design prose alone.

## TASK

1. Establish and maintain a one-page compatibility ledger in this packet: actual source of truth, producer, consumer, failure behavior, and test evidence for turn ownership, completion delivery, session reason, node maintenance, and runtime identity. Read the current tree and worker outputs; do not repeat design claims as facts.
2. Before each worker starts a behavior-changing stage, review its proposed tests and current seams. Require the worker to revise its packet/plan when it duplicates authority, widens a state machine, creates an unbounded scan, violates queue ownership, or lacks a recovery/rollback proof. Record the decision and the evidence in this packet.
3. Gate A84 on A82's reviewed ownership/admission contract. Reconcile A83's `waiting_workers` derivation with A84's legacy/new Case cutover: it must report the correct durable source for each Case without using the secondary UI label as control authority.
4. Gate A86 on A85's executable container acceptance, not a written declaration. Verify that all deployment actions require a human node+digest approval and that image merge, candidate availability, node maintenance, and worker restart remain separate facts.
5. Perform an independent behavior review at each worker handoff. Re-derive central claims from code and tests: atomic terminal/outbox linkage, exactly-once/coalesced continuation delivery, queue fencing, read-path bounds, drain claim/routing fences, volume persistence, and rollback. Ask for targeted fixes; do not waive a failed invariant because a design says it is intended.
6. Maintain dependency/status recommendations in `DISPATCH_LOG.md`. A worker cannot be marked reviewed/merged based only on self-report; A87 records the independent verdict and unresolved risk. Escalate only genuine product/operational decisions, with concrete options and evidence.

## ACCEPTANCE

1. Compatibility ledger covers A82–A86 and cites current source/tests rather than only documents.
2. Every worker handoff has a review entry with claimed behavior, reproduced evidence, findings, disposition, and remaining operational gate.
3. Cross-job tests or a documented test matrix prove: a managed completion wakes the correct late-bound Manager exactly once; A83 presents it accurately during legacy/new cutover; and maintenance cannot disrupt or reroute an owned managed turn.
4. A86 cannot progress from blocked without A85 evidence and A87's explicit review; no job recommends a live rollout without the operator gate.

## OPERATING RULES

- Judge outcomes by the current repository, tested failure modes, production constraints in `.ai/CONTEXT.md`, and least-disruptive architecture—not automatic obedience to any design document.
- Prefer a narrow worker correction over a manager-owned rewrite. Do not implement unrelated features merely to make the plan look complete.
- Treat flags as rollout controls, not correctness proof. Default OFF never closes a missing recovery, migration, or rollback guarantee.
- Keep status legibility read-only. It may inform review, never make control decisions in this program.

## SCOPE OUT

No replacement of the assigned worker jobs, no autonomous production deployment, no worker restart, no flag activation, and no broad harness rewrite. This packet creates review/gating evidence, not a second implementation path.

## TRAIL / EVIDENCE

- Compatibility ledger, per-worker review records, dependency decisions, reproduced tests, and explicit operator-gated rollout notes.

---
## Compatibility Ledger (A87 — grounded from the tree 2026-09-25, Case 58c2f812)

Coordinator: Manager session `a2a819ff55c0`. Built from repository + running-env evidence, not design prose.

### Truth-by-truth (source of truth · producer · consumer · today's failure · evidence)
| Truth | Source of truth | Producer → Consumer | Failure today | Repo evidence |
|---|---|---|---|---|
| **Turn ordering (A82)** | `mesh_tasks` (managed turn intent; protocol 1) | orchestrator `submit_instruction`/`_enqueue_task` → control_api instruction route → worker carrier + new scheduler | API acceptance mutates session state before the durable row lands; worker can reschedule the same pending id; result goes terminal before native-id reconciled | `src/orchestrator.py:6241` `submit_instruction`; `docs/SESSION_TURN_QUEUE_DESIGN.md` (46 KB, present) |
| **Completion delivery (A84)** | *today:* mutable Manager-authored wait-groups reconstructed via orchestrator continuation + `_cache_heartbeat_owner_live` case_wait_group branch. *target:* durable Case-scoped outbox row, atomic with terminal task write | terminal task writer → outbox → coalesced continuation → late-bound Manager | silent completions AND indefinite waits | `src/orchestrator.py:1306` `_cache_heartbeat_owner_live`; `docs/TBD/WORKER_COMPLETION_NOTIFY_REDESIGN.md` (18 KB, present) |
| **Session reason (A83)** | derived, read-path only, **non-authoritative**; enum `SessionStatus` UNTOUCHED | new `SessionReason` derivation on `/api/sessions` read → web pillow | must NOT become an N+1/background scan (repeat of #145/#147 stall) | `src/control/db.py:4710` `list_jobs_for_sessions`; spec at `docs/TBD/SESSION_WAIT_STATE_GRANULARITY.md` (**packet path `docs/SESSION_WAIT_STATE_GRANULARITY.md` is STALE — it lives under `docs/TBD/`**) |
| **Node maintenance + runtime identity (A85/A86)** | immutable image digest + per-node expiring single-use approval | Renovate/CI merge → available image; approval → drain/verify/rollback state machine | disruptive host-process assumption; no separation of merge/availability/maintenance/restart | `Dockerfile`, `compose.yaml`, `deploy/compose.worker.yaml` present; **docker CLI ABSENT in exec env** |

### Cross-job reconciliation gates (A87 owns these)
- **A83.`waiting_workers` ↔ A84 outbox cutover:** A83 derives `waiting_workers` from the legacy wait-group substrate. When A84 moves new Cases to the outbox, A83 must report the correct *durable source per Case* without the UI label ever becoming control authority. → reviewed jointly at A84 cutover.
- **A84 gated on A82:** do not start A84 until A82's queue ownership/admission contract is built AND independently reviewed (my gate). Correct sequencing, not idleness.
- **A86 gated on A85:** A86 stays `blocked` until A85 produces *executable* acceptance evidence and I review it. — **SUPERSEDED 2026-09-25 (see scope change below).**

### SCOPE CHANGE 2026-09-25 — container track retired (operator decision, Case 58c2f812)
Operator decided **workers move OUT of the container**, and to drop the "SDK-upgrades-via-PR" job (A86).
Coordinator ruling (cross-job contradiction the operator did not name explicitly): the container-exit
retires the premise of **BOTH** container-track jobs, not just A86 —
- **A86 → dead (dropped).** Its entire design keys on an immutable worker *image* as the release/rollback
  identity (Renovate→PR→image→approval→drain→rollback-to-digest). No container ⇒ no image ⇒ not legible.
  Zero code was written. Reversible from the packet if workers are ever re-containerized.
- **A85 → deferred (premise retired).** Its only downstream consumer was A86; a container-acceptance
  baseline has no purpose if workers aren't containerized. Built code preserved on
  `feat/worker-container-acceptance` @ `1cd15b3` (unmerged, no docker ever run). Resume-or-drop pending
  the operator's non-container worker plan.
- **Session-runtime track (A82/A83/A84) is UNAFFECTED** — turn ordering / session-reason / completion
  delivery are session+DB concerns independent of worker packaging. A82's carrier abstraction works for a
  host-process worker exactly as for a container. Continue driving them.
- **Surfaced successor need (not built, no job yet):** a non-container worker still needs *some* approved,
  reversible runtime-version update path (host venv/package pin + approval + rollback) — a DIFFERENT
  mechanism from image digests. To be authored once the operator's non-container worker plan exists; do
  not silently morph A86 into it.

### Cross-job reconciliation gates (A87 owns these)

### Execution environment facts (binding on every dispatched worker — packets predate the Docker migration)
- **Python:** `/opt/venv/bin/python` (NOT `.venv/bin/python`, which no longer has a working interpreter). Editable install ⇒ `import src` resolves to THIS checkout — verify `src.__file__` before trusting any test.
- **pytest:** absent from the read-only `/opt/venv`; run as `PYTHONPATH=/tmp/tvenv/lib/python3.11/site-packages /opt/venv/bin/python -m pytest <paths>`. Proven green: `tests/test_session_timeline.py` (5 passed).
- **web:** node v22 + pnpm 10.30 present, `web/node_modules` installed.
- **No live gateway** (`curl :9003/health` dead here) ⇒ acceptance = targeted tests + import smoke, NOT live-health probe.
- **No `gh`, no docker/podman**; git remote `origin` (SSH) exists. ⇒ PR-via-CLI impossible; deliverable = reviewed local commit on `feat/*`; push/PR/merge-to-remote is operator-gated.

### Coordination decision (this Case)
1. **Track-1 src editors run SEQUENTIALLY** (A83 → A82 → A84) — because the shared editable install would make parallel worktrees mistest each other's `src` (A82 §1.5 trap). A83 is small; the serialization cost is low and it eliminates the collision the operator warned about.
2. **A83 first** (fully executable): build → my verification (run its tests, trace cross-layer) → **fresh adversarial reviewer** → remediate findings → local merge.
3. **A82 next, stage-gated:** authorize Stage 0–1 (source/producer inventory + red acceptance tests — non-behavior-changing) as the first reviewed gate per A82 §4/§12; later stages are a multi-session continuation. I review before authorizing behavior-changing stages.
4. **A84 NOT started** — dependency-gated on A82's reviewed contract.
5. **A85/A86 BLOCKED by environment** — no docker/podman here, so executable container acceptance is impossible; dispatching a build would yield only the written declaration this charter rejects. → escalate to operator (needs a docker-capable env or the prod host).

---
## Per-worker review records

### A83 — Session state legibility — handoff 2026-09-25 — VERDICT: ACCEPT (merged)
- **Claimed behavior:** additive, read-path-only derived `SessionReason` on `/api/sessions` + timeline; enum/migration/loop untouched; `needs_input`/`is_active` byte-identical.
- **Independently reproduced (Manager):** `src.__file__` = this checkout (tests exercise edited code); `pytest tests/test_session_reason*.py` → 21 passed; scope guards: `SessionStatus` enum diff empty, `src/control/db.py` NOT in diff (no schema/DDL), no `create_task`/timer/loop added (only #145/#147 guard comments), `from_session`/`needs_input`/`is_active` unchanged; `pnpm --dir web typecheck` clean + 144 web tests pass.
- **Fresh adversarial reviewer (no parent context, told to falsify 8 targets):** ACCEPT — all 8 survived; independently ran the 21 py tests and traced the real `list_views → build_reason_batch` path (1× `list_jobs_for_sessions`, 1× `max_flow_event_ids`, per-open-case pause + manager-only wait reads; BUSY→None zero-read confirmed in code, not stub; priority order matches spec §4; `open_case_idle` for both roles; forward-compat unknown web kind → null; no-N+1/zero-read tests non-vacuous).
- **Findings:** 0 blocker/major. Nits/observations recorded: (1) `session_reason.py:109` comment overstates "EXACT reuse" of the heartbeat fold (it re-implements the same per-group fold with all-groups scope — behavior correct); (2) `session_reason.py:~190` reads openness via one `get_flow_run(cid)` per distinct open case on the page (O(open-cases-on-page), each an indexed PK lookup — bounded, but not batched; a `get_flow_runs(ids)` batch primitive doesn't exist and adding one would violate least-action). Neither blocks; both are candidate micro-optimizations.
- **A83↔A84 reconciliation note (carry to A84):** A83's `waiting_workers`/`open_case_idle` derive from the legacy wait-group substrate via `_case_has_unresolved_wait_group`. When A84 moves new Cases to the outbox, A83's derivation MUST switch source per-Case (outbox vs wait-group) so a new-Case Manager isn't mislabeled `open_case_idle` while a durable outbox row is pending. The UI label must never become control authority. → jointly reviewed at A84 cutover.
- **Disposition:** merged to `main` (local, `--no-ff` `7c5c100`). Remaining operator-gated: remote push/PR (no `gh` here) and `web/dist` rebuild + gateway restart to deploy the pillow.

### A82 — Session turn queue — Stage 0 gate 2026-09-25 — VERDICT: ACCEPT (Stage 1 authorized)
- **Reviewed:** read-only Stage 0 execution-path/producer inventory + terminal-writer inventory + Claude/Codex SDK ownership oracle + schema baseline + Stage-1 feasibility + design contradictions.
- **Independently re-verified (Manager, 5/5 pillars):** control_api.py:1789 root-cause window; db.py:2268 `complete_task` swallow + no ownership predicate; claude_driver.py:1127-1134 `cancel_inflight`-on-lock-conflict; SDK `TERMINAL_TASK_STATUSES`/`TaskNotificationMessage`/`TaskUpdatedMessage` present (types.py:1074/1115/1140) while driver code references none; `flow_run_id` added by `_ensure_substrate_columns` ALTER not a numbered migration.
- **Three escalated decisions resolved** (recorded in A82 §15): (1) distinct managed no-interrupt send path, legacy byte-identical; (2) new strict completion helpers on the protocol-1 path only, legacy swallowing helpers untouched; (3) Stage-1 red scoped to assertion-capable suites, module-dependent suites accepted ImportError-red until Stage 2.
- **Disposition:** Stage 1 (assertion-capable red tests) authorized on branch `feat/session-turn-queue`. **Stage 2+ (behavior-changing) remains gated on my review of Stage 1.** A82 is a multi-session build; A84 stays blocked until A82's ownership/admission contract is built AND reviewed.

### A82 — Session turn queue — Stage 1 + Stage 2 gate 2026-09-25 — VERDICT: ACCEPT (Stage 3 authorized)
- **Stage 1 (red tests) review:** ran the 6 suites myself → 40 red / 2 green (compat guards), zero skip/xfail; spot-checked SDK01 non-vacuity (drives real `_SDKSession`, asserts `interrupts==0` + fails via explicit `pytest.fail` on the missing managed path). ACCEPT.
- **Stage 2 (schema + strict helpers + managed ownership) — commit `f2119c2` then remediation `0f6964d`:**
  - Independently verified: migration 34 additive (all `ADD COLUMN`, no renumber of 1–33), legacy `complete_task`/`fail_task`/`send`/`cancel_inflight` byte-identical (zero deletions), flag-gated on `queue_protocol DEFAULT 0`; DB01–08 + ownership/SDK = 28→30 pass.
  - **Fresh adversarial reviewer found a REAL introduced regression (REWORK):** migration-34's `claim_token`/`idempotency_key`/`admission_hash` leaked through the `SELECT *` in `list_tasks` → `/api/tasks` (design §6/§3.13 forbids serializing the claim credential); plus a latent Stage-4 bypass (`get_pending_tasks` lacked `queue_protocol=0`).
  - **Remediated (narrow Manager correction):** `list_tasks` strips the three sensitive columns; `get_pending_tasks` guarded `AND queue_protocol=0`; OWN01b amended to the legacy/managed boundary (legacy mints no token) per §15 dec.2; added OWN01c leak regression. Re-run: 30 pass / 1 (SDK02, Stage-3). Legacy regressions (claim_reaper/task_state_truth/mesh_enqueue_affinity) 47 pass.
  - **Reviewer re-verified the revised candidate from git objects in an isolated worktree:** ACCEPT — both findings closed, OWN01c non-vacuous (fails when the strip is removed), OWN01b faithful, legacy byte-identity preserved. Remaining out-of-scope: SDK02 + reviewer nit #3 (`send_managed` not gated on `is_quiescent`) — both the **managed-path correlation** facet, to be CLOSED in Stage 3; nit #4 (NULL-hash idempotency) benign.
- **Disposition:** Stage 2 ACCEPTED. **Stage 3 (carrier protocol / result spool / recovery + managed correlation that closes SDK02) authorized.** A82 full-contract review still pending before A84 can start.

### A82 — Session turn queue — Stage 3 gate 2026-09-25 — VERDICT: REWORK (commit `b309f84`)
- **Manager independent verify:** 38 turn-queue tests pass (SDK02 flipped green); no legacy signature removed vs Stage 2. BUT those are unit tests of predicates — they do NOT prove the carrier protocol is wired end-to-end. Sent to a fresh adversarial reviewer with a hard falsification brief.
- **Fresh adversarial reviewer → REWORK. Real defects (green tests hid them):**
  - **B1 (blocker):** `_deliver_managed_result` (`worker/agent.py:1219`) POSTs to legacy `/tasks/{id}/result`, not `/result-managed` → the `{status:accepted}` receipt never matches `task_id`+`claim_token` → spool never pruned → **session ownership held forever**; also routes through the swallowing `complete_task` (bypasses atomic `complete_turn`, §15 dec.2).
  - **B2 (blocker):** worker still polls `/tasks/pending` + claims `/claim` (protocol-0) → `is_managed` always False → the entire managed worker path is **dead at the integration seam**; §7 "carrier protocol" gate + the required fake-carrier remote-native-ID integration test are UNMET.
  - **M1/M2/M3 (major, dead code):** `_managed_shutdown_release_ok` (WRK06 drain guard), `_reserve_result_envelope` (WRK05 pre-start reservation / 128MiB budget), and boot-replay re-delivery are never called → the safety properties aren't actually enforced (drain still releases a running backend; "run-and-discard" possible; unacked result never re-sent).
  - **M4 (major):** `is_quiescent` checked on the caller thread, not the SDK loop → TOCTOU vs binding §6 "reserve on the SDK loop before submitting."
  - **M5/M6 (major, incl. LEGACY regression):** a bare terminal `ResultMessage` reply hits the new `_dispatch` gate → routes to proactive → pending future never fulfilled → deadlock → after `sdk_turn_timeout` triggers the **forbidden `cancel_inflight`**; and because `_dispatch` is shared, this **regresses the legacy `send` path** (§15 dec.1). Proactive suites don't cover bare-result, so they didn't catch it.
  - Minors m1 (`/quiescence` accepts any non-null result as evidence), m2 (stale-receipt echoes unverified token).
  - **Holds up:** DB claim/start/complete/recovery fencing is real; SDK02 reader-gate is non-vacuous; flag-off/poll isolation + credential-strip on `get_pending_managed_turns`; `classify_completion_outcome` behavior-preserving.
- **Disposition: REWORK sent back to the Stage-3 worker** (has context) with the full findings. Priority: M5/M6 (legacy regression + forbidden interrupt) and M4 (loop-thread reservation) are correctness-critical; B1/B2 + M1/M2/M3 must wire the managed path and add a fake-carrier integration test. Re-verify + re-review after remediation. Stage 3 NOT accepted; A84 stays blocked.

## Milestone (burndown)

- [x] Current compatibility ledger created from repository evidence (2026-09-25)
- [x] A83 legacy/new wait-source compatibility reviewed (verdict ACCEPT; reconciliation note carried to A84)
- [~] A82 ownership/queue review: Stage 0 gate ACCEPT (2026-09-25); Stage 1 authorized, Stage 2+ gated — full contract review still pending before A84
- [ ] A82 ownership/queue review completed before A84 schema/cutover work
- [ ] A83 legacy/new wait-source compatibility reviewed with A84
- [ ] A84 atomicity, recovery, barrier, and liveness evidence independently reviewed
- [ ] A85 image/volume/runtime acceptance independently reviewed
- [ ] A86 approval/drain/rollback and managed-turn continuity independently reviewed
- [ ] Final cross-job verdicts and operator gates recorded in dispatch index

## Closure (fill on completion)

State reviewed job verdicts, unresolved evidence, and why any release/deploy action remains operator-gated.
