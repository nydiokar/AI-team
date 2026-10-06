```yaml
job_id: AGENT_97_FAILURE_TEXT_ERROR_CLASS_FIX
created_at: "2026-10-05T08:46:36.619883+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: cloud-burn #4 (PR #18)
depends_on: []
results_ref: DISPATCH_LOG.md#A97             # -> DISPATCH_LOG.md section with the verdict prose
evidence: tests/test_failure_text_scope.py                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-06T12:34:20.442987+00:00"
```

# DISPATCH — A97 · `_classify_error` reads the agent's own reply as error text (misclassification fix)

**Level:** 2 (localized retry/pause classification logic; no migration, no new state) · **Type:** fix
**Authored:** 2026-10-05 (found during the S1 / Jev analysis; deliberately *not* a Jev job — spec §2)
· **Status of this packet:** ready
**Depends on:** —
**Branch:** `feat/failure-text-scope` + PR + self-merge.

> **Read this first — why this packet exists.** The authoritative error class drives retry counts and
> quota/transient pauses (`_get_retry_strategy` at `src/orchestrator.py:9594`; the pause predicates at
> `:361-392`). It is derived by substring-matching `_failure_text(result)`, and that text **includes the
> agent's own reply** (`src/orchestrator.py:1006-1025` appends `output` and `parsed_output`). A failed
> turn whose reply merely *talks about* timeouts, "503", "permission denied" or "rate limit" (common in
> code work) gets the wrong class. That means wrong retries (wasted paid turns) or a wrong pause. Bare
> `"503"`/`"504"` substrings (`:9800`) are especially loose.

## Why (intent)
Error classes reflect what actually failed, not what the agent wrote about. The class precedence and
structured signals (SDK `subtype`, `api_error_status`, `rate_limit_event`) stay unchanged.

## TASK
1. Confirm the defect with a failing test: a `TaskResult(success=False)` whose `errors`/stderr hold a
   genuine fatal error but whose `output` discusses "timeout"/"503" is currently classified
   `timeout`/`network`.
2. Scope the keyword pass to error-bearing fields (`errors`, `raw_stderr`, terminal error payloads in
   `raw_stdout`/`parsed_output`). The agent reply (`output`) is consulted **only** when no error-bearing
   text exists, preserving today's behaviour for results that carry their error only in `output`.
   Tighten `"503"`/`"504"` to word-bounded HTTP-status patterns.
3. Align `_short_failure_reason` (`src/orchestrator.py:1028`) and its drifted copy
   `src/services/result_text.py:293-353`, which is missing "session limit"/"usage limit". One shared
   marker tuple; no third copy.
4. Investigate and record (fix only if trivial and in scope): `_run_backend_local`
   (`src/orchestrator.py:10296-10309`) reportedly does not copy `raw.error_class` into `TaskResult`, so
   backend-provided classes (`permission_block`, `session_lost`, `cache_unhealthy`, `transient`) are
   re-derived from text.

## ACCEPTANCE (proof, not vibes)
1. New tests: reply-mentions-timeout/503 cases classify by the real error; structured-signal tests and
   existing classification tests unchanged and green (`tests/test_claude_driver.py`,
   `tests/test_retry_transient.py`, `tests/test_case_quota_resume.py`, `tests/test_output_truncation.py`;
   targeted only).
2. One shared marker set for the failure label; the `result_text.py` drift is removed.
3. Finding on item 4 recorded in Closure (fixed, or a follow-up packet).

## RESERVED DECISIONS (surface, do not guess)
- None expected. If scoping changes a class for a historically common case, list it in Closure.

## SCOPE OUT
- Any model-based classification.
- Changing retry counts or pause policy.
- Codex/OpenCode backend error mapping beyond what item 4 reveals.

## TRAIL / EVIDENCE (fill at close)
- `evidence:` → test file(s), PR.

---
## Milestone (burndown)
- [x] Failing test reproduces misclassification
- [x] Scoped failure text + tightened status patterns
- [x] Shared marker tuple; drift removed
- [x] `_run_backend_local` error_class finding recorded
- [x] Targeted tests green; PR opened (#18 — merge is the operator's/governor's during the burn-down)

## Closure (fill on completion)
**2026-10-06 — done on `feat/failure-text-scope`, PR Yuird/AI-team#18 (issue #4; not self-merged per
`.claude/rules/cloud-burn.md`).**
- **TASK 1/2:** `_failure_text` (and `result_text.failure_text`) read only error-bearing fields —
  `errors`, `raw_stderr`, error events in `raw_stdout` (is_error result, REJECTED rate_limit_event,
  `error` events / CLI-synthesised API-error messages), a terminal-error `parsed_output` (never its
  `assistant_text`). The reply is read only when nothing error-bearing exists (fallback = today's
  reading). Bare `503`/`504` → standalone-token regex `UPSTREAM_STATUS_RE`.
- **TASK 3:** one marker vocabulary, `src/core/failure_markers.py`, imported by the driver,
  `_classify_error`, `_short_failure_reason`, `result_text.short_failure_reason`; drifted copies gone.
- **TASK 4 finding:** confirmed — BOTH `TaskResult` builders (`_run_backend_local` and the main
  in-process path) dropped `raw.error_class`, and `_classify_error` overwrote it anyway. Fixed: the
  field is copied, and `_classify_error` prefers backend classes in `PREFERRED_BACKEND_ERROR_CLASSES`
  (`max_turns`, `upstream_error`, `sdk_stream_closed`, `context_overflow`, `rate_limit`; `usage_limit`
  via `_usage_limit_class`). NOT preferred (still re-derived from text): `session_lost`,
  `cache_unhealthy`, `permission_block`, `transient`, `managed_conflict`, `recovery_required` and the
  OpenCode classes — they have no retry-policy entry, so honouring them would route them to the
  default (2 retries): a retry-policy decision, out of scope. Follow-up only if the owner wants it.
- **Class changes for common cases** (RESERVED DECISIONS): listed in full in PR #18 §3 — chiefly
  reply prose no longer yields usage_limit/rate_limit/timeout/network; allowed_warning
  rate_limit_events no longer usage_limit; remote 5xx now `upstream_error` via the worker's class;
  `sdk_stream_closed` now gets its own 0-retry policy.
- **Review round 1 (CHANGES REQUESTED on `570b4dc`) — both findings fixed:**
  (1) where `raw_stdout` only mirrors the reply (legacy worker, managed-turn, reattach builders),
  no reader takes structure from it (`stdout_is_reply_mirror`, applied to the text pass AND the
  `rate_limit_event` / terminal-result parsers), and only RECOGNISED event types count;
  (2) the mesh session path (`_dispatch_to_node` failed + completed builders, and
  `_reattach_remote_task`) now keeps the worker's `error_class`, so remote `sdk_stream_closed` /
  `upstream_error` are honoured.
- **Evidence:** `tests/test_failure_text_scope.py` (32 tests; the original 24 — 17 fail on `main` —
  plus 8 for round 1, 7 of which fail on `570b4dc`; all pass on the branch). Targeted `pytest` of `test_claude_driver`, `test_retry_transient`,
  `test_case_quota_resume`, `test_output_truncation`, `test_quota_window_coordinator` green (2
  `test_case_quota_resume` cases fail identically on `main`: container tzdata lacks `Europe/Kiev`).
