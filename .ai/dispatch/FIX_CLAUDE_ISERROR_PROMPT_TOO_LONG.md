# FIX: SDK `is_error` result stored as a successful reply ("Prompt is too long")

**Status:** root cause CONFIRMED with live evidence + SDK source verification.
**Repro turn:** `task_68d7e8c2`, session `31128d41ba31`, ran on node `Horse`, 2026-07-03 10:00–10:04 (4m33s).
**Reference conversation id:** b29fded5-9a83-43c6-abdc-7d3bc29321ff

---

## What actually happened (not a hypothesis)

The user's turn ran ~4m33s on worker `Horse` and did **real, expensive work**
(`usage_json` on the live row: `output_tokens=9749`, `cached_input_tokens=5,958,178`).
On the **final wrap-up message**, the model's cumulative context exceeded the window, so the
Claude Agent SDK returned a terminal `ResultMessage` with:

- `is_error = True`
- `subtype = "error_during_execution"` (or similar error subtype)
- `result = "Prompt is too long"`

Our driver copied `result` verbatim into the chat reply and marked the turn `success=True`.
The gateway stored `status=completed`, `error_class=none`, `return_code=0`,
`reply_text="Prompt is too long"`. That is the bug the user saw.

## Why the SDK did NOT raise (this is the key subtlety)

Verified against installed `claude-agent-sdk==0.2.110` source:

- `ClaudeSDKClient.receive_response()` **yields the `ResultMessage` (including `is_error=True`)
  and terminates normally** — it does not raise.
- The `ProcessError` → structured-error rewrite in `_internal/query.py:334-343` only fires when
  the CLI process **exits non-zero**. Our `_SDKSession` keeps ONE long-lived `claude` process
  alive across turns (`--input-format stream-json`), so the process does **not** exit after an
  error result. Therefore **no exception ever reaches `_run_turn`'s `except` block.**

**Conclusion:** we cannot rely on the SDK raising. The `is_error` check MUST live in our code.

## Two compounding gaps

1. **Primary — `src/backends/claude_driver.py`, `ClaudeSDKClientDriver._do_query` / `_run_turn`:**
   never inspects `ResultMessage.is_error` / `.subtype` / `.errors`; copies `.result` into the
   reply and hardcodes `success=True` for any non-exception response.
   (`grep -n "is_error\|subtype\|stop_reason" src/backends/claude_driver.py` → 0 hits.)

2. **Secondary — `src/control/task_server.py`, `submit_result` (line ~539):** the gateway trusts
   `payload.success` from the worker unconditionally. A bad `success=True` from a worker becomes a
   persisted `completed` task with zero re-validation.

## Observability gap (worth fixing alongside)

`_do_query` synthesizes NDJSON only from usage dicts; it does **not** persist the SDK's real
terminal `result` line. So on exactly the turns you'd want raw fidelity (errors), `raw_stdout`
is just the literal `"Prompt is too long"`. The M3 telemetry adapter reads `is_error`
(`claude_stream_json.py:264`) but never sees it because the driver's synthesized result line omits it.

---

## THE FIX (definitive)

### Fix 1 — driver: detect error results, fail honestly, and SALVAGE the work (primary)

In `src/backends/claude_driver.py`:

**`_do_query`** — when iterating `ResultMessage`, capture the error signal and return it:

- Read `is_error = bool(getattr(msg, "is_error", False))`, `subtype = getattr(msg, "subtype", "")`,
  `errors = getattr(msg, "errors", None)`.
- Change `_do_query`'s return signature to also carry `(is_error, subtype, error_text)`.
  `error_text` = `"; ".join(errors)` if present, else `result_text`, else `subtype`.
- **Salvage:** keep the existing `last_assistant_text` (the streamed narration of the real work).
  On an error result, `output` should be `last_assistant_text` (the useful progress), NOT the
  error string. If there's no assistant text, `output=""`.
- **Include `is_error`/`subtype` in the synthesized result NDJSON line** so the M3 telemetry
  adapter classifies the turn correctly:
  `{"type":"result","subtype":subtype,"is_error":is_error,"usage":usage,"result":result_text}`.

**`_run_turn`** — when the turn came back with `is_error=True`:

```python
success = not is_error
errors = [] if success else [error_text or "Claude returned an error result"]
return ExecutionResult(
    success=success,
    output=output,               # salvaged last_assistant_text (may be "")
    errors=errors,
    error_class="context_overflow" if _looks_like_context_overflow(error_text) else "backend_error",
    execution_time=elapsed,
    raw_stdout=raw_ndjson,
    ...
)
```

`_looks_like_context_overflow(text)` = any of `"prompt is too long"`, `"context_window"`,
`"context window"`, `"blocking_limit"` in `text.lower()` — mirrors the existing checks in
`result_text.py:328` and `orchestrator.py:294/3044`.

Result: the chat shows the friendly **"Session context full — use /compact or start a new
session"** (via the existing failure mapping) OR the salvaged progress text, instead of a bare
"Prompt is too long". `_classify_error` → `context_overflow`, so any compact/retry policy engages.

### Fix 2 — gateway trust boundary (defense in depth)

In `src/control/task_server.py::submit_result`, do NOT blindly trust `payload.success`. If
`payload.success` is True but `payload.output` is a known error phrase and `payload.usage`/errors
indicate an error result, downgrade to failure. Minimal version: re-run the same
`_looks_like_context_overflow` / error-phrase check on `payload.output` when `errors` is empty, and
if it matches, treat as failure (`db.fail_task` with `error_class="context_overflow"`). This
protects against older workers that haven't picked up Fix 1.

### Fix 3 — persist the real terminal result line (observability)

In `_do_query`, when building `raw_ndjson`, always emit the terminal result line with the FULL
`is_error`, `subtype`, `stop_reason`, `errors`, and `result` fields (not just usage). This makes
error turns diagnosable from `raw_stdout` and lets the M3 adapter emit a correct
`invocation.completed status=failed`.

---

## Tests to add

- `tests/test_claude_driver.py`: a fake `ResultMessage` with `is_error=True,
  result="Prompt is too long"` → `_run_turn` returns `success=False`,
  `error_class="context_overflow"`, and `output` is the salvaged assistant text (or "").
- Assert the synthesized NDJSON contains `"is_error": true` and that
  `ClaudeStreamJsonAdapter` emits `invocation.completed status=failed` for it.
- `tests/test_control_api_write.py` (or task_server test): `submit_result` with
  `success=True, output="Prompt is too long", errors=[]` is persisted as a failure.

## Verification

Re-run a long session to context overflow (or unit-simulate). Expect: chat shows the actionable
message / salvaged progress, `mesh_tasks.status=failed`, `error_class=context_overflow`,
`llm_turns.final_status=failed`, and `raw_stdout` contains a full result line with `is_error`.

## Access note for the server manager

Fixes 1 & 3 land in the repo run by BOTH kanebra (gateway) and Horse (worker) —
`C:\Users\Cicada38\Projects\AI-team` on Horse, `/home/cifran/dev/AI-team` on kanebra. **Horse must
be redeployed** (it's a Windows mesh node running the SDK driver) for Fix 1 to take effect on
remote turns; Fix 2 protects the gateway even before Horse is updated. Confirm Horse's
`claude-agent-sdk` version — behavior above is verified for 0.2.110.
