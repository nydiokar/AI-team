```yaml
job_id: AGENT_87_SESSION_RUNTIME_COORDINATOR
created_at: "2026-09-24T18:00:20+00:00"
status: ready
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A87
evidence: []
updated_at: "2026-09-24T18:00:20+00:00"
```

# DISPATCH — A87 · Manager review and integration control for session reliability and runtime delivery

**Level:** 3 (cross-cutting behavior/architecture review and release gating) · **Type:** manager / audit
**Authored:** 2026-09-24 · **Status:** ready. **Manages:** A82, A83, A84, A85, and A86.
**Branch:** `main` for packet/review-only changes; feature branch + PR only if a corrective code change is required. No merge/deploy/restart authority beyond existing branch policy and explicit operator approval.

> These jobs change adjacent truths: turn ordering (A82), what an operator is told about waiting (A83), how a Manager is notified of worker completion (A84), and when a worker image may disrupt a node (A85/A86). A document-compliant patch can still make these truths contradict each other. This manager owns the behavioral contract and reviews workers against the repository and operational evidence, not against design prose alone.

## TASK

1. Establish and maintain a one-page compatibility ledger in this packet: actual source of truth, producer, consumer, failure behavior, and test evidence for turn ownership, completion delivery, session reason, node maintenance, and runtime identity. Read the current tree and worker outputs; do not repeat design claims as facts.
2. Before each worker starts a behavior-changing stage, review its proposed tests and current seams. Require the worker to revise its packet/plan when it duplicates authority, widens a state machine, creates an unbounded scan, violates queue ownership, or lacks a recovery/rollback proof. Record the decision and the evidence in this packet.
3. Gate A84 on A82's reviewed ownership/admission contract. Reconcile A83's `waiting_workers` derivation with A84's legacy/new Case cutover: it must report the correct durable source for each Case without using the secondary UI label as control authority.
4. Gate A86 on A85's executable container acceptance, not a written declaration. Verify that all deployment actions require a human node+digest approval and that image merge, candidate availability, node maintenance, and worker restart remain separate facts.
5. Perform an independent behavior review at each worker handoff. Re-derive central claims from code and tests: atomic terminal/outbox linkage, exactly-once/coalesced continuation delivery, queue fencing, read-path bounds, drain claim/routing fences, volume persistence, and rollback. Ask for targeted fixes; do not waive a failed invariant because a design says it is intended.
6. Maintain dependency/status recommendations in `DISPATCH_LOG.md`. A worker cannot be marked reviewed/merged based only on self-report; A87 records the independent verdict and unresolved risk. Escalate only genuine product/operational decisions, with concrete options and evidence.

## ACCEPTANCE

1. Compatibility ledger covers A82–A86 and cites current source/tests rather than only documents.
2. Every worker handoff has a review entry with claimed behavior, reproduced evidence, findings, disposition, and remaining operational gate.
3. Cross-job tests or a documented test matrix prove: a managed completion wakes the correct late-bound Manager exactly once; A83 presents it accurately during legacy/new cutover; and maintenance cannot disrupt or reroute an owned managed turn.
4. A86 cannot progress from blocked without A85 evidence and A87's explicit review; no job recommends a live rollout without the operator gate.

## OPERATING RULES

- Judge outcomes by the current repository, tested failure modes, production constraints in `.ai/CONTEXT.md`, and least-disruptive architecture—not automatic obedience to any design document.
- Prefer a narrow worker correction over a manager-owned rewrite. Do not implement unrelated features merely to make the plan look complete.
- Treat flags as rollout controls, not correctness proof. Default OFF never closes a missing recovery, migration, or rollback guarantee.
- Keep status legibility read-only. It may inform review, never make control decisions in this program.

## SCOPE OUT

No replacement of the assigned worker jobs, no autonomous production deployment, no worker restart, no flag activation, and no broad harness rewrite. This packet creates review/gating evidence, not a second implementation path.

## TRAIL / EVIDENCE

- Compatibility ledger, per-worker review records, dependency decisions, reproduced tests, and explicit operator-gated rollout notes.

---
## Milestone (burndown)

- [ ] Current compatibility ledger created from repository evidence
- [ ] A82 ownership/queue review completed before A84 schema/cutover work
- [ ] A83 legacy/new wait-source compatibility reviewed with A84
- [ ] A84 atomicity, recovery, barrier, and liveness evidence independently reviewed
- [ ] A85 image/volume/runtime acceptance independently reviewed
- [ ] A86 approval/drain/rollback and managed-turn continuity independently reviewed
- [ ] Final cross-job verdicts and operator gates recorded in dispatch index

## Closure (fill on completion)

State reviewed job verdicts, unresolved evidence, and why any release/deploy action remains operator-gated.
