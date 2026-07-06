# A22 — Authoritative stage transitions (shadow, flag-guarded) (M1)

**Milestone:** v0.6 M1. **Level:** 3. **Branch:** code → `feat/m1-stage-transitions` → PR.
**Depends on:** A21 (schema + stage vocabulary). **Parallelism:** after A21 lands, runs **in
parallel with A23** — disjoint files (A22 = orchestrator/driver surface; A23 = task_server.py).

```xml
<task_packet>
  <meta><task_name>A22-flow-stage-transitions</task_name><harness_level>3</harness_level></meta>
  <objective_lock>
    <real_objective>current_stage becomes a real, written reflection of where a flow is in the
      §1 loop — while NOTHING in execution depends on it — so the operator can later see true
      stage state without any risk of a stage-write bug stalling a real task.</real_objective>
    <literal_request>"make the stages real"</literal_request>
    <interpreted_task>Write current_stage (+ updated_at, + the matching §11 field when produced,
      e.g. plan_review at the review step) at each harness transition, driven from the loop
      surface (orchestrator hook / driver path). Guard behind HARNESS_FLOW_DRIVE (default OFF ⇒
      A19 best-effort record behavior, byte-identical). SHADOW ONLY: no code path reads
      current_stage to decide what runs.</interpreted_task>
    <constraints>Flag OFF ⇒ byte-identical to A19. Stage writes are best-effort/wrapped so a
      write failure never raises into task execution. Execution MUST NOT branch on current_stage.
      No paid CLI; plain pytest.</constraints>
    <non_goals>No execution reading current_stage (explicitly deferred — separate later gate).
      No lineage (M2). No read API (A23). No new stage semantics beyond A21's vocabulary.</non_goals>
    <assumptions>A21's stage vocabulary constant + extended update methods exist — VERIFY.</assumptions>
    <drift_risks>Execution starting to depend on stage; a stage-write exception propagating into
      task run; flag defaulting ON.</drift_risks>
  </objective_lock>
  <approved_plan>
    <steps>1. Identify the transition points on the loop/orchestrator surface. 2. Behind
      HARNESS_FLOW_DRIVE, call update at each transition with the vocabulary stage + updated_at.
      3. Wrap every write so failure logs and returns (never raises into execution). 4. Tests:
      flag OFF ⇒ no stage writes / byte-identical; flag ON ⇒ stages advance in order; a forced
      write exception does NOT break task execution.</steps>
    <validation>pytest green incl. the OFF-parity test + the write-failure-isolation test;
      grep confirms no `if ... current_stage` execution branch exists.</validation>
    <definition_of_done>Stages are written in order when the flag is ON, invisibly best-effort,
      with zero execution dependency and byte-identical OFF behavior.</definition_of_done>
    <risks>Transition points scattered — keep the write in one helper called at each point.</risks>
  </approved_plan>
  <execution_rules>
    <do>Update milestone Live Log per step; commit at checkpoints; open PR at close. Do NOT edit
      DISPATCH_LOG (A20 owns the batch rows).</do>
    <do_not>No execution read of current_stage; no unguarded/ON-by-default flag; no paid CLI.</do_not>
    <report_format>closure_summary.md + /code-review on committed diff; relay to operator.</report_format>
  </execution_rules>
</task_packet>
```

## Milestone: A22 stage transitions
**Current Status:** dispatched
**Burndown:** [ ] find transition points · [ ] flag-guarded write helper · [ ] best-effort isolation · [ ] tests (OFF-parity, ordered, failure-isolation) · [ ] PR
**Live Log:** — dispatched 2026-07-06
**Next Action:** worker maps transition points on the orchestrator/driver surface.
