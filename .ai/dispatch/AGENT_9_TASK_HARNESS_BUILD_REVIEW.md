# Adversarial Review — AGENT 9 Task Harness Kernel, as built

**Reviews:** the shipped T1/T2 code on `feat/task-harness` (commits `8914ca0`,
`8bc292a`) + the uncommitted T3 working tree (`docs/harness/dispatch_pipeline.md`,
`tests/test_harness_level3_guard.py`, `src/orchestrator.py` guard).
**Date:** 2026-07-03
**Verdict:** What shipped is coherent and **harmless** (the Level-3 guard is
convention-first and flag-gated OFF by default — byte-identical legacy behavior).
But there is one **scope/aim** finding the operator raised directly that must be
decided before build-review sign-off and merge. Not a correctness bug.

---

## Findings

### B1 (SCOPE — operator-raised, decide before merge) — the pipeline is wired onto the *secondary* ingestion lane

`dispatch_pipeline.md` (DISPATCH step, lines ~53–63) and the Level-3 guard
(`orchestrator.py::_harness_level3_allows_autopickup`, called from
`_handle_new_task_file`) attach the harness's automated handoff + safety boundary to
the **`.task.md` / `file_watcher` auto-pickup path**.

**Evidence that this is the minor lane, not the main one:**
- `file_watcher` is live-wired (`orchestrator.py:1176` starts it on `config.system.tasks_dir`),
  so it is a real running path — not dead code.
- BUT the only `.task.md` files that have ever existed are **June-7 e2e smoke
  fixtures** (`tasks/processed/e2e_smoke_*.task.md`) + one test file. No human/agent
  drops `.task.md` files to do real work.
- **Real work enters via Telegram / Web UI → `orchestrator.submit_instruction`**
  (`_make_task → _enqueue_task`). That path — where #31/#32 already wired
  `load_compact_context` — has **zero harness awareness**.

**Consequence:** the dispatch pipeline's automation and the Level-3 approval guard
protect a door almost nobody walks through, while the main door (`submit_instruction`)
is unguarded and un-harnessed.

**Why it is not a bug:** the guard is opt-in (`HARNESS_LEVEL3_GUARD` OFF by default)
and convention-first, so nothing regresses. The build correctly implements the scope
the dispatch/spec framed — the spec (§14) reached for the one *auto-pickup* primitive
that existed and treated "auto-pickup lives here" as "work enters here." That framing
is the actual defect, inherited by the build.

**Decision required (operator) — three coherent options, none urgent:**
1. **Accept `.task.md` as the deliberate batch/queued-dispatch lane** (separate from
   live chat). What shipped is internally consistent for that reading → build-review
   the diff, then merge. The harness becomes a batch discipline; live chat untouched.
2. **Re-aim T3 at `submit_instruction`** before merge — attach the pipeline + Level-3
   guard to the real ingestion path. More work; puts the loop where work enters.
3. **Merge as-is (harmless, off by default) + file a follow-up dispatch** to extend
   the harness onto `submit_instruction` once the discipline proves itself (matches
   the spec's own "build Phase 2 only if needed" stance). ← reviewer's default pick.

**Status:** UNRESOLVED — awaiting operator. Working tree left as the builder made it;
nothing merged; T3 not committed.

### B2 (STALE PROMPT — note only) — dispatch told the builder to build an already-built guard

`_harness_level3_allows_autopickup` logic already existed in the tree before T3; the
A9H packet's T3 reads as if it must be built from scratch. Harmless (the builder
reconciled it), but the packet should be updated so future readers know the guard
predates this dispatch. Fold into the packet's implementation log.

---

## Kept as-is (genuinely good)

- T1 templates + `level_rubric.md` (Level-3 triggers lead, "when in doubt escalate").
- T2 DRAFT/REVIEW/CLOSE generators as prompt artifacts, not services.
- ZERO new gateway state honored (no migration, no `flow_runs`, no stage machine).
- Guard is convention-first + flag-gated + byte-identical-when-absent — the right
  shape for a safety backstop.

## Next step

Operator picks B1 option. Until then: **do not merge `feat/task-harness`.** DISPATCH_LOG
A9H row stays `built`; do not advance to `reviewed`/`merged` while B1 is open.
