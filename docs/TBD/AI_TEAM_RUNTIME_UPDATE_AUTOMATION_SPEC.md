# AI-Team Runtime Update Automation — Implementation Specification

Status: DESIGN — implement after Docker worker acceptance is complete.

## Objective

Make Codex and Claude runtime updates low-friction and reproducible without allowing an upstream release or a Git merge to restart a live worker automatically.

The deployment unit is the worker Docker image.

The required separation is:

1. **Release discovered** — Renovate opens a PR.
2. **Runtime approved** — a human merges the PR after CI/review.
3. **Node disruption approved** — a human explicitly deploys that image to a selected node.
4. **Deployment executes** — node enters maintenance, drains, switches image, validates, and either returns online or rolls back.

A PR merge MUST NOT drain or restart any worker.

---

## Target Runtime Ownership

The worker image owns executable runtimes:

- AI-Team worker code
- exact pinned Codex CLI/runtime
- `git`, Python, Node/pnpm, and other declared agent tooling
- `claude-agent-sdk` and its bundled Claude Code runtime

Persistent data is external to the image:

- Codex authentication/state: persistent `CODEX_HOME` volume
- AI-Team durable state: existing application/database storage
- repositories/projects: read-write host bind mount(s)

The Codex provider keeps the existing direct `codex app-server --stdio` integration. Migrating to the Codex SDK is explicitly out of scope.

Do not self-update Codex or Claude inside a running container.

---

## Docker Version Pins

Pin Codex in the worker Dockerfile using a Renovate-readable version variable.

Example:

```dockerfile
# renovate: datasource=npm depName=@openai/codex
ARG CODEX_VERSION=0.156.1

RUN npm install --global "@openai/codex@${CODEX_VERSION}"
```

Pin `claude-agent-sdk` through the project's normal Python dependency mechanism.

Example:

```text
claude-agent-sdk==0.2.159
```

The image digest is the immutable runtime identity. Record at build time:

- AI-Team git SHA
- Codex version
- Claude Agent SDK version
- bundled Claude Code version if discoverable
- image digest
- build timestamp

Expose this inventory through the worker heartbeat/status API.

---

## Renovate

Use Renovate for deterministic release discovery.

Required behavior:

- Detect newer stable `@openai/codex` versions.
- Detect newer stable `claude-agent-sdk` versions.
- Open separate runtime-update PRs unless both upgrades are intentionally grouped.
- Never automerge these PRs.
- Label them `runtime-update`.
- Include old/new versions in the PR title/body.

For the Docker `ARG`, use Renovate's regex custom manager or the documented Dockerfile `_VERSION` custom-manager preset.

Example `renovate.json5` shape:

```json5
{
  "extends": ["config:recommended"],
  "packageRules": [
    {
      "matchPackageNames": ["@openai/codex", "claude-agent-sdk"],
      "automerge": false,
      "labels": ["runtime-update"]
    }
  ],
  "customManagers": [
    {
      "customType": "regex",
      "managerFilePatterns": ["/(^|/)Dockerfile(\\.worker)?$/"],
      "matchStrings": [
        "# renovate: datasource=(?<datasource>\\S+) depName=(?<depName>\\S+)\\s+ARG CODEX_VERSION=(?<currentValue>\\S+)"
      ]
    }
  ]
}
```

Adjust file names to the repository's actual layout.

---

## Runtime-Update PR CI

Every `runtime-update` PR MUST build the real worker image and validate the candidate runtime inside that image.

### Deterministic checks

Codex:

```text
codex --version
spawn codex app-server --stdio
initialize
model/list
adapter protocol tests
```

Claude:

```text
import claude_agent_sdk
report SDK version
start SDK transport
adapter lifecycle tests
```

Worker/container:

```text
worker boots non-root
repository/project-root mount is writable
create/modify/delete file
git status
git diff
git add
git reset
representative project command/test
```

The CI must fail if the actual runtime version differs from the requested version.

### Real-runtime smoke

Run one bounded authenticated smoke in the deployment environment/canary when credentials are available:

```text
start turn 1
receive expected sentinel
persist session/thread identity
destroy/recreate worker process or container
resume same session
run turn 2
receive expected sentinel
```

This test specifically protects restart/resume continuity.

Do not require a model ID to be coupled to a particular Codex version. Model availability is server/account-side state and must be observed separately via `model/list`.

---

## Compatibility Review

A runtime-update PR should have an automated compatibility report generated after deterministic CI.

The report is advisory, not an authorization mechanism.

For Codex review:

- upstream release notes/changelog
- app-server protocol changes
- sandbox/container changes
- authentication changes
- model-catalog behavior changes
- removed/deprecated flags
- issues touching Linux/Docker/app-server

For Claude review:

- Agent SDK release notes
- bundled CLI/runtime changes
- transport/session/resume changes
- permission/sandbox changes

The report must distinguish:

```text
NO RELEVANT CHANGE
RELEVANT BUT COVERED BY TESTS
MANUAL REVIEW REQUIRED
BLOCKED
```

A language model must never be the sole release gate.

---

## Merge Semantics

Merging a runtime PR means only:

> This runtime version is approved for AI-Team and its immutable worker image may be published.

After merge:

1. Build the production worker image.
2. Tag it with immutable identifiers, e.g. git SHA and runtime versions.
3. Push it to the configured container registry.
4. Record the image as `available`.
5. Do not alter any running worker.

Never deploy `latest` as the rollback identity. Retain the previous known-good image digest.

---

## Per-Node Human Deployment Gate

Each node exposes:

```text
running image
available image
Codex running → candidate
Claude SDK running → candidate
active tasks
active turns
open/persisted sessions if known
Manager/Case continuation indicators if available
deployment state
```

Deployment requires explicit operator approval for a specific node and candidate image.

Example:

```text
Deploy worker sha256:ABC to Horse
```

Approval must be expiring and single-use.

Only after approval may the node enter maintenance/drain.

---

## Maintenance and Drain

State machine:

```text
online
  -> maintenance_requested
  -> draining
  -> drained
  -> switching
  -> verifying
  -> online

failure during switching/verifying
  -> rollback
  -> verifying_previous
  -> online | blocked
```

Once maintenance begins:

- gateway MUST stop routing new work to the node;
- claim admission MUST reject new claims transactionally;
- worker MUST stop local polling/claiming;
- existing work may finish;
- timeout MUST NOT kill work automatically.

`active_tasks == 0` is necessary but not sufficient evidence of semantic safety.

Until cross-restart continuity is proven, surface open Managers/Cases/sessions to the operator before approval rather than automatically deciding they are safe.

---

## Deployment

After drain:

1. Preserve all persistent volumes.
2. Stop old worker container.
3. Start candidate image using the same declared mounts/volumes/configuration.
4. Verify runtime inventory.
5. Verify worker registration/heartbeat.
6. Run backend health checks.
7. Run bounded continuation/resume smoke where applicable.
8. Release maintenance lock only after verification passes.

Do not mutate packages in place.

---

## Rollback

Rollback unit = previous immutable worker image digest.

If candidate verification fails:

1. keep node unavailable;
2. stop candidate;
3. restart previous image with unchanged persistent volumes;
4. validate worker registration and backend health;
5. restore routing only after validation passes;
6. mark candidate/node pair failed;
7. require a new explicit deployment attempt after remediation.

Never "npm downgrade" or modify the live image to roll back.

---

## Model Catalog Updates

Runtime release discovery and model discovery are separate concerns.

Worker periodically calls Codex `model/list`.

If catalog changes:

- update the AI-Team model inventory/UI;
- do not rebuild/redeploy solely because a model appeared;
- if Codex reports that a newer runtime is required, surface that fact and let the normal Renovate/runtime-release path handle it.

---

## Required Docker Acceptance Before Implementing This Automation

Do not implement CD until the containerized worker proves all of the following:

```text
Codex runtime executes inside the worker image
no production host-socket bridge
persistent dedicated CODEX_HOME survives container recreation
real host-owned project root is writable
new repositories can be created inside the mounted project root
git metadata can be modified
representative project commands run
Claude backend works
Codex initialize/model-list works
real Codex turn works
container recreation preserves auth
session/thread resume across recreation works or its limitation is explicitly documented
worker runs as intended UID/GID
```

---

## Explicit Non-Goals

This system does not:

- migrate the Codex adapter to the Codex SDK;
- auto-merge runtime PRs;
- auto-restart nodes after merge;
- infer that an idle node is semantically restart-safe;
- provide unrestricted host administration from the normal worker container;
- self-update runtimes inside live containers.

Host-level maintenance, if required, must remain an explicit separately governed execution capability.

---

## Acceptance Criteria

The feature is complete when:

1. a new Codex or Claude SDK release automatically creates a version-bump PR;
2. the PR builds and tests the exact candidate worker image;
3. merge publishes but does not deploy that image;
4. UI/API shows the candidate per node;
5. deployment requires explicit per-node approval;
6. maintenance blocks new work before drain;
7. successful deployment preserves auth/state and resumes normal work;
8. failed deployment automatically restores the previous immutable image;
9. model-list changes can appear independently of runtime releases;
10. no host-global Codex installation is part of the production worker path.
