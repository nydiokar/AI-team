# Manager Context Continuity — Spec (context-pressure rollover)

**Status:** DRAFT / proposed. Not built. Flag-gated design; default OFF.
**Author:** harness track. **Date:** 2026-08-12.
**Owner doc role:** durable reference doctrine (`docs/`), per `.ai/DOC_MAP.md`.

> Purpose: keep long-running **Managers** out of the context-degradation band without
> inflating token usage or adding operator friction, by generalizing the already-proven
> **crash-respawn** machinery into a **proactive, context-pressure-triggered rollover**.
> This spec is grounded against the actual backend (`claude_agent_sdk` over the `claude`
> CLI) and the actual Claude capability surface — not against assumptions.

---

## 1. Problem (grounded)

A Manager orchestrates a Case over many rounds: it dispatches workers, reads their full
replies, records review verdicts, requests rework, re-dispatches. Every one of those turns
appends to the Manager's **in-process transcript**. Across a multi-round Case that transcript
grows monotonically and is the thing that decays in quality.

Two facts frame the size of the problem:

- **Live model is Opus** (`.env`: `CLAUDE_DEFAULT_MODEL=opus`), and Opus 4.6/4.7/4.8 carry a
  **1M-token** context window (verified against the Claude capability reference; **do not**
  quote a 200k window for this stack). The "degradation by feel at ~300k–400k" that motivated
  this spec is therefore **~30–40% of a 1M window** — consistent with documented long-context
  quality decay that appears well before the hard limit, i.e. **gradual**, not a thrown error.
- The redundancy is total: **all durable Case state already lives in the DB**
  (`flow_runs`, `flow_links`, `flow_events`, `mesh_tasks`). The growing transcript re-encodes,
  in a lossy and expensive form, information that `get_case_brief()` returns cheaply and exactly.

**Root cause:** the Manager's degradation is driven by transcript growth, and that transcript
is redundant with DB-canonical Case state. Nothing today acts on context pressure — the
gateway only *measures* it.

---

## 2. What already exists (do not rebuild)

This spec is deliberately small because the load-bearing pieces are built and proven.

### 2.1 The sensor (context telemetry) — BUILT
- Per-turn projection computes `context_window_tokens`, `context_used_ratio`,
  `context_remaining_tokens` — `src/control/session_timeline.py:392`
  (`_context_fill_summary`) and `src/core/telemetry_projection.py:832`.
- Stored per turn in `llm_turns.metrics_json`; surfaced in the Web UI `ContextFillGauge`.
- Secondary degradation signal: `CacheStats.is_unhealthy`
  (`src/backends/claude_driver.py:87` — cache_creation > 50k **and** hit_ratio < 0.2).

### 2.2 The continuity primitive (respawn / reconstruction) — BUILT & PROVEN
- `db.get_case_brief(case_id)` — single bounded, N+1-free read of Case state
  (`src/control/db.py:3426`); N+1-free proven in `tests/test_case_brief.py`.
- `db.boot_reconcile_case(case_id)` — idempotently re-arms waits/groups at boot
  (`src/control/db.py:3583`).
- `orchestrator._respawn_manager_for_case()` — spawns a **fresh** Manager session, binds it
  to the **same** Case via `create_flow_link` (**no new `flow_run`, no `open_case`**),
  reconciles, delivers a resume turn (`src/orchestrator.py:1165`). Single-flight via the
  atomic `claim_task()` lease (`respawn:{case_id}:{generation}`). Anti-goal test:
  `test_respawn_preserves_flow_run_id_and_creates_no_new_case`.
- Wake-Dispatcher loop that already ticks open Cases and can reach the respawn path
  (`src/orchestrator.py:934` / `961` / `1165`), gated by `CASE_CONTINUATION_ENABLED`.

### 2.3 The governors / flag surface — BUILT
- Numeric runtime flags land via A62: `CLAUDE_SDK_MAX_TURNS`, `CLAUDE_SDK_MAX_BUDGET_USD`
  applied in `_governor_option_kwargs` (`src/backends/claude_driver.py:474`), settable at
  runtime via `/api/flags` (`src/control/control_api.py:1034`), registry at
  `src/control/db.py:206`.

### 2.4 What is NOT available (constrains the design)
- The backend is `claude_agent_sdk` wrapping the local **`claude` CLI**
  (`src/backends/claude_driver.py:589`), **not** the raw Anthropic Messages API. Therefore the
  server-side **compaction beta** (`compact-2026-01-12`) and `context_management` params are
  **not reachable** from this stack. The only compaction lever is the CLI's own `/compact`
  (lossy, unpredictable) — already surfaced to operators in the salvage banner
  (`src/backends/claude_driver.py:217`).
- There is **no session-fork primitive** (`--fork-session`) wired. "Forking" today is
  client-side digest injection (`_maybe_inject_compact_context`, `src/orchestrator.py:4321`),
  used for one-shot task continuation, not for Manager rollover.

**Consequence:** the correct primary mechanism for this stack is **rollover (bounded re-boot
from the DB brief)**, not compaction. This matches the standing architecture rule — *DB is
canonical; the session is disposable.*

---

## 3. Design: context-pressure rollover

**One sentence:** when a Manager's context crosses a degradation threshold, at a *safe idle
boundary*, re-boot it on the same Case from `get_case_brief` — discarding the bloated
transcript, preserving all work — reusing the crash-respawn path with a new trigger.

Crash-respawn already answers "reconstruct a Manager on a live Case from durable state." The
only new thing here is a **second, proactive trigger** for the same action:

| | Crash-respawn (built) | Context rollover (this spec) |
|---|---|---|
| Trigger | Manager session dead (None/CLOSED/CANCELLED) | Manager alive but `context_used_ratio ≥ threshold` |
| When evaluated | Wake-Dispatcher tick, on a satisfied wait | At a safe idle boundary (see §3.2) |
| Action | `_respawn_manager_for_case` | **same** `_respawn_manager_for_case` |
| Lease id | `respawn:{case_id}:{gen}` | `rollover:{case_id}:{gen}` (same atomic-claim mechanism) |
| Resume turn | "you were respawned after a crash…" | "you were rolled over to keep context healthy…" |

### 3.1 The trigger (sensor → decision)
- Read the Manager session's latest turn `context_used_ratio` from the existing projection
  (§2.1). No new measurement code.
- Fire when **either**:
  - `context_used_ratio ≥ MANAGER_CONTEXT_ROLLOVER_RATIO` (default proposed **0.35**), **or**
  - `context_window_tokens - context_remaining_tokens ≥ MANAGER_CONTEXT_ROLLOVER_TOKENS`
    (absolute floor, default proposed **350_000**), whichever trips first.
- Both are **numeric runtime flags** on the A62 surface (`/api/flags`), so the band is tunable
  live without a deploy. Rationale for a **ratio** primary: it survives a model/window change
  (e.g. a future 200k model) where an absolute token count would silently mis-fire.

### 3.2 The boundary (never mid-turn)
Rollover is only permitted when the Manager is at an **idle/terminal boundary** — the same
states the wake path already treats as safe:
- session status is `IDLE` or `AWAITING_INPUT` (a running turn is never interrupted), and
- no in-flight proactive/continuation turn is being delivered.

Preferred evaluation points, cheapest first:
1. **After the Manager returns control** from a turn (post-turn hook) — evaluate the just-
   -recorded `context_used_ratio`. This is the natural, lowest-latency point.
2. **At `arm_wait_group` return** — the Manager is about to sleep anyway; rolling it over here
   costs nothing extra and the next wake boots the fresh session.
3. **Wake-Dispatcher tick** as a backstop, for a Manager parked above threshold that isn't
   taking turns.

### 3.3 The action (reuse, don't reinvent)
On trigger at a safe boundary:
1. Acquire single-flight lease `rollover:{case_id}:{generation}` via `claim_task()` (same
   atomic UPDATE-WHERE-pending mechanism as respawn — one winner, crash-safe).
2. Close the old Manager session cleanly (existing close path; **workers are untouched** —
   `close_case`/session-close already keep workers warm).
3. `_respawn_manager_for_case(case_id)` — fresh session, `create_flow_link` to the **same**
   `flow_run`, `boot_reconcile_case`, deliver a **rollover resume turn** rendered from
   `get_case_brief`.
4. Emit a `case.manager_rolled_over` audit event (mirror of `case.manager_respawned`),
   carrying `{from_session, to_session, generation, trigger: "context", ratio_at_rollover}`.

The resume turn content is exactly the brief the Manager would otherwise reconstruct by hand:
objective, criteria + reconciliation state, round budget, worker roster + session ids, latest
review, open/ready waits. No conversation is carried.

---

## 4. Why this does not inflate tokens (the core requirement)

- A rolled-over Manager starts from a **bounded brief** (`get_case_brief` is capped: ≤500
  events, bucketed in-memory) instead of a **growing 300k–400k transcript**. Every post-
  rollover turn is dramatically cheaper. Net token usage over a long Case **drops**.
- One-time cost: the resume-turn boot (role prompt + brief). This is a few thousand tokens,
  paid once per rollover, against tens/hundreds of thousands saved per subsequent turn.
- The trigger reuses telemetry that is **already computed** every turn — zero added
  measurement cost, no extra model calls, no summarizer.
- Rollover is **not** a summarization pass: there is no "compact the history" model call. It is
  a discard-and-reboot-from-DB. That is precisely why it is cheaper than CLI `/compact`.

---

## 5. Service-boundary checklist (CLAUDE.md §7)

Rollover is triggered off an internal tick/hook, not a raw external endpoint, but it mutates
Case↔session bindings, so the checklist applies.

- **Concurrency / single-flight:** enforced by the `rollover:{case_id}:{gen}` atomic-claim
  lease. Two ticks cannot both roll over the same Case; the loser no-ops. Reuses proven
  respawn concurrency semantics.
- **Idempotency:** at-least-once, effects idempotent — if the process dies between old-session
  close and new-session bind, the next tick sees a Manager-less open Case and reaches the same
  respawn path (crash-respawn covers the gap). No duplicate `flow_run`, no invented work
  (anti-goal test already guards this).
- **Boundary safety:** never fires mid-turn (§3.2). A running turn always completes and posts
  its outcome first.
- **Malformed / oversized state:** if `get_case_brief` exceeds a sane render budget (e.g. a
  pathological event count), **do not** roll over silently into a still-too-big boot — emit
  `case.rollover_deferred` and escalate to the operator (a Case that can't be re-boarded from a
  bounded brief is a design smell to surface, not paper over).
- **Backing-resource failure:** if respawn's session creation fails, the old session is **not**
  closed until the new one is confirmed bound (close-after-bind ordering), so a failed rollover
  leaves the working Manager intact rather than orphaning the Case.
- **Timeout / liveness:** rollover adds no long-held lock; the lease is a normal `mesh_tasks`
  row reaped like any other.

---

## 6. Flags (all default OFF / inert)

| Flag | Type | Default | Effect |
|---|---|---|---|
| `MANAGER_CONTEXT_ROLLOVER_ENABLED` | bool (registry, live) | **OFF** | Master switch. OFF ⇒ byte-identical to today. |
| `MANAGER_CONTEXT_ROLLOVER_RATIO` | float (A62 numeric) | `0.35` | Primary trigger: `context_used_ratio` ceiling. |
| `MANAGER_CONTEXT_ROLLOVER_TOKENS` | int (A62 numeric) | `350000` | Absolute-token backstop trigger. |

With the master flag OFF the post-turn hook returns immediately and no path changes — same
inertness discipline as every prior harness flag (M3.x).

---

## 7. Minimal build plan (increments, each verifiable)

1. **Sensor read helper** — `manager_context_pressure(session_id) -> (ratio, used_tokens)`
   reading the latest `llm_turns.metrics_json`. Unit-test off fixture rows. *No behavior.*
2. **Trigger + boundary gate** — pure decision function
   `should_roll_over(pressure, status, flags) -> bool`. Unit-test the truth table incl.
   IDLE/AWAITING_INPUT gate and both thresholds. *No behavior.*
3. **Wire the action** — call the **existing** `_respawn_manager_for_case` under a
   `rollover:` lease from the post-turn hook (and wake-tick backstop), behind the master flag.
   New audit event `case.manager_rolled_over`. Reuse respawn tests; add a rollover-specific
   trigger test (no paid CLI — assert lease + link + event + preserved `flow_run`).
4. **Deferral / escalation path** — oversized-brief guard → `case.rollover_deferred`.
5. **Live activation** — flip `MANAGER_CONTEXT_ROLLOVER_ENABLED=1` + gateway restart; observe
   `case.manager_rolled_over` on a real long Case; confirm post-rollover `context_used_ratio`
   drops and no work is lost. (Operator-gated, like every prior continuation activation.)

Steps 1–2 are pure functions (TDD-friendly, zero risk). Step 3 is the only behavioral change
and it reuses a proven path.

---

## 8. Explicitly out of scope / rejected

- **Raw Messages-API compaction (`compact-2026-01-12`) / `context_management`** — unreachable
  from a `claude_agent_sdk`-over-CLI backend (§2.4). Would require replacing the backend driver
  with direct Anthropic SDK calls — a far larger change, rejected here.
- **A server-side summarizer** (Haiku digest of the transcript) — adds a model call per
  rollover and a lossy artifact, when `get_case_brief` already gives an exact, free
  reconstruction. Rejected on the "don't inflate tokens" requirement.
- **CLI `/compact` as the primary path** — kept only as the existing *manual* operator lever
  for non-Manager sessions; it is lossy and unpredictable, and it doesn't shrink to the DB
  brief. Not the Manager continuity mechanism.
- **Worker rollover** — workers are short-lived and single-task; their transcripts don't reach
  the degradation band. Not addressed here.

---

## 9. Open decisions to escalate (operator fork)

1. **Threshold band.** `0.35` ratio / `350k` tokens are *proposed*, chosen to sit at the low
   end of the observed degradation band with headroom to boot. The honest way to set these is
   to read the existing `context_used_ratio` telemetry across recent real Manager Cases and
   pick the knee empirically before activation — not to hard-code a guess. Flagged numeric so
   it's tunable live regardless.
2. **Primary boundary.** Post-turn hook (lowest latency) vs. `arm_wait_group`-return (free,
   but only helps Managers that sleep). Recommend **both**, post-turn as primary.
3. **Does a mid-round rollover confuse review continuity?** A Manager that rolls over *between*
   dispatching rework and reading it must re-derive "what am I waiting on" from the brief — the
   brief carries `open_waits`/`ready_waits`/`latest_review`, so it should, but this is the one
   seam to prove live before trusting it unattended.
