# DISPATCH — A58 · Quota window coordinator: activation + Phase-2 (act on windows)

**Level:** 3 (runtime behavior on the shared Claude account) · **Type:** code + operator gate
**Authored:** 2026-07-30 · **Status of this packet:** ready (authored, not yet executed)
**Depends on:** N1 salvage (**MERGED** PR #47, `3d9af82` — coordinator now on `main`, flag OFF).
**Unblocks:** honest observation (then mitigation) of the shared-account session-limit windows that kill live runs.
**Ultimate goal this serves:** the "resets 4:40pm" quota failure the operator keeps hitting on live Manager+worker runs.

> **Outcome, not a script.** The observe-only coordinator is on `main` inert. This job turns it on
> (Phase-1: observe) and — only if observation proves useful — decides the Phase-2 *acting* surface.
> Activation is a genuine behavior change on a paid, shared account: it is an **operator decision**.

## Why (intent)
N1 landed `src/services/quota_window_coordinator.py` on `main` behind `QUOTA_COORDINATOR_ENABLED`
(default OFF ⇒ byte-identical; construction gated so no DB file / no loop when off). It is **observe-only**:
it records session/quota-window observations, it does not yet gate or reshape dispatch. The whole reason
it exists is the shared-account limit that has repeatedly halted live loops mid-run. It cannot pay off
while it sits inert.

## TASK
1. **Phase-1 activation (operator-gated):** set `QUOTA_COORDINATOR_ENABLED=1` (+ optional `QUOTA_DB_PATH`,
   `QUOTA_OBSERVE_INTERVAL_SEC`), restart the gateway, and let it observe for a real quota cycle. Verify the
   observation store (`state/quota_windows.db`) fills with honest window data and the observe loop is stable
   (no error spam, bounded interval, no slot/thread leak).
2. **Phase-2 decision (design fork):** from the observed data, decide whether the coordinator should *act*
   (e.g. surface "N minutes to reset" to the Manager so it defers non-urgent dispatch, or warm a window) —
   or stay observe-only telemetry. Do NOT build Phase-2 acting before Phase-1 observation justifies it.

## TYPE
code (Phase-2, if taken). Phase-1 is config + restart. Branch `feat/quota-coordinator-phase2` for any acting logic.

## CONTEXT (reuse verbatim)
- Coordinator: `src/services/quota_window_coordinator.py` (`QuotaWindowCoordinator`, `QuotaWindowStore` sqlite,
  `build_default_quota_adapters`, `build_quota_coordinator_from_config`).
- Flag/config: `config/settings.py` `QuotaConfig` (`enabled`/`db_path`/`observe_interval_sec`) + env overrides.
- Orchestrator lifecycle hook: `src/orchestrator.py` (build only when enabled; `start()`/`stop()` in the
  server lifespan). `start()` no-ops when disabled.
- Spec: `docs/SESSION_WINDOW_WARMING_SPEC.md` + `docs/QUOTA_WINDOW_COORDINATOR_PHASE1.md`.
- Overlap check: this is orthogonal to M3.4 (wake-dispatch) and A53 (turn/cost governor). It observes the
  *account-level quota window*; those bound a *single loop's* turns/rounds. Keep them distinct.

## ACCEPTANCE (proof, not vibes)
1. With the flag ON, one real quota cycle observed; `state/quota_windows.db` holds honest window rows; loop stable.
2. Flag OFF still byte-identical (already verified at salvage: no DB file, no task).
3. If Phase-2 is taken: acting is behind its own flag, default OFF; the "act" decision is written down with the
   observed data that justified it.

## REALITY CONSTRAINTS
- Activation runs on the **paid, shared** account — bound the observation, watch cost, and it is the operator's
  call to flip the flag and restart.
- Observe-only must never itself consume quota (it reads local signals/telemetry, it must not spawn CLI turns).

## RESERVED DECISIONS
- **R1 — activate vs formally retire.** If a cycle of observation yields nothing actionable, decide explicitly:
  keep as telemetry, or retire the subsystem. Do not leave it half-on.

## SCOPE OUT
Turn/cost governor (A53); wake-dispatch (M3.4/A52).

## TRAIL / EVIDENCE (fill at close)
- Flag state + restart · observation-store sample · Phase-2 go/no-go with rationale.

---
## Milestone (burndown)
- [ ] Flag ON + gateway restart + one observed quota cycle
- [ ] Observation store verified honest + loop stable (no leak)
- [ ] Phase-2 act-vs-telemetry decision written from observed data
- [ ] (if acting) Phase-2 behind its own default-OFF flag, PR merged

## Closure (fill on completion)
_(verdict + evidence)_
