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

Durable descriptions of how the system is built and the boundaries other code must respect.

| Doc | Status | What it's for |
|---|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 🟢 | Process topology + HTTP surface map, incl. §2b the flag-gated Manager/Case surface (`/api/manager`, `/api/work`). Keep current when routes/processes change. |
| [`CONTROL_CONTRACT.md`](CONTROL_CONTRACT.md) | 🟢 | The M1 inbound/outbound contract — event envelope, entry points, backend registry, read model. Read before adding a second surface or a new backend. |
| [`CONVERSATION_DATA_FLOW.md`](CONVERSATION_DATA_FLOW.md) | 🟢 | Where conversation/artifact data lives and how it flows; §0 documents the DB-canonical migration (2026-06-30). |
| [`ENV_FEATURE_FLAGS.md`](ENV_FEATURE_FLAGS.md) | 🟢 | Complete inventory of default-OFF feature flags. Check here before assuming a built feature is live. |

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
| [`PRIOR_ART_MAX_REUSE.md`](PRIOR_ART_MAX_REUSE.md) | 🔵 | Advisory salvage map — ideas mined from the retired MAX orchestrator for harness M3/M4. Not a build surface itself. |

### `docs/harness/` — the loop's own operating docs (templates, generators, runbook)

| Doc | Status | What it's for |
|---|---|---|
| [`harness/README.md`](harness/README.md) | 🟢 | What the harness is (prompt-and-artifact loop, zero new gateway state) and how the pieces fit. |
| [`harness/dispatch_pipeline.md`](harness/dispatch_pipeline.md) | 🟢 | The end-to-end runbook — how a task moves from idea to executed change. Start here to run a loop. |
| [`harness/level_rubric.md`](harness/level_rubric.md) | 🟢 | Deterministic checklist for picking harness level 0–3. |
| [`harness/loop_config_map.md`](harness/loop_config_map.md) | 🟢 | The loop's control surface — every configurable knob, who drives it, what file programs it. |
| [`harness/operating_model.md`](harness/operating_model.md) | 🟢 | How the loop is actually run in practice; wins over the spec where they differ on *how*, not *discipline*. |
| [`harness/manager_invocation.md`](harness/manager_invocation.md) | 🟢 | The driver doc — paste this to cold-boot a Manager and run a loop. Now a compatibility wrapper around `roles/manager.md`. |
| [`harness/roles/manager.md`](harness/roles/manager.md) | 🟢 | Canonical, provider-neutral Manager role profile — stable identity/authority, loaded at session boot. |
| [`harness/FLOW_MAP.md`](harness/FLOW_MAP.md) | 🟢 | Where state lands as a flow runs through the automated (v0.6+) harness. |
| [`harness/milestone_template.md`](harness/milestone_template.md) | 🟢 | Template for a dispatch's `## Milestone` burndown section. |
| [`harness/packet_template.xml`](harness/packet_template.xml) | 🟢 | XML task packet template. |
| [`harness/generators/draft_packet.md`](harness/generators/draft_packet.md) | 🟢 | DRAFT generator — intent → task packet. |
| [`harness/generators/adversarial_review.md`](harness/generators/adversarial_review.md) | 🟢 | REVIEW generator — adversarial pass over packet/diff. |
| [`harness/generators/closure_summary.md`](harness/generators/closure_summary.md) | 🟢 | CLOSE generator — closure summary + doc-update stub. |
| [`harness/promotion_ladder.md`](harness/promotion_ladder.md) | ⚪ | **Retired 2026-07-06**, superseded by v0.6 automation build spec. Kept for the evidence-gated prototype-era decision trail only. |

## Specs — session/state timeline

| Doc | Status | What it's for |
|---|---|---|
| [`SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md`](SESSION_STATE_TIMELINE_ARCHITECTURE_REVIEW.md) | 🟢 | Adversarial review of Web UI session/job/task/artifact/telemetry state honesty (2026-07-01). |
| [`SESSION_STATE_TIMELINE_EXECUTION_PLAN.md`](SESSION_STATE_TIMELINE_EXECUTION_PLAN.md) | 🟢 | Implementation-ready roadmap that followed the review above. Cross-check `.ai/CONTEXT.md` Shipped Ledger for what's actually landed. |
| [`LLM_TURN_OBSERVABILITY_SPEC.md`](LLM_TURN_OBSERVABILITY_SPEC.md) | 🟢 | Turn-observability/usage-accounting spec (M1–M4). M1/M2/M3 shipped per `.ai/CONTEXT.md`; M4 (OpenCode) deferred. |
| [`DEFERRED.md`](DEFERRED.md) | 🟢 | Web UI/Cockpit items deliberately not built, with why. |

## Runbooks — operational procedures

| Doc | Status | What it's for |
|---|---|---|
| [`RUNBOOKS/OPERATIONS_PM2.md`](RUNBOOKS/OPERATIONS_PM2.md) | 🟢 | Running the gateway under PM2 (the supported way to keep it alive). |
| [`RUNBOOKS/CONTROL_SURFACE_DEPLOY_RUNBOOK.md`](RUNBOOKS/CONTROL_SURFACE_DEPLOY_RUNBOOK.md) | 🟢 | Deploying the unified gateway (Telegram + Web on one process). |
| [`RUNBOOKS/PHASE_4_RUNBOOK.md`](RUNBOOKS/PHASE_4_RUNBOOK.md) | 🔵 | VPS cutover runbook — migrate control plane off this PC. Not executed yet. |
| [`RUNBOOK_db_self_sufficient.md`](RUNBOOK_db_self_sufficient.md) | 🟢 | Procedure to migrate conversation/artifact data into `mesh.db` and drop fat `results/*.json`. Migration itself is done; kept as the reversibility procedure. |

## Reference / schema

| Doc | Status | What it's for |
|---|---|---|
| [`schema/results.schema.json`](schema/results.schema.json) | 🟢 | JSON schema for task result artifacts. |
| [`dictionary/words_&_relations.md`](dictionary/words_&_relations.md) | 🔵 | Working glossary — Case/Task/Session/Event/Artifact vocabulary and the Manager/Skill/Tool layering. Not yet cross-linked from other specs; treat as draft until reconciled with `harness/roles/manager.md` and the M3 spec. |

## TBD — proposals, not committed work

Ideas and analyses that haven't been scheduled. See `.ai/CONTEXT.md` "Deferred"
tables for the authoritative prioritization; these are the supporting writeups.

| Doc | Status | What it's for |
|---|---|---|
| [`TBD/BACKEND_HOOKS_STRATEGY.md`](TBD/BACKEND_HOOKS_STRATEGY.md) | 🔵 | Whether backend lifecycle hooks (Claude Code/Codex/OpenCode) can replace/supplement gateway state management. |
| [`TBD/CLAUDE_HOOK_IDEAS.md`](TBD/CLAUDE_HOOK_IDEAS.md) | 🔵 | Claude Code hooks as a leverage point for deterministic lifecycle behavior. |
| [`TBD/SESSION_WINDOW_WARMING_SPEC.md`](TBD/SESSION_WINDOW_WARMING_SPEC.md) | 🔵 | Quota window coordinator proposal — no implementation yet. Corresponds to the unmerged `phase1-quota-window-coordinator` branch (see `.ai/CONTEXT.md`). |

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
- **Superseding a doc?** Mark it 🟡 here (don't delete — see harness convention of
  keeping prior versions as trace) and update the entry that replaces it to point
  back for history if relevant.
- **This file indexes `docs/` only.** Don't add `.ai/` files here — that tree has
  its own contract (`.ai/DOC_MAP.md`).
