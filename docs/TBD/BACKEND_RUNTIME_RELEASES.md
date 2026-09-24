# Backend runtime releases

**Status:** operator-approved design; implementation is not approved or scheduled.

**Scope:** Codex and Claude Code runtimes on mesh workers. OpenCode is explicitly
out of scope for the first implementation.

## Decision

Keep the local Codex app-server and Claude Agent SDK/Claude Code runtime model.
They are the right execution boundary for workers that own local repositories,
tools, credentials, persistent sessions, and process lifecycle. The missing
piece is not a different model SDK; it is an operator-controlled runtime release
process.

The process has two distinct approvals:

1. A release PR may be automatically merged after deterministic CI and a
   harness review report pass.
2. **No merged PR authorizes a worker restart.** A release stops at
   `ready_for_operator` for each selected node. Only an explicit per-node
   operator approval may begin draining or change the active runtime.

This separation is intentional. A worker can have no currently active task yet
still own an idle, semantically live Manager session. A worker result can wake a
Manager after the apparent idle moment. Restarting in that interval can break a
Case even though a simple task-count drain reported zero.

## Current facts and resulting constraint

`scripts/safe_worker_deploy.py` is a useful checkout-deployment canary, but it
is not a runtime release controller:

- its canary starts with `WORKER_BACKENDS=""` and `WORKER_MAX_CONCURRENT=0`, so
  it proves worker boot but not Codex/Claude runtime compatibility;
- it restarts the real worker after canary registration; it does not first stop
  task admission and drain that worker to a semantic safe point;
- a worker process owns all its configured backends, so restarting it for a
  Codex update also interrupts Claude and worker job state.

The Codex adapter has proven exact thread continuity across turns in a live
app-server process, but not across a worker restart. Claude's primary driver
also keeps live SDK clients per session. Therefore neither backend may claim
restart-transparent session continuity until an explicit restart/recovery test
proves it for the installed runtime versions.

## Non-goals

- No automatic worker restart, even after a release PR merges.
- No force-drain, cancellation, or task release caused by an update deadline.
- No generic remote-command executor or arbitrary manifest-provided shell
  commands.
- No OpenCode rollout in this feature.
- No claim that a model available from the API must be available to a Codex
  account or its installed CLI.

## Terms

- **Release:** exact, immutable desired versions for one or more backend
  runtime artifacts.
- **Candidate:** an installed but inactive release on one worker.
- **Active runtime:** the versioned environment used by the running worker.
- **Node maintenance window:** the operator's per-node authorization to drain,
  switch, verify, and, if necessary, roll back one worker.
- **Semantic safety:** a stronger condition than zero tasks. It includes the
  impact of idle persistent sessions, open Cases, pending continuations, and
  queued results.

## Architecture

```
release watcher -> version PR -> CI + harness report -> merge
                                              |
                              desired release, no node action
                                              |
operator selects node + approves maintenance window
                                              |
worker-local reconciler -> stage candidate -> runtime canary
                                              |
                                  controlled admission drain
                                              |
                         operator-visible safety recheck + switch
                                              |
                         restart/recover/verify or rollback
```

The gateway never SSHes to workers. Each worker runs a separate, low-privilege
`ai-team-runtime-reconciler` process under PM2. It polls authenticated desired
release state and can stage a candidate locally. It cannot activate a release
until the gateway records a valid, unexpired operator approval for that node.

The reconciler is separate from `ai-team-worker`: it remains alive while the
worker carrier is restarted and can perform rollback if the new carrier fails
to return.

## Shared abstraction

The shared controller owns release lifecycle and worker-wide safety. Backend
adapters own only package and probe mechanics. They are curated code, not
configuration that can execute arbitrary commands.

```python
class BackendRuntimeAdapter(Protocol):
    backend: Literal["codex", "claude"]

    def discover_installed(self) -> RuntimeInventory: ...
    def stage(self, release: RuntimeReleaseSpec) -> CandidateRuntime: ...
    def verify_candidate(self, candidate: CandidateRuntime) -> ProbeResult: ...
    def activation_environment(self, candidate: CandidateRuntime) -> dict[str, str]: ...
    def verify_running(self, expected: RuntimeReleaseSpec) -> ProbeResult: ...
    def rollback(self, previous: CandidateRuntime) -> None: ...
```

All public data structures should be Pydantic models. The controller must not
import a concrete Codex or Claude backend to make lifecycle decisions.

| Owner | Responsibilities |
|---|---|
| `RuntimeReleaseController` | durable release state, per-node approval, admission drain, canary/promotion/rollback state machine, UI/read model |
| `codex` adapter | exact Codex package, executable/app-server probe, model catalog inventory |
| `claude` adapter | exact `claude-agent-sdk` and Claude Code CLI/runtime inventory, SDK/CLI probe |
| worker reconciler | local staging, runtime pointer changes, starts canary, invokes the existing safe deploy mechanism |
| gateway | source of truth for approval, node admission, Cases, task claims, and rollout observation |

## Desired release manifest

Store desired, exact versions in a checked-in manifest such as
`config/backend_runtimes.toml`. It is desired state, not executable code.

```toml
schema_version = 1

[runtime.codex]
enabled = true
version = "0.156.1"
package = "@openai/codex"
integrity = "sha512-..."
expected_model_ids = ["gpt-6-sol"]

[runtime.claude]
enabled = true
agent_sdk_version = "0.2.157"
cli_version = "2.1.278"
```

Claude deliberately records both the Python SDK and the local CLI/runtime. The
current driver uses `claude-agent-sdk` and a local Claude process; treating
either as an invisible global dependency would make rollback and diagnostics
ambiguous.

Candidates must be immutable and isolated. The active runtime is an atomic
pointer, never an in-place global npm or live virtualenv mutation:

```
worker-runtime/
  releases/<release-id>/{codex,python-venv,metadata.json}
  current  -> releases/<active-release-id>
  previous -> releases/<last-known-good-release-id>
```

The worker launcher resolves `current`, allowing each adapter to supply the
correct executable, `PATH`, Python interpreter, and any explicit Claude CLI
path. Reversing `current` is the rollback primitive.

## State machine and operator gate

```
detected -> pr_open -> ci_verified -> merged -> staged
                                           |
                              per-node operator approval
                                           v
ready_for_operator -> canary_verified -> draining -> drained
                                                    |
                               operator-visible final recheck
                                                    v
                                              switching -> verifying -> healthy
                                                   |             |
                                                   +--------> rollback -> rolled_back

any timeout, lost approval, or safety ambiguity -> blocked
```

`staged` and `ready_for_operator` perform no task-admission change and never
restart a worker. The dashboard and an optional browser push notification must
say which node, runtime versions, and current safety evidence are awaiting
approval.

An approval is scoped to one `release_id`, one `node_id`, and a bounded expiry.
It is consumed when the transition to `draining` succeeds. A new version, a
rollback, or an expired approval requires a new operator action.

The UI action is intentionally explicit:

```
Approve maintenance window for Horse
Codex 0.153.2 -> 0.156.1
Claude unchanged
Current state: 0 active tasks; 1 open Case; manager session idle
[Cancel] [Approve this node]
```

The UI does not imply that it is safe merely because it displays a green task
count. It presents evidence; the operator makes the semantic judgment.

## Admission drain

Once approval is consumed, the gateway marks the node `draining`. `draining`
means live and observable, but ineligible for new work. It is not `offline`.

The following are mandatory and must be tested together:

1. Routing excludes draining nodes.
2. `/tasks/pending` returns no new work for a draining node.
3. `/tasks/{id}/claim` checks node admission in the same DB transaction as the
   claim, so a task fetched just before the transition cannot win afterwards.
4. The worker observes its maintenance state before claiming, closing the local
   poll/claim race.
5. Existing work is allowed to finish and report normally.

Drain is worker-wide, irrespective of the backend being updated. Completion
requires gateway and worker observations to agree on all of:

- zero active/claimed worker tasks across every backend;
- zero worker-owned background job processes;
- no result or telemetry handoff still awaiting durable receipt;
- fresh worker heartbeat and fresh gateway claim view.

An open Case, an idle Manager, queued continuation, or a pending Manager wake
is displayed as a prominent warning but does not become a fragile automatic
rule in the first version. The operator may approve a known maintenance window
despite it; the system never silently interprets it as safe.

On drain timeout the node remains draining and the release becomes `blocked`.
It never cancels, releases, or kills a task. The operator can return the node
to `online` or take an independent cancellation action.

## Canary and probes

Extend `safe_worker_deploy.py`; do not replace its current safety properties.
The candidate canary remains non-routable (`WORKER_CANARY=true`, zero task
concurrency), but it must load the selected backend adapters and report a
candidate runtime inventory.

Required no-paid probes:

| Backend | Candidate probe |
|---|---|
| Codex | exact `codex --version`; app-server initialize; bounded `model/list`; adapter protocol fixture compatibility |
| Claude | exact Python package and CLI versions; SDK import; bounded local transport/connect compatibility |

For a model-driven release, Codex `model/list` can require an expected model
such as `gpt-6-sol`. A missing model produces `model_unavailable`; it does not
trigger a retry loop or claim that an account rollout has completed.

A real two-turn smoke is paid. It must be optional by an explicit release
policy, isolated in an empty temporary workspace, tool-free, low-effort,
time-bounded, and capped to one tiny smoke per backend per canary release.
There is no honest zero-cost substitute for validating a remote model turn.

More importantly, two turns in one new process do not prove restart recovery.
Before automatic runtime activation is enabled for either adapter, CI and a
canary must prove this exact sequence:

```
turn 1 -> persist identity -> recycle worker/runtime -> resume exact identity -> turn 2
```

Until that test is proven, the release UI must warn that idle persistent
sessions will be invalidated. The operator can still choose the maintenance
window, but the system must not claim transparent resume.

## Promotion and rollback

After the node is drained, the reconciler atomically points `current` to the
candidate and promotes using the safe worker canary path. The normal worker
re-registers with its new incarnation and inventory. Only then can the node
return to `online`.

Failure before changing `current` removes the canary and leaves the normal
worker untouched. Failure after changing `current` performs this bounded
rollback:

1. mark the node unavailable for routing;
2. restore `previous` as `current`;
3. restart the ordinary worker on the old runtime;
4. require registration, exact old inventory, and health evidence;
5. record the `(release, node)` pair as poisoned and stop rollout.

No other node begins its maintenance window automatically. A rollback failure
is an operator alert, not a loop.

## PR, CI, and review policy

A scheduled release watcher may open a bounded PR when an upstream version
changes. The PR contains only the manifest, resolved integrity/lock metadata,
and generated compatibility report.

Required checks:

- manifest validation and artifact integrity;
- Linux x86_64 and ARM64 candidate-install checks;
- focused Codex app-server transport/adapter tests;
- focused Claude SDK/CLI lifecycle tests;
- existing worker/canary and claim-admission tests;
- a harness review report: release/API diff, adapter impact, tests, model
  catalog observation where available, and `safe`/`needs_review`/`blocked`
  verdict.

Automatic merge may be enabled only for a release-only PR with every required
check green and a `safe` report. It authorizes desired state only. It does not
authorize deployment, draining, restarting, or paid smoke. In particular,
Codex `0.x` minor updates are treated as potentially breaking.

## Control surface and service boundaries

The implementation adds durable release records and narrow authenticated
control actions, not arbitrary remote shell access. Payloads are bounded and
strictly validated. The release controller holds one durable lease per node;
concurrent approvals or reconcilers cannot run competing transitions.

Every action has a deadline: candidate staging, canary boot, drain observation,
promotion registration, running probe, and rollback. Gateway/DB failure means
no activation; the existing worker continues on its known runtime. Worker
heartbeats and claim state are treated as evidence, never as authorization to
skip the operator gate.

At 100 concurrently staged nodes, staging must be rate-limited by the
controller and each reconciler must permit only one release operation. Only one
node may be in `draining` or later per release cohort in the first version.
Reports and inventories are size-capped; full tool output stays local and only
bounded diagnostics are persisted.

## Delivery order

This work touches worker admission and session continuity, so it must be
coordinated with the active durable turn-queue work rather than raced against
it.

1. Define Pydantic release models, persistence, inventory read model, and UI
   status; no activation.
2. Add and test the worker-wide `draining` admission state and its atomic claim
   gate.
3. Extend the existing canary script to use an immutable candidate environment
   and backend-aware no-paid probes.
4. Implement Codex and Claude adapters plus reversible local staging.
5. Prove restart/recovery semantics per backend, or expose the unsupported
   state honestly.
6. Add the worker-local reconciler and operator-approved single-node promotion.
7. Add the release watcher, bounded PR report, and policy-gated auto-merge.

No code changes from this document are authorized merely by this design status.
