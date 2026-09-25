```yaml
job_id: AGENT_87_SESSION_RUNTIME_COORDINATOR
created_at: "2026-09-24T18:00:20+00:00"
status: active
owner: mgr-a2a819ff
depends_on: []
results_ref: DISPATCH_LOG.md#A87
evidence: []
updated_at: "2026-09-25T08:26:14.805928+00:00"
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
## Compatibility Ledger (A87 — grounded from the tree 2026-09-25, Case 58c2f812)

Coordinator: Manager session `a2a819ff55c0`. Built from repository + running-env evidence, not design prose.

### Truth-by-truth (source of truth · producer · consumer · today's failure · evidence)
| Truth | Source of truth | Producer → Consumer | Failure today | Repo evidence |
|---|---|---|---|---|
| **Turn ordering (A82)** | `mesh_tasks` (managed turn intent; protocol 1) | orchestrator `submit_instruction`/`_enqueue_task` → control_api instruction route → worker carrier + new scheduler | API acceptance mutates session state before the durable row lands; worker can reschedule the same pending id; result goes terminal before native-id reconciled | `src/orchestrator.py:6241` `submit_instruction`; `docs/SESSION_TURN_QUEUE_DESIGN.md` (46 KB, present) |
| **Completion delivery (A84)** | *today:* mutable Manager-authored wait-groups reconstructed via orchestrator continuation + `_cache_heartbeat_owner_live` case_wait_group branch. *target:* durable Case-scoped outbox row, atomic with terminal task write | terminal task writer → outbox → coalesced continuation → late-bound Manager | silent completions AND indefinite waits | `src/orchestrator.py:1306` `_cache_heartbeat_owner_live`; `docs/TBD/WORKER_COMPLETION_NOTIFY_REDESIGN.md` (18 KB, present) |
| **Session reason (A83)** | derived, read-path only, **non-authoritative**; enum `SessionStatus` UNTOUCHED | new `SessionReason` derivation on `/api/sessions` read → web pillow | must NOT become an N+1/background scan (repeat of #145/#147 stall) | `src/control/db.py:4710` `list_jobs_for_sessions`; spec at `docs/TBD/SESSION_WAIT_STATE_GRANULARITY.md` (**packet path `docs/SESSION_WAIT_STATE_GRANULARITY.md` is STALE — it lives under `docs/TBD/`**) |
| **Node maintenance + runtime identity (A85/A86)** | immutable image digest + per-node expiring single-use approval | Renovate/CI merge → available image; approval → drain/verify/rollback state machine | disruptive host-process assumption; no separation of merge/availability/maintenance/restart | `Dockerfile`, `compose.yaml`, `deploy/compose.worker.yaml` present; **docker CLI ABSENT in exec env** |

### Cross-job reconciliation gates (A87 owns these)
- **A83.`waiting_workers` ↔ A84 outbox cutover:** A83 derives `waiting_workers` from the legacy wait-group substrate. When A84 moves new Cases to the outbox, A83 must report the correct *durable source per Case* without the UI label ever becoming control authority. → reviewed jointly at A84 cutover.
- **A84 gated on A82:** do not start A84 until A82's queue ownership/admission contract is built AND independently reviewed (my gate). Correct sequencing, not idleness.
- **A86 gated on A85:** A86 stays `blocked` until A85 produces *executable* acceptance evidence and I review it.

### Execution environment facts (binding on every dispatched worker — packets predate the Docker migration)
- **Python:** `/opt/venv/bin/python` (NOT `.venv/bin/python`, which no longer has a working interpreter). Editable install ⇒ `import src` resolves to THIS checkout — verify `src.__file__` before trusting any test.
- **pytest:** absent from the read-only `/opt/venv`; run as `PYTHONPATH=/tmp/tvenv/lib/python3.11/site-packages /opt/venv/bin/python -m pytest <paths>`. Proven green: `tests/test_session_timeline.py` (5 passed).
- **web:** node v22 + pnpm 10.30 present, `web/node_modules` installed.
- **No live gateway** (`curl :9003/health` dead here) ⇒ acceptance = targeted tests + import smoke, NOT live-health probe.
- **No `gh`, no docker/podman**; git remote `origin` (SSH) exists. ⇒ PR-via-CLI impossible; deliverable = reviewed local commit on `feat/*`; push/PR/merge-to-remote is operator-gated.

### Coordination decision (this Case)
1. **Track-1 src editors run SEQUENTIALLY** (A83 → A82 → A84) — because the shared editable install would make parallel worktrees mistest each other's `src` (A82 §1.5 trap). A83 is small; the serialization cost is low and it eliminates the collision the operator warned about.
2. **A83 first** (fully executable): build → my verification (run its tests, trace cross-layer) → **fresh adversarial reviewer** → remediate findings → local merge.
3. **A82 next, stage-gated:** authorize Stage 0–1 (source/producer inventory + red acceptance tests — non-behavior-changing) as the first reviewed gate per A82 §4/§12; later stages are a multi-session continuation. I review before authorizing behavior-changing stages.
4. **A84 NOT started** — dependency-gated on A82's reviewed contract.
5. **A85/A86 BLOCKED by environment** — no docker/podman here, so executable container acceptance is impossible; dispatching a build would yield only the written declaration this charter rejects. → escalate to operator (needs a docker-capable env or the prod host).

---
## Milestone (burndown)

- [x] Current compatibility ledger created from repository evidence (2026-09-25)
- [ ] A82 ownership/queue review completed before A84 schema/cutover work
- [ ] A83 legacy/new wait-source compatibility reviewed with A84
- [ ] A84 atomicity, recovery, barrier, and liveness evidence independently reviewed
- [ ] A85 image/volume/runtime acceptance independently reviewed
- [ ] A86 approval/drain/rollback and managed-turn continuity independently reviewed
- [ ] Final cross-job verdicts and operator gates recorded in dispatch index

## Closure (fill on completion)

State reviewed job verdicts, unresolved evidence, and why any release/deploy action remains operator-gated.
