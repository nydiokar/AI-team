# Adversarial Review — AGENT 9 Compact-Context, as built

**Reviews:** the shipped T1/T2 code on `feat/compact-context` (not the dispatch).
**Date:** 2026-07-03
**Verdict:** One real security-shaped bug (fence escaping via prior-task output),
fixed inline; two lower findings noted. Re-verified — 13 targeted tests green,
loader tests unchanged, orchestrator imports/constructs.

---

## Findings

### B1 (BUG — prompt-injection / fence escape) — prior content could break the reference fence

`_build_compact_prefix` interpolated the prior task's `summary`, `files_modified`,
and `errors` **verbatim** into the `<prior_context>…</prior_context>` block. Those
fields are a *prior task's stored output* — untrusted structure. If a prior summary
contained the literal string `</prior_context>` followed by
`<current_instruction>…`, the reference fence would close early and the smuggled
text would land in the live-instruction region — a task continuing a prior turn
could be steered by whatever the prior turn happened to emit. Same "content is not
structure" class as the transcript-overlay fence bug.

**Action:** added `_defuse_fence()` — replaces the angle brackets of any
`<prior_context`, `</prior_context>`, `<current_instruction>`,
`</current_instruction>` token found inside interpolated content (`<`→`(`, `>`→`)`),
applied to summary, each file name, the parent id, and the error line. The block
now always has exactly one real opening/closing fence. **Fixed.** Regression test:
`test_fence_escape_in_prior_content_is_defused` (asserts exactly one of each fence
token and that the live instruction stays verbatim).

### B2 (NOTE — verified good) — hard cap slice is fence-safe

The `_COMPACT_PREFIX_MAX_CHARS` truncation reserves room for the
`…(truncated)\n</prior_context>` tail and re-appends it, so a truncated block still
ends with a valid closing fence and the separate `<current_instruction>` wrapper is
appended outside the capped block. `test_oversized_prefix_respects_hard_cap`
confirms `len(block) <= cap` and the live instruction survives. No change needed.

### B3 (NOTE — accepted) — `if not prefix:` guard is effectively dead

`_build_compact_prefix` always returns at least the fence + reference line, so the
`if not prefix: return` in `_maybe_inject_compact_context` never fires. It's a cheap
defensive guard against a future refactor that could make the builder return empty;
kept intentionally. Accepted.

## Cross-cutting checks (pass)

- **Opt-in invariant [F1]:** no `continues:` ⇒ loader never called, prompt
  unchanged — asserted directly (`loader.assert_not_called()`). Non-continuation
  tasks and `MESH_ENABLED=false` are byte-identical.
- **Event loop [F2]:** loader runs via `asyncio.to_thread` — verified in source.
- **Inject-once [F5/R1]:** guarded by the instance-local `_compact_injected_ids`
  set, NOT `task.metadata` — the guard cannot leak into the remote payload or
  persisted artifacts. `test_injected_only_once_across_repeated_calls` confirms the
  second call short-circuits before the loader.
- **No-op degradation [F4]:** self-reference, `source:none`, empty summary+files,
  and a raising loader all leave the prompt intact — each has a test.
- **Malformed input [F6]:** list / whitespace `continues:` treated as absent, no
  loader call. Tested.
- **Metadata-field, not file-only [R3]:** read uniformly from `task.metadata`, so
  `submit_instruction(extra_metadata=...)` works too. Documented in
  `Task_harness_workflow.md` §14.
- **No new gateway state:** no migration, no `flow_runs`, no parser change —
  consistent with §11/§16 deferral.
- **Test-cost guard:** loader mocked in every test; no path spawns a paid CLI.

## Re-verification

- `pytest tests/test_compact_context_injection.py tests/test_context_loader.py` →
  13 passed (10 injection incl. fence-escape regression + 2 loader unchanged +
  1 new — count reflects the added B1 test). Loader tests unaffected.
- `python -c "from src.orchestrator import TaskOrchestrator; TaskOrchestrator()"`
  imports and constructs; `_compact_injected_ids` initialized empty.
