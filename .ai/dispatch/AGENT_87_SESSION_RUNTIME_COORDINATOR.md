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

### Execution environment facts (REPLACED 2026-09-25 — previous block described the retired worker container)
- **Topology (operator decision 2026-09-25):** gateway + task-server run in Docker (`compose.yaml`, data under `DOCKER_DATA_ROOT`); the worker runs **natively under pm2 on the host** as the operator user (PR #165 activity/quota cross the boundary over HTTP; PR #167 publishes the control API on the tailnet IP for mesh nodes). Docker worker is non-canonical (PR #166).
- **Python:** `/home/cifran/dev/AI-team/.venv/bin/python`. Develop ONLY in a git worktree (e.g. `/home/cifran/dev/AI-team-wt/<job>`); run pytest with cwd = the worktree so `import src` resolves there (verified). **Never switch branches in `/home/cifran/dev/AI-team`** — the live pm2 worker runs from that checkout.
- **`gh` available**; push/PR/merge per repo branch policy. Live probe: `curl http://127.0.0.1:9003/health`.

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

### A82 — Session turn queue — Stage 3 gate 2026-09-25/26 — VERDICT: ACCEPT (branch `feat/session-turn-queue` @ `f4cebbb`, pushed; NOT merged — Stages 4/6/7 suites still red by design)
- **Path:** 6 rework rounds, 5 fresh adversarial reviewers (REWORK ×4 → ACCEPT). Every reviewer probe adopted as a permanent test (tests/test_turn_queue_carrier_recovery.py, _r2.._r5.py); each new guard mutation-verified.
- **Key design corrections forced by review:** managed-turn contract on `CodingBackend` (not a Claude side door); reply correlation by caller-chosen echo uuid (`--replay-user-messages`, verified by a 3-turn haiku spike) replacing a refuted continuation counter; write-ahead claim store + boot-relative /proc process proof (clock-step immune) before any auto-resolve; late replies bound by turn uuid, never session; every managed state has a live exit (own echo+result · stream end ⇒ dead session replaced · close · server-terminal ⇒ forget); legacy routes/helpers fenced to protocol 0.
- **Manager verification:** 137 turn-queue + 212 legacy-regression tests passed on my rerun; flag-OFF byte-identity confirmed by 3 independent reviewers.
- **Incidents:** worker used `git worktree remove --force` once on its own throwaway worktree (no loss, disclosed); one mutation run spawned the real CLI briefly (invalid resume id, exit 1, no model turn) — test now guards process start.
- **Carried residuals (CONTEXT.md):** reparented `run_in_background` descendants undetectable (SDK has no process-group hook); oversize output not persisted to artifacts; psutil not yet installed in the live venv (declared + pinned on the branch).
- **Disposition:** Stage 3 ACCEPTED. Stage 4 authorized, split into gated sub-stages (4a admission+scheduler+session instructions; 4b–4h one producer each).

### A82 — Stage 4a (admission + fair scheduler + producer 1) gate 2026-09-26 — VERDICT: ACCEPT (branch @ `9acc32b`, pushed; unmerged)
- **Path:** 4 fresh adversarial reviews (REWORK ×4; round 4 = one narrow major, fixed and verified by Manager with the reviewer's own probes: 6/6). Reviewer probes adopted as permanent tests (`tests/test_turn_queue_4a_*.py`, producer1, scheduler, admission); every new guard mutation-verified.
- **Forced corrections:** blocked heads back off (no LIMIT-25 starvation); zero-enrollment ⇒ no marker read, legacy == main; claim verifies carrier assignment in the CAS; assignment to a registered, online, heartbeat-fresh managed carrier (never hostname) else typed 503; durable lineage-pending state + ONE convergent idempotent lineage procedure (get-or-create keyed on task_id) run by live writer and recovery, raising on DB error, lease-fenced, never re-affiliating to a closed Case; dead-carrier requeue with bounded idle wake; enrollment flag never lowered across an in-flight enroll.
- **Manager verification:** 260 turn-queue passed (7 red = SYS03–07 Stage 4b+, api×2 Stage 6); 419 regression passed (incl. case_closure/case_interrupt/mcp_manager).
- **Carried (CONTEXT.md):** enrollment only inside the gateway process (Stage-7 precondition); `MESH_LOCAL_CARRIER_NODE_ID` must equal the local daemon's `WORKER_NODE_ID` before enrolling; Stage-6 withdraw of a lineage-pending/partially-lineaged row must void the child flow_run + clear affiliation; compat `/api/instructions` cap derived ≈3.8 MiB (deviation from design 2 MiB, applies to all callers); cross-process completion wakes scheduler within ≤30 s; heartbeat timeout must be ≥2× worker heartbeat; Telegram retries not deduplicated.

### A82 — Stage 4b (producer 2: compaction, fenced cancel, managed close, operator-stop hold) gate 2026-09-26 — VERDICT: ACCEPT (branch @ `0be7420`, pushed; unmerged)
- **Path:** 3 fresh adversarial reviews (REWORK ×2 → ACCEPT with minors, closed and Manager-verified: reviewer probes 6/6).
- **Forced corrections:** cancel during the CLI boot window armed by turn uuid / caught pre-invoke (never silently no-op'd); managed stop = durable `turn_queue_hold` (migration 37) honored by head selection, activation, wake/transient/quota/respawn automation — released only by an operator admission (incl. coalesced), never by automation (`dispatch_worker` sends `X-AI-Team-Principal: automation` — a trust-model label, not authentication); close withdraws + voids lineage (4a Stage-6 precondition implemented for close); compaction as a managed turn on a never-queried process (attribution verified against bundled CLI source).
- **Manager verification:** 323 turn-queue passed (7 red = SYS03–07 Stage 4c+, api×2 Stage 6); 506 regression passed.
- **Carried (CONTEXT.md):** stale whole-row session saves can undo a concurrent stop status (hold record itself is safe); stop with no active turn holds nothing (legacy parity); operator orphan sweep force-closes a held Manager's Case; automation close ends a hold; principal header is self-declared (authenticated principals = A71).

### A82 — Stage 4c (producer 3: Case continuation token→turn linkage + durable finalization) gate 2026-09-26 — VERDICT: ACCEPT (branch @ `49bdb7c`, pushed; unmerged)
- **Path:** 2 fresh adversarial reviews (narrow REWORK → ACCEPT; last test-gap follow-up Manager-verified). SYS03/SYS04 green.
- **Delivered:** deterministic continuation turn id from token + durable attempt, linked inside the admission txn (exactly-once across crashes/replays); durable finalizer reconcile per wake tick (round counted once via fenced CAS; withdrawn/cancelled re-arms); activation-time obsolete withdrawal (closed/blocked Case, rebound Manager, reviewed, unlinked); rebound Manager withdraws a wake queued on the old (possibly held) Manager; continuation lineage pinned to the woken Case (Manager decision — fixed now rather than carried to A84); no events after `flow.closed`.
- **Carried (CONTEXT.md):** `failed`/`failed_node_offline` consume the wake even if the Manager never ran (legacy parity); operator-stopped wake re-fires after release (accepted, desired); A84 must fold `wait_resolved` + outbox consumption + token CAS into one txn; wait groups on closed Cases stay pending in projections (cosmetic).

### A82 — Stage 4d (producers 4 watched-job + 6 cache heartbeat) gate 2026-09-26 — VERDICT: ACCEPT (branch @ `9e25172`, pushed; unmerged)
- **Path:** 2 fresh adversarial reviews (narrow REWORK → focused ACCEPT with minors, closed and Manager-verified). SYS06 green.
- **Delivered:** automation-principal admission for both producers (never release/bypass a stop hold); watched-job turn keyed `watched:<job_id>` (one turn per completion), refused/errored admission ⇒ audit record (per-job containment, never escapes the poller batch), withdrawal audit written in the same txn as withdraw/close; heartbeat admitted idle-only (in-txn predicate incl. hold record, pause, any open managed/legacy row), 300 s deadline, withdrawn at claim when expired or no longer idle (a heartbeat never runs ahead of a human turn), beat counted once by fenced finalizer that runs before the flag gate; nothing-enrolled adds zero reads (all-thread trace).
- **Carried (CONTEXT.md):** watched-job delivery at-most-once across restarts (in-memory watermark, legacy parity); heartbeat `failed_node_offline`/`cancelled` count no beat (intentional divergence: outage/operator stop must not disable heartbeats); linked heartbeat leases finalize only while either wake/heartbeat flag is on.

### A87 architecture rulings 2026-09-25 (binding on A82 remaining stages and A84)
1. **One pathway is the end state.** Protocol 0 (legacy poll/claim/result + legacy `send`) and protocol 1 (managed) coexist ONLY while A82 is being built, behind default-OFF flags. A82 gains a mandatory final **Stage 8 — cutover and legacy deletion**: enroll all sessions, drain in-flight protocol-0 work, then delete the protocol-0 routes, the legacy send branch, and the enrollment/`WORKER_MANAGED_TURNS` flags. A82 is not closed while two paths exist.
2. **The managed-turn contract lives once on `CodingBackend`** (`supports_managed_turns` / `run_managed_turn` / `is_quiescent`). The carrier calls only the interface — no backend-name branching, no private side doors into a driver. Unsupported backends fail closed (claim refused at worker and server), never fall back to legacy.
3. **Codex and OpenCode must implement the contract** before Stage 8 can delete protocol 0 (scope added to A82; each adapter implements against its own native protocol).
4. **A84 is built only on the managed path** — no completion-delivery logic for protocol 0.

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
