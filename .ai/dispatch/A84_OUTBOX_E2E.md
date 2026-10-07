# A84 — completion-outbox FULL-path live e2e + lost-carrier reaper evidence

**Date:** 2026-10-07 (UTC) · **Branch:** `feat/a84-reaper` (worktree `.worktrees/a84-reaper`)
**Backend used:** `opencode-server` / model **`opencode/big-pickle`** (FREE) · **Measured cost ≈ $0**
(no paid Claude/Codex path; pytest cost-guard NOT bypassed; prod flag NOT flipped).

This closes the two items A84 carry (o) left open per **R2**: the live **lost-carrier
reaper** (protocol-1 stale-scan + terminal synthesizer) and the **live e2e proof** of the
full outbox path. With both proven, the outbox is **operational once the flag is ON** — not
"one flag away" (see proof below).

---

## 1. What was built (PART 1 — reaper, TASK 6)

A bounded liveness backstop: a managed (protocol-1) Case worker child whose carrier dies
before reporting terminal is detected and its terminal **synthesized through the SAME atomic
outbox seam**, so a lost carrier still wakes its Manager exactly once instead of stranding it
forever.

- **`MeshDB._claim_staleness_reason`** (`src/control/db.py`) — the single node-truth predicate
  extracted from `list_stale_claims` (DRY: the protocol-0 claim reaper and the new protocol-1
  reaper share ONE predicate, so they can never diverge). Behaviour of `list_stale_claims`
  unchanged (proven by `test_claim_reaper` + `test_turn_queue_carrier_recovery`).
- **`MeshDB.list_stale_managed_children(limit=25)`** — the protocol-1 analog: managed worker
  children of an **open, outbox-mode** Case, in a live state (`claimed`/`running`/
  `recovery_required`), whose carrier is provably gone by the same predicate (lease-expired AND
  node missing/offline/incarnation-mismatch/missing-from-fresh-live_state/over-runtime-cap).
  Bounded (`LIMIT 25`), outbox-mode-only, non-terminal-Case-only; returns ids + reason only
  (no large result bodies loaded).
- **`MeshDB.synthesize_managed_terminal(task_id)`** — in ONE `self._write()` txn: flips the
  live row to `failed`/`error_class='carrier_lost'` (guarded to the live states),
  `effects_state='pending'` (slice-1 parity), and writes the outbox row via the EXACT same
  `_record_case_child_outbox` seam as a real completion. **Fence:** the carrier's `claim_token`
  is left untouched, so a late real `complete_turn` with that token hits the idempotent-replay
  leg (already terminal ⇒ equal result, no re-write, no 2nd row); the PK `INSERT OR IGNORE`
  and terminal-status guard make a double no-op. Returns `synthesized` | `already_terminal`
  (fenced) | `skipped`.
- **`TaskOrchestrator._reap_lost_carriers(db)`** (`src/orchestrator.py`) — folded into the
  existing per-tick `_reconcile_managed_recovery` (**NO new timer, NO unbounded event-loop
  scan**). **Gated by `CASE_COMPLETION_OUTBOX_ENABLED`** ⇒ inert until the outbox is enabled.
  Per-item error containment; emits `case_worker_carrier_reaped`.

**Reaper bound (R2, from existing leases — no invented value):** claim lease 300 s · node
offline 90 s · live_state freshness 90 s · active-task hard cap 1800 s · existing reaper cadence
30 s. **§7 service boundary:** bounded (`LIMIT 25`) index-light scan per tick; ids-only rows
(no body load) ⇒ memory bounded at N=100; serialized single `_write()` txn + PK fence ⇒ no
double-synthesis/double-wake; malformed/missing → `skipped`; DB failure → next-tick retry
(row stays live). No Manager-facing wake semantics changed (the synthesized terminal drains
through the already-proven outbox drain — not a new wake path).

### Reaper tests (real file-backed SQLite, no mocks) — `tests/test_completion_outbox_reaper.py`
13 tests, all green:
`R01` lost carrier detected · `R02` fresh claim not detected · `R03` healthy online carrier not
detected · `R04` incarnation-mismatch detected · `R05` legacy-mode child never detected ·
`R06` closed-Case child never detected · `R10` synth → exactly one outbox row (failed,
effects pending) · `R11` **late real result after synth → fenced** (idempotent replay, no 2nd
row) · `R12` real result before synth → `already_terminal` (fenced) · `R13` idempotent re-scan ·
`R14` missing/non-managed → skipped · `R20` **reaper inert when flag OFF** · `R21` reaper synth →
real drain wakes once → late result fenced.

---

## 2. Live e2e (PART 2) — isolated harness, real FREE backend

Harness: `scripts/a84_outbox_e2e.py` — real `task_server.app` (in-process `TestClient`), real
file-backed `MeshDB`, real `WorkerAgent` carrier, **real `OpenCodeServerBackend` driving
`opencode/big-pickle`**. Throwaway temp DB; `CASE_COMPLETION_OUTBOX_ENABLED=1` set in THIS
process only (prod flag untouched). This is the way the A82 INT-tests ran; the only fakes are
the Manager's in-memory session store + the wake-delivery sink (the genuine
`TaskOrchestrator._continue_case_once` / `_reap_lost_carriers` / `MeshDB.complete_turn` run
against the real DB).

Run: `.venv/bin/python scripts/a84_outbox_e2e.py` → **ALL ASSERTIONS PASSED**, exit 0.

### Raw evidence (passing run 2026-10-07T22:23–22:25 UTC)
```
isolated temp dir: /tmp/a84_e2e_x992vsz7
backend=opencode-server model=opencode/big-pickle (FREE)  node=kanebra
LEG 1: real opencode/big-pickle worker turn under an outbox-mode Case
  opened outbox Case 9b645c1c3dd1444fb239a183c290c7b9 (continuation_mode=outbox)
  dispatched worker child w-real; driving the REAL carrier + backend ...
  carrier returned in 86.1s: task status=completed backend_session_id='ses_ee788d42fffep56576NzCEkv1r'
  real big-pickle reply: 'PICKLE_OK'
  completion_outbox: 1 row (child=w-real, outcome=success, undelivered) ✓
  Wake-Dispatcher delivered EXACTLY ONE coalesced wake presenting w-real ✓
  ACK marked delivered(reason=wake); re-tick is a no-op (exactly-once) ✓
LEG 2: lost-carrier reaper on the SAME real outbox Case
  carrier claimed w-lost (real token captured); now simulating carrier death
  list_stale_managed_children detected w-lost (reason=node_offline) ✓
  reaper synthesized terminal(failed, carrier_lost) + ONE outbox row ✓
  Wake-Dispatcher woke the Manager once for the reaped child ✓
  LATE real result FENCED: idempotent replay, no 2nd row, no re-wake ✓
ALL ASSERTIONS PASSED — outbox delivery + lost-carrier reaper proven live.
```

**LEG 1 (real worker completion → outbox → ONE Manager wake):** a REAL `opencode/big-pickle`
worker turn (86.1 s, native session `ses_ee788d42fffep56576NzCEkv1r`, reply `PICKLE_OK`)
completed through the real carrier + real `complete_turn`, which wrote exactly one
`completion_outbox` row in the terminal txn. The real Wake-Dispatcher delivered **exactly one**
coalesced wake to the Manager (NOT via a legacy wait-group — asserted zero `worker.wait*`
events), the crash-safe ACK marked it `delivered(reason='wake')`, and the re-tick was a no-op
(exactly-once).

**LEG 2 (lost-carrier reaper + late-result fence):** a second worker child (`w-lost`) was
**genuinely claimed through the real `/tasks/{id}/claim-managed` route** (real token captured),
then its carrier was made provably gone (claim aged past the lease + node offline — the exact
DB state a crashed/OOM'd carrier leaves). The REAL reaper detected it (`node_offline`),
synthesized `failed`/`carrier_lost` + one outbox row, the Manager was woken once, and the
carrier's **late real result was fenced** (idempotent replay → status stays `failed`, no 2nd
row, no re-wake).

**Cost:** big-pickle is free; the single real LLM turn cost ≈ **$0**. No paid backend invoked.

---

## 3. Tests / CI

- **Reaper suite:** `tests/test_completion_outbox_reaper.py` — 13 passed.
- **Targeted regression (real SQLite, no e2e):** `test_completion_outbox`,
  `test_completion_outbox_drain`, `test_claim_reaper`, `test_turn_queue_carrier_recovery`,
  `test_case_continuation`, `test_case_respawn`, `test_turn_queue_a84_effects` — **147 passed**;
  `test_control_api`, `test_task_state_truth`, `test_turn_queue_stage6`, `test_turn_queue_4c`,
  `test_wake_dispatcher_eventdriven`, `test_turn_queue_stage8a` — **157 passed**. All rc=0.
- **Full `pytest -q` (the exact CI invocation):** runs on the **PR CI** (GitHub Actions
  `.github/workflows/ci.yml`). It is intentionally NOT run locally here: the project's
  safety-critical cost guard (`.claude/hooks/pytest_guard.py`) blocks a bare full-suite run
  ("pytest with no explicit targets runs the FULL suite"), and the hard rule forbids running
  the full/e2e suite to verify. CI is the sanctioned place for the full green.

---

## 4. "Operational once the flag is ON" — the proof

Enabling `CASE_COMPLETION_OUTBOX_ENABLED` (together with `CASE_CONTINUATION_ENABLED`, already ON
in prod) now yields a genuinely operational outbox:
1. **Happy path PROVEN LIVE:** a real worker completion is delivered through the durable outbox
   and wakes the Manager exactly once (LEG 1).
2. **Lost-carrier backstop PROVEN LIVE:** a carrier that dies without reporting no longer
   strands the Manager — the reaper synthesizes the terminal through the same atomic seam and
   wakes once (LEG 2), gated by the same flag so it is inert until enabled.
3. **Exactly-once / fence invariants intact:** one row or neither (atomic txn); a late real
   result after synthesis is fenced; re-scan is idempotent.

**Reserved for the Manager (NOT done here):** flag flip / deploy / merge, and **TASK 7**
(wait-group removal → A87). No Manager-facing wake semantics were changed; no worker restarted.
