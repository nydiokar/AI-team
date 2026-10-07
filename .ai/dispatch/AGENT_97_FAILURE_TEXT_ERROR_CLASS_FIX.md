```yaml
job_id: AGENT_97_FAILURE_TEXT_ERROR_CLASS_FIX
created_at: "2026-10-05T08:46:36.619883+00:00"        # CANONICAL — set once at dispatch, never derive again
status: ready              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A97             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-10-05T08:46:50.314095+00:00"
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
- [ ] Failing test reproduces misclassification
- [ ] Scoped failure text + tightened status patterns
- [ ] Shared marker tuple; drift removed
- [ ] `_run_backend_local` error_class finding recorded
- [ ] Targeted tests green; PR merged

## Closure (fill on completion)
