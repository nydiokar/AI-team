# AI-Team Gateway — Hot Context

**Last Updated:** 2026-07-23
**Active branch:** `main` — M2 Work Control Substrate + full M3 survivability arc merged; `HARNESS_FLOW_DRIVE` **ON** live.

> **🟢 STATUS 2026-07-23 — RECONCILED AGAINST GIT (the doc had drifted ~5 days / 16 PRs behind).**
> Everything the 2026-07-18 block below lists as "OPEN / op-merge" is now **MERGED to `main`**
> (PRs #22–#28), PLUS a full **2026-07-20→22 arc** that wasn't recorded here at all:
> - **PR #29** session fork into a fresh session under one Case · **PR #31** fork a prior
>   conversation into a Manager session (role + tools + `<prior_context>`) · **PR #32** deliver the
>   first-turn objective to node-pinned Managers (empty-message boot) · **PR #33** Manager context
>   fidelity — generous tail-keep carry + `read_session_history` tool · **PR #34** keyboard-safe
>   New-Session sheet · **PR #35** CI schema-version-drift fix (CI now on ubuntu, project is
>   Linux-only) · **PR #36** Manager per-worker **model tiering** in `dispatch_worker` + live Case
>   **roster cockpit** (sessions/tokens/running scripts) · **PR #37** (`aaf1cb2`) **canonical SDK
>   agent spawn** — agents always spawn on the persistent SDK driver, never `claude -p`; also
>   restores the Manager MCP tools by setting role sessions to `setting_sources=["user","project"]`
>   (the `["project"]`-only pin from PR #28/`703faf5` had dropped user scope so the `manager` MCP
>   server never connected). Latest commit `d02eb85` — session total token usage + approx cost.
>
> **⚠️ OPERATIONAL GATE — the Manager dispatch substrate is MERGED-BUT-NOT-LIVE.** PR #37's
> `setting_sources` fix needs a **gateway restart** to take effect, and that restart has NOT happened
> (operator-deferred as of 2026-07-23). A freshly-fired Manager (`/api/manager`, PR #28's fireable
> loop) currently boots with its role prompt but **zero `manager` MCP tools** (`dispatch_worker` /
> `open_case` / `close_case` / `record_review` / `release_worker` / `reconcile_waits` all absent) —
> verified live in Case `1b59822e…`. Until the restart, a fired Manager can only derive / escalate /
> edit docs / build directly; it **cannot dispatch a worker through the substrate.**
>
> **🟢 A46 M3.3 DURABLE RELAY — BUILT 2026-07-23 (PR #38, op-merge).** The last named structural
> fragility. `wait_for_worker` was a pure in-process poll → a Manager/gateway crash mid-wait lost the
> wait. New flag `DURABLE_RELAY_ENABLED` (default OFF ⇒ byte-identical): `dispatch_worker` records a
> durable append-only `worker.wait_pending` marker on the Case (reuses the A25/A26 `flow_events`
> substrate — no new schema, `event_type` is unconstrained) + a `reconcile_waits` tool /
> `POST /api/cases/{id}/waits/reconcile` that resolves each outstanding wait against the durable
> `task.finished` event (resolved ⇒ `worker.wait_resolved`; open ⇒ re-arm), idempotent. 213 targeted
> pytest green. Built directly by the fired Manager because the dispatch restart is deferred. **Live
> e2e (marker→crash→reconcile through the running gateway) needs `DURABLE_RELAY_ENABLED=1` + a gateway
> restart — operator-gated.** Not merged.
>
> **What this leaves open / next forks (operator decisions):** (1) the **gateway restart** on `aaf1cb2`
> to make `dispatch_worker` live again (unblocks all Manager dispatch); (2) **merge PR #38** (A46); (3)
> the **Rank-1 node re-run of A43** — carrier-independent Manager acceptance on a real node — still
> operator-gated/paid; (4) direction beyond the survivability arc (the 07-20→22 arc — fork / telemetry
> / SDK-canonical spawn — is merged but has **no live proof** yet).

> **🟢 STATUS 2026-07-18 — PRs #24/#25/#26 MERGED to `main` (in order #24→#25→#26, textually clean) +
> Manager/Worker role PROFILES REWRITTEN to the operator's behavioral profiles.** `docs/harness/roles/manager.md`
> + `worker.md` now carry the operator-authored behavioral identity (autonomous evidence-loop worker;
> case-level adversarial manager) with the repo mechanics preserved as an *Operating constraints* appendix.
> The Manager profile also embeds the **dispatch-envelope template** (TASK/TYPE/CONTEXT/ACCEPTANCE/REALITY
> CONSTRAINTS/AUTHORITY/RESERVED DECISIONS/SCOPE OUT/TRAIL — the manager composes one per worker objective)
> and the **behavioral-evaluation rubric** (6 dims ×0–2, pass ≥10/12, critical-failure list) as the review
> gate. Three-layer model: worker profile + manager profile are persistent role behavior; the dispatch
> envelope is per-task and is NOT baked into the worker profile. Roles load OK (import smoke + 31 role tests
> + 94 targeted tests green). Gateway restarted on merged code + new profiles.
>
> **🟢 §7 trace RESOLVED 2026-07-18 — findings 1 & 2 FIXED in PR #27 (`feat/release-worker-guard`, OPEN, pending operator merge; Case `dfabf68a`):**
> 1. **~~No ownership/role guard.~~ FIXED.** `_release_worker` now takes a REQUIRED `case_id` (the Manager's
>    own Case) and verifies the target against the authoritative session→case index
>    (`GET /api/work/affiliations/sessions`) before `/close`: refuses (structured message, not an exception)
>    unless the target exists, has role `worker`, AND `flow_run_id==case_id`. (`SessionView.to_dict()` does
>    not expose `case_role`/`current_case_id`, so the affiliations endpoint is the one authoritative source.)
> 2. **~~Dead refusal branch.~~ FIXED.** The unreachable `if result.get("ok")` else-branch is gone; the real
>    404→RuntimeError from `/close` is now caught and returned as a structured refusal.
>    `tests/test_mcp_manager.py` = 37 passed (guard happy-path + 3 refusal modes + requires-case_id + 404).
>    **Reconciled alongside:** DEFECT 2 (decision vocab) — `manager.md` now enumerates exactly the FIVE Case
>    verdicts and frames `release` as a worker-lifecycle action; `MANAGER_ALLOWED_DECISIONS` unchanged (five).
> 3. **By-design (STILL DEFERRED):** warm workers hold a backend slot with no idle-reaper — unbounded
>    accumulation gated only on Manager discipline (`release_worker`). Acceptable now; size a bound/idle-reaper
>    if live load shows it.
> **⚠️ Live proof pending:** the running gateway still has the OLD unguarded `release_worker` loaded — proving
> the new guard live needs a gateway restart (operator-gated, post-merge), same as prior PRs.

> **🟢 STATUS 2026-07-17 — LOOP QUALITY VERIFIED LIVE (one Manager, many workers, real rework, manager-decided closure).**
> Cleanup: `core` coredump removed+gitignored; **PRs #22/#23 merged** (gateway restarted, flags
> re-probed live). Then a live Manager (`df8b7e024864`) ran TWO Cases across THREE operator turns:
> Case `9f3d34…` shipped the Worker role layer (**PR #24**, A45); Case `62bb3a…` shipped Case
> observability (**PR #25**, A47 — incl. the Finding-A `role_boot` node round-trip fix + regression
> test) and Manager-decided worker closure (**PR #26**, A48). **Proven live:** continuity across
> operator turns (warm resume, same `backend_session_id`); ONE Manager driving 3 worker tasks; a
> genuine `review.rework_requested`→fix→`review.accepted`; sequential gating; **manager-decided
> `flow.closed`**; worker sessions left WARM (not auto-closed). PRs #24/#25/#26 OPEN (op-merge).
> **⚠️ OPERATIONAL FINDING — quota economics:** the loop hit the Claude account **session/quota limit
> mid-run** ("resets 4:10pm") — Manager+workers share ONE account, so run length × worker count is
> gated by the quota window, not just prompt quality. Resumed cleanly after reset. This is the M3-spec
> "session-cost economics" question, now observed. **⚠️ Also: workers used `cwd=repo` (the live
> checkout) as their tree — future runs should use a separate git worktree, not the gateway's own dir.**
> **⚠️ Not yet observable in THIS run's graph:** A47's fix isn't deployed, so the just-run workers show
> only as `sessions` rows, and the two warm workers (`157c1d0eac95`,`8033ec60ecb3`) carry dangling
> `current_case_id=62bb3a`; both resolve for future cases once #25/#26 merge + gateway restarts.
>
> **🟢 STATUS 2026-07-14 — THE FOUR SURVIVABILITY PRs ARE MERGED + PROVEN LIVE (A44).**
> **PRs #18 (carrier role), #19 (observable workers), #20 (native time), #21 (multi-case session)
> ALL MERGED to `main`** (`cd7f358`, in dependency order #18→#19→#20→#21; dry-run merge was
> conflict-free — the flagged #21 conflict didn't materialize; 93 targeted tests green on the merged
> tree). **Gateway RESTARTED on the merged code (PM2 restart #17); all flags verified LIVE via API
> probe** (review emitter → 422 `invalid_verdict`; manager role → 400 `invalid_repo_path`, NOT 409
> disabled). **A44 live Manager run (in-gateway `__local__` path) PASSED end-to-end on the merged
> code:** `/api/manager` → Case `7616715e…` (session `db911753d4ce`) → dispatched worker
> `task_bfbd354a` → worker did TDD (RED test → GREEN fix, 2 commits) → **`review.accepted`** →
> **`flow.closed`**. Deliverable is real + independently verified (23/23 closure tests green, diff
> inspected): **PR #22** — `close_case` now also closes joined WORKER sessions (`case_role='worker'`)
> on real close, best-effort/isolated, ordered before the affiliation-clear; Manager/non-worker
> sessions untouched. **This closes PR #19's deferred §7 lifecycle gap.** PR #22 OPEN, NOT merged.
> **⚠️ A44 did NOT prove PR #19's observable worker session:** the Manager's `dispatch_worker(cwd=…)`
> hit a **422** (`scripts/mcp_manager.py` opens the worker session via `POST /api/sessions` with
> `{repo_path}` but NO `backend` — and `SessionCreateBody.backend` is required, no default) → it fell
> back to a **legacy one-off task joined to the Case** (Case evidence shows the worker as a `task`
> link, not a `session`). So the review-gated loop + §7 deliverable are proven, but the
> observable-session mechanism from PR #19 is still UNPROVEN live and has a one-line latent bug (add
> `"backend": "claude"` to the `sess_body` in `_dispatch_worker`). **Node re-run of A43 (#18
> acceptance) also remains operator-gated.**
>
> **🟢 STATUS 2026-07-12 — THE INVOKED-MANAGER LOOP RAN LIVE FOR THE FIRST TIME AND PASSED (A41).**
> A real Claude Manager was invoked via `POST /api/manager` → opened ONE Case (`case_role=manager`) →
> autonomously `dispatch_worker`ed a worker that **JOINed the Case** (`membership:worker`, **no child
> Case**) → `wait_for_worker` resolved off the `task.finished` timeline (no slot starvation) → Manager
> reviewed the **committed git diff** → `close_case` with all criteria `{"status":"met"}` → Case `closed`.
> Every M3.1 invariant held live. The run's deliverable is real: it **built M3.2 slice-1** (the `review.*`
> verdict emitter). Full evidence: A41 row in [`DISPATCH_LOG`](dispatch/DISPATCH_LOG.md) + PR #11.
>
> **All three PRs MERGED to `main` 2026-07-12 (`637c6c1`):**
> - **PR #10** — A38 M3 Phase 3.1 Manager role wiring (the code already running live).
> - **PR #11** — A40 M3.2 slice-1 `review.*` emitter, behind `REVIEW_EMITTER_ENABLED` (default OFF ⇒ byte-identical).
> - **PR #12** — F2 fix: gateway keeps its OWN host node `online`+heartbeated so long (>300s) in-process
>   self-claims aren't reaped as `node_offline`.
>
> **Live gateway flag state right now (2026-07-13, gateway restarted on merged code):**
> `HARNESS_FLOW_DRIVE` ON, `MANAGER_ROLE_ENABLED` **ON**, `REVIEW_EMITTER_ENABLED` **ON**,
> `manager` in `~/.claude.json`. **A40 emitter + F2 are LIVE** (verified: `POST /api/cases/_/review`
> with an invalid verdict returns **422 invalid_verdict**, i.e. it passed the emitter flag-gate; the
> unresolved-rework close-gate is active). F2's own-node liveness fix is running post-merge.
>
> **✅ 2026-07-13 — F1 PASSED (A42) and the REWORK CYCLE is now PROVEN LIVE (A43). PRs #13/#16/#17 MERGED.**
> PR #13 closed the verdict-in-loop gap (granted `record_review` + instructed the verdict); F1/A42
> then ran the first review-gated Manager loop (two clean `review.accepted`, feature #41 gauge, PR #16).
> **A43 (this run) closed the last unproven gap — the rework cycle:** an operator-driven in-gateway
> Manager (`/api/manager`, Case `e8bb1b92…`) ran a sequential 2-task loop and issued the **first-ever
> live `review.rework_requested`** → re-dispatched → **`review.accepted`** → 2nd task → `review.accepted`
> → `close_case`. Deliverable is real and merged: **PR #17** (`feat/manager-restart-resilience`) hardens
> the gateway against the very restart incident debugged today (dead-subprocess retry-eligibility +
> honest `driver_lost` state), 3 commits + tests. **PR #16 (#41 gauge) and PR #17 both MERGED to `main`.**
>
> **🔴 2026-07-13 CRITICAL FINDING (A43) — the Manager role is COUPLED TO THE IN-GATEWAY DRIVER.**
> Ran the same invoke with `node_id="kanebra-worker"` (a node agent-worker process). The session pinned
> to the node correctly, but the boot turn came up as a **bare, role-less, tool-less Claude session**
> ("I'm ready to help. What would you like me to work on?") — **no manager role prompt, no manager MCP
> tools, no assignment delivery.** The role boot (`_role_boot`) + `render_first_assignment` +
> scoped-tools wiring exist ONLY on the in-gateway SDK driver path, NOT on the node-worker execution
> path. **Consequence: the Manager cannot run on ANY node (Horse included) — only on the gateway host,
> where a gateway restart kills it.** This is the single most important thing learned today and the
> **next build.** Full write-up + the two other drops:
> - [`dispatch/DROP_MANAGER_ROLE_CARRIER_INDEPENDENT.md`](dispatch/DROP_MANAGER_ROLE_CARRIER_INDEPENDENT.md) — **the fix**: role/driver boots on any carrier (gateway embedded OR node agent worker), one-time global machine setup (MCP in `~/.claude.json`) is acceptable.
> - [`dispatch/DROP_DISPATCH_WORKER_REAL_SESSION.md`](dispatch/DROP_DISPATCH_WORKER_REAL_SESSION.md) — workers are sessionless `run_oneoff` tasks (50 to date, 0 worker sessions ever) → make `dispatch_worker` open a real, openable worker session; run automation on a node so it survives gateway restarts.
> - [`dispatch/DROP_TIMEZONE_NATIVE_TIME.md`](dispatch/DROP_TIMEZONE_NATIVE_TIME.md) — one clock, native local time everywhere; `session_service.py` writes naive-local, `db.py` writes UTC. **Timezone is a data-hygiene defect to eliminate, NOT a root-cause to hand-wave.**
>
> **⚠️ UNREVIEWED LOCAL WORK preserved on `feat/manager-multicase-session` (pushed, NO PR, NOT merged).**
> Commit `31f648a` "persistent multi-Case session + fix affiliation clobber race" (M3.3-ish: `open_case`
> tool/route + fix for `close_case` clobbering `current_case_id` via `upsert_session` ON CONFLICT). Real
> work authored 11:25Z, orphaned by the 11:26Z restart. **Needs a review pass before merge.**
>
> **Immediate next steps (operator will spawn a session to work these one by one):**
> 1. **Carrier-independent Manager role** (`DROP_MANAGER_ROLE_CARRIER_INDEPENDENT.md`) — THE blocker;
>    without it the Manager only runs on the fragile gateway host.
> 2. **Observable worker sessions + node survivability** (`DROP_DISPATCH_WORKER_REAL_SESSION.md`).
> 3. **Timezone standardization** (`DROP_TIMEZONE_NATIVE_TIME.md`).
> 4. **Review + merge `feat/manager-multicase-session`** (`31f648a`).
> 5. **M3.3 durable relay** — `wait_for_worker` is still in-process (a Manager crash loses the wait).
> **Cost reframe (operator, 2026-07-12):** a bounded+supervised live Manager run is NOT "burning tokens" —
> it proves the machinery AND ships real work. The old scar was UNBOUNDED unsupervised spend, not this.
>
> **Superseded:** the old A35 Phase-3.0 manual-pattern live runbook — A41 replaced it with the real
> `/api/manager` role-boot path. Don't run A35 standalone.

> This is the **fast-orientation** doc: what the project is, how it's wired *right
> now*, the current priorities, and the constraints. It is intentionally short.
> - Dispatched-job state → [`.ai/dispatch/DISPATCH_LOG.md`](dispatch/DISPATCH_LOG.md) (the manual state machine).
> - Product intent → [`.ai/context/production_vision.md`](context/production_vision.md).
> - Completed-work history → `docs/archive/progress/_archive_PROGRESS_LOG.md`.

> ⚠️ **TEST COST GUARD — READ BEFORE RUNNING ANYTHING.** Tests can invoke the
> **live, paid Claude CLI** and previously burned millions of tokens. A guard now
> prevents it, but respect the rules:
> - Run tests with plain `pytest` only. Prefer cheap targeted checks (import smoke,
>   direct function calls, `--collect-only`, single skipped-test).
> - **NEVER** run the full e2e suite "to verify." Real e2e is OpenCode-only:
>   `AI_TEAM_ALLOW_OPENCODE_E2E=1 pytest --run-e2e`.
> - **Do NOT run `python main.py status`** — it acquires the gateway lock and KILLS
>   the live PM2 gateway. Check the running gateway with
>   `curl http://127.0.0.1:9003/health` (or a tailscale IP).

---

## Current Focus

*What's active right now.* For per-job status see
[`dispatch/DISPATCH_LOG.md`](dispatch/DISPATCH_LOG.md); for forward priorities see the
**Current Priorities** table below; for who-owns-what-doc see [`DOC_MAP.md`](DOC_MAP.md).

> **✅ FOUNDATION DEFECT RESOLVED (2026-07-11 audit → M2.5 built on `feat/m2.5-case-admission`).**
> The per-turn Case shatter + `Task finished == Case completed` defect is fixed by **A36** (Case
> admission: a turn attaches to the session's open Case or runs Case-less; Cases born only via
> `open_case`/dispatch) + **A37** (task-end is task-only, no auto stage-stamps or auto-close;
> authoritative `close_case` with open-child/approval/`completion_criteria` guards). It was a
> **writer-policy bug over a sound substrate** — M1/M2 stayed valid, nothing rolled back. Flag OFF
> ⇒ byte-identical. **Status: MERGED to `main` (`33b3f76`, PR #9, 2026-07-11).** Full audit:
> [`.ai/workflow_architecture_audit.md`](workflow_architecture_audit.md); v0.7 spec
> `docs/Task_Harness_v0.7_AUTOMATION.md`. **M3 Phase 3.1 vertical slice is now BUILT** on
> `feat/m3-phase31-manager-role` (**A38**): canonical Manager-role layer separation (role profile
> `docs/harness/roles/manager.md` / skills-seam / `manager_v1` tools / gateway-state / provider-neutral
> `AgentRoleDefinition`+Claude adapter) + a thin end-to-end path — `POST /api/manager` → `open_case`
> (one Case, `case_role="manager"`) → Manager Session boots with the role prompt via the Claude adapter
> (`system_prompt` preset+append) + per-session tools → worker JOINS the same Case (not a child) →
> completion leaves the Case OPEN → A37 `close_case`. New flag `MANAGER_ROLE_ENABLED` (default OFF ⇒
> byte-identical). 924 tests pass. **Not yet merged (PR #10 open); live proof deferred to the
> combined A35+3.1 operator-gated run.**

> **⚠️ M3 SEQUENCING & KNOWN GAPS (2026-07-12) — read before building further.**
> Phases 3.0 (dispatch/wait plumbing, on `main`) + 3.1 (A38, PR #10) are **built entirely on tests
> — NEVER run live.** Do **NOT** stack 3.2/3.3 before the base is proven: the adversarial pass on
> A38 found two loop-breaking bugs (wait-for-joined-worker, missing close path) that unit tests
> missed. Order:
> 1. **A39 — cheap integration proof (no paid CLI):** `TestClient` over a real `TaskOrchestrator`
>    + real `MeshDB` + a fake `claude` backend, `HARNESS_FLOW_DRIVE`+`MANAGER_ROLE_ENABLED` ON;
>    drive `/api/manager` → dispatch worker with `case_id` → assert one Case (no child), worker
>    task JOINs, `task.finished` on timeline, `wait_for_worker(task_id, flow_run_id=case)`
>    resolves, `close_case` refuses-then-closes. De-risks ~90% of the surface for free.
> 2. **A35+3.1 combined live spike (operator-gated, paid):** the only proof of the real Claude
>    boot (`system_prompt` preset+append + scoped tools) + real dispatch/join/close. Prereq: PR #10
>    merged/deployed.
> 3. **M3.2 = `review.*` verdict emitter (NOT a new role).** The reviewer **IS the Manager**
>    (reviewing is already a shipped Manager duty). The gap: the A38 loop emits **no `review.*`
>    event** when the Manager accepts/reworks — the Case ledger shows dispatch→finished→close but
>    not the *verdict*. 3.2 wires `record_review` → `flow_events` (vocab already reserved in
>    `db.py`) + a close-gate on unresolved rework + an optional distinct *plan*-reviewer pass.
> **The live spike (step 2) does NOT test `review.*`** — expect its absence in the timeline; that
> is by design until 3.2, not a bug.

> **➡️ FORWARD POINTER (2026-07-08): the harness is now being AUTOMATED, and Work
> Control Substrate is the next dependency.** The active
> roadmap is [`docs/Task_Harness_v0.6_AUTOMATION.md`](../docs/Task_Harness_v0.6_AUTOMATION.md)
> (operator-authorized build spec). It promotes the proven manual kernel + the A19
> `flow_runs` record into a gateway-driven, queryable state machine (M0 reconcile → M1
> flow-state machine → M2 dispatch lineage → M3 invoked-Manager → M4 spec layer). The
> prototype-era `docs/harness/promotion_ladder.md` is **RETIRED** by it (§0.3) — do not cite
> its "Phase 2 = NO / deferred / drop" verdicts against v0.6 work. A20–A23 shipped M0/M1.
>
> **➡️ STATUS 2026-07-09: M2 (Work Control Substrate, A25–A30) DONE & merged to `main`
> (`24dff9b`).** `HARNESS_FLOW_DRIVE` is **ON in the live environment** (gateway restarted on the
> merged code) so the substrate now populates from real execution. **M3 is the next milestone and
> is UNBLOCKED** — spec + backend-readiness dossier authored at
> [`docs/M3_MANAGER_INVOCATION_SPEC.md`](../docs/M3_MANAGER_INVOCATION_SPEC.md) (F4 spike answered
> on paper: the dispatch/session/approval/read-model primitives + the MCP-tool-to-session pattern
> all exist; M3 = a new `mcp_manager` tool surface + Manager role wiring + loop guardrails, in 4
> reversible flag-gated phases). **Next unblocked build: M3 Phase 3.0 (F4 spike).**

- **v0.6 M0 + M1 SHIPPED on `main` (2026-07-07).** A20 (M0 base reconcile), then A21→A22→A23
  merged in order (`6fdf8f0` → `56e4180` → `d1ea2f7`): `flow_runs` now carries the full §11
  field set + lineage cols (A21, migration 22 — additive/NULLable/version-guarded idempotent),
  `current_stage` is written at each loop transition behind `HARNESS_FLOW_DRIVE` (**default OFF
  ⇒ byte-identical A19; SHADOW only — nothing reads stage to drive execution**), and the flow
  record is queryable via read-only `GET /api/flows` + `/api/flows/{id}` (A23, auth-guarded,
  loopback/tailnet, no mutation/public bind). 41/41 M1 tests green in-tree. **M2 Work Control
  Substrate (A25–A30) is now DONE & merged (`24dff9b`)** — `flow_links`, append-only `flow_events`,
  write wiring, read model, read-only mobile Work surface, and honest uncapped session affiliations.
  See [`docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md`](../docs/WORK_CONTROL_SUBSTRATE_MILESTONE.md).
  **Next: M3** (Manager-as-invoked-role) — spec at
  [`docs/M3_MANAGER_INVOCATION_SPEC.md`](../docs/M3_MANAGER_INVOCATION_SPEC.md); start with Phase 3.0
  (F4 spike: prove a gateway-spawned session can dispatch a child worker session via `mcp_manager`).
- **⚠️ M2 has TWO coordinated halves — reconciled 2026-07-08 (was an overlapping-lane risk).**
  M2 dispatch lineage = **(A26a)** the `flow_runs` column wiring — `parent_flow_run_id`/
  `dispatched_by`/`dispatch_file` stamped at the child-dispatch seam behind `HARNESS_FLOW_DRIVE`
  (default OFF ⇒ byte-identical), plus the `_stamp_child_dispatch_lineage` **supplier** +
  `list_child_flow_runs` — **now MERGED to `main` (`24dff9b`)** via the substrate branch, which was
  built byte-identically on top of A26a, 19/19 tests green — **and (A25–A30)** the authoritative
  `flow_links`/`flow_events` substrate, also merged.
  **Authority:** `flow_links(child_flow)` is authoritative; the `flow_runs` column is a convenience
  index. **A26 CONSUMES A26a's stamped edge — it must NOT add a second stamping hook** (avoids the
  milestone's F4 "duplicate ledger"). The old roadmap phrase "M2 is wiring-only" (which spawned the
  duplicate lane) is corrected in `Task_Harness_v0.6_AUTOMATION.md` §F3.
- **⚠️ CONCURRENCY (2026-07-08): A25 is being built by another agent** on
  `feat/work-control-substrate` (migration 23 + `flow_links`/`flow_events`). One worker per branch —
  do NOT open a second substrate lane.
- **⚠️ MERGE ORDER (avoid a double-apply): `feat/work-control-substrate` is built ON TOP of the exact
  A26a code** (byte-identical), i.e. it already contains A26a's `_stamp_child_dispatch_lineage` +
  `list_child_flow_runs`. So merging that branch lands A26a + A25+ together. **Do NOT also merge
  `feat/m2-dispatch-lineage-wiring` separately** — it would double-apply A26a and conflict. That
  standalone branch is now the packet/closure/reference home for A26a only.
- **A24 decomposer generator is deferred (2026-07-08).** It remains valid M4 prompt work, but
  decomposition before durable Work/Case linkage creates more loose artifacts. Resume A24 only
  after the Work Control Substrate can attach decomposed packets/tasks to cases.
- **Task harness is COMPLETE and on `main` (one branch).** A13/A14/A15 all merged
  2026-07-03/04. The loop now has: the `docs/harness/` templates + generators, the
  **config map** (`loop_config_map.md` — the knobs), the **doc-structure contract**
  (`DOC_MAP.md`, lean DISPATCH_LOG, one-dispatch-one-file), the **promotion ladder**
  (`promotion_ladder.md` — evidence-gated v0.4 roadmap; 3 of 6 elements are drop-candidates),
  and the **driver** (`manager_invocation.md` — paste this to fire a loop).
- **Proven on docs (A12/A13/A14/A15); NOT yet on a real code task.** The adversarial-review
  / checkpoint half still wants a real *code* diff to validate — the next loop should run
  on a real feature/fix.
- **How to start a loop:** paste `docs/harness/manager_invocation.md`, fill the spec slot.
- **Branch policy (anti-sprawl, 2026-07-06):** the driver no longer reflexively branches.
  **Docs-only loops commit straight to `main`** (no branch/PR/merge); **code loops** cut
  `feat/<loop>-<slug>` and **open a PR at close** (`gh pr create`) — never a dangling local
  branch. See `manager_invocation.md` "Branch policy" + CLOSE step.
- **Newcomer front door shipped (A18) — MERGED to `main`.** `docs/OVERVIEW.md`: a static
  "you are here" router. v0.4 §2.3 human-orientation need, not the deferred wiki renderer.
- **Branch cleanup done (2026-07-06):** merged A18 + A19 to `main`, deleted them + the stale
  `feat/task-harness` (already fully in `main`).
- **⚠️ OPERATOR FORK 1 — `phase1-quota-window-coordinator` (unmerged remote branch).**
  **State VERIFIED in git 2026-07-07:** it is **9 ahead / 2 behind `main`**, **+1773 / −0
  across 6 files** vs its merge-base (`eec87c4`) — a stale *additive* feature branch, NOT the
  "~293-file-deleting" branch an earlier read reported (the only "deletions" are files `main`
  added while it sat behind). It is **salvageable via rebase-to-current, not destructive.**
  **Recommendation:** rebase onto current `main` to salvage its quota-coordinator commits when
  the quota work is scheduled; until then keep it a **separate fork, never entangled with the
  v0.6 flow-machine (M1) work.** Do NOT merge/rebase/delete it now — operator's call.
  (v0.6 §M0 / §4 F1 carry the same verified state; the A20 DISPATCH_LOG row too.)
- **⚠️ OPERATOR FORK 2 — A17 orphan-code drift (`d1556ad`, `AGENT_17_WIP_MERGE_RECONCILE.md`).**
  The "WIP snapshot before main merge" commit landed the reviewed A16 admission-block scope
  (4 files, verified on main) **plus 9 files of undispatched, unreviewed orphan code** — see
  the "Known drift" bullet below for the four clusters. **Recommendation:** retro-dispatch the
  two clusters that fix real bugs (backend-usage peak-vs-sum — already done as A17b; mesh-fleet
  tz/count) through a Level-3 harness loop, and take an explicit keep-with-tests-vs-revert
  decision on `_ActivityForwarder` (live, zero tests) and the `sonnet→opus` default flip.
  Remediation touches worker/mesh code ⇒ **Level-3 fork needing operator approval** — surfaced
  here, not resolved.
- **Known drift (A17 audit, `d1556ad`):** the "WIP snapshot before main merge" commit landed
  the reviewed A16 admission-block scope (4 files, verified on main) **plus 9 files of
  undispatched, unreviewed orphan code** in 4 clusters — **activity-forwarder** (live
  `_ActivityForwarder` @ `src/worker/agent.py:896`, remote-worker path, zero tests),
  **backend-usage-ext** (Codex peak-vs-sum token fix — untested), **mesh-fleet-count**
  (`_count_fleet_nodes` + tz-aware `_mesh_load_stats` fix in `src/control/db.py` — untested),
  and **models-default** (`config/models.py` flips Claude default `sonnet`→`opus`). Well-formed
  and two clusters fix real bugs, but none went through the harness. Not cleanly shipped → not
  a Shipped-Ledger entry. **Remediation (keep+test / retro-dispatch / revert) is a Level-3
  fork needing operator approval** — see `dispatch/AGENT_17_WIP_MERGE_RECONCILE.md`.

## What this project is

A gateway for local coding agents (Claude Code, Codex, OpenCode CLI, OpenCode
server), controlled from its own Web UI (or Telegram). You open a session from
either surface, follow-up messages route to that session, and each turn resumes
the native backend session. State is DB-canonical with a file-backed fallback. It
is **not** a generic autonomous-agent framework — see `production_vision.md` for
the strategic frame and the anti-goals (no opaque memory, no broad self-directed
execution, no PTY-persistence backbone).

Two surfaces over one gateway process:
1. **Web UI** (`web/`, React 19 + Vite + Tailwind v4) — our own primary UI, a
   mobile web app served in-process by `python main.py` at `/` + `/api/*` on one
   tailnet-bound port. It consumes the M1 control contract; no separate core
   refactor.
2. **Telegram** — the original command surface, kept as a secondary interface
   over the same backend.

---

## Current Priorities

Ranked. Pull the next unblocked item; a dispatcher agent should turn these into
job packets in `.ai/dispatch/` and log them in `DISPATCH_LOG.md`.

| Rank | Item | Why it matters | State |
|---|---|---|---|
| **0** | **Gateway restart on `aaf1cb2` (PR #37)** | The `setting_sources=["user","project"]` fix that re-connects the Manager MCP tools is merged but INERT until a restart — a fired Manager currently boots tool-less and cannot dispatch. Unblocks ALL Manager dispatch. | 🟡 **OPERATOR-GATED.** Merged; needs the operator's gateway (+ node-worker) restart. |
| **1** | **Node re-run of A43 — carrier-independent Manager acceptance** (#18) | The one unproven gap: A44 proved the merged code on the in-gateway `__local__` path only. Booting a role-full, tool-full Manager on a real node (`Horse`/`kanebra-worker`) is the acceptance test that survivable automation actually works off the fragile gateway host. | 🟡 **OPERATOR-GATED (paid).** Code merged (#18). Remote-node MCP reachability still deferred (on-box only). This is the highest-signal next validation. |
| **2** | **M3.3 durable relay** — `wait_for_worker` is in-process | Last structural fragility: even a carrier-independent Manager loses its wait if the session/gateway crashes mid-wait. Persist the wait off the `task.finished` timeline so it's recoverable. | ✅ **BUILT 2026-07-23 (PR #38, op-merge).** Flag `DURABLE_RELAY_ENABLED` (default OFF); `worker.wait_pending`/`worker.wait_resolved` on the Case ledger + `reconcile_waits`; 213 pytest green. Live e2e + merge operator-gated. |
| — | ~~Carrier-independent Manager role (#18)~~ | Manager booted only on in-gateway driver. | **MERGED** (PR #18, `main`, 2026-07-14). Dropped `case_role` restored across the dispatch seam. |
| — | ~~Observable worker sessions + node survivability (#19)~~ | Workers were sessionless `run_oneoff`. | **MERGED** (PR #19 + **#23** the 422 `backend` fix, `main` 2026-07-17). §7 close-on-Case-close also MERGED (**PR #22**). **Still NOT proven live** — needs one paid Manager dispatch that actually opens a worker session row (now unblocked). Deferred: live proof, node-default routing, Web UI linkage. |
| — | ~~Timezone → native local everywhere (#20)~~ | Mixed naive/UTC clocks. | **MERGED** (PR #20, `main`). **⛔ TIMEZONE IS STANDARDIZED — do NOT cite tz as a root cause; a wrong time is a writer/render defect, not a UTC-offset to explain away.** |
| — | ~~Persistent multi-Case Manager session (#21)~~ | `close_case` affiliation-clobber race. | **MERGED** (PR #21, `main`). Single-writer clobber fix. |
| — | ~~PR #19 §7 gap: close worker sessions on Case-close~~ | Joined worker sessions lingered open after Case-close, holding a backend slot. | **MERGED** (PR #22, `main` 2026-07-17) **but LIVE-PROVEN INERT for the real observable-session path** (A45 run 2026-07-17): the worker joins the Case as a *task* link, not a `flow_links(session,worker)` row, so `close_case`'s session-link scan never sees it (stray session `717441320dcc` left open). ~~**New follow-up job** — see A45 Finding B in DISPATCH_LOG.~~ **✅ SUPERSEDED 2026-07-21 (verified in merged code):** A47 (PR #25, merged) writes the `flow_links(entity_type='session', role='worker')` graph node on JOIN, so the worker session is now first-class in the Case graph; A48 (PR #26, merged) then **deliberately removed auto-close-on-Case-close** — a joined worker is left **WARM** by design and closed only by the Manager's explicit `release_worker`. So a worker session lingering open after Case-close is now *intended warm-keep*, not a leak. Finding B needs no follow-up job. (Deferred, unchanged: warm workers have no idle-reaper — see the §7 note in the 2026-07-18 STATUS block.) |
| — | ~~M2.5 Case Admission (A36) + Continuity/Closure (A37)~~ | Per-turn Case shatter + task-end auto-close defect. | **merged** (PR #9, `main`). |
| — | ~~A43 live rework-cycle proof + restart hardening~~ | Prove `rework_requested→re-dispatch→accept` live; ship the restart-incident fix. | **done + merged** (PR #17, `main`, 2026-07-13). First live rework cycle. |
| — | ~~Build Task Harness Workflow Kernel (v1)~~ | Prompt+artifact task-quality loop; addresses the #1 scar (false-success / burned tokens from ungrounded execution). | **merged** (A9H, PR #8) on `main` — see Shipped Ledger + `docs/harness/` |
| — | ~~WebUI-first surfacing of the Level-3 admission block~~ (A9H "Next") | A blocked Level-3 submit must read as "needs approval," not an opaque 500 / stuck session. | **built** (A16) on `feat/harness-block-surface` — awaiting operator merge |

**To run a task through the harness:** start at
[`docs/harness/dispatch_pipeline.md`](../docs/harness/dispatch_pipeline.md)
(pick the level with `docs/harness/level_rubric.md`).

Everything else in the recent dispatch set is **shipped and on `main`**: M1/M2 + M3
observability, Operator Signal (Web Push + Backend Usage), Compact-Context, the
is_error fix. Web Push VAPID setup is **done** (operator, 2026-07-03). See the
Shipped Ledger below and `dispatch/DISPATCH_LOG.md`.

Deferred-but-valid work lives in the two "Deferred" tables at the end of this file.

---

## Shipped Ledger (one line each)

Detailed implementation notes are in git history and the dispatch `*_BUILD_REVIEW.md`
files. This is the "don't rebuild it, it's done" list.

**Mesh / State Separation (on `main`):**
- **State Sep P0–P3** — mesh live across `kanebra` + `Horse`; real two-machine dispatch; gateway-restart reattach delivers real worker results (no fabricated "Task failed").
- **P4 / P4.1 / P4.2** — graceful degradation audited & closed; DB-reconcile spool (`results/reconcile/`) replays failed completions; transition-only `mesh_degraded`/`mesh_restored` events.
- **M5** — `mesh_health_samples` trend ledger (migration 19); `/metrics` + `/api/mesh/health` expose recent trends read-only.
- **T1** auto-deploy (`scripts/auto_deploy.sh`) · **T2** long Telegram output · **T3/T3.1** watched jobs (`jobs` table, `/jobs` API, process-identity probe) · **T4** worker-restart claim reclaim (release + stale-claim reaper).
- **Cockpit M1** — backend `registry.py`, `SessionService` (create/bind), `SessionOrigin` (migration 12), `docs/CONTROL_CONTRACT.md`. Telegram byte-identical.

**Conversation/artifacts DB-canonical (2026-06-30):**
- Migration 17 — `mesh_tasks` holds full untruncated reply + prompt + parsed_output + file_changes + usage. Chat + Files/Info tabs read DB-first; `results/*.json` are fallback/debug only and droppable. Backfill done (`scripts/backfill_conversation_turns.py --verify`). Audit: `docs/CONVERSATION_DATA_FLOW.md` §0.

**Session/task state truth (2026-07-01):**
- `src/core/task_state_truth.py` derives honest state from DB rows + worker live_state + node incarnation + telemetry + stale evidence. `/api/sessions/{id}/timeline` exposes the bounded durable sequence; Session Detail renders stale/unknown/detached/recovered explicitly.

**Web UI ladder (merged from `feat/webui-ui0`, PR #4):** every rung M1 → UI-6 shipped (PWA + service worker + manifest). Plus the operator UX fixes:
- **#34** Stop Task = run outcome, not lifecycle (cancelled sessions stay resumable) · **#36** Tasks page removed, jobs render in Session Detail · **#37** job/task history owned by durable timeline API · **#38** System tab is infra-focused · **#39** honest worker/session state reporting.

**Operator Signal (merged from `feat/operator-signal`, PR #5):**
- **#21** Web Push (migration 20, `push_service.py`, SW handlers; notification fan-out only, NOT approval-gated). **VAPID env configured 2026-07-03 — push is live.** · **#30/#33** Backend Account + Usage Visibility (`backend_usage.py`, `/api/backends/usage`; honesty-first — unknown limits return `null` + reason, never fabricated).

**Task Harness Workflow Kernel v1 (A9H + A12 — MERGED to `main` via PR #8, `fd90a46`):**
- Prompt-and-artifact task-quality loop under `docs/harness/` — templates (packet
  XML, milestone, level rubric, README), DRAFT/REVIEW/CLOSE generators, and the
  `dispatch_pipeline.md` runbook. **Zero new gateway state** (spec §0). **Level-3
  admission gate on the HOT path** (build-review B1 follow-up, Option 3):
  `_harness_level3_allows_autopickup` runs in `orchestrator._enqueue_task` — the
  choke point every ingestion lane shares (Telegram/Web `submit_instruction`,
  `.task.md`, internal). Blocked ⇒ raises `HarnessAdmissionBlocked` (no faked
  task_id / no side effect); control API → 409, Telegram → approval reply.
  Flag-guarded `HARNESS_LEVEL3_GUARD`, OFF by default → byte-identical legacy when
  the field/flag is absent. 35 harness/compact + 51 control-API + 24 telegram tests
  green. Spec `docs/Task_harness_workflow.md` §13 ticked.
- **Harness self-test (A12, `feat/task-harness`)** — ran one real task through the
  §14 loop by hand to prove the operating model works before any Phase-2 build.
  `dispatch_pipeline.md` now carries a two-lane scope banner + a copyable all-7-stage
  worked example (real packet/milestone/F-tags/closure). Friction report verdict:
  **Phase 2 NOT justified** — file/dispatch discipline held; see `AGENT_12_HARNESS_SELFTEST.md`.
- **⚠️ A19 FlowRun record — Phase-2 `flow_runs` shipped under an OPERATOR OVERRIDE —
  MERGED to `main` (`0b6b1ec`, 2026-07-05).** Migration 21 + a
  5-col `flow_runs` table + `create/update/list_flow_runs` + a best-effort orchestrator
  write hook in `_enqueue_task` + `tests/test_flow_runs.py` (39 passed w/ regressions).
  It is a **RECORD, not a stage machine** — nothing reads `current_stage`; existing task
  execution is untouched and it is trivially revertible. **The promotion-ladder Row 1
  trigger was NOT observed** — this is an operator-directed experiment, recorded as such
  in `docs/harness/promotion_ladder.md`. Do NOT treat the table's existence as a tripped
  gate. First real *code* loop driven through the harness (`AGENT_19_FLOW_RUNS_RECORD.md`).
- **A16 admission-block surfacing (built, `feat/harness-block-surface`, awaiting merge)** —
  the Web `/api/instructions` lane now catches `HarnessAdmissionBlocked` → **409**
  (`reason=harness_level3_needs_approval` + human `detail` + `task_id`) instead of an
  opaque 500; `session_service.mark_idle` reverts the optimistically-BUSY session; the
  Composer renders the approval-needed copy, not "tap send to retry". Gate predicate
  untouched; guard OFF ⇒ byte-identical. ZERO new gateway state. See
  `dispatch/AGENT_16_HARNESS_BLOCK_SURFACE.md`. Telegram surfacing + approve-from-UI
  remain out of scope (deferred).

**Compact-Context (merged from `feat/compact-context`, PR #6):**
- **#31/#32** `load_compact_context` wired via opt-in `continues: <task_id>` frontmatter → `process_task` prepends bounded, fence-hardened `<prior_context>` block. No new gateway state. Docs: `docs/Task_harness_workflow.md` §7/§14.

**LLM Turn Observability:**
- **M1/M2** — **SHIPPED** (2026-07-03). Local Codex smoke + controlled mesh smoke passed 2026-07-02; SQLite benchmarks passed (#8). Spec: `docs/LLM_TURN_OBSERVABILITY_SPEC.md`. **#9 gateway-routed mesh smoke — CLOSED (2026-07-03, run from kanebra):** `task_35655be9`, `gateway_node_id=kanebra`, `execution_node_id=Horse`, non-null and distinct; privacy scan zero-hit; no `affinity_unrouted`. Control API (`:9003`) is loopback-only on kanebra — run §T1-style checks from kanebra itself, not from a worker box. Detail in `dispatch/AGENT_10_M3_CLAUDE_TELEMETRY.md` (T1 log) + `DISPATCH_LOG.md`.
- **M3** — Claude stream-json telemetry adapter **merged on `main`** (commit `c168028`, A10) and **verified live** (2026-07-03) on the canonical worker-agent/SDK-driver path (`task_bfe8c90b`, `task_f89edffb`) — real `model.request.usage` + coverage events land in `llm_events` for real Claude turns, not just fixtures. Unit test suite was previously vacuously green (missing fixtures, assertions on a nonexistent `send_batch` method) — fixed, 18/18 now genuinely pass. **M4** (OpenCode) deferred.
- **Fix (merged a3f734b)** — SDK `is_error` result no longer stored as a successful "Prompt is too long" reply; salvaged work + honest failure delivered instead. Memory `claude-iserror-prompt-too-long`.

---

## Architecture — as it runs today

**One process** (`ai-team-gateway`, PM2). When `MESH_ENABLED=true` it also hosts
the task server embedded on its own event loop.

```
[Web UI] / [Telegram] → [Gateway process]
  ├── src/telegram/interface.py     secondary command surface (/status, /nodes, pickers…)
  ├── src/orchestrator.py           task queue, in-process workers, routing, recovery
  ├── src/core/session_service.py   transport-neutral session lifecycle — M1 inbound seam
  ├── src/services/session_store.py DB-first reads, dual-write to JSON + DB
  ├── src/control/db.py             SQLite mesh DB (WAL, busy_timeout=5000, migrations)
  ├── src/control/embedded_server.py task server, embedded (mesh on)
  ├── src/control/{task_server,node_registry}.py  HTTP API + node registry
  ├── src/worker/agent.py           worker daemon — own process on worker nodes (e.g. Horse)
  └── src/backends/                 claude_code, codex, opencode, opencode-server (declared in registry.py — M1)
```

**Control contract (M1):** the inbound/outbound boundary is `docs/CONTROL_CONTRACT.md`
— event envelope + catalog, the **two** inbound entry points
(`SessionService.create_session/bind_active`; `orchestrator.submit_instruction`),
`SessionOrigin`, backend extension via `registry.py`, and the `db.list_*` read model.

**Mesh (LIVE since 2026-06-11):** gateway + embedded task server on **Pi5 (`kanebra`)**;
worker daemon on **`Horse`**. Tasks dispatch machine-to-machine and survive a gateway
restart end-to-end. `MESH_ENABLED=false` ⇒ gateway is byte-for-byte the old behavior.

State layout:
```
state/sessions/<id>.json              session records (legacy-authoritative, dual-written, NEVER deleted)
state/telegram/active_bindings.json   chat_id → session_id
state/summaries/<id>.md               per-session summary
state/mesh.db                         SQLite — read-first; CANONICAL for conversation + artifacts (migration 17); mesh_health_samples (migration 19); push_subscriptions (migration 20)
results/<task_id>.json                task artifact — FALLBACK/debug only (DB-canonical since 2026-06-30); droppable
results/reconcile/<task_id>.json      DB-reconcile spool; replayed on startup / next DB-available completion
results/raw/<task_id>.ndjson.gz       gzipped raw_stdout debug stream (when system.slim_artifacts=on)
logs/session_events/<id>.log          per-session NDJSON
logs/events.ndjson                    system-wide event log
```

**Config flags that matter:** `MESH_ENABLED` (default `false`), `MESH_SHADOW_WRITE`
(default `true`), `WORKER_TOKEN`, `MESH_TAILSCALE_IP`, `MESH_TASK_SERVER_PORT` (9002).

---

## Architecture rules (do not violate)

- DB is the canonical **read** source. `state/sessions/<id>.json` dual-write is the
  ultimate session fallback and is **never deleted**. `results/task_*.json` are NO
  LONGER a source — `mesh_tasks` holds the full conversation + artifact data (migration
  17); the fat files are droppable (see `docs/RUNBOOK_db_self_sufficient.md`).
- The gateway host keeps its **own embedded worker capacity** (configurable pool,
  default ≥1 — **not** capped at 1) that runs tasks when no remote node is available.
  Prefer remote nodes when online. **This applies to UNPINNED work only** — see the
  two-class rule below.
- **Two task classes, two routing policies (A11/A18 — do not conflate):**
  - **Unpinned** (`session.machine_id` empty / `machine_id IS NULL`): may run
    anywhere. Local embedded capacity is a legitimate fallback when no remote node
    is online. *Unchanged.*
  - **Pinned** (`session.machine_id = <node>`): **host-or-nothing.** The turn runs on
    its pinned host or does not run. Fallback for a pinned turn means **wait / requeue /
    operator re-pin — never relocate** to a substitute host (that silently forks the
    conversation; `backend_session_id` is machine-local). The embedded worker pool must
    NEVER claim a pinned task (enforced by the mesh claim filter in `db.py` + a
    defense-in-depth assert at dispatch in `_process_task_remote`).
- Session affinity is a hard correctness requirement: a session pinned to a machine
  must execute on that machine. `backend_session_id` is machine-local.
- **Pinned-node-offline handling (A18):** when a pinned node is offline at dispatch,
  the gateway holds the session in `PAUSED_PINNED_NODE_OFFLINE` and polls liveness for
  up to `MESH_AFFINITY_OFFLINE_GRACE_SEC` (default **0 = disabled** ⇒ legacy immediate
  ERROR). If the node returns within the window the turn dispatches normally; if the
  window expires the session ends in the honest, resumable `PINNED_NODE_OFFLINE` state
  (retry or operator re-pin) — never a bare ERROR, never an off-host run.
- `MESH_ENABLED=false` ⇒ gateway is byte-for-byte the old behavior.
- No uncontrolled autonomous behavior. Ollama is optional/helper-only. Per-turn audit
  data (full reply, files changed, usage) is **mandatory** — it lives canonically in
  `mesh_tasks`.

---

## Key files

| Path | Purpose |
|:-----|:--------|
| `src/orchestrator.py` | runtime, task queue, workers, routing, recovery, mesh hooks |
| `src/core/session_service.py` | transport-neutral session lifecycle (create/bind) — M1 inbound seam |
| `src/core/task_state_truth.py` | honest task/job state read-model |
| `src/backends/registry.py` | single declaration site for the backend set — M1 |
| `src/services/session_store.py` | DB-first session reads + JSON/DB dual-write |
| `src/control/db.py` | SQLite mesh DB — canonical DB layer |
| `src/control/task_server.py` | FastAPI task server (embedded); `/metrics.history.recent` = M5 health samples |
| `src/worker/agent.py` | worker daemon (own process on worker nodes) |
| `config/settings.py` | all config incl. `MeshConfig` |
| `docs/ENV_FEATURE_FLAGS.md` | feature-flag reference — default-OFF gates that must be enabled separately (incl. M1 `HARNESS_FLOW_DRIVE`) |
| `docs/CONTROL_CONTRACT.md` | **M1** — event + inbound-command + backend + read-model contract |
| `docs/CONVERSATION_DATA_FLOW.md` | conversation+artifact data-flow audit (§0 = DB-canonical, migration 17) |
| `docs/RUNBOOK_db_self_sufficient.md` | backfill `mesh_tasks` + drop fat `results/*.json` |
| `docs/LLM_TURN_OBSERVABILITY_SPEC.md` | turn-observability spec (M1–M4) |
| `docs/Task_harness_workflow.md` | task-quality loop spec (v0.5) — A9H |
| `docs/Task_Harness_v0.6_AUTOMATION.md` | automation roadmap (M0–M4) — the active harness spec |
| `docs/PRIOR_ART_MAX_REUSE.md` | salvage map of the retired MAX orchestrator — idea-only inputs to harness M3/M4 (decomposer prompt + `SubTask` DAG shape); mirror & warning, not a platform |
| `docs/harness/` | task-harness v1: templates, generators, `dispatch_pipeline.md` runbook |
| `docs/RUNBOOKS/PHASE_4_RUNBOOK.md` | VPS cutover runbook (= State Sep end-state) |
| `docs/archive/progress/_archive_PROGRESS_LOG.md` | completed-work history |
| `ecosystem.config.js` | PM2 supervisor config |

---

## Deferred — Web UI / Cockpit track (`docs/DEFERRED.md`)

| # | Task | Notes |
|---|---|---|
| 22 | Token streaming (`message.delta`) | ⛔ DROP — timeline shows per-turn summary |
| 23 | Diff hunks / file-content preview | no backend source |
| 24 | Terminal / raw stdout-stderr stream | out (security) |
| 25 | Approvals automation | durable gate exists but inert; auto-emit belongs to a future workflow-automation track ("needs to be thought out better" — operator 2026-06-24). **Do not extend the H/UI-3 gate.** |
| 35 | Per-project "Current Focus" panel | reads CONTEXT.md as source of truth | DEFER until workflow settled |

## Deferred — runtime / lower priority

- Backend lifecycle hooks (session-ID detection, PreToolUse security, PostToolUse quality gates) — `docs/TBD/BACKEND_HOOKS_STRATEGY.md`.
- Codex end-to-end validation.
- OpenCode server cross-machine sessions (needs shared DB mount).
- Postgres migration — trigger: >5 nodes or observed SQLite write contention.
- **M-Mesh** (distributed event bus, shared state store, leader election) — "DO NOT build until the app is operable."
- **ACP / A2A bridges**, **Supervisor agents & workflow engine**, **Transport/role/prompt/tool registries**, **Native mobile** — all deferred from the cockpit spec; no consuming surface yet.
