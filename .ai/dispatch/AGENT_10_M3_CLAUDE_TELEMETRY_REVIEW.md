# Adversarial Review — AGENT_10_M3_CLAUDE_TELEMETRY dispatch

**Reviews:** `AGENT_10_M3_CLAUDE_TELEMETRY.md` (pre-implementation)
**Date:** 2026-07-03
**Verdict:** One critical architectural error found and corrected inline (R1).
Two structural findings added as required dispatch edits. One process-constraint
finding that required correcting the "wire into `_run()`" instruction to
"wire into the public method boundary." Remaining findings are advisory. Ship
after the corrections are folded in.

---

## Findings

### R1 (CRITICAL — corrected inline before saving) — `_run()` is NOT the single funnel

The dispatch's "Step 3" originally said to add telemetry post-processing inside
`claude_code.py::_run()`. This is wrong:

- `create_session()` and `resume_session()` call `self._driver.start_session()` /
  `self._driver.send_turn()` — which dispatches to either `ClaudeSDKClientDriver`
  (the primary, SDK-based driver) or `ClaudePrintResumeDriver` (the fallback). Both
  of these bypass `claude_code.py::_run()` entirely.
- `run_oneoff()` calls `self._run()` (the `claude_code.py` private method) directly.

So `_run()` in `claude_code.py` only covers `run_oneoff`. Adding telemetry there would
silently skip all session turns (the common case). The correct boundary is the three
**public** methods on `ClaudeCodeBackend`: `create_session()`, `resume_session()`,
`run_oneoff()` — all three already receive `telemetry_context` and `telemetry_sink`
as named parameters, and all three return `ExecutionResult` with `raw_stdout` populated.

**Required correction:** replaced "wire into `_run()`" with "wire into the three public
methods via a shared `_maybe_emit_telemetry()` helper." DONE in the dispatch before
implementation started.

### R2 (MAJOR) — SDK driver's `_run_turn` does not pass `telemetry_context` through

`ClaudeSDKClientDriver.start_session()` and `send_turn()` both accept
`telemetry_context` as a parameter but pass it to `_run_turn()` — which does NOT
accept or use it:

```python
def _run_turn(self, session, message, *, model, proc_env):  # no telemetry_context
```

This means `telemetry_subprocess_env(telemetry_context)` is NOT being set on the SDK
driver's process environment — the subprocess correlation env vars
(`AI_TEAM_TURN_ID`, `AI_TEAM_INVOCATION_ID`, etc.) are not passed through to the SDK
process today for the `ClaudeSDKClientDriver` path. This is a pre-existing gap, not
introduced by this dispatch — but the dispatch must not make it worse.

**Implication for T2:** the post-process approach (reading `raw_stdout` after the fact)
still works correctly for the adapter — it does not require live telemetry context in the
subprocess env. Just note in the implementation log that subprocess env vars are currently
not propagated via the SDK driver path, and that fixing `_run_turn()` to accept and use
`telemetry_context` is a separate follow-up (not in scope for M3).

**Required action:** add a note in the implementation log that the SDK driver has this
pre-existing gap; do NOT fix it in this dispatch (separate scope, no tests for it yet).

### R3 (MODERATE) — `ClaudeSDKClientDriver._run_turn` returns empty `raw_stdout` on failure

When the SDK driver raises an exception (line ~445), `_run_turn` returns:
```python
ExecutionResult(success=False, output="", errors=[err_str], execution_time=elapsed)
```
No `raw_stdout`. So `_maybe_emit_telemetry` will receive `result.raw_stdout = ""` on
SDK failure. The dispatch's guard `if result.raw_stdout is empty: return` handles this
correctly — no adapter will be instantiated, and no spurious events emitted. Confirm
that the `getattr(result, "raw_stdout", "") or ""` pattern is used (not a bare attribute
access, since `raw_stdout` might not be set on all `ExecutionResult` construction paths
given optional kwargs).

### R4 (MINOR) — Token semantics claim needs fixture verification before hardcoding

The dispatch states `input_token_semantics = "includes_cache"` as a verified fact for
Claude, citing `parse_cache_stats_from_ndjson`. That function proves Claude's API returns
`cache_read_input_tokens` and `cache_creation_input_tokens` as separate fields alongside
`input_tokens` — but it does NOT prove whether `input_tokens` already includes cached
tokens or excludes them. The Claude API documentation (not in this codebase) specifies
the inclusive semantics, but the code has not asserted this via a fixture test.

**Required action:** in Step 1 (fixture capture), specifically record and test a turn
where `cache_read_input_tokens > 0`. The fixture test should assert that
`input_tokens >= cache_read_input_tokens` (which is always true if inclusive) and that
`context_tokens = input_tokens` (not the sum). If the fixture shows
`input_tokens + cache_read_input_tokens = total_context_size` instead, the semantics
are exclusive and the adapter must use the exclusive normalization. Do NOT hardcode
`includes_cache` without a fixture proving it.

### R5 (MINOR) — `_gen_process_instance_id()` does not exist yet

The dispatch references `_gen_process_instance_id()` as if it already exists. It does
not. Use the existing ID generation pattern from `src/core/telemetry.py` (check for
`gen_event_id`, `new_invocation_id`, or similar helpers). Do not introduce a new
naming convention.

---

## Cross-cutting checks (pass)

- **T1 scope:** correctly scoped as manual operational step + doc update. No pytest gate
  required. `[F2]` DB verification step is precise and correct. Privacy scan is specific.
  The "if the smoke fails" contingency is helpful.
- **T2 post-process approach [F6]:** correct for M3. SDK driver already returns NDJSON
  in `raw_stdout` (verified: `raw_ndjson` in `_run_turn` return). Post-processing is
  exactly how `parse_cache_stats_from_ndjson` works — well-precedented.
- **Privacy/allowlist [F8]:** correctly prohibits `content`, `text`, `input`, `arguments`,
  `result`, `output`, `completion`. Default-deny approach matches Codex adapter.
- **Double-count guard [F9]:** the `--include-partial-messages` omission comment in the
  driver is the right reference. With it omitted, usage appears only in the final
  `type=result` plus the last `type=assistant` — the "prefer `type=result`" rule in [F9]
  is correct.
- **Test coverage:** 8 proposed tests cover the critical paths. Privacy sentinel test is
  essential. `test_wire_smoke` (mocked `_run()`-level) should actually mock at the
  `ClaudeCodeBackend` public method level to match where the hook lives.
- **No M4, no turn model change, no dashboard contract change:** correctly bounded.
- **Test cost guard:** no test spawns a real CLI. The fixture-capture step is a manual
  operational step, not a pytest gate.

---

## Required edits before implementation

1. **R1** — DONE inline: wire via `_maybe_emit_telemetry()` at public method boundary,
   not inside `_run()`.
2. **R3** — use `getattr(result, "raw_stdout", None) or ""` (or check `result.raw_stdout
   is not None`) in the guard at the top of `_maybe_emit_telemetry`.
3. **R4** — Step 1 must include a cache fixture and explicitly verify inclusive vs.
   exclusive semantics before hardcoding `input_token_semantics`.
4. **R5** — use the existing ID helper from `src/core/telemetry.py` instead of a
   non-existent `_gen_process_instance_id()`.
5. **R2** — note in implementation log; do NOT fix in this dispatch.
