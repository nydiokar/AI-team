```yaml
job_id: AGENT_95_SYSTEM_ONE_DISPATCH_PREFLIGHT
created_at: "2026-10-05T08:46:34.378580+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: AGENT_94_SYSTEM_ONE_CORE_DELIVERY_SCORECARD
results_ref: DISPATCH_LOG.md#A95             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-05T08:46:51.273644+00:00"
```

# DISPATCH — A95 · System-One Move 2 "soft pre-flight" on `dispatch_worker`

**Level:** 3 (agent-behaviour change: the Manager's dispatch can be held; crosses the MCP→gateway
service boundary) · **Type:** code
**Authored:** 2026-10-05 · **Status of this packet:** ready (authored; execute after A94 merges)
**Depends on:** A94 (S1 core, decision log, flags pattern)
**Branch:** `feat/system-one-preflight` + PR + self-merge.
**Canonical spec:** [`docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md`](../../docs/SYSTEM_ONE_DECISION_LAYER_SPEC.md)
§6.2 (normative), §4 principles, §7 M6.

> **Read this first — why this packet exists.** The costliest strategy failures are born at dispatch.
> Examples: the agent builds the literal words rather than the outcome; work drifts out of scope;
> components get duplicated (`docs/harness/loop_config_map.md:285-298`). Today nothing checks an envelope
> before a worker session (and its spend) starts. A calibrated pre-flight can return a high-risk
> envelope to the Manager *inside the same turn*, the cheapest moment to fix it, while the Manager keeps
> authority via an explicit override.

## Why (intent)
Fewer reworked, off-objective or duplicate worker tasks, by catching weak envelopes before dispatch.
The Manager retains control (P1), and the effect is measured as flagged-vs-unflagged rework separation.

Second-order effect on A94: forcing criteria to pass `acc_<i>_uncheckable` here shifts A94's delivery
coverage toward `acc_<i>_unmet` — the strongest question category (criteria-grounded, one hop,
literal). Vague criteria that slip through pre-flight fall back to open-ended text-only questions that
are structurally weaker. The two moves are mutually reinforcing: better envelopes at dispatch → better
Jev precision at delivery review.

## TASK
1. Battery `src/system_one/batteries/preflight.py`: state, code-only checks, questions and gate per
   spec §6.2.
   - `case_tasks` comes from one batched query (≤ 10 recent worker tasks of the Case; no N+1).
   - Unit tests over fixture envelopes.
2. Gateway endpoint `POST /api/system-one/preflight` in `src/control/control_api.py`: authenticated,
   bounded request size (follow the `_INSTRUCTIONS_MAX_REQUEST_BYTES` pattern at line 326), runs the
   battery via `run_battery`, returns `{verdict, reasons, render, decision_id}`; fail-open returns
   `{verdict:"pass"}`.
3. `scripts/mcp_manager.py::_dispatch_worker` (line 401): when a `case_id` is present, call the
   endpoint **before** `POST /api/sessions` (line 490).
   - On `act`, return the hold text (spec §6.2) without dispatching.
   - New optional tool arg `preflight_ack` (bool) skips the hold and is logged as an override.
   - Update the tool schema in `_TOOLS` and `manager.md`'s dispatch section with one sentence on the
     hold/ack semantics.
4. Flags `S1_PREFLIGHT_SHADOW` and `S1_PREFLIGHT_ACTIVE` (registry plus `docs/backend/ENV_FEATURE_FLAGS.md`).
5. Tests:
   - endpoint auth, size bound and fail-open;
   - `mcp_manager` hold/ack paths with a stubbed `_api_request`;
   - flags OFF gives a byte-identical dispatch payload (extend `tests/test_mcp_manager.py`).

## TYPE
code — branch + PR + self-merge.

## CONTEXT (reuse verbatim)
- `mcp_manager` runs on the Manager's node, from that node's checkout (`src/mcp_launchers.py`), and calls
  the gateway over HTTP. The key and battery therefore live gateway-side. Nodes pick up the
  `mcp_manager` change when their checkout updates; new Manager sessions spawn the MCP server from disk.
  **No worker-daemon restart is required** — verify this at implementation and record the result.
- Envelope template: `docs/harness/roles/manager.md:95-121`. The model-selection contract forbids S1
  choosing models (spec §1.4).

## ACCEPTANCE (proof, not vibes)
1. Shadow period: ≥ 60 Case dispatches logged; flagged envelopes reworked **≥ 2×** as often as unflagged
   ones (spec §7 M6). Report artefact `results/s1/preflight_<version>_<date>.json`. Fail → stop rule;
   the job closes with an honest FAIL and ACTIVE stays off.
2. Targeted pytest green, no network.
3. Added dispatch latency p95 < 1.5 s (decision log).
4. ACTIVE enabled only with operator approval; override rate < 50% over the first 30 holds, else back
   to shadow.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — Enabling ACTIVE.** Operator, after ACCEPTANCE 1. Default: shadow only.
- **R2 — Threshold `T_ACT_PREFLIGHT`.** Starts at 0.85 and is fitted from shadow data. Operator sees the
  fitted value before ACTIVE.

## SCOPE OUT
- Any model/tier selection.
- Blocking without override.
- Envelope rewriting by S1.
- Pre-flight for non-Case dispatches.
- Push/Telegram notifications on holds.

## TRAIL / EVIDENCE (fill at close)
- `evidence:` → preflight report artefact, tests, PR.
- DISPATCH_LOG A95 closure.
- CONTEXT shift note.

---
## Milestone (burndown)
- [ ] Battery + tests
- [ ] Endpoint + tests
- [ ] mcp_manager hold/ack + schema + manager.md sentence + tests
- [ ] Flags + docs
- [ ] Shadow period ≥ 60 dispatches → report
- [ ] Decision: ACTIVE (R1) or stop

## Closure (fill on completion)
