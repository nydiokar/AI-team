# Documentation Index

The complete catalog of `docs/` — every file, grouped by what it's *for*, tagged
with whether it's current. This is the exhaustive reference; if you just landed in
the repo, start at [`OVERVIEW.md`](OVERVIEW.md) instead (a short front door that
routes to the handful of docs a newcomer actually needs).

**Scope note:** this indexes `docs/` only. Live project state (current focus,
priorities, shipped ledger, dispatch status) lives in `.ai/` — see
[`.ai/DOC_MAP.md`](../.ai/DOC_MAP.md) for that boundary. Nothing below duplicates
`.ai/` content; where a doc's status is really an `.ai/CONTEXT.md` fact (e.g. "is
this flag on"), that's noted but not restated in full.

**Status legend:** 🟢 current — the live doc for its topic · 🟡 superseded — kept
as trace/history, don't build against it · 🔵 planning — spec/proposal, not yet
(fully) built · ⚪ archived — retired, historical record only.

---

## Start here

| Doc | Status | What it's for |
|---|---|---|
| [`OVERVIEW.md`](OVERVIEW.md) | 🟢 | Front door — "you are here" + routing table. Not a source of truth itself. |
| [`README.md`](README.md) | 🟢 | Repo README: product summary, commands, config, links out to canonical docs. |
| [`QUICK_START.md`](QUICK_START.md) | 🟢 | Install + first run. |
| [`ROADMAP.md`](ROADMAP.md) | 🟢 | Pointer only — the real roadmap lives in `.ai/CONTEXT.md`. Kept so links from `docs/` land somewhere correct. |

## Architecture & contracts

Durable descriptions of how the system is built and the boundaries other code must
respect. Backend and frontend each have their own folder and front door.

### `docs/backend/` — the gateway backend (processes, APIs, contracts, data)

| Doc | Status | What it's for |
|---|---|---|
| [`backend/INDEX.md`](backend/INDEX.md) | 🟢 | Front door for backend docs — start here before the individual files below. |
| [`backend/ARCHITECTURE.md`](backend/ARCHITECTURE.md) | 🟢 | Process/deployment topology (PM2 + Docker), the Control API (`:9003`) and mesh task-server (`:9002`) route maps, incl. the Manager/Case surface. Keep current when routes/processes change. |
| [`backend/CONTROL_CONTRACT.md`](backend/CONTROL_CONTRACT.md) | 🟢 | The M1 inbound/outbound contract — event envelope, entry points, backend registry, read model. Read before adding a second surface or a new backend. |
| [`backend/CONVERSATION_DATA_FLOW.md`](backend/CONVERSATION_DATA_FLOW.md) | 🟢 | Where conversation/artifact data lives and how it flows; §0 documents the DB-canonical migration (2026-06-30). |
| [`backend/DATABASE_AUTHORITY.md`](backend/DATABASE_AUTHORITY.md) | 🟢 | Controller vs. worker DB authority (A88, merged PR #180): the controller owns `mesh.db`, workers read controller state over the task-server API. |
| [`backend/ENV_FEATURE_FLAGS.md`](backend/ENV_FEATURE_FLAGS.md) | 🟢 | Complete inventory of default-OFF feature flags. Check here before assuming a built feature is live. |
| [`backend/MESH_SECURITY.md`](backend/MESH_SECURITY.md) | 🟢 | Mesh trust domain + threat model: what the operator is trusted with, what a leaked token means, how to respond. |
| [`backend/CODEX_APP_SERVER_ADAPTER_CONVERGENCE.md`](backend/CODEX_APP_SERVER_ADAPTER_CONVERGENCE.md) | 🟢 | Closed convergence record + code boundary of the single Codex backend (app-server RPC) and its runtime-recycle procedure. |
| [`backend/QUOTA_WINDOW_COORDINATOR_PHASE1.md`](backend/QUOTA_WINDOW_COORDINATOR_PHASE1.md) | 🟢 | Architecture of the observe-only quota window coordinator (A61 baseline). |

### `docs/frontend/` — the Web UI (`web/`), frontend-only docs

| Doc | Status | What it's for |
|---|---|---|
| [`frontend/INDEX.md`](frontend/INDEX.md) | 🟢 | Front door for `web/` docs — start here before the individual files below. |
| [`frontend/OVERVIEW.md`](frontend/OVERVIEW.md) | 🟢 | Stack, the domain/transport/adapter layering rule, directory map, state model, routing. |
| [`frontend/DATA_FLOW.md`](frontend/DATA_FLOW.md) | 🟢 | Polling vs. SSE, the two-timelines split, write/idempotency, the Work/Case read model. |
| [`frontend/SCREENS_AND_COMPONENTS.md`](frontend/SCREENS_AND_COMPONENTS.md) | 🟢 | Route-by-route + component-group tour. |
| [`frontend/DEV_AND_BUILD.md`](frontend/DEV_AND_BUILD.md) | 🟢 | Running dev, testing, building, how the gateway serves the build, PWA notes. |

## Specs — task harness (the automation loop)

Version chain — **read `Task_Harness_v0.7_AUTOMATION.md` for current state**; the
others are kept as trace, not duplicated history.

| Doc | Status | What it's for |
|---|---|---|
| [`Task_Harness_v0.7_AUTOMATION.md`](Task_Harness_v0.7_AUTOMATION.md) | 🟢 | **Current automation roadmap.** Supersedes v0.6 (adds Case Admission/M2.5). |
| [`Task_Harness_v0.6_AUTOMATION.md`](Task_Harness_v0.6_AUTOMATION.md) | 🟡 | Superseded by v0.7 — kept verbatim as trace, do not delete or build against. |
| [`Task_harness_workflow.md`](Task_harness_workflow.md) | 🟢 | v0.5 kernel spec — the quality-loop discipline (artifacts, roles, gateway-state fields). Still governs loop mechanics; v0.6/v0.7 are the automation layer on top. |
| [`Task_Harness_v0.4.md`](Task_Harness_v0.4.md) | 🟡 | Original v0.4 kernel spec — superseded in substance by v0.5, kept as origin trace. |
| [`WORK_CONTROL_SUBSTRATE_MILESTONE.md`](WORK_CONTROL_SUBSTRATE_MILESTONE.md) | 🟢 | M2 milestone record — shipped & merged; describes `flow_links`/`flow_events`. |
| [`M3_MANAGER_INVOCATION_SPEC.md`](M3_MANAGER_INVOCATION_SPEC.md) | 🟢 | M3 (Manager-as-invoked-role) spec + backend-readiness dossier. Check `.ai/CONTEXT.md` for build progress against this spec. |
| [`AUTONOMOUS_CASE_CONTINUATION_DESIGN.md`](AUTONOMOUS_CASE_CONTINUATION_DESIGN.md) | 🟢 | M3.4 design detail — wait-group state machine, delta, acceptance test. Job 1 merged (A52); A54/A55 (reconstruction, crash-respawn) still open. Its wait-group half is challenged by `TBD/WORKER_COMPLETION_NOTIFY_REDESIGN.md`. |
| [`PERSISTENT_MANAGER_LOOP_ANALYSIS.md`](PERSISTENT_MANAGER_LOOP_ANALYSIS.md) | 🟡 | Opening analysis for M3.4. §0–§4 still valid; §5–§7 superseded by the continuation design above (see its banner). |
| [`SPEC_COMPLETION_PLAN.md`](SPEC_COMPLETION_PLAN.md) | 🟡 | Ordered v0.7 backlog as of 2026-07-30. Forward priorities now live in `.ai/CONTEXT.md` — use this only for the dependency reasoning. |
| [`MANAGER_CONTEXT_CONTINUITY_SPEC.md`](MANAGER_CONTEXT_CONTINUITY_SPEC.md) | 🔵 | Proposed context-pressure rollover for long-running Managers (generalizes crash-respawn). Not built; flag-gated, default OFF. |
| [`SKILLS_LIBRARY_O1.md`](SKILLS_LIBRARY_O1.md) | 🟡 | **Superseded 2026-10-06** by native project skills in `.claude/skills/`. A76's `skills/` text-expansion slice and `SKILLS_LIBRARY_ENABLED` are removed. |
| [`SYSTEM_ONE_DECISION_LAYER_SPEC.md`](SYSTEM_ONE_DECISION_LAYER_SPEC.md) | 🔵 | System-One (TypeSafe Jev) calibrated decision layer — accepted 2026-10-05; build dispatched as A94 → A95 → A96. |
| [`PEER_MESSAGING_INVESTIGATION.md`](PEER_MESSAGING_INVESTIGATION.md) | 🔵 | A68 design recommendation for a durable, authority-free agent-to-agent message primitive. No implementation. |
| [`PRIOR_ART_MAX_REUSE.md`](PRIOR_ART_MAX_REUSE.md) | 🔵 | Advisory salvage map — ideas mined from the retired MAX orchestrator for harness M3/M4. Not a build surface itself. |

### `docs/harness/` — the loop's own operating docs (templates, generators, runbook)

| Doc | Status | What it's for |
|---|---|---|
| [`harness/README.md`](harness/README.md) | 🟢 | What the harness is (prompt-and-artifact loop, zero new gateway state) and how the pieces fit. |
| [`harness/dispatch_pipeline.md`](harness/dispatch_pipeline.md) | 🟢 | The end-to-end runbook — how a task moves from idea to executed change. Start here to run a loop. |
| [`harness/level_rubric.md`](harness/level_rubric.md) | 🟢 | Deterministic checklist for picking harness level 0–3. |
| [`harness/loop_config_map.md`](harness/loop_config_map.md) | 🟢 | The loop's control surface — every configurable knob, who drives it, what file programs it. |
| [`harness/operating_model.md`](harness/operating_model.md) | 🟢 | How the loop is actually run in practice; wins over the spec where they differ on *how*, not *discipline*. |
| [`harness/roles/manager.md`](harness/roles/manager.md) | 🟢 | Canonical, provider-neutral Manager role profile — stable identity/authority, loaded verbatim into the system prompt at session boot. |
| [`harness/roles/worker.md`](harness/roles/worker.md) | 🟢 | Canonical Worker role profile — mirrors `manager.md`. |
| [`harness/FLOW_MAP.md`](harness/FLOW_MAP.md) | 🟡 | Historical v0.6 task-flow state-machine snapshot; its “NOT YET” labels are not current status. Use the Manager role, pipeline, and CONTEXT for the live loop. |
| [`harness/milestone_template.md`](harness/milestone_template.md) | 🟢 | Template for the current inline `## Milestone (burndown)` checkbox section. |
| [`harness/packet_template.md`](harness/packet_template.md) | 🟢 | Current free-prose dispatch-packet template, grounded in post-A29 packets. |
| [`harness/generators/draft_packet.md`](harness/generators/draft_packet.md) | 🟢 | DRAFT generator — intent → current free-prose dispatch packet. |
| [`harness/generators/adversarial_review.md`](harness/generators/adversarial_review.md) | 🟢 | REVIEW generator — adversarial pass, F-tag convention still followed. |
| [`harness/generators/closure_summary.md`](harness/generators/closure_summary.md) | 🟢 | CLOSE generator — closure summary + doc-update stub, still broadly accurate. |
| [`harness/generators/spec_authoring.md`](harness/generators/spec_authoring.md) | 🟢 | SPEC generator (M4, A56) — feature intent → authored spec + rubric-scored review gate. |
| [`harness/generators/decomposer.md`](harness/generators/decomposer.md) | 🟢 | DECOMPOSE generator (M4, A56) — approved objective → dependency-linked task-DAG inside one Case. |

**Retired and removed (2026-08-01, A64 cleanup):** `harness/manager_invocation.md` (legacy
paste-driver, fully superseded by `harness/roles/manager.md` + live `/api/manager` role-boot, zero
code references) and `harness/promotion_ladder.md` (self-marked retired 2026-07-06, superseded by
v0.6 automation; the file said the operator may delete it). Both existed only in git history now.

## Specs — session/state timeline

| Doc | Status | What it's for |
|---|---|---|
| [`SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md`](SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md) | 🟢 | Adversarial review of Web UI session/job/task/artifact/telemetry state honesty (2026-07-01). |
| [`SESSION_STATE_TIMELINE_EXECUTION_PLAN.md`](SESSION_STATE_TIMELINE_EXECUTION_PLAN.md) | 🟢 | Implementation-ready roadmap that followed the review above. Cross-check `.ai/CONTEXT.md` Shipped Ledger for what's actually landed. |
| [`LLM_TURN_OBSERVABILITY_SPEC.md`](LLM_TURN_OBSERVABILITY_SPEC.md) | 🟢 | Turn-observability/usage-accounting spec (M1–M4). M1/M2/M3 shipped per `.ai/CONTEXT.md`; M4 (OpenCode) deferred. |
| [`SESSION_CACHE_HEARTBEAT_SPEC.md`](SESSION_CACHE_HEARTBEAT_SPEC.md) | 🟢 | Session-keyed Claude Code prompt-cache heartbeat for durable long waits. Built (A80, PR #111); act-mode flags default OFF. Open follow-ups: `.ai/CONTEXT.md` "Deferred — A80". |
| [`WORKER_CACHE_HEARTBEAT_EXTENSION.md`](WORKER_CACHE_HEARTBEAT_EXTENSION.md) | ⚪ | Rejected 2026-09-22 — extending the heartbeat to parked worker sessions. Kept for the reasoning only. |
| [`SESSION_TURN_QUEUE_DESIGN.md`](SESSION_TURN_QUEUE_DESIGN.md) | 🔵 | Unified durable turn queue on `mesh_tasks` (A82). R0 merged + deployed 2026-10-02 with flags OFF; Stage 8 enrollment pending — see the A82 packet. |
| [`TBD/SESSION_WAIT_STATE_GRANULARITY.md`](TBD/SESSION_WAIT_STATE_GRANULARITY.md) | 🟢 | Session state legibility — primary status + derived secondary reason. Shipped as A83 (`7c5c100`) despite the file's "not built" header and `TBD/` location. |
| [`EVENT_DRIVEN_READ_REFRESH.md`](EVENT_DRIVEN_READ_REFRESH.md) | 🟢 | A81 (PR #142): Web UI read models refresh on SSE events instead of redundant 3–5 s polls. |
| [`session_kept_pins_design.md`](session_kept_pins_design.md) | 🟢 | "Keep" mark on a chat session (survives close/restart, searchable note). Built — `POST /api/sessions/{id}/keep`. |
| [`SESSION_WINDOW_WARMING_SPEC.md`](SESSION_WINDOW_WARMING_SPEC.md) | 🟢 | Quota window coordinator spec — implemented 2026-08-19; §19 records divergences. Architecture summary: `backend/QUOTA_WINDOW_COORDINATOR_PHASE1.md`. |
| [`DEFERRED.md`](DEFERRED.md) | 🟢 | Web UI/Cockpit items deliberately not built, with why. |

## Runbooks — operational procedures

| Doc | Status | What it's for |
|---|---|---|
| [`RUNBOOKS/OPERATIONS_PM2.md`](RUNBOOKS/OPERATIONS_PM2.md) | 🟢 | PM2 supervision: the canonical native worker ("Native Worker" section) and the PM2 controller mode. |
| [`RUNBOOKS/OPERATIONS_DOCKER.md`](RUNBOOKS/OPERATIONS_DOCKER.md) | 🟢 | Docker Compose control plane (gateway + task-server). Its containerized-worker sections are non-canonical (see its scope note). |
| [`RUNBOOKS/CONTROL_SURFACE_DEPLOY_RUNBOOK.md`](RUNBOOKS/CONTROL_SURFACE_DEPLOY_RUNBOOK.md) | 🟢 | Deploying the unified gateway (Telegram + Web on one process). |
| [`RUNBOOKS/PHASE_4_RUNBOOK.md`](RUNBOOKS/PHASE_4_RUNBOOK.md) | 🔵 | VPS cutover runbook — migrate control plane off this PC. Not executed yet. |
| [`RUNBOOK_db_self_sufficient.md`](RUNBOOK_db_self_sufficient.md) | 🟢 | Procedure to migrate conversation/artifact data into `mesh.db` and drop fat `results/*.json`. Migration itself is done; kept as the reversibility procedure. |
| [`MESH_NODE_CREDENTIALS_ROLLOUT.md`](MESH_NODE_CREDENTIALS_ROLLOUT.md) | 🔵 | Step-by-step rollout + rollback for per-node mesh credentials. Runs only after A71 merges; A71 is not built yet. |
| [`INCIDENTS/HORSE_WORKER_RESTARTS.md`](INCIDENTS/HORSE_WORKER_RESTARTS.md) | 🟢 | Append-only incident ledger for `ai-team-worker` self-restarts on node Horse (sessions marked `lost`). Add every new occurrence. |

### Docker deployment design

| Doc | Status | What it's for |
|---|---|---|
| [`DOCKER_WORKER_HOST_INTEGRATION.md`](DOCKER_WORKER_HOST_INTEGRATION.md) | 🟢 | Canonical Docker worker/host integration architecture (locked 2026-09-25); live acceptance operator-gated. |
| [`WORKER_CONTAINER_ACCEPTANCE.md`](WORKER_CONTAINER_ACCEPTANCE.md) | 🔵 | A85 container acceptance baseline: static invariants proven, executable gate deferred to a docker-capable host. |
| [`DEPLOYMENT_DOCKER_DESIGN.md`](DEPLOYMENT_DOCKER_DESIGN.md) | 🟡 | Original production Docker design. Still valid for the control plane; superseded for the worker, which stays native under PM2. |

## Reference / schema

| Doc | Status | What it's for |
|---|---|---|
| [`schema/results.schema.json`](schema/results.schema.json) | 🟢 | JSON schema for task result artifacts. |
| [`adr/0001-canonical-sdk-driver-for-agent-spawn.md`](adr/0001-canonical-sdk-driver-for-agent-spawn.md) | 🟢 | ADR-0001 (accepted 2026-07-22): agents always spawn on the persistent `ClaudeSDKClientDriver`, never the CLI driver. |
| [`REPO_READABILITY_O4.md`](REPO_READABILITY_O4.md) | 🟢 | A77 measurement + rationale behind the ctags symbol index (`scripts/repo_index/symbol_lookup.py`) that agents use to orient cheaply. |
| [`dictionary/words_&_relations.md`](dictionary/words_&_relations.md) | 🔵 | Working glossary — Case/Task/Session/Event/Artifact vocabulary and the Manager/Skill/Tool layering. Not yet cross-linked from other specs; treat as draft until reconciled with `harness/roles/manager.md` and the M3 spec. |
| [`cost_monitoring_audit.md`](cost_monitoring_audit.md) | 🟢 | A65 Phase-0 truthfulness audit of the cost telemetry the Cost read-model/dashboard is built on: real-usage provenance, codex `includes_cache` double-count, 51% unpriced share, standalone-session dominance, attribution gaps, `total`-definition bug. Update as the A65 read-model lands. |

## TBD — proposals, not committed work

Ideas and analyses that haven't been scheduled. See `.ai/CONTEXT.md` "Deferred"
tables for the authoritative prioritization; these are the supporting writeups.

| Doc | Status | What it's for |
|---|---|---|
| [`TBD/BACKEND_HOOKS_STRATEGY.md`](TBD/BACKEND_HOOKS_STRATEGY.md) | 🔵 | Whether backend lifecycle hooks (Claude Code/Codex/OpenCode) can replace/supplement gateway state management. |
| [`TBD/CLAUDE_HOOK_IDEAS.md`](TBD/CLAUDE_HOOK_IDEAS.md) | 🔵 | Claude Code hooks as a leverage point for deterministic lifecycle behavior. |
| [`TBD/SESSION_WINDOW_WARMING_SPEC.md`](TBD/SESSION_WINDOW_WARMING_SPEC.md) | 🟡 | Older "proposal only" copy of the quota window coordinator spec. The implemented, maintained version is [`SESSION_WINDOW_WARMING_SPEC.md`](SESSION_WINDOW_WARMING_SPEC.md). |
| [`TBD/AI_TEAM_RUNTIME_UPDATE_AUTOMATION_SPEC.md`](TBD/AI_TEAM_RUNTIME_UPDATE_AUTOMATION_SPEC.md) | 🔵 | Operator-gated Codex/Claude runtime-update automation (replaced `BACKEND_RUNTIME_RELEASES.md` on 2026-09-25). A86 is blocked: workers still run as host processes. |
| [`TBD/WORKER_COMPLETION_NOTIFY_REDESIGN.md`](TBD/WORKER_COMPLETION_NOTIFY_REDESIGN.md) | 🔵 | Proposal: redesign how a Manager learns a worker finished, retiring the wait-group half of the M3.4 design. A84 slice 1 merged; Case-outbox slice open. |

## Archive — retired, historical record only

Superseded plans and completed-phase checklists. Do not build against these;
kept for the decision trail. See `archive/progress/_archive_PROGRESS_LOG.md` for
the narrative history that ties them together.

| Doc | What it was |
|---|---|
| [`archive/progress/_archive_PROGRESS_LOG.md`](archive/progress/_archive_PROGRESS_LOG.md) | Completed-work history log — the narrative index for everything else in `archive/`. |
| [`archive/AGENT_MESH_SPEC.md`](archive/AGENT_MESH_SPEC.md) | Original agent-mesh design (VPS control plane + Tailscale workers). |
| [`archive/STATE_SEPARATION_PLAN.md`](archive/STATE_SEPARATION_PLAN.md) | State Separation plan (P0–P4, now shipped). |
| [`archive/MODEL_PICKER_PLAN.md`](archive/MODEL_PICKER_PLAN.md) | Model picker feature plan. |
| [`archive/OPENCODE_SERVER_CONTEXT.md`](archive/OPENCODE_SERVER_CONTEXT.md) | OpenCode server integration context. |
| [`archive/opencode_gateway_backend_spec.md`](archive/opencode_gateway_backend_spec.md) | OpenCode backend spec. |
| [`archive/P0_CLAUDE_GATEWAY_RESUME_REPLACEMENT_PLAN.md`](archive/P0_CLAUDE_GATEWAY_RESUME_REPLACEMENT_PLAN.md) | Claude SDK driver replacement plan (P0 — shipped, see memory `p0-claude-driver-replacement`). |
| [`archive/TELEGRAM_UX_PARITY.md`](archive/TELEGRAM_UX_PARITY.md) | Telegram UX parity plan. |
| [`archive/WATCHED_JOBS_SPEC.md`](archive/WATCHED_JOBS_SPEC.md) | Watched-jobs feature spec (T3/T3.1 — shipped). |
| [`archive/U1_CHECKLIST.md`](archive/U1_CHECKLIST.md) | Control-surface-unification U1 checklist. |
| [`archive/U3_5_CHECKLIST.md`](archive/U3_5_CHECKLIST.md) | Control-surface-unification U3.5 checklist. |
| [`archive/control-surface-unification/CONTROL_SURFACE_UNIFICATION.md`](archive/control-surface-unification/CONTROL_SURFACE_UNIFICATION.md) | Full U1–U6 control-surface unification plan. |
| [`archive/cockpit-refactor-spec/COCKPIT_REFACTOR_SPEC.md`](archive/cockpit-refactor-spec/COCKPIT_REFACTOR_SPEC.md) | Cockpit refactor spec ladder. |
| [`archive/cockpit-refactor-spec/GPRIME_CHECKLIST.md`](archive/cockpit-refactor-spec/GPRIME_CHECKLIST.md) | Cockpit ladder checklist. |
| [`archive/cockpit-refactor-spec/M1_CHECKLIST.md`](archive/cockpit-refactor-spec/M1_CHECKLIST.md) | Cockpit ladder checklist. |
| [`archive/cockpit-refactor-spec/MOVE_H_CHECKLIST.md`](archive/cockpit-refactor-spec/MOVE_H_CHECKLIST.md) | Cockpit ladder checklist. |
| [`archive/cockpit-refactor-spec/U3_CHECKLIST.md`](archive/cockpit-refactor-spec/U3_CHECKLIST.md) | Cockpit ladder checklist. |
| [`archive/cockpit-refactor-spec/UI4_CHECKLIST.md`](archive/cockpit-refactor-spec/UI4_CHECKLIST.md) | Cockpit ladder checklist (UI-4). |
| [`archive/cockpit-refactor-spec/UI5_CHECKLIST.md`](archive/cockpit-refactor-spec/UI5_CHECKLIST.md) | Cockpit ladder checklist (UI-5). |
| [`archive/cockpit-refactor-spec/UI6_CHECKLIST.md`](archive/cockpit-refactor-spec/UI6_CHECKLIST.md) | Cockpit ladder checklist (UI-6, PWA). |
| [`archive/frontend-backend-gap/FRONTEND_BACKEND_GAP.md`](archive/frontend-backend-gap/FRONTEND_BACKEND_GAP.md) | Frontend/backend sync gap analysis. |

---

## Maintenance

- **Adding a doc?** Check [`.ai/DOC_MAP.md`](../.ai/DOC_MAP.md) first — a new file in
  `docs/` is justified only when no existing surface owns the information. Then add
  one row here, in the category it fits; don't create a new category for one file.
  A doc that describes the backend **as built** (processes, APIs, contracts, data
  ownership) goes in `docs/backend/` and also gets a row in
  [`backend/INDEX.md`](backend/INDEX.md). Web UI docs go in `docs/frontend/`.
- **Superseding a doc?** Mark it 🟡 here (don't delete — see harness convention of
  keeping prior versions as trace) and update the entry that replaces it to point
  back for history if relevant.
- **This file indexes `docs/` only.** Don't add `.ai/` files here — that tree has
  its own contract (`.ai/DOC_MAP.md`).
