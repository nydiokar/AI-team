# A20 — Reconcile the base + ready the spec (M0) before flow-machine build

**Milestone:** v0.6 M0 (`docs/Task_Harness_v0.6_AUTOMATION.md` §3). **Level:** 1 (docs) +
surfacing two operator forks. **Branch:** docs-only → `main` (per driver branch policy).
**Parallelism:** runs **in parallel with A21** — disjoint files (A20 = `*.md`/`.ai/`/`docs/`
only; A21 = `src/`+tests). No ordering dependency between them.

```xml
<task_packet>
  <meta><task_name>A20-reconcile-base</task_name><harness_level>1</harness_level></meta>
  <objective_lock>
    <real_objective>The base the flow-machine (M1) builds on is honestly labeled and free of
      silent/self-contradictory drift, the Manager driver no longer forbids the work v0.6
      authorizes, and v0.6 carries the two guardrails it currently lacks — so every downstream
      stage builds on a clean, consistent base.</real_objective>
    <literal_request>"reconcile the base and ready the spec before we build the state machine"</literal_request>
    <interpreted_task>Docs-only reconcile of the base. IMPORTANT — dispatch-prep (Manager) has
      ALREADY landed several of the originally-scoped edits in the working tree; your job is to
      VERIFY those and finish the genuinely-remaining ones. Do NOT re-add anything already present.

      ALREADY DONE (verify only, fix only if wrong — do not duplicate):
        • v0.6 §5 exists = "Build guardrails & success criteria" with §5.1 Cost Guard, §5.2
          Abort/reap (marked M3-time), §5.3 Success criteria. Confirm present + tight; do not
          re-add or restate v0.5.
        • DISPATCH_LOG index rows A20–A23 (`dispatched`) exist, and the A19 row already reads
          `merged (0b6b1ec)`. Confirm present.
        • v0.6 §0.1/M0/F1 already carry the CORRECTED quota-branch framing (additive, 9-ahead/
          2-behind, "~293" was wrong). Do not reintroduce the "293 files" scare anywhere.
        • Set A is already archived to .ai/dispatch/deferred/SUPERSEDED_AGENT_2{0,1,2}_*.

      REMAINING (do these):
        (1) Fix .ai/CONTEXT.md — it is SELF-CONTRADICTORY: line ~47 says "merged A18 + A19 to
            main" while line ~145-146 says A19 "awaiting op merge-to-main". Resolve BOTH to
            merged; cite `0b6b1ec`. (DISPATCH_LOG's A19 row is already correct — leave it.)
        (2) Surface the two open drift forks in CONTEXT.md as explicit operator decisions WITH a
            one-line recommendation each (do NOT resolve them): the A17 orphan code
            (AGENT_17_WIP_MERGE_RECONCILE.md → keep/test/revert per cluster) and the unmerged
            remote `phase1-quota-window-coordinator` branch. Use its VERIFIED state — 9 ahead /
            2 behind `main`, additive (+1773/−0 across 6 files vs merge-base) → recommend
            rebase-to-current, NOT drop; keep it a separate fork never entangled with M1. Verify
            the numbers yourself (`git rev-list --left-right --count` + diffstat vs merge-base);
            do NOT write "293 files deleted".
        (3) Reconcile docs/harness/manager_invocation.md with v0.6: the preamble (~L8-10, "a code
            driver … is Phase 2 — deferred … Phase 2 = NO") and standing rule 2 (~L49-50, "No
            speculative machinery … flow_runs … forbidden") DIRECTLY CONTRADICT v0.6's
            authorization. Add a carve-out: v0.6 (2026-07-06) supersedes this for the flow-machine
            build — flow_runs / stage-driver / read-API work is authorized under v0.6 §0.1-0.3.
            Keep the anti-sprawl spirit (no UNplanned machinery) intact; lift ONLY the flow_runs
            prohibition.
        (4) Add a one-line note to DISPATCH_LOG (near the rows or a footnote) recording that Set A
            (SUPERSEDED_AGENT_20_FLOW_STATE_SCHEMA / _21_STAGE_INSTRUMENTATION / _22_TRACE_SURFACE)
            was superseded by this batch and moved to deferred/ (number collision).
        (5) Point CONTEXT.md's forward pointer at docs/Task_Harness_v0.6_AUTOMATION.md.</interpreted_task>
    <constraints>Docs-only. No code, no migration, no branch. Do not merge/rebase/delete any git
      branch (that is the operator's fork). Do not touch src/, web/, migrations. No paid CLI; no
      `python main.py status`. Keep the two new v0.6 sections short (cross-ref, don't restate).</constraints>
    <non_goals>Not fixing the A17 orphan code. Not touching/merging the quota branch. Not building
      any flow-machine schema (A21). Not authoring M3 role-separation or abort/reap guardrails
      (deferred to M3 spec-time). Not writing an execution reader of current_stage.</non_goals>
    <assumptions>A19 is on main (`0b6b1ec`) — VERIFY with `git log main --oneline | grep -i A19`
      and `git merge-base --is-ancestor 0b6b1ec HEAD` before editing (do not trust this packet).</assumptions>
    <drift_risks>Editing code; silently merging/deleting a fork; overwriting real drift notes;
      bloating v0.6 by restating v0.5 instead of cross-referencing; lifting more of the driver's
      anti-sprawl guard than just the flow_runs prohibition.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>0. VERIFY the already-landed items (v0.6 §5; DISPATCH_LOG A20-A23 rows + A19=merged;
      v0.6 quota-branch correction; Set A archived) — if any is missing/wrong, note it, else move
      on. Do NOT re-add. 1. Verify A19 merge on main (`git log main --oneline | grep -i A19`;
      `git merge-base --is-ancestor 0b6b1ec HEAD`). 2. Fix the CONTEXT.md A19 self-contradiction
      (both mentions → merged, cite `0b6b1ec`). 3. Surface the two forks in CONTEXT.md with a
      one-line recommendation each — quota branch with VERIFIED numbers, no "293". 4. Add the
      v0.6-supersedes-rule-2 carve-out to manager_invocation.md (preamble + rule 2). 5. Add the
      Set-A supersession note line to DISPATCH_LOG. 6. Repoint CONTEXT forward pointer at v0.6.
      7. Verify only `.md`/`.ai/` changed (`git diff --name-only`).</steps>
    <validation>grep shows no remaining "awaiting op-merge"/contradiction for A19; both forks named
      with a recommendation and NO "293 files" text anywhere; manager_invocation.md no longer
      forbids flow_runs (has the v0.6 carve-out); DISPATCH_LOG notes the Set-A supersession;
      v0.6 §5 + the A20-A23 rows are present and un-duplicated; `git diff --name-only` shows only
      `.md`/`.ai/` paths.</validation>
    <definition_of_done>Base docs state the true, self-consistent merge status; both forks are
      surfaced as operator decisions; the driver authorizes the v0.6 build; v0.6 owns its
      acceptance test + cost guard; the batch is logged. Base is clean-labeled for M1.</definition_of_done>
    <risks>None beyond doc accuracy. All reversible via git.</risks>
  </approved_plan>
  <execution_rules>
    <do>Update the milestone Live Log; commit docs-only to main. This loop OWNS the DISPATCH_LOG
      rows for the whole batch — A21/A22/A23 do NOT edit the shared log (avoids merge contention);
      the Manager advances their status at close.</do>
    <do_not>No code, no branch, no merge, no delete, no paid CLI, no M3 guardrail authoring.</do_not>
    <report_format>closure_summary.md shape; relay to operator at stop.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone: A20 reconcile base + ready spec
**Current Status:** closed (docs-only, on `main`)
**Burndown:** [x] verify already-landed items (v0.6 §5, A20-A23 rows, quota correction, Set-A archive — all confirmed present) · [x] verify A19 on main (`git merge-base --is-ancestor 0b6b1ec HEAD` → MERGED; 3 A19 commits on main) · [x] fix CONTEXT self-contradiction (A19 ledger entry "awaiting op merge-to-main" → "MERGED `0b6b1ec`") · [x] surface 2 forks w/ recommendation (Fork 1 quota branch VERIFIED 9-ahead/2-behind additive, no "293"; Fork 2 A17 orphan-code, Level-3) · [x] driver rule-2 carve-out (manager_invocation.md preamble + rule 2) · [x] Set-A supersession note in DISPATCH_LOG · [x] v0.6 forward pointer in CONTEXT Current Focus
**Live Log:**
- dispatched 2026-07-06; reconciled 2026-07-07 (Manager pre-flight already landed v0.6 §5 + DISPATCH_LOG rows + quota correction + Set-A archive; scope trimmed to the remaining CONTEXT.md + driver edits; "293" defect removed).
- 2026-07-07 closed — A19 merge CONFIRMED in git; quota-branch state CONFIRMED in git (9 ahead / 2 behind main, additive). Six docs-only edits made: CONTEXT.md (A19 status, both forks w/ recommendation, v0.6 forward pointer), manager_invocation.md (rule-2 carve-out), v0.6 §5 (tightened, cross-ref v0.5 §3/§9, M3 abort/reap + role-sep marked deferred to M3 spec-time), DISPATCH_LOG (Set-A supersession note). No code/branch/merge/delete touched.
**Next Action:** none — closed. Manager advances A21/A22/A23 status at their close.
