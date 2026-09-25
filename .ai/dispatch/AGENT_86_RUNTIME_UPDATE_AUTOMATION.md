```yaml
job_id: AGENT_86_RUNTIME_UPDATE_AUTOMATION
created_at: "2026-09-24T18:00:20+00:00"
status: dead
owner: ""
depends_on: [AGENT_85_WORKER_CONTAINER_ACCEPTANCE]
results_ref: DISPATCH_LOG.md#A86
evidence: []
updated_at: "2026-09-25T10:40:36.283600+00:00"
```

# DISPATCH — A86 · Approved, reversible worker runtime update automation

**Level:** 3 (CI/release automation, deployment control boundary, worker routing) · **Type:** code
**Authored:** 2026-09-24 · **Status:** blocked on A85 acceptance evidence. **Reviewed by:** A87 before merge and before any activation proposal.
**Branch:** `feat/runtime-update-automation` + PR; never deploy, restart, drain, publish, or merge a live candidate as part of this job.

> A merged Codex or Claude SDK version must become an available immutable worker image—not an implicit instruction to disrupt a node. After this job, discovery, human approval, image publication, per-node approval, drain, verification, and rollback are distinct auditable states.

## TASK

1. After A85 is accepted, implement Renovate discovery for pinned Codex and Claude SDK versions: separate labeled `runtime-update` PRs, no automerge, and old/new version visibility. Use the repository's actual CI provider/configuration rather than assuming a GitHub-only shape.
2. Add runtime-update CI that builds the A85 image and verifies requested versus actual versions plus deterministic Codex app-server/Claude lifecycle and worker-container acceptance checks. Generate an advisory compatibility report with the four states in the spec; it is never the sole release gate.
3. Implement merge-to-available-image behavior with immutable digest identity, provenance/inventory, retained known-good digest, and no routing or process change. Do not use `latest` as deployment or rollback identity.
4. Add the smallest authenticated control/read model necessary to show a node's running/candidate runtime, active work, maintenance state, and explicit expiring single-use operator approval. Follow the service-boundary checklist: bounded input, authorization, concurrency, timeout, malformed input, unavailable registry/DB, and 100-concurrent-request memory behavior.
5. Implement the maintenance state machine: transactionally stop new routing/claims, let existing work finish without automatic killing, switch only after a drain plus operator-visible semantic context, verify candidate, and rollback to the prior immutable digest on failure. Preserve volumes and require a fresh approval after failed candidate verification.
6. Test every state transition, duplicate/racing approval, expiry, gateway/worker restart during drain, failed verification/rollback, late task result, and merge-without-deploy invariant. Integrate safely with A82/A84 session ownership: maintenance cannot duplicate, relocate, or strand managed turns/Cases.

## ACCEPTANCE

1. A runtime bump PR is discovered and CI proves the exact candidate image/runtime; merge produces only an available immutable image record.
2. No test or code path allows a merge to drain/restart a worker. Only an authorized, unexpired, single-use node+digest approval can begin maintenance.
3. State-machine integration tests prove routing/claim admission fencing, drain behavior, success verification, failure rollback, retained volumes, and honest blocked state when rollback verification fails.
4. Node status exposes runtime inventory and operational context without secrets. Model-list/catalog changes remain independent from runtime release/deployment.
5. A87's final review confirms implementation behavior—not conformance-by-wording—to the source spec and rejects any unsafe expansion.

## RESERVED DECISIONS

- **R1 — Registry/publisher capability.** Use only an already configured registry/CI credential path. If absent, build/test the abstraction locally and stop before external publication.
- **R2 — Semantic restart safety.** Do not automate a decision from `active_tasks == 0`; surface open sessions/Managers/Cases to the approver until cross-restart continuity has executable proof.

## SCOPE OUT

No host-global runtime installation, in-place package update/downgrade, auto-merge, automatic deployment after merge, auto-kill on drain timeout, Codex SDK migration, or unrestricted host administration.

## TRAIL / EVIDENCE

- Renovate/CI configuration, state-transition tests, approval/authorization tests, image provenance evidence, rollback tests, and A87 review verdict.

---
## Milestone (burndown)

- [ ] A85 acceptance reviewed; A86 explicitly unblocked by A87
- [ ] Renovate discovery and in-image candidate CI prove actual pinned runtimes
- [ ] Merge records an immutable available image and does not deploy
- [ ] Read/control surfaces enforce bounded single-use per-node approval
- [ ] Drain/switch/verify/rollback state machine proven under races and failures
- [ ] A82/A84 managed-turn continuity and full service-boundary review pass
- [ ] A87 accepts behavioral evidence; activation remains operator-gated

## Closure — DROPPED / DESCOPED 2026-09-25 (operator decision, Case 58c2f812)

**Status: dead (descoped, not built).** The operator decided workers will be taken **OUT of the
container**. This job's entire design — Renovate discovery of pinned Codex/Claude-SDK versions →
labeled `runtime-update` PRs → in-image CI → **merge produces an immutable available worker IMAGE**
→ per-node approval → maintenance/drain/verify/**rollback to the prior immutable digest** — is built
end-to-end on the containerized-worker deployment unit (the "immutable image" is the release/rollback
identity). With no container, the plan is no longer legible: there is no image to build, publish,
approve, or roll back to. Nothing was implemented (job was `blocked`, zero code, only A85 upstream).

**Reversibility:** if a future direction re-containerizes workers, re-open from this packet — the
design is intact and still valid for a container target; it simply has no substrate today.

**Successor need (surfaced, not built):** a non-container worker runtime STILL needs *some* approved,
reversible way to update Codex/Claude-SDK versions on a host-process worker (pin discovery + approval +
rollback), but that is a DIFFERENT mechanism (host package/venv management, not image digests) and a
DIFFERENT job to be authored once the operator's non-container worker plan exists. Do not silently
morph A86 into it.
