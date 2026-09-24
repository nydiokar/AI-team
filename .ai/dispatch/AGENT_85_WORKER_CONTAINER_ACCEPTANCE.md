```yaml
job_id: AGENT_85_WORKER_CONTAINER_ACCEPTANCE
created_at: "2026-09-24T18:00:20+00:00"
status: ready
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A85
evidence: []
updated_at: "2026-09-24T18:00:20+00:00"
```

# DISPATCH — A85 · Containerized worker acceptance baseline

**Level:** 3 (new deployment boundary, runtime/tooling compatibility) · **Type:** code + test
**Authored:** 2026-09-24 · **Status:** ready. **Reviewed by:** A87 before A86 can start.
**Branch:** `feat/worker-container-acceptance` + PR; no production container switch or worker restart.

> Runtime-update automation is unsafe to build against an unproven deployment unit. Establish a reproducible, non-root worker image and executable acceptance evidence first, so an update later has a real immutable target rather than a host-process assumption.

## TASK

1. Map current worker launch/deploy, runtime installation, volume/state paths, project mounts, identity/permissions, and gateway registration before choosing Docker files or compose tooling. Record current behavior and the smallest compatible container boundary.
2. Build the worker image definition using repo-native configuration. Pin Codex with a Renovate-readable Docker build argument and preserve Python dependencies through `pyproject.toml`/constraints; do not invent a second dependency authority or install runtimes in a running container.
3. Add a reproducible local/CI acceptance harness that proves the image runs non-root, has writable declared project mounts, preserves a dedicated `CODEX_HOME` volume, and reports a runtime inventory (git SHA, requested/actual Codex, Claude SDK, bundled Claude version if discoverable, image identity where available).
4. Exercise the existing Codex app-server and Claude adapters inside the image with non-paid deterministic checks. Add authenticated real-runtime smoke only when credentials/canary capability already exist; otherwise leave it explicitly operator-gated and do not fabricate success.
5. Prove container recreation preserves authentication/state and attempt session/thread resume. If provider behavior cannot support resume, document the exact observed limit and ensure the image does not claim it can.

## ACCEPTANCE

1. Image build is reproducible from a clean checkout and requested versus actual runtime versions are asserted.
2. Tests prove non-root execution; declared project mount read/write plus git operations; Codex initialize/model-list/app-server protocol; Claude SDK import/transport lifecycle; and worker registration/heartbeat against a fake/local gateway.
3. Container recreation preserves the dedicated state volume and produces an honest resume verdict. No host socket bridge or host-global Codex is needed for the production path.
4. A87 reviews the image boundary, credentials/volume handling, and evidence before A86 status changes from blocked.

## RESERVED DECISIONS

- **R1 — Container orchestration file.** Reuse an existing deployment convention if present; otherwise add the smallest documented local/CI invocation, not a production orchestrator.
- **R2 — Real credentials.** Missing credentials are an operator gate for the authenticated smoke, never a reason to skip deterministic adapter tests or assert production readiness.

## SCOPE OUT

No Renovate rollout automation, registry publication, node maintenance API, deployment approval endpoint, production switch, or worker restart. Those belong to A86 after this acceptance gate.

## TRAIL / EVIDENCE

- Docker/config paths, image build command, deterministic test output, runtime inventory sample, recreation/resume verdict, and A87 review.

---
## Milestone (burndown)

- [ ] Current worker/deploy/runtime boundary mapped
- [ ] Pinned non-root worker image builds reproducibly
- [ ] Deterministic in-image adapter, mount, git, registration, and inventory tests pass
- [ ] Recreation/auth/session-resume evidence recorded honestly
- [ ] A87 accepts the Docker acceptance gate or records a concrete block

## Closure (fill on completion)

State whether A86 is unblocked, what credentials/canary evidence remains operator-gated, and confirm no live worker was changed.
