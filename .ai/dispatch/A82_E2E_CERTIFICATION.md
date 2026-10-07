# A82 Session Turn-Queue — End-to-End Reconciliation & Certification

- **Date:** 2026-10-07 (UTC)
- **Author:** read-only audit (code/doc/git/live), no `src/` change, no legacy deletion.
- **Question (operator):** Is the "A82 done" claim true? What is fully merged/live vs deferred vs
  OPEN/half-implemented, before new work starts?
- **Method:** every claim grounded in git (`git merge-base --is-ancestor`), code
  (`symbol_lookup`/`grep` + `file:line`), the running gateway (read-only GETs on
  `http://127.0.0.1:9003`), and the test files on disk. Packet = `AGENT_82_SESSION_TURN_QUEUE.md`
  (2499 lines); live docs = `A82_STAGE8A_VALIDATION.md`, `A82_STAGE8A_BACKEND_AUDIT.md`,
  `A82_STAGE8A_DEPLOY_BURNDOWN.md`.
- **HEAD at audit:** `9b5520f` (main). All A82 shas below verified as ancestors of this HEAD.

---

## VERDICT (one line)

**A82 is fully complete e2e and clean: NO.** Stages 0→8a are merged, on `main`, and Stage 8a is
**live and FIFO-proven for the Claude path** — the core feature works. But the project is **not
closed**: **Stage 8b (the convergence cutoff = deleting the now-dormant legacy execution/respawn
path) is not done and has no packet**; the multi-backend managed live turn (Codex / opencode-server)
is **never proven live**; agent-source send is **enabled but unproven**; and both the CONTEXT.md and
DISPATCH_LOG A82 rows are **stale** (still say "cutover pending / NOT merged"). See the burndown in
§9.

---

## 1. Stage matrix (Stage 0 → 8a) — all confirmed ON `main`

Every sha below returned "ON main" under `git merge-base --is-ancestor <sha> HEAD`. Verdicts are the
adversarial-review verdicts recorded in the packet §16 tail (fresh-context Opus reviewer, 2026-10-02
unless noted). Test files all confirmed present on disk (line counts in §7/§10).

| Stage | Delivers | Key merged sha(s) on main | PR | Review verdict (packet §16) | Covering tests |
|---|---|---|---|---|---|
| **0** Ground-truth inventory | Read-only symbol inventory | `96cba58` | — | **ACCEPT** (authorize Stage 1), L2454 | — (read-only) |
| **1** Red acceptance tests | Assertion-capable red tests | (folded into Stage 2 commits) | — | **ACCEPT** (Stage 1 authorized), L2454 | `test_turn_queue_db.py`, `_ownership.py`, `_sdk_ownership.py` |
| **2** Schema+txn seams+ownership | Migration 34, strict managed DB helpers, managed SDK ownership | `feat/session-turn-queue` series | — | gated → accepted via Stage 3 | `test_turn_queue_db.py` (DB01-08), `_ownership.py` (OWN01-10), `_sdk_ownership.py` (SDK01-04) |
| **3** (+6 rework rounds) | A87 findings closed, protocol hardening | `e4f766d` `fb45026` `a4b572b` `bcba6e7` `4eaf369` `31ffb62` `2bca420` `ca50f0a` `071a78f` | — | **ACCEPT (round 5)**, L987 | SDK05-07, INT01-14 |
| **4a** Admission + fair scheduler + producer 1 | Shared allowance, FIFO scheduler, task enqueue | `1a58247`..`163cd6b` | — | **ACCEPT (round 2)**, L1200 | `test_turn_queue_admission.py` (ADM01-04f) |
| **4b** Producer 2 (compaction, cancel/stop, close) | Operator cancel/stop, session close | `fc207b3`..`fdc4365` | — | **ACCEPT**, L1378 | `test_turn_queue_4b.py` |
| **4c** Producer 3 (Case continuation linkage) | `producer_turn_id`, token linkage, continuation recovery | `9a8db54`..`8dde7d2` | — | **ACCEPT (round 2)**, L1491 | Q02/Q02b/Q17 |
| **4d** Producers 4+6 (watched-job, cache heartbeat) | SYS08, cache heartbeat | `91ad69f`..HEAD-of-series | — | **ACCEPT (round 2)**, L1601 | SYS08, heartbeat suite |
| **4e** Producers 5+7 (quota/transient retry, respawn) | Quota pause/retry, managed respawn | `00586b2` `517c5ae` `1bef7fb` `deccc28` `226ac1c` `498b1c6` `581eaa9` `a9e796a` `85ef3e4` | — | **ACCEPT (round 2)**, L2462 | quota/transient/respawn suites |
| **5** Scoped agent sender | Capability issue/validate/revoke, sender binding | `3eec1f0` `1b91d42` `80c68ec` `29816e9` `95fd837` | — | **ACCEPT (round 2)**, L2467 | `test_turn_queue_sender.py` (AUTH01-11) |
| **6** (+follow-ups) UI/API truth | turn-requests API, queue panel, session badge, effects-failed surface, Telegram parity | `491372d` `d9b2f14` `c45d586` `fae95fa` `78cd8b4` `ba47746` `8425294` `4b3b8a0` | — | **ACCEPT (round 2)**, L2473 | `test_turn_queue_api.py` (API01-08), `web/src/lib/turnQueue.test.ts` (UI01-08) |
| **7** Regression/pressure/rollout rehearsal | 8 pressure + 19 rollout tests, enrollment service, service-boundary walk | `732f632`..`cb2f768` | — | submitted; coverage walk passes (see §8) | `test_turn_queue_stage7_pressure.py`, `test_turn_queue_rollout.py` (ROLL01-06) |
| **R0** Merge flags-OFF | Migrations 34-41 on main, all new flags OFF, 0 enrolled | **`e6838f6`** | **#182** | **ACCEPT for R0 (flags OFF)**, L2490 | 193 offline + 182 web (packet) |
| **A84** Managed-completion effects consumer | gateway-side exactly-once completion consumer (`effects_state`) | `06f3f69` (merge), docs `1749f47` | **#184** | merged+deployed 2026-10-02 | A84 suite (N1-N3 kill tests) |
| **8a** Cutover | Born-managed sessions, protocol-0 fences, migration 43, OpenCode CLI retired, coverage/health, F1-F6 minors | `9e39b52` `e03b80f` `a51b832` `7871203` `0f67ba0` `a39556e` `e97e7e6` `ea8a7cb` `13857dc` `b867379`; cutover `999f95d`; merge **`60d5747`** | **#185** | **ACCEPT, no blockers** (@ `e5fc7a4`), L2438 | `test_turn_queue_stage8a.py` (S8-01..S8-15, 861L) |

**Branch-containment caveat resolved:** the PR-merge commits `e6838f6` (#182), `60d5747` (#185),
and the cutover `999f95d` are all ancestors of `main` HEAD — this is *on main*, not merely on a
feature branch. Confirmed.

---

## 2. Stage 8a live-state confirmation (the running gateway)

| Fact | Evidence | Source |
|---|---|---|
| Schema **43** applied cleanly | "schema_version=43 (migration 43 applied cleanly)" | `A82_STAGE8A_DEPLOY_BURNDOWN.md` L100-101; `A82_STAGE8A_VALIDATION.md` L6 |
| `coverage_ok = true` (live, at audit) | `{"turn_queue":{"coverage_ok":true}}` | `GET /health` 2026-10-07T16:17Z |
| No uncovered carriers, 0 retired backend sessions | `{"checked":true,"coverage_ok":true,"managed_carrier_missing":[],"retired_backend_sessions":0}` | `GET /api/turn-queue/coverage` |
| Carrier env set | `MESH_LOCAL_CARRIER_NODE_ID=kanebra`, `WORKER_MANAGED_TURNS=1`, `MESH_ENABLED=true` | `docker exec ai-team-gateway-1 printenv` |
| Managed carriers online | `kanebra` online `[claude,codex,opencode,opencode-server]`; `Horse` online same; **old `kanebra-worker` offline** | `GET /api/nodes` |
| FIFO + born-managed + resume proven live | All 10 assertions PASS: 202 accept of 2nd turn on busy session, distinct ids, no clobber, FIFO delivery, `resume_session` (not create), both `effects_state=done` | `A82_STAGE8A_VALIDATION.md` (2026-10-07T15:20-15:21Z, carrier `kanebra`, session `13c4a3019ba7`) |
| Single admission authority HONORED | Both `POST /api/instructions` (enrolled branch) and `POST /…/turn-requests` converge on `MeshDB.enqueue_turn` (`db.py:2868`); no parallel queue; no premature BUSY write | `A82_STAGE8A_BACKEND_AUDIT.md` (VERDICT: HONORED) |

**Note on the burndown's `coverage_ok=false`:** that was a *transitional* read (3 stale sessions
pinned to the old `kanebra-worker`). At this audit `coverage_ok=true` and `kanebra-worker` is
**offline** — the transitional gap has cleared (burndown S7 effectively reached; see §9).

**Image name nuance:** validation/burndown reference `ai-team:prod-cdef7ec`; the running compose
containers currently report image tag `ai-team:local` (up 3h). This is the compose image name, not a
contradiction — schema 43 + coverage_ok + live FIFO prove the cutover code is what is running.

---

## 3. Carried-residual ledger — every deferral & carry with current status

Legend: **CLOSED** (evidence) · **STILL-DEFERRED-TRACKED** (documented, by-design/accepted) ·
**OPEN-UNTRACKED** (open, no job/packet) · **SUPERSEDED-BY-CUTOVER**.

### 3a. Lettered final-review deferrals (s)-(v) — packet §7, L2383-2386
| Id | Item | Status |
|---|---|---|
| **(s)** L2383 | Out-of-process `running→pending` `backend_not_invoked` seen by legacy gate only at next scheduler pass; window bounded by re-claim; managed admission stays exact | **STILL-DEFERRED-TRACKED** (documented bound, by-design) |
| **(t)** L2384 | Staged-file size: set `GATEWAY_UPLOAD_MAX_MB` before enrolling sessions that take web uploads (inherited legacy path) | **OPEN-UNTRACKED** — operational precondition; verify the var is set on the gateway before web-upload sessions enroll (not confirmed set) |
| **(u)** L2385 | Native unsolicited work on a remote carrier not observable at enrollment; per-turn `is_quiescent` is the backstop | **STILL-DEFERRED-TRACKED** (backstopped by-design) |
| **(v)** L2386 | One 360 ms event-loop lag sample from on-loop JSON parse of 96 KiB bodies; within bounds | **CLOSED** (accepted, no change — WONTFIX, measured within bounds) |

### 3b. Stage-2 numbered deferrals (1)-(8) — packet §15, L1058-1069
| Id | Item | Status |
|---|---|---|
| (1) L1058 | Telegram `update_id` dedup not plumbed (key=`task:<id>`) | **STILL-DEFERRED-TRACKED** (no evidence of later closure) |
| (2) L1059 | Cold shared-allowance cache at boot; legacy could briefly exceed shared cap; managed exact | **STILL-DEFERRED-TRACKED** (transient, by-design) |
| (3) L1060 | RSS at 100 concurrent not measured | **CLOSED → Stage 7 pressure rehearsal** measured +49.5 MiB peak at 100 concurrent (`test_turn_queue_stage7_pressure.py`) |
| (4) L1061 | `revise_turn`/`withdraw_turn` on legacy `_write` | **CLOSED → Stage 6 surfaces** |
| (5) L1062 | Enrollment service not implemented | **CLOSED → Stage 7** (`TURN_QUEUE_ENROLLMENT_ENABLED`, enrollment routes) + Stage 8a born-managed |
| (6) L1063 | `compact_session` / direct exec paths unguarded | **CLOSED → Stage 4b producer 2** |
| (7) L1064 | Session BUSY/IDLE display not queue-driven | **CLOSED → Stage 6 session badge** |
| (8) L1065 | No carrier inside gateway; managed rows run on a `WORKER_MANAGED_TURNS` carrier | **SUPERSEDED-BY-CUTOVER** — carrier model is now the live production shape (`kanebra`/`Horse`) |

### 3c. Per-stage "A87 → CONTEXT.md" minor carries
| Id | Item | Status |
|---|---|---|
| S3 m2 L1119 | Carrier assignment runs synchronously on the event loop (bounded, not offloaded) | **STILL-DEFERRED-TRACKED** (accepted perf) |
| S3 m3 L1120 | Shared-allowance blocking `put` polls every 50 ms (≤50 ms latency) | **STILL-DEFERRED-TRACKED** |
| S3 m4 L1121 | Compat cap ≈3.8 MiB (vs design 2 MiB) applies to all `/api/instructions` callers | **STILL-DEFERRED-TRACKED** (documented) |
| S3b m3 L1195 | `node_heartbeat_timeout_sec` must be ≥2× worker heartbeat; documented not clamped | **STILL-DEFERRED-TRACKED** |
| S3b m4 L1196 | Requeued-row backoff can grow ~48 s after gateway restart | **STILL-DEFERRED-TRACKED** |
| S4b L1340-1341 | Interrupt via `ensure_future` may land late; slow lineage writer can birth open child Case | **STILL-DEFERRED-TRACKED** (accepted, lease model) |
| S4c MINOR4 L1372 | Stale whole-row `upsert_session` can rewrite `cancelled` status (legacy parity) | **STILL-DEFERRED-TRACKED** (status-only readers; durable record unaffected) |
| S4c MINOR5 L1373 | Stop with no active turn does not hold (legacy parity) | **STILL-DEFERRED-TRACKED** |
| S4c m4 L1484 | `failed`/`failed_node_offline` consume the wake even if Manager never ran (legacy parity) | **STILL-DEFERRED-TRACKED** |
| S4c Residual 2 L1485 | Re-arm after operator releases hold | **CLOSED** (accepted as desired behavior) |
| S4c A84 note L1486 | When A84 lands, fold `wait_resolved`+outbox consumption+token CAS into one txn | **PARTIAL** — A84 slice 1 merged (effects consumer); the single-txn fold rides on the **Case-outbox slice (carry (o)) which is still OPEN** (A84 row) |
| S4d m3 L1592 | Claim-time withdrawal doesn't run lineage-void (harmless today) | **STILL-DEFERRED-TRACKED** (future deadline-carrying producer must add) |
| S4d m4 L1593 | `failed_node_offline`/`cancelled` heartbeat counts no beat (deliberate deviation) | **STILL-DEFERRED-TRACKED** (by-design) |
| S4e L1728 | No live gateway run; F4 DB fence doesn't stop gateway-LOCAL legacy exec; stuck-mark operator exit deferred | **SUPERSEDED-BY-CUTOVER** for the first two (live run done at cutover+validation; `_LEGACY_SESSION_EXECUTION_RETIRED` + enrollment routing now guard gateway-local exec). **stuck-mark operator exit = STILL-DEFERRED-TRACKED** |
| Carry (o) | A84 Case-scoped completion **outbox** slice | **OPEN-TRACKED** — A84 DISPATCH_LOG row: "Case-outbox slice (carry (o)) open" |

### 3d. Stage 8a review F-items & pre-cutover carries (packet L2417, L2438-2446)
All merged in the Stage 8a minor commits (`a39556e` F2, `e97e7e6` F3, `ea8a7cb` F4, `13857dc` F5,
`b867379` F6) — all ancestors of main. **CLOSED:**
- **F2** unenroll ⇒ 409 `legacy_execution_retired`; `_enqueue_task` refuses non-enrolled up front. CLOSED.
- **F3** unenroll drain now honors `retry_pause_state='pending'` + unfinalized producer links (`managed_obligation_remaining`). **CLOSED** — this is exactly the "F3 Stage-8 blocker" from the final-gate review (L2493), now resolved.
- **F4** `/health` exposes only `turn_queue.coverage_ok`; details auth-gated at `/api/turn-queue/coverage`. CLOSED (verified live).
- **F5** `effects_failed` counts only non-superseded failures. CLOSED.
- **F6** coverage re-check interval 60 s. CLOSED.
- **Session badge / effects-failed surface / N-A (Codex close key) / N-B (managed close teardown) / Level-3 invoke `_abandon_manager_boot`** — delivered in `a51b832`. CLOSED (code present).

### 3e. Final-gate Stage-8 blockers (packet L2493)
- **F1 MAJOR** — managed completions skipped post-commit effects; needs a gateway-side exactly-once
  consumer. **CLOSED by A84 slice 1** (PR #184 `06f3f69`, merged+deployed; CONTEXT.md L122). Live
  proof: validation turns show `effects_state=done` on both turns.
- **F3** unenroll drain gaps — **CLOSED by Stage 8a F3** (above).

---

## 4. The three re-check items the cutover may have changed

**(i) "agent send not reachable live until Stage 8 enrolls fresh sessions AND
`MESH_LOCAL_CARRIER_NODE_ID` set" (packet L2471).** Both preconditions now **hold**: born-managed
enrollment is live (validation L21/L136 — `enrolled:true` with no explicit `/enroll`) and
`MESH_LOCAL_CARRIER_NODE_ID=kanebra` is set in the live gateway. **BUT** the live validation
exercised only `source:operator` turns (validation L155/L202). An actual **agent-source** send over a
minted capability was **not** run live. ⇒ **PARTIALLY-CLOSED: enabled, not proven.** OPEN-UNTRACKED
(proof).

**(ii) Codex / opencode-server managed live turn — ever proven?** **NO.** Packet L2436: "No live
Codex/OpenCode-server managed turn yet (unchanged from step 4)"; L2146: "validated against fakes
only"; L1768: Codex has no `run_managed_turn`/`supports_managed_turns` so a Codex session "can never
be enrolled or receive a managed claim." The live carriers *advertise* `codex`/`opencode-server`
(`/api/nodes`), but no managed turn on those backends has ever been driven end-to-end. ⇒
**OPEN-UNTRACKED (multi-backend live proof).**

**(iii) Stage-8 cutover preconditions — did they hold?** Yes, per evidence:
- R1 workers-first on managed code — Horse `S3` DONE (burndown L81-82: canary=false, slots=4,
  managed_backends populated); `kanebra` managed carrier online.
- `MESH_LOCAL_CARRIER_NODE_ID` set — yes (`=kanebra`, live).
- Migration 43 applied, 0 unenrolled, `coverage_ok=true`, one web turn complete — all satisfied
  (schema 43; validation PASS; coverage_ok live).
- **A84 completion consumer (hard prerequisite)** — landed before cutover (PR #184). Satisfied.

---

## 5. Stage 8b — precise definition (what a future job must delete; gated by what)

**One-paragraph definition:** *Stage 8b is the convergence cutoff — it deletes the now-dormant legacy
session-execution and single-flight respawn code that Stage 8a made unreachable in production (behind
the `_LEGACY_SESSION_EXECUTION_RETIRED` invariant), plus the harness-only tests that only exercise
it, and folds the dual admission front-door into one.*

**Concrete deletion targets (verified present on `main`):**
1. `TaskOrchestrator._LEGACY_SESSION_EXECUTION_RETIRED = True` — `src/orchestrator.py:12281`
   (branches at `:11161`, `:12286`). The cutover invariant flag + the legacy branch it guards.
2. `_do_respawn_manager_for_case` legacy branch — `src/orchestrator.py:3900`: the NO-dead-session-id
   fallback that still takes the legacy single-flight path (packet L2432: "8b deletes the branch").
3. The legacy `RESPAWN_ACTION` single-flight respawn mechanics and their tests —
   `tests/test_case_respawn.py` (346L; 5 tests packet L2406 flags as pure-legacy, retire).
4. The legacy `/api/instructions` non-enrolled execution branch + legacy in-memory `SessionTaskQueue`
   path for enrolled sessions (now bypassed, `orchestrator.py:6265` comment).
5. The dual front-door fold — `routes/turn_requests.py:115` REVISIT note ("overlaps POST
   /api/instructions for enrolled sessions — fold plan at that route"); backend audit §6.1.
6. The 14 harness-only converted tests + their `_setup(enroll=False)` fixtures, which run only under
   the offline harness that flips `_REFUSE_SESSION_TURNS_WITHOUT_MESH`/`_LEGACY_SESSION_EXECUTION_RETIRED`
   to False (packet L2401, L2448). `_REFUSE_SESSION_TURNS_WITHOUT_MESH` = `orchestrator.py:12273`;
   `_QUEUE_TURNS_FOR_OFFLINE_CARRIER` = `:12313` (keep — operational switches, not legacy-exec).

**Preconditions gating the deletion (all currently MET except the live-proof gaps):**
- Stage 8a merged + live (MET — §2).
- All sessions `turn_queue_enrolled=1` / migration 43 applied, no pending protocol-0 rows (MET).
- Legacy work drained; `kanebra-worker` (old non-managed) retired (MET — offline, coverage_ok=true).
- A84 completion consumer live (MET — PR #184).
- **Advisable but not met:** multi-backend managed live proof (ii) and agent-send live proof (i) —
  deleting legacy before these are proven removes the fallback, so a Manager should weigh proving
  them first. **Case-outbox carry (o)** also still open.

**This audit deletes nothing** — it specifies the scope so a future Stage-8b job can execute it.

---

## 6. Stage 7 rollout rehearsal + six-item service-boundary walk — evidence

- **Rollout rehearsal: PERFORMED.** `tests/test_turn_queue_rollout.py` (429L) — 19 tests ROLL01-ROLL06
  (flag-OFF refusal, no-canonical-DB 503, idempotent enroll, cutover interleavings, unenroll-refused-
  until-drain, mixed-version/rollback) — packet L2359-2366.
- **Pressure rehearsal: PERFORMED.** `tests/test_turn_queue_stage7_pressure.py` (835L, opt-in
  `A82_PRESSURE=1`) — 8/8 pass, 54 s on Pi: 100 max-size requests (14×202/86×429), peak concurrency
  **4**, 100 MiB intent (51 rows), +49.5 MiB VmRSS, event-loop lag ≤4.5 ms — packet L2338-2358.
- **Six-item service-boundary walk: PERFORMED & tabulated** — packet L2371-2380, bounds measured:
  (1) admission 4 permits/429/≤50 rows·100 MiB/256 KiB/5 s body/422; (2) start-result token-CAS/≤8 MiB/
  spool ≤128 MiB/10 s; (3) credential send same as admission; (4) recovery CAS/16 KiB·8 MiB/503;
  (5) staged-file fetch per-turn semaphore — **`GATEWAY_UPLOAD_MAX_MB` bound explicitly DEFERRED
  (deferral (t))**; (6) Codex/OpenCode capacity 8 / semaphore 4 / app-server 32 / 30 s / turn ≤36000 s;
  (7-new) enrollment operator-only/16 KiB/5 s.
- **Evidence gap (honest):** Stage 7 carries the review-tail note "SUBMIT FOR REVIEW, NOT ACCEPTED"
  (packet L2295) — there is **no explicit standalone "Stage 7 ACCEPT" verdict line** in §16 the way
  Stages 4e/5/6 have one; Stage 7's content was carried into and accepted via the pre-cutover /
  final-gate reviews (L2485-2494) and the Stage 8a ACCEPT. The *rehearsal itself was performed* (tests
  exist and pass); the *clean standalone verdict line* is the gap. Not blocking (Stage 8a supersedes).

---

## 7. Doc-drift & hygiene findings (report-only; corrections NOT applied per scope)

1. **CONTEXT.md A82 row is STALE (line ~56).** Says "Stage 8a SUBMITTED for review (branch
   `feat/a82-stage8a`, PR open, NOT merged)". Reality: PR #185 **merged** (`60d5747`), cutover live
   (schema 43). **Needs correction** to "8a MERGED + LIVE; 8b next (no packet)".
2. **DISPATCH_LOG.md A82 row is STALE (line 52).** Says "Stage 8 cutover pending (needs A84
   completion consumer + R1 worker restarts, operator-gated)". Reality: cutover done, A84 merged,
   workers restarted, `kanebra`/`Horse` managed. **Needs correction.**
3. **A82 job yaml `status: active`** (packet head) — should move to `blocked` (on Stage-8b decision)
   or stay `active` only if Stage 8b is folded in; it is not `done`. `evidence:` points at
   `tests/test_turn_queue_respawn_revalidation.py`.
4. **Carrier-id config drift.** CONTEXT.md runbook + burndown S7 say the carrier should be
   `kanebra-worker`; the live gateway uses `MESH_LOCAL_CARRIER_NODE_ID=kanebra` and the online
   managed node is `kanebra` (old `kanebra-worker` is offline). Functionally fine (`coverage_ok=true`)
   but the docs name the wrong node id — reconcile to `kanebra`.
5. **Burndown checkbox drift.** `A82_STAGE8A_DEPLOY_BURNDOWN.md` still shows S5/S6 unchecked `[ ]`,
   but its own PROGRESS section + the validation doc + live state show they are done; S7 is
   effectively done (old worker offline). Only S8 (Stage 8b) genuinely remains.
6. **Merged-but-undeleted A82 branches** (`git branch --merged main`, all fully merged — safe to
   delete; `--force` not required, not run per scope):
   `feat/session-turn-queue`, `feat/session-turn-queue-integration`, `feat/session-turn-queue-mainline`,
   `feat/a82-producers`, `feat/a82-opencode-managed`, `feat/a82-stage8a`, `feat/a82-s8a-recon`
   (+ related `feat/a84-managed-completion-consumer`). 7 A82 branches dangling.
7. **A99 (concurrent, tracked):** the Stage-6 UI shipped but the operator flagged it "awful, not 100%
   functioning" — A99 frontend overhaul is `active` (DISPATCH_LOG). Not an A82 regression, but the
   A82 *UI acceptance* is effectively re-opened under A99.

---

## 8. FINAL VERDICT & prioritized remaining-work ledger (the true burndown)

**A82 fully complete e2e and clean: NO.**

Stages 0→8a are merged and on `main`; Stage 8a is live and the Claude FIFO path is proven
end-to-end with a single admission authority. The **feature core is done and working in production.**
It is **not closed** because the convergence/cleanup phase and multi-backend proof remain, and the
canonical docs lie about the current state.

**OPEN-UNTRACKED item count: 5** (no packet/job covers these):
1. Stage 8b legacy deletion (no packet).
2. Codex / opencode-server managed live-turn proof (ii).
3. Agent-source send live proof (i).
4. `GATEWAY_UPLOAD_MAX_MB` operational precondition (t) — verify/set.
5. Doc-drift corrections + branch hygiene (§7 items 1-6).
*(Carry (o) Case-outbox is OPEN but TRACKED under A84, so not counted here.)*

**Prioritized remaining work (actionable — a Manager can dispatch each):**

| # | Item | Why / gate | Suggested action |
|---|---|---|---|
| **1** | **Fix doc drift FIRST** — CONTEXT.md A82 row + DISPATCH_LOG A82 row + carrier-id `kanebra` + A82 job status | Cheap, stops every future reader inheriting the false "not merged / cutover pending" state | One docs commit (Manager/doc job) |
| **2** | **Author the Stage-8b packet** (deletion scope in §5) | The headline gap; no packet exists. Decide: delete-now vs gate on items 3-4 | Manager writes `AGENT_8x` packet; `src/` branch + PR |
| **3** | **Prove a Codex + an opencode-server managed live turn** (ii) | Multi-backend contract never exercised live; Codex lacks `run_managed_turn` (L1768) — may need code, not just a probe | Dispatch a live-validation worker on `kanebra`/`Horse`; if Codex can't enroll, that's a real build gap |
| **4** | **Prove one agent-source send live** (i) | Preconditions now hold; only operator-source was validated | Live validation via a minted sender capability |
| **5** | **Close A84 carry (o)** Case-scoped completion outbox + the single-txn fold (S4c L1486) | Completes durable completion delivery; tracked under A84 | Continue A84 (slice 2) |

Secondary / hygiene (fold into item 1 or Stage 8b): delete the 7 merged A82 branches; verify
`GATEWAY_UPLOAD_MAX_MB` (t); add a clean Stage-7 standalone verdict note if desired.

---

## Appendix — grounding commands run (read-only)
- `git merge-base --is-ancestor <sha> HEAD` for all shas in §1 (all ON main).
- `git branch --merged main | grep -i a82` → 7 dangling A82 branches (§7.6).
- `symbol_lookup _do_respawn_manager_for_case` → `orchestrator.py:3900`; `grep
  _LEGACY_SESSION_EXECUTION_RETIRED` → `orchestrator.py:12281`; `_REFUSE_SESSION_TURNS_WITHOUT_MESH`
  → `:12273`; `_QUEUE_TURNS_FOR_OFFLINE_CARRIER` → `:12313` (§5 targets confirmed live in code).
- `GET /health`, `GET /api/turn-queue/coverage`, `GET /api/nodes` (§2 live facts).
- `docker exec ai-team-gateway-1 printenv` (carrier env), `docker ps` (image).
- Test files confirmed on disk: `test_turn_queue_{db,ownership,sdk_ownership,admission,sender,api,
  stage7_pressure,rollout,stage8a}.py`, `test_case_respawn.py`, `web/src/lib/turnQueue.test.ts`.
</content>
</invoke>
