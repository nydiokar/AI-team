# A17 — Regression coverage for backend-usage token aggregation (sum vs peak)

## Locked Packet

<objective_lock>
  <real_objective>The honesty-critical fix that reports codex context as PEAK (max), NOT a meaningless SUM of cumulative running-total snapshots (the bug that produced "166,700,822 tok"), must be protected by tests so it cannot silently regress. The new per-backend usage_aggregation field ("peak"/"sum") must also be asserted.</real_objective>
  <literal_request>Add the missing regression tests for the cumulative/peak aggregation path in src/services/backend_usage.py.</literal_request>
  <interpreted_task>In tests/test_backend_usage.py add tests asserting: (a) codex turns carrying cumulative/growing token counters yield PEAK (max), not SUM; (b) an additive backend (claude) still SUMS; (c) each backend row exposes usage_aggregation == "peak" for codex and "sum" for others; (d) a regression-shaped case where summing would balloon far past the peak, so a future revert-to-sum breaks the test.</interpreted_task>
  <constraints>
    - TEST-ONLY change. Do NOT edit src/ production code. IF you believe you found a genuine defect in backend_usage.py, STOP and report it in your final message — do NOT fix it.
    - No paid CLI. No live gateway calls. Never run `python main.py status`.
    - Verify only with: pytest tests/test_backend_usage.py -q  (see EXACT command below).
  </constraints>
  <non_goals>
    - Do NOT implement the deferred "durable fix" (carrying per-turn counter_semantics through the projection). The code comment defers it on purpose; you only add tests for the current stopgap.
    - Do NOT change _CUMULATIVE_TOKEN_BACKENDS membership. Do NOT touch any other test file or any src file.
  </non_goals>
  <assumptions>
    - build_backend_usage(cfg, valid_backends=[...], telemetry_store=ts) reads each turn's usage via turn.get("metrics"). Confirm by reading src/services/backend_usage.py before writing.
    - The existing _FakeTS + _cfg helpers in tests/test_backend_usage.py are the fixtures to reuse. _cfg() already defines a codex namespace.
    - codex is currently the only cumulative backend.
  </assumptions>
</objective_lock>

## Milestone

**Status:** Complete (pending manager review)

### Burndown
- [x] 1. Read src/services/backend_usage.py + tests/test_backend_usage.py to confirm fixture shapes.
- [x] 2. Add test_codex_cumulative_usage_takes_peak_not_sum (3 growing codex turns; assert turn_count==3, total_tokens==120M peak not 240M sum, input_tokens==90M peak not 180M sum).
- [x] 3. Add test_usage_aggregation_field_reflects_backend (codex row "peak", claude row "sum").
- [x] 4. Add test_additive_backend_still_sums_two_keys (claude sums both keys; sum != max).
- [x] 5. Run EXACT verify command — all green (11 passed).
- [x] 6. Update Milestone Live Log + tick burndown.
- [x] 7. Create this dispatch doc (packet + Milestone in ONE file).
- [x] 8. Commit test file + dispatch doc on branch harness/A17-backend-usage-aggregation-tests.

### Live Log
- Confirmed `_aggregate_usage(dst, usage, *, cumulative)` at src/services/backend_usage.py:66 branches: `cumulative=True` -> `max()`, else additive sum. Usage read via `turn.get("metrics")` (line 141). `_CUMULATIVE_TOKEN_BACKENDS = frozenset({"codex"})` (line 42). `usage_aggregation` set to `"peak" if cumulative else "sum"` (line 112).
- Reused existing `_FakeTS` + `_cfg` fixtures. `_cfg()` already carries a codex namespace.
- Added 3 tests; verify command: 11 passed in 0.59s (8 original + 3 new). --collect-only confirms all 3 new names collected.
- Diff is TEST-ONLY plus this dispatch doc. No src/ change.

### Blockers
None.

### Next Action
Manager review.

## Closure
closure: pending manager review
