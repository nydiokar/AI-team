# Adversarial Review — AGENT 9 Compact-Context dispatch

**Reviews:** `AGENT_9_COMPACT_CONTEXT.md`
**Date:** 2026-07-03
**Verdict:** Sound and well-grounded — the loader really is dead-but-tested code
and `process_task` really is the universal funnel (verified: `submit_instruction`
and file-tasks both reach it via `_enqueue_task → _task_worker → process_task`).
Ship it after folding the three findings below. F1–F6 in the dispatch already
cover the build-blocking traps; these are the gaps the dispatch missed.

---

## Findings

### R1 (MAJOR) — the re-injection guard leaks into persisted metadata and the remote payload

The dispatch's **[F5]** guard uses `task.metadata["__compact_injected"] = True`.
But `task.metadata` is (a) serialized into the **remote mesh payload**
(`_process_task_remote` sets `payload["metadata"] = task.metadata or {}`, line
~3822) and (b) persisted with the task. A private control flag then travels to the
worker and into stored artifacts — harmless functionally, but it's leakage of an
internal concern into a wire contract and the audit ledger.

**Required correction:** prefer a **transient instance-local guard** that does not
ride in `task.metadata` — e.g. a `set()` of task ids on the orchestrator
(`self._compact_injected_ids`) checked/added around the injection, or a plain
local variable given the mutation happens once before the retry loop anyway (the
retry loop does not re-enter `process_task`, so a local `injected = False` may
suffice — confirm the retry `while` stays inside one `process_task` call; it does).
If a metadata flag is unavoidable, name it and strip it from the remote payload.
State which you did.

### R2 (MODERATE) — "inject once before the retry loop" needs the retry structure confirmed, not assumed

**[F5]** says mutate "before the `while` retry loop." Verify the claim it rests on:
the retry loop at ~2050 is *inside a single `process_task` invocation* (it does not
re-call `process_task`), so a one-shot mutation before the loop is genuinely
once-per-turn. That is true in the current code — but the dispatch should tell the
executor to **confirm it** rather than assume, because if any future path re-enters
`process_task` for the same `Task` object, a local flag won't protect it (R1's
instance-set would). Cheap to check; make it explicit.

### R3 (MINOR) — `submit_instruction` can also carry `continues:`, and that's fine — say so

The dispatch frames `continues:` as a `.task.md` field. But `process_task` reads it
from `task.metadata`, and `submit_instruction(..., extra_metadata=...)` also lands
in `task.metadata`. So a Telegram/CLI/Web caller passing
`extra_metadata={"continues": "<id>"}` gets the same opt-in injection for free —
no extra work. This is a *feature*, not a risk (still opt-in, still bounded), but
the dispatch should name it so the executor doesn't add a `.task.md`-only special
case that breaks the other callers. One line in T1.

---

## Cross-cutting checks (pass)

- **Dead-code claim verified:** `load_compact_context` is referenced only by its
  definition, `.ai/CONTEXT.md`, the workflow doc, and `tests/test_context_loader.py`
  — no production caller. The dispatch's premise holds.
- **Funnel claim verified:** both inbound entry points reach `process_task`;
  mutating `task.prompt` there covers Telegram, CLI, Web, and file tasks, local and
  remote.
- **Opt-in / byte-identical invariant:** correctly enforced by **[F1]** — no
  `continues:` ⇒ no loader call, no prompt change. This preserves the
  `MESH_ENABLED=false` guarantee and every existing task.
- **No new gateway state:** consistent with `docs/Task_harness_workflow.md` §11/§16
  (Phase-2 `flow_runs` explicitly deferred). Good.
- **Test-cost guard:** loader is mocked/spied in every test; no path spawns a paid
  CLI. Good.
- **Prompt-injection safety [F3]:** fencing prior context as reference-only and
  wrapping the live instruction verbatim is the right shape; keep the ≤ 4 KB cap.
- **Relationship to the harness memory:** `memory/task-harness-workflow.md` points
  at a broader `AGENT_9_TASK_HARNESS.md`. This dispatch is the **#31/#32 slice**
  that harness names as its open dependency, not the whole harness — correctly
  scoped down, not a duplicate.

## Required edits before implementation

Fold **R1** into **[F5]** (use an instance-local guard, keep the flag out of
`task.metadata`/the remote payload), add **R2**'s "confirm the retry loop is
single-invocation" note, and add **R3**'s one line to T1. With those, the handoff
is executable without further clarification.
