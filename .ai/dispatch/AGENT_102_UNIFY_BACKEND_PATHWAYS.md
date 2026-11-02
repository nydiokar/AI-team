```yaml
job_id: AGENT_102_UNIFY_BACKEND_PATHWAYS
created_at: "2026-10-08T11:03:02.721707+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: "A102 S2 reconciliation, 2026-11-02"
depends_on: []
results_ref: null             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-02T14:28:09.291192+00:00"
```

# DISPATCH — 102 · One pathway per operation per backend (natural names, managed logic, policy out of backends)

**Level:** 3 (crosses worker + every backend, >5 files; operator-requested 2026-10-08) · **Type:** code
**Authored:** 2026-10-08 · **Status of this packet:** S2 engineering accepted; runtime promotion and live S3 acceptance pending (2026-11-03)
**Depends on:** S0 = PR #205 (`TurnControl` in `src/core/turn_liveness.py`), PR #204 (`turn_liveness`, merged `ba77b9f`)
**Branch:** per stage: `feat/a102-claude`, `feat/a102-codex`, `feat/a102-opencode` (S1, parallel, one git worktree each under `.worktrees/`), then `feat/a102-carrier` (S2). Each opens a PR; the Manager reviews and merges.

> **Read this first — why this packet exists.** On 2026-10-08 a *working* OpenCode turn on Horse
> (`task_e05a467a`) was cut off after exactly 30 minutes, which wedged its session. The fix
> (PR #204) exposed the real problem: A82 made the *system* unified (one admission point → one
> managed queue → one carrier, live since the cutover on 2026-10-07 13:36Z). But in the
> *backends* it **forked instead of upgrading**: every backend now has the old methods
> (`create_session` / `resume_session` / `compact_session` / `cancel`) **and** a parallel managed
> copy (`run_managed_turn` / `run_managed_compaction` / `cancel_managed_turn`) with different
> logic and bugs. Each backend also re-implemented turn-wait *policy* with its own wall-clock
> limits. Stage 8b (PR #199) deleted admission branches only and touched no file under
> `src/backends/` or `src/worker/`. The A87 ruling ("A82 is not closed while two paths exist")
> was never met. This job finishes it.

## Why (intent)
Backends are thin, fixed adapters that *rule the agents*: start, send, compact, cancel, close,
and report busy/idle. The system above them (queue, scheduler, carrier) already decides when to
run what and how to react. After this job:
- each operation in each backend has **exactly one implementation**, under its **original
  natural name**, carrying the logic that was actually correct (the managed semantics);
- **no backend contains turn-wait policy**: no wall clocks and no timeout config reads. The
  carrier decides the limits once and hands them over in `TurnControl`.

## Operator rules (non-negotiable)
1. **Keep the original method names.** `create_session`, `resume_session`, `compact_session`,
   `cancel`, `close`, `run_oneoff`, `is_quiescent`, and inside backends the natural private names
   (OpenCode `_send_message`, Claude `_SDKSession.send`, Codex `_run`). Teach *them* the new
   logic. Do **not** keep or create `*_managed_*` twins.
2. **Port the best of both.** Where the old body did something better (e.g. no-output-based
   inactivity), keep it. The managed semantics are the baseline for behaviour.
3. **Policy out of backends.** A backend never reads `timeout_seconds` / `sdk_turn_timeout_sec`
   / `MAX_TURN_SECONDS` / `inactivity_timeout_sec`, and never decides how long to wait. It calls
   `turn.touch()` on every native event and gives up (typed `RecoveryRequiredError`, never an
   interrupt, never an abort) only when `turn.expired()` is non-empty.
4. **No `if managed` branches.** There is one behaviour.

## Target backend interface (the contract every S1 worker implements)
`turn: TurnControl` (PR #205) = `turn_uuid` (tag the prompt with it), `ownership` (fencing),
`on_process` (report process identity BEFORE submit), `touch()`, `expired()`, `remaining()`.

| Method | Semantics (from today's managed path) | Replaces |
|---|---|---|
| `create_session(session, *, turn, telemetry_context=None, telemetry_sink=None)` | Start the native session if needed and run the first prompt (`session.last_user_message`) tagged with `turn.turn_uuid` | `run_managed_turn` (no-native-session branch), legacy `create_session` |
| `resume_session(session, message, *, turn, telemetry_context=None, telemetry_sink=None)` | Busy/not quiescent ⇒ typed `OwnershipConflictError` **before** submit (never interrupt). Submit tagged with `turn.turn_uuid`. Lost ack ⇒ reconcile by id, never resubmit. Return only OUR correlated reply. Expiry or unattributable result ⇒ `RecoveryRequiredError`, with a late result delivered via the proactive sink | `run_managed_turn`, legacy `resume_session` |
| `compact_session(session, *, turn, ...)` | The managed compaction semantics | `run_managed_compaction`, legacy `compact_session`, the interface default that sends `/compact` as text |
| `cancel(session, turn_uuid)` | Abort **only** that turn if running; arm it if not started; refuse otherwise | `cancel_managed_turn`, legacy session-wide `cancel` |
| `close(session)` | unchanged | — |
| `is_quiescent(session)` / `quiescence_reason(session)` / `forget_turn(session, turn_uuid)` / `set_proactive_sink` | unchanged semantics (`forget_managed_turn` → `forget_turn`) | — |
| `run_oneoff(cwd, message, *, turn, ...)` | A thin wrapper: a throwaway `Session` + `create_session` (R1) | per-backend one-off stacks |

## TASK

### S0 — contract (DONE by Manager: PR #205)
`TurnControl` + `turn_control()` in `src/core/turn_liveness.py`.

### S1 — three backend workers, IN PARALLEL (one worktree + branch each; strict file ownership)
In every S1 branch, `run_managed_turn` / `run_managed_compaction` / `cancel_managed_turn` /
`forget_managed_turn` **stay only as one-line deprecated shims**. Each shim builds
`turn_control(ownership.turn_uuid, ownership=ownership, on_process=on_process)` and calls the
natural method. That keeps the worker carrier (`src/worker/agent.py`, unchanged in S1) working
until S2 deletes the shims.

**S1-Claude** — owns `src/backends/claude_code.py`, `src/backends/claude_driver.py`,
`src/backends/claude_role_adapter.py`, `tests/test_claude_*`, `tests/test_turn_queue_{sdk_ownership,r2,r3,r4,r5,4b_carrier,carrier_recovery,carrier_integration}.py`.
1. `ClaudeCodeBackend.create_session/resume_session/compact_session/cancel` take today's managed
   bodies (`run_managed_turn` → `_run_managed` → driver `send_managed` …).
2. `_SDKSession.send()` becomes the single send, with refuse-don't-interrupt and echo correlation.
   Delete:
   - the legacy `send` body
   - interrupt-on-conflict
   - `submit()`'s cancel-on-timeout
   - `_turn_timeout_sec`
   - `send_managed`
   - `_submit_managed_no_interrupt` (keep its wait logic inside `send`, driven by `turn`)
3. The reader `touch()`es the turn's clock on every message (today `_turn_progress`).
4. Delete `ClaudePrintResumeDriver` and driver selection (R2).
5. `run_oneoff` per R1.

**S1-Codex** — owns `src/backends/codex_native.py`, `src/backends/codex_app_server.py`,
`src/backends/codex_ownership.py`, `tests/test_codex_*`.
1. `_run()` keeps one behaviour: delete every `managed is None` branch, plus `MAX_TURN_SECONDS`,
   `MANAGED_STALL_SECONDS` and `MANAGED_TURN_SECONDS`.
2. `create_session/resume_session/compact_session/cancel` take the managed bodies.
3. Collapse the four cancel mechanisms (`cancel`, `cancel_execution`, `CodexOwnership.request_cancel`
   polling, `cancel_managed_turn`) into `cancel(session, turn_uuid)`. If something outside this
   worker's files still calls `cancel_execution`, leave it as a shim for S2 to delete.

**S1-OpenCode** — owns `src/backends/opencode.py`, `src/backends/registry.py`, `tests/test_opencode_*`.
1. `_send_message` becomes the **single** turn body: deterministic `managed_message_id(turn_uuid)`,
   lost-ack reconcile, correlated reply only, no abort on expiry, late capture.
   Merge `_run_managed` / `_managed_turn_body` / `_managed_submit` / `_managed_wait` into it, and
   remove the duplicates (event-reader setup ×2, body/model ×2, status parsing ×2, lock+capacity ×3,
   default provider ×2, native session creation ×2).
2. One compaction (`/summarize`). Its socket timeout comes from `turn.remaining()`, not config.
3. One `cancel(session, turn_uuid)`, which aborts only if the running turn is ours.
4. **Delete `OpenCodeBackend` (the CLI class, operator-retired)** and its registry entry and tests (R3).
5. Known defect, fix if contained: `is_quiescent` can spawn a server (`_ensure_server`). A probe
   must not start one.

**Every S1 worker:**
- Tests are tied to the natural names. Keep every existing behavioural assertion: the
  managed-turn tests are the safety net, renamed, not dropped.
- Keep the PR #204 regression tests: a working turn longer than the stall window is never cut
  off. Drive them via `turn_control(..., stall_override=...)`.
- Run **plain targeted pytest on the owned test files only** (TEST COST GUARD: never the
  full/e2e suite, never the paid CLI).

### S2 — carrier + core + gateway cleanup (ONE worker, after all three S1 PRs merge)
Owns `src/worker/**`, `src/core/interfaces.py`, `src/core/backend_call.py`, `src/orchestrator.py`,
`src/control/**`, `src/services/session_service.py`, `config/settings.py`, `worker_main.py`, and their tests.
1. `CodingBackend` gets the target interface. Delete `run_managed_turn`,
   `run_managed_compaction`, `cancel_managed_turn`, `forget_managed_turn` and
   `supports_managed_turns` (every backend supports it now). Delete the S1 shims.
2. The worker builds `turn_control(...)` per claimed turn and calls the natural names.
   **The carrier is the only place turn limits are decided.**
   Delete the worker's legacy `action` branch for session ops (`agent.py` ~944-970).
3. Delete dead gateway code:
   - `_run_backend_local`, `_dispatch_or_run_local`
   - the session branch of `_process_task`
   - the legacy branches of `compact_session` (in-process `backend.compact_session`)
   - the legacy fallbacks in `cancel_task` / `_enqueue_remote_cancel_turn`
   - the gateway-side `backend.close` fallback
   - `_legacy_put_guard`, `_enrollment_exclusion`
   - stale comments (e.g. `orchestrator.py:6280` "legacy path below, unchanged")
4. Delete the flags `WORKER_MANAGED_TURNS` and `TURN_QUEUE_ENROLLMENT_ENABLED` and their branches.
   Fix `_managed_backends` (stale docstring) and the advertisement, so retired backends are not
   advertised.
5. Keep the protocol-0 **control** rows (`close_session`, cancel, `fetch_staged_file`, `inspect`)
   as signals only. They never run an agent turn.

### S3 — docs + live proof (Manager)
- Correct `.ai/CONTEXT.md` (A82 "legacy execution path deleted" was inaccurate) and the A82 §17 closure line.
- Deploy the gateway.
- **Operator-gated:** restart the workers (kanebra + Horse).
- Live proof: one long turn per backend (> 30 min of native progress) completes.

## ACCEPTANCE (proof, not vibes)
1. **Grep proof:** each item below yields **0 hits**.
   - `rg -n "def (run_managed_turn|run_managed_compaction|cancel_managed_turn|send_managed|_run_managed|_managed_wait)\b" src/`
   - `rg -n "managed is None|managed is not None|if managed" src/backends/`
   - `rg -n "timeout_seconds|sdk_turn_timeout_sec|MAX_TURN_SECONDS|inactivity_timeout_sec" src/backends/`
2. **One body per operation:** each backend's ctags outline shows exactly one public
   `create_session`, `resume_session`, `compact_session` and `cancel`, and no private twin
   doing the same job. The reviewer checks the outline.
3. **Behaviour kept:** every pre-existing managed-turn test (conflict ⇒ no interrupt; lost ack ⇒
   no resubmit; foreign reply never ours; expiry ⇒ recovery + late delivery; cancel only ours;
   quiescence) still passes under the natural names. The PR #204 long-turn regression tests pass.
   Each S1 PR lists the exact pytest command and its result line.
4. **Policy single-sourced:** turn limits are computed only in the carrier (`turn_control(...)`
   call sites: worker plus the one-off wrapper). A test proves a backend gives up exactly when
   `turn.expired()` does and never earlier.
5. **Live (S3):** after deploy and the operator's worker restart, one turn per backend
   (claude, codex, opencode-server) runs > 30 min with progress and completes successfully. The
   task ids are recorded here.

## RESERVED DECISIONS (surface, do not guess)
- **R1 — `run_oneoff`.** Default: keep the name; implement it as a throwaway `Session` +
  `create_session` with its own `turn_control`. Production refuses gateway-local one-offs
  (`GATEWAY_LOCAL_EXECUTION_ENABLED=false`), so this is consolidation only.
- **R2 — Claude PrintResume CLI driver.** Default: delete it (unreachable under the default
  `driver=sdk`; `build_driver` raises instead of falling back). Its inactivity-reader idea is
  already in `turn_liveness`.
- **R3 — OpenCode CLI backend (`opencode`).** Default: delete it (operator retired it on
  2026-10-02). Nodes stop advertising it.
- **R4 — Shims.** Exist only between S1 and S2 merges. S2 must delete them; no shim survives A102.

## SCOPE OUT
- Turn queue, scheduler, admission, DB schema, carrier HTTP protocol, UI. These are already
  unified and stay.
- The protocol-0 claim reaper (`mesh.claim_max_runtime_sec`) and the reaper treating held
  recovery turns as `missing_from_live_state`. These are tracked separately (memory note,
  PR #204 "Not verified").
- New features or behaviour beyond what the managed path does today.

## TRAIL / EVIDENCE (fill at close)
- PRs: S0 #205; S1-Claude #209, S1-Codex #208, S1-OpenCode #207 (all merged); S2 PR #271 (`feat/dispatch-reconciliation`), engineering accepted at `f68d11a` (2026-11-03).
- Dispatched 2026-10-08 (node `kanebra`, opus, role=worker, one worktree each):
  S1-Claude `task_f40508df` / session `ead810e403bc` (`.worktrees/a102-claude`);
  S1-Codex `task_b5afb280` / session `f65a69f8b50c` (`.worktrees/a102-codex`);
  S1-OpenCode `task_90c5ba29` / session `7f2f8e988829` (`.worktrees/a102-opencode`).
- Grep proof output; per-PR pytest lines; live long-turn task ids.

---
## Milestone (burndown)
- [x] S0 `TurnControl` contract (PR #205)
- [x] S1-Claude merged
- [x] S1-Codex merged
- [x] S1-OpenCode merged
- [ ] S2 carrier/core/gateway cleanup merged (shims + flags + dead code gone)
- [x] Runtime retirement/policy searches clear; arch-contract passes across source/scripts/tests/prompts (only explicit negative-env test waivers)
- [ ] Gateway deployed; workers restarted (operator)
- [ ] Live long-turn proof per backend
- [x] CONTEXT.md / A82 closure corrected

## Closure — ENGINEERING ACCEPTED, OPERATIONAL GATES PENDING (2026-11-03)

S1 had already merged; its unchecked boxes and empty PR fields were stale. S2 now
removes deprecated managed APIs/probes, rollout flags and the dead gateway session
execution/remote-dispatch bodies. The carrier supplies one `TurnControl` and invokes
natural create/resume/compact/cancel/forget operations. Stateless oneoffs retain their
wrapper, retry/summary behavior and turn-scoped cancellation; a cancellation waits for
the native executor outcome instead of orphaning its thread. Host-or-nothing affinity
is verified at real admission/activation/claim boundaries. Failed turns preserve native
resume identity; missing conversations never start an unowned hidden retry.

Independent review initially requested rework for oneoff cancellation, false Codex
readiness and attachment side effects before legacy-turn rejection. All three are
fixed with regression tests. Final independent verdict: ACCEPT at `f68d11a`, 118
focused offline assertions passed. No queue schema or carrier HTTP envelope was changed;
existing workers remain wire-compatible with the upgraded controller.

Verification (repo venv, isolated worktree; no paid/full/e2e suite):
- `pytest tests/test_turn_queue_rollout.py tests/test_worker_managed_turns_default.py
  tests/test_session_service.py tests/test_session_service_lifecycle.py
  tests/test_session_close_propagation.py tests/test_turn_queue_worker.py
  tests/test_turn_queue_precutover.py tests/test_turn_queue_precutover_rework.py
  tests/test_turn_queue_backend_conflict.py tests/test_turn_queue_main_merge2.py
  tests/test_turn_queue_sender.py tests/test_turn_queue_r2.py tests/test_turn_queue_r3.py
  tests/test_turn_queue_r4.py tests/test_turn_queue_r5.py tests/test_claude_driver.py
  --tb=short -n 4` — 269 passed. The obsolete clock fixture was corrected to inject
  the carrier's clock; fake-SDK pending coroutines are now cancelled/awaited at cleanup.
- `pytest tests/test_a102_carrier_contract.py tests/test_turn_queue_r4.py
  tests/test_turn_queue_sdk_ownership.py tests/test_claude_job_session_env.py --tb=short`
  — 58 passed after cleanup, including 31-minute fake-clock progress/stall proof.
- `pytest tests/test_session_payload_roundtrip.py tests/test_worker_startup_registration.py
  tests/test_backend_registry.py tests/test_claude_session_backend.py tests/test_docker_boundary.py
  tests/test_docker_runtime_config.py tests/test_mesh_dispatch_timeout.py
  tests/test_retry_transient.py tests/test_usage_propagation.py --tb=short -n 4`
  — 63 passed.
- `pytest tests/test_turn_queue_carrier_recovery.py --tb=short` — 24 passed after
  moving the recovery fixture clock to the carrier and asserting permanent capacity/state.
- Architecture end-state checker at `f68d11a`: CONTRACT MET over 408 files; only
  retired environment strings in negative compatibility tests are waived. No retired
  implementation/API definition or production flag is waived. `git diff --check` passes.

Service boundary review:
- Concurrency/memory: existing worker slots, scheduled-work bound, backend capacity
  semaphores, staged/result byte limits and 128 MiB per-carrier result-spool budget
  remain. No new tasks, pools, result copies or parallel DB reads were introduced.
- Request size/malformed input: existing HTTP/intent limits remain; worker payload,
  nested metadata/session/staged-file shapes are checked before side effects. Unowned
  session operations are refused before fetching/deleting attachments. Cancel targets
  are bounded to 256 characters and scoped to the identified turn.
- Timeout: carrier hard-cap/progress policy is passed once; the adapters retain
  ownership/late-recovery semantics on expiry. A cooperative oneoff cancel waits for
  the native outcome. Existing control-task admission and native IO limits are unchanged;
  no new hard process-kill or absolute native/Python deadline is claimed.
- Resource failure: unavailable/unsupported Codex runtime fails readiness offline;
  registry probe failure excludes only that adapter. Existing DB/claim-token failure,
  recovery and retry semantics remain fail-closed.

S3 is not certified by offline tests: gateway deployment, worker loaded-build promotion
and naturally observed long progressing turns are separate facts. Worker restarts need
the operator decision under AGENTS.md; Kanebra hosts this agent, so any approved local
promotion must wait for this turn to finish under supervised handoff. No paid 30-minute
turn will be manufactured. A78's managed reset-metadata gap stays explicit in its own
blocked packet; a test on retired dispatch code no longer disguises that gap.

## End-state contract (S2, 2026-11-02)

Checks cover runtime code, scripts, tests and role prompts. The only exceptions
are tests feeding retired environment values adversarially to prove that they
cannot restore a legacy execution mode. No retired API definition is waived.

```arch-contract
scope: [src, config, scripts, tests, worker_main.py, docs/harness/roles]
retired:
  - run_managed_turn
  - run_managed_compaction
  - cancel_managed_turn
  - forget_managed_turn
  - supports_managed_turns
  - cancel_execution
  - prepare_execution
  - WORKER_MANAGED_TURNS
  - TURN_QUEUE_ENROLLMENT_ENABLED
  - _run_backend_local
  - _dispatch_or_run_local
  - _process_task_remote
  - _dispatch_to_node
  - _legacy_put_guard
  - _enrollment_exclusion
  - _enqueue_remote_cancel_turn
forbidden_in:
  src/backends: ['^timeout_seconds$', '^sdk_turn_timeout_sec$', '^MAX_TURN_SECONDS$', '^inactivity_timeout_sec$']
waivers:
  - {match: 'retired:WORKER_MANAGED_TURNS', path: tests/test_worker_managed_turns_default.py, reason: 'Adversarial old environment values cannot disable the permanent carrier or SDK correlation.'}
  - {match: 'retired:WORKER_MANAGED_TURNS', path: tests/test_turn_queue_r4.py, reason: 'Regression checks that a retired opt-out cannot disable turn echo correlation.'}
  - {match: 'retired:WORKER_MANAGED_TURNS', path: tests/test_turn_queue_r2.py, reason: 'Regression checks that a retired opt-out cannot disable the common worker contract.'}
  - {match: 'retired:TURN_QUEUE_ENROLLMENT_ENABLED', path: tests/test_turn_queue_rollout.py, reason: 'Adversarial old rollout-flag values cannot enable enrollment rollback or a second execution mode.'}
```
