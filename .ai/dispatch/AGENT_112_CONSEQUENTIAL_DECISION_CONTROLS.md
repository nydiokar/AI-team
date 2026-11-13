```yaml
job_id: 112
created_at: "2026-11-10T11:22:28.696328+00:00"        # CANONICAL — set once at dispatch, never derive again
status: done              # ready | active | blocked | done | dead
owner: ""
depends_on: []
results_ref: DISPATCH_LOG.md#A112
evidence: docs/harness/AGENT_FAILURE_MODES.md
updated_at: "2026-11-10T11:26:09.132053+00:00"
```

# DISPATCH — A112 · Consequential configuration and informed decision controls

**Level:** 3 (cross-cutting authority, workflow admission, configuration provenance).
**Type:** grounded investigation + design + minimal implementation recommendation.
**Authored:** 2026-11-10, Europe/Vilnius. **Status:** complete; design/research only, no runtime change made.
**Depends on:** none. Coordinate with A111; reuse its evidence-bound closure work and do not build a parallel reviewer/approval system.

## Verified baseline and judgment (2026-11-10)

Inspected current `main` (`ab851e8`) and traced these seams:

| Seam | Current behavior observed | Consequence for FM-1 |
|---|---|---|
| Manager/Case authoring (`docs/harness/roles/manager.md`; `src/orchestrator.py:6177`) | Manager prose requires grounding, objective/criteria, authority and review. `open_case` creates the Case; prose is not a generic parameter certification schema. | Helps catch bad assumptions but cannot reliably detect/stamp every consequential choice. |
| Level-3 approval (`docs/harness/level_rubric.md`, `docs/harness/dispatch_pipeline.md`, `src/orchestrator.py`) | Authoring convention requires operator approval before Level-3 dispatch. `docs/backend/ENV_FEATURE_FLAGS.md` says `HARNESS_LEVEL3_GUARD` and its predicate were removed. Current code search found no `_harness_level3_allows_autopickup`; the old pipeline section describing its code backstop is stale. | Human approval is a coarse task-level convention, not a per-value durable gate, and enforcement is not guaranteed at runtime. Reconcile stale docs as part of any implementation touching this seam. |
| Durable approvals (`src/services/approval_service.py`; `src/control/db.py:8766`, `:6961`, `:7075`) | Approval rows have pending/resolved lifecycle and Case links; unresolved required approvals block Case close. They are action/Case scoped, with no general binding to config value, objective/config revision, options shown, or evaluation. | Reuse the existing durable approval substrate if a specific control needs one; do not equate existing approvals with informed configuration decisions. |
| Runtime flags (`src/control/db.py:276`, `:444-573`; `src/control/routes/admin.py:176-245`; `docs/backend/ENV_FEATURE_FLAGS.md`) | Typed definitions, validation, registry values, sources/precedence and API writes exist. Git confirms A62 code merged in PR #246 (`a113094`), while its dispatch row still says dispatched and its packet has no completed closure. | Strong deterministic enforcement substrate for valid values, but flag typing/source does not itself establish who chose a consequential value, what alternatives were explained, or whether an outcome was evaluated. The ledger discrepancy is real; reconcile A62 separately rather than folding it into this job. |
| Role/tool profiles (`src/core/roles.py:124-138`; `src/backends/tool_profile_source.py:200`; A107/A108) | Role→tool profile mapping and dynamic overlay exist. General role registry/factory work was explicitly left outside A108; roles are not a general visual-first workflow configuration surface. | Role/tool choice can materially affect authority and capability. Treat permission changes as high impact and preserve current least-privilege fallbacks; do not build a registry as part of this design-only job. |
| Scientific review (`docs/harness/scientific_governor_contract.md`; A111) | Governor review is a worker-dispatched contract; A111 plans evidence-bound review/closure and calibration. Current conventions and fixtures do not prove per-setting informed choice or evaluator quality. | A111 can provide the review/acceptance seam for objective fitness; it should not become a universal knob certifier. |
| Visual-first configuration | No general visual-first configuration editor exists. Web UI is a surface over current Cases, sessions, approvals, and flags. | Treat palette/token selection as a future scenario. Do not imply a present user journey or create an isolated settings UI prematurely. |

**Critical assessment.** The operator's distinction is valid: some defaults are useful scaffolding, while others encode the objective. The control should attach to *consequential decisions*, not the mere presence of a configurable field. The two examples differ in validation: training losses often have measurable offline metrics but metric choice itself can encode values; palettes can be mechanically checked for contrast/accessibility and still need brand/product judgment. Evaluation is a separate, conditional control, not a universal release gate.

**Most natural enforcement point.** Keep decision ownership with the component that owns the canonical value (currently runtime flag registry for registered flags; Case contract for objective/acceptance; tool profile source for grants). At a shared admission/use boundary, require an authorized, revision-bound decision only for choices declared consequential. Reuse Case approvals for human attention and A111's existing review/closure path for objective fitness; do not create a parallel queue or universal reviewer. A later visual editor should edit these same canonical records, never become an independent authority.

**Deterministic vs semantic.** Deterministically enforce typed/range constraints, declared materiality, source/provenance, required decision existence, principal/delegation, revision match, and required evaluation artifact. Semantic review is needed to discover whether an undeclared choice is material and to explain trade-offs. Treat model classification as advisory or independently checked; for high-impact uncertain cases, hold dependent production use and ask the accountable authority. Let labeled, isolated exploratory runs proceed with provisional values and no production promotion.

**Recommendation / next implementation slice.** Do not start a universal knob registry or mandate human approval for every parameter. First, reconcile stale Level-3 docs with code and complete A111's single evidence-bound closure gate. Then pilot an additive, typed `decision-critical` declaration on one real consequential config (not a speculative color UI): bind its accepted value to owner/delegation, explanation/alternatives revision, and objective/config revision; make stale/missing decisions visible at the existing decision surface; preserve a clearly marked provisional sandbox path. Add evaluation linkage only where a project has a defined measure, recording explicit N/A otherwise. Measure missed material choices, unnecessary blocks, override/reversal rate, and operator review burden before generalizing to roles/tools or a future visual-first editor. This proposal is scoped for an implementation dispatch after the operator chooses the pilot configuration; no runtime changes are made here.

## Goal

Determine how AI-Team should detect and control consequential choices—parameters, defaults,
objective/loss definitions, role/tool/model choices, and similar inputs—so provisional values do
not silently become the foundation for downstream work. Ground the result in current code and
workflow, distinguish existing controls from gaps, and recommend the smallest enforceable design
that preserves safe exploratory work while requiring an informed, authorized decision before a
consequential value becomes binding. The durable starting definition and cross-industry references
are in [`docs/harness/AGENT_FAILURE_MODES.md`](../../docs/harness/AGENT_FAILURE_MODES.md), FM-1.

## Observed failure model

Construction pressure leads an agent to use a convenient/default value to make a task runnable.
That value becomes an inherited premise without clear provenance, an informed decision, or
fit-for-purpose evaluation. Execution may be technically correct while downstream results are
invalid for the owner's objective. The failure is omission plus propagation, not simply “bad
defaults.”

## Work

1. **Verify the current baseline in code.** Trace one end-to-end path each for Case/task admission,
   review/approval/closure, runtime flags, role/tool profiles, and the front-end surfaces. Use
   existing symbols/files; verify which parts are live, planned, flag-gated, or convention-only.
   Include the effect of A111 and A62 without treating their packets as proof of implementation.
2. **Build a decision/control map.** For each seam, record: what values are configured; who can
   set/change them; their defaults and precedence; whether they are consequential or revisioned;
   whether alternatives/effects are explained; whether authorization is durable and specific;
   what blocks downstream use; and what evaluation/outcome is recorded. Include a future
   visual-first configuration workflow as a scenario, while stating that no such general editor
   exists today.
3. **Critically assess FM-1 and proposed controls.** Test for over-blocking, approval fatigue,
   false semantic classifications, inherited decisions, stale approvals, delegated authority,
   routine safe defaults, exploration/sandbox runs, and cases where no meaningful evaluation
   exists. Specify which checks can be deterministic and which require semantic/domain judgment.
4. **Propose the natural enforcement point.** Prefer the existing canonical decision/configuration
   owner and admission/closure choke point. Define a minimal record (value and source, rationale or
   explanation revision, authorized principal/delegation, scope/revision, status, and optional
   evaluation reference), invalidation rules, migration/compatibility approach, and operator-facing
   unresolved-decision surface. Keep execution completion distinct from acceptance.
5. **Recommend sequencing.** Identify what can be documented or enforced now with existing Case,
   approvals, flag, and Governor mechanisms; what depends on A111; and what should wait for
   visual-first configuration/role registry work. If code changes are needed, present a narrowly
   scoped follow-up with concrete files and acceptance criteria. Do not implement a cross-cutting
   runtime redesign under this investigation packet.

## Done when

- The current behavior is verified from code and the map names concrete seams and limits.
- The operator has a clear, critical judgment on the failure model and controls, including when
  deterministic enforcement is appropriate and where semantic/domain review remains necessary.
- A minimal architecture and staged follow-up are recommended, with explicit no-parallel-system
  ownership and A111/A62 dependency boundaries.
- Update FM-1 if investigation disproves or sharpens it; add another ledger entry only if a distinct
  failure mode is established. Do not bloat the ledger with every knob.
- Evidence is linked here and recorded in YAML before marking done. No production config mutation,
  live approval, paid model run, or worker restart is part of this investigation.

## Trail / evidence

Initial doctrine: [`docs/harness/AGENT_FAILURE_MODES.md`](../../docs/harness/AGENT_FAILURE_MODES.md).
External research references are linked there. Add code map/findings and a bounded implementation
proposal here or under `.ai/dispatch/evidence/` if it needs a separate supporting artifact.

## Milestone (burndown)

- [x] Verify cross-layer baseline against current code and A111/A62.
- [x] Complete decision/control map for current and visual-first future surfaces.
- [x] Adversarially assess materiality, approval, validation, revision, and exploration controls.
- [x] Recommend enforcement seam and staged follow-up.
- [x] Record evidence and closure honestly.

## Closure

Completed 2026-11-10 as a documentation/research job. FM-1 is defined in the durable ledger;
the code map above distinguishes implemented mechanisms, prose conventions, planned A111 work,
and a future visual-first scenario. External sources and their actual contribution are linked in
the ledger. Recommended next step is a bounded pilot after choosing a real consequential setting;
this packet does not claim that any general per-knob gate exists today.

Verification: dispatch-state audit passes with zero gaps/unproven-done/stale-active; `git diff
--check` passes. No tests were needed for documentation-only changes. No production settings were
changed and no worker restarted.
