# Worker Cache-Heartbeat Extension — Design

Status: **REJECTED / RETIRED (2026-09-22).** Kept as a record of the reasoning, not a plan.
Date: 2026-09-19 (proposed), 2026-09-22 (rejected).
Scope: extend the session-cache heartbeat so **worker** sessions preserve their prompt cache.

> ## Why this was rejected (operator decision, grounded)
> The proposed `worker_warm` producer would keep a parked worker's cache hot on the *guess* that
> the Manager returns to it. **We do not know that** — in most cases keeping it warm is redundant
> paid work. Separately, the two legitimate needs are already met or are not heartbeat problems:
> - A worker waiting on a **detached** script (`watch_job`) already gets a heartbeat — the
>   `watched_job` producer is role-agnostic and live. **Already have it.**
> - The real observed failure was a worker running a script **in the foreground without
>   detaching** it — so the turn was genuinely BUSY and only *looked* like a stuck wait. No
>   heartbeat can (or should) touch a BUSY turn; the cure is detaching via `watch_job`, and the
>   *state-legibility* half is handled by `docs/SESSION_WAIT_STATE_GRANULARITY.md`.
>
> Net: objective 1 is redundant. Work continues only on the secondary-label design (objective 2).
> The analysis below is retained for provenance; do not implement it.

---

_Original proposal follows (superseded)._

---

## 1. Problem (as observed)

The cache heartbeat (A80) is live and working: it keeps a Manager's Claude Code prompt cache
hot while the Manager durably waits on workers, and — since 2026-09-18 — it also matches
detached **scripts (watched jobs) to their owning session** and beats them. Confirmed in code.

But a Manager frequently dispatches **one** worker and then waits. The Manager gets the
heartbeat; the **worker** — which usually holds the *larger, more expensive* cache (it carries
the real code context) — does not, unless it happened to register a watched job. When the
worker sits idle for more than the cache TTL (≈1h), its prefix is evicted and the next turn
pays a full cache-creation write again. That is the "cache gets divided as well" the operator
is seeing.

The operator's own framing, grounded: workers are "the same conditions … besides that they're
not waiting for other workers" (the `case_wait_group` producer never fires for a worker), but a
worker "can genuinely be running, or it can be waiting on a drop [a job]."

---

## 2. What already exists (grounded in code — do NOT rebuild)

| Piece | Location | Relevant fact |
|---|---|---|
| Eligibility gate | `src/orchestrator.py:1381` `_cache_heartbeat_session_eligible` | **Role-agnostic already.** Gates on backend=claude, driver=sdk, `backend_session_id`, driver live, no in-flight task, quota available, not case-paused, `next_due_at` past, **`status == AWAITING_INPUT`**, pinned node online, cache tokens ≥ `CACHE_HEARTBEAT_MIN_CACHE_TOKENS` (100k). It never reads `case_role`. |
| Beat delivery / single-flight | `src/orchestrator.py:1433` `_process_due_cache_heartbeats` | Deterministic `cachehb:{session_id}:{slot_epoch}` lease + `claim_task()`. Session-keyed, role-blind. |
| Owner liveness / GC | `src/orchestrator.py:1296` `_cache_heartbeat_owner_live`, `:1322` `_sync_cache_heartbeat_state` | Per-`reason` liveness; stops owners that are no longer live. |
| Producer: manager wait-group | `src/control/db.py:3668` inside `arm_wait_group` | Arms an owner for **`case_manager_session_id(flow_run_id)` only** — the Manager, never the members. `reason="case_wait_group"`. |
| Producer: watched job | `src/control/task_server.py:1135` inside `register_job` | Arms an owner for `payload.session_id` — **role-agnostic**. This is the "scripts matched to sessions" that already covers a worker *if it detaches a job with `notify_agent`*. |
| Producer: manual/agent | `src/control/control_api.py:1284` | Operator/agent-armed, any session. |
| Owner schema | `session_cache_heartbeat_owners` (`db.py:1107`) | `reason ∈ {case_wait_group, watched_job, manual, agent_requested}`; `(session_id, reason, owner_type, owner_id)` unique per active owner; multiple owners → one controller per session. |

**Conclusion: the gate and the delivery path need no change. The gap is a missing *producer*.**

---

## 3. The three worker idle windows, classified

A worker can be in exactly one of these when its cache is at risk:

1. **BUSY, mid-turn (a single long foreground turn).**
   - The gate *forbids* this (`status == AWAITING_INPUT` required) — and correctly so
     (A80 §16: injecting a prompt into a BUSY SDK session is unsafe).
   - **This is NOT the decay problem.** A worker mid-turn is executing a tool loop; each tool
     round-trip re-reads and re-writes the prompt cache, so the cache is continuously touched
     *by the model's own turn*. Decay happens in the **gap between turns**, not inside one.
   - Documented live evidence: A80 §17 (worker `1b3f4686` BUSY on a 67-min GCM step → not
     heartbeated; only its watched job was). **Out of scope, by design, unchanged.**

2. **AWAITING_INPUT, waiting on a detached job ("waiting on a drop").**
   - **Already covered** by the `watched_job` producer, role-agnostic. No new code.
   - Design action: nothing to build; only ensure workers actually *use* `watch_job` for long
     scripts (tool-wording nudge, already prescribed in A80 §6.2). Optionally surface this in
     the Worker role prompt.

3. **AWAITING_INPUT, parked WARM between dispatches (the real gap).**
   - The worker finished a turn; the Manager has not yet sent the next instruction (it is
     reviewing the diff, dispatching a sibling, or itself waiting). Per A48 the worker is kept
     **warm** (session reused, `case_role='worker'`, `current_case_id=<case>`), so its
     expensive cache is exactly what we want to preserve — and it decays during this idle gap.
   - **This is the missing producer.** Proposed below.

---

## 4. Proposed change: one new producer — `reason="worker_warm"`

Add a fourth `reason`. **No gate change, no delivery change, no schema change** (the owners
table already stores an arbitrary `reason`/`owner_type`/`owner_id`).

### 4.1 Arming (when to create the owner)

Arm a `worker_warm` owner for a session when **all** hold:
- `session.case_role == 'worker'` and `session.current_case_id` points at an **open** Case
  (`flow_runs.status NOT IN {closed, completed, cancelled, blocked}`);
- the session just became `AWAITING_INPUT` (turn finished, warm-parked);
- `cache_heartbeat_observe_enabled()` (observe) or `..._active_enabled()` (act).

**Where to hook (reuse an existing seam — do NOT add a scheduler):** the natural trigger is the
same transition that A48/A60 already key on — a worker session settling to `AWAITING_INPUT`
while joined to an open Case. Two equally minimal options:
- **(preferred) at the result-settle path** in `orchestrator._session_status_after_result`
  neighbourhood (`src/orchestrator.py:~391`): when a session is set to `AWAITING_INPUT` and it
  is a joined worker on an open Case, call `db.ensure_cache_heartbeat_owner(session_id,
  reason="worker_warm", owner_type="session", owner_id=current_case_id, ...)`. Single call,
  mirrors how `arm_wait_group` arms the Manager owner.
- **(alternative)** piggyback on the existing `_stale_busy_reconciliation_loop`
  (`src/orchestrator.py:916`, the same loop A60's reaper is told to mirror): each tick, for each
  joined-worker-on-open-Case that is `AWAITING_INPUT`, `ensure_cache_heartbeat_owner(...)`.
  `ensure_*` is idempotent, so re-arming every tick is safe. This is the lowest-risk option
  because it needs zero change to the hot result path.

Recommendation: the **reconciliation-loop** hook. It is idempotent, bounded, off the request
path, and shares the exact worker-selection predicate A60 already defines — one predicate, two
consumers (heartbeat-arm and idle-reap), which keeps the two policies provably disjoint (§6).

### 4.2 Liveness (when the owner dies) — extend `_cache_heartbeat_owner_live`

Add a branch for `reason == "worker_warm"` (`owner_id == case_id`):
```
live  ⟺  Case open (flow_runs.status not terminal)
     AND  session still affiliated (case_role='worker' AND current_case_id == case_id)
```
When the Case closes, the worker is `release_worker`-d, or the session closes/cancels, the
predicate flips false → `_sync_cache_heartbeat_state` stops the owner → controller stops when it
has no live owners. This reuses the existing GC path unchanged.

### 4.3 Everything else is inherited

Beat interval, TTL, `max_beats` (6) / `hard_max_beats` (15), min-cache-tokens gate (100k),
cache-miss circuit, single-flight lease, quota/node/pause gating, `STOP_CACHE_HEARTBEAT`
honouring — all inherited from A80 with no change, because the worker owner rides the exact same
controller and gate.

---

## 5. Rollout (mirror A80 §13)

1. **Observe-only** (`CACHE_HEARTBEAT_OBSERVE` on, already default ON): arm `worker_warm`
   owners, record would-beat decisions, **send nothing**. Measure: how many worker idle gaps
   exceed the interval, and how big those caches are. This tells us if worker beats are worth it
   before paying for a single one.
2. **Active for `worker_warm`** (`CACHE_HEARTBEAT_ACTIVE` on): send beats. Same guardrails.
3. Reassess `max_beats` per session now that a 1-worker Case may run **two** controllers
   (Manager + worker) — see §6.3.

---

## 6. Traps, mismatches, and things to watch (designed *before* implementing)

### 6.1 The "genuinely running" trap — extending to workers does NOT fix the long single turn
The most intuitive reading — "the worker's cache decays while it runs for 90 minutes, so beat
it" — is **wrong and unfixable in v1**. A BUSY worker is mid-turn and (a) cannot be safely
injected, (b) is already touching its cache each tool round-trip. Worker heartbeat only helps the
**idle gap between turns**. This must be stated plainly so the feature is not mis-sold: it will
NOT stop cache loss for a worker doing one uninterrupted hours-long turn. (If that becomes the
real cost driver, the answer is *turn chunking* / watched-job detachment, not heartbeats — a
separate line of work, A80 §13 step 5.)

### 6.2 Interaction with A60 warm-worker idle-reaper — complementary, not conflicting
A60 (`AGENT_60_WARM_WORKER_IDLE_REAPER`, active) closes warm workers **only when their Case is
closed or none** and it is idle beyond a TTL; it **never** reaps a worker still joined to an
**open** Case. The `worker_warm` heartbeat arms **only** for a worker joined to an **open** Case.
The two sets are **disjoint by construction** — heartbeat preserves the workers A60 refuses to
reap; A60 reclaims the workers heartbeat refuses to beat. **Design rule to enforce:** both must
read the *same* worker-selection predicate (joined + open-Case + `AWAITING_INPUT`/idle). If they
ever diverge, we could both pay to keep a worker warm and then reap it — waste. Share the
predicate (one helper), do not copy it.

### 6.3 Double-charge: a 1-worker Case can now run two controllers
Previously a waiting 1-worker Case beat once (Manager). Now it may beat twice (Manager +
worker), up to `2 × max_beats` per wait. That is intended (the worker cache is usually the
bigger one), but it is a real cost increase per waiting Case. Mitigations already in the gate:
the 100k `MIN_CACHE_TOKENS` floor skips a session whose cache is too cheap to protect, so a thin
Manager cache simply won't beat. Still: surface both controllers in the cost view (A80 §12) so
the operator sees "2 beats/Case" and can tune `max_beats` down for the worker if desired.

### 6.4 Warm-park detection must be a real idle, not a mislabeled BUSY
The trigger relies on the worker being `AWAITING_INPUT`. Verify the worker actually *reaches*
`AWAITING_INPUT` when parked warm (A48 keep-warm) and does not linger `BUSY`. From code
(`_session_status_after_result`, `src/orchestrator.py:~391`) a successful/ salvaged turn →
`AWAITING_INPUT`, so this holds — but confirm against a live warm worker in observe-only before
activating. If a worker is ever left `BUSY` after finishing (driver_status stale), the gate
skips it and no beat is wasted — fail-safe, but also means no protection; the observe-only phase
will reveal it.

### 6.5 Two owners, one session, across re-dialogue
When the Manager finally re-dispatches the worker, the worker goes `BUSY` → gate skips (correct).
When it settles back to `AWAITING_INPUT`, the idempotent `ensure_*` re-arms. Beat count is
per-session-episode; we must decide whether each warm gap is a new episode (resets count) or one
continuous episode (accumulates toward `hard_max_beats`). **Recommendation: one continuous
episode per (session, case)** — a chatty Case with many short gaps should still be bounded by
`hard_max_beats=15`, not reset every gap into an unbounded paid loop. This matches A80's "adding
an owner does not reset beat count."

### 6.6 TTL assumption unchanged, still unproven
A80 §7 flags that the cache TTL class (5-min vs 1-h) is not reliably observed on this install.
Worker extension inherits that risk verbatim — it does not add a new one. Keep the
operator-configurable TTL until observed.

### 6.7 Node/remote pinning
Workers commonly run pinned to a remote node (e.g. `Horse`). The gate already checks the pinned
node is `online` and routes through existing affinity (A80 §14 acceptance: "remote-pinned session
routes through existing affinity path, never locally"). Worker sessions are the *common* remote
case, so this path — currently exercised mostly by Manager-local beats — will now carry real
remote traffic. **Verify a remote-pinned worker beat end-to-end in observe→active on Horse
before trusting it.** This is the seam most likely to surprise us.

---

## 7. Adversarial review (self-challenge, A80 §16 style)

- **Claim:** "Just remove the role check so workers get heartbeats." → **False premise:** there
  is no role check to remove; the gate is already role-blind. The work is a producer, not a gate
  relaxation. Removing nothing; adding one arming call + one liveness branch.
- **Claim:** "Workers waiting on a job aren't covered." → **Already covered** by `watched_job`
  (`task_server.py:1135`), role-agnostic, live since 2026-09-18. Do not rebuild; only nudge
  workers to detach long scripts via `watch_job`.
- **Claim:** "Heartbeat the worker while it runs its long turn." → **Unsafe and pointless in
  v1** (§6.1): BUSY sessions are forbidden and are already touching cache mid-turn. The decay is
  in the idle gap only.
- **Claim:** "Heartbeat and idle-reaper will fight over the same worker." → **Disjoint by
  construction** (§6.2) *iff* they share the selection predicate. Enforce the shared helper;
  otherwise this becomes true and wasteful.
- **Claim:** "This is free — same machinery." → **Not free:** it roughly doubles beats per
  waiting 1-worker Case (§6.3). Bounded by `hard_max_beats` and the 100k floor, but a real,
  visible cost that must be observed (observe-only) before activating.
- **Claim:** "A worker owner can outlive its Case." → Prevented by the `worker_warm` liveness
  branch (§4.2) keyed on Case-open + affiliation, plus the existing `_sync_cache_heartbeat_state`
  GC — the same backstop A80 §16 relies on for `case_wait_group`.
- **Residual unknown (stated, not hidden):** the real payoff depends on how often workers sit in
  a >interval idle gap with a >100k cache. That is exactly what observe-only measures; do not
  activate paid worker beats until that number justifies it.

---

## 8. Minimal-change summary (least action)

| Change | File | Size |
|---|---|---|
| New `reason="worker_warm"` arming in the reconciliation loop (idempotent `ensure_cache_heartbeat_owner`) for joined-worker-on-open-Case that is `AWAITING_INPUT` | `src/orchestrator.py` (near `:916` loop) | ~1 predicate + 1 call |
| Liveness branch for `worker_warm` (Case-open + affiliation) | `src/orchestrator.py:1296` `_cache_heartbeat_owner_live` | ~6 lines |
| Shared worker-selection predicate helper (co-used by A60 reaper) | `src/orchestrator.py` | 1 helper |
| Observe-only measurement + cost-view attribution for the new reason | existing A80 read-model / UI | reuse |
| Acceptance tests (fake backend + real `MeshDB`), mirroring A80 §14 | `tests/` | new |

No new table, no gate change, no delivery change, no scheduler. Flag-gated behind the existing
`CACHE_HEARTBEAT_OBSERVE` / `CACHE_HEARTBEAT_ACTIVE`, default-safe.
