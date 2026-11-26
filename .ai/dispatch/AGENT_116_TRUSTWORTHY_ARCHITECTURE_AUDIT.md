```yaml
job_id: AGENT_116_TRUSTWORTHY_ARCHITECTURE_AUDIT
created_at: "2026-11-16T13:15:02.851908+00:00"        # CANONICAL — set once at dispatch, never derive again
status: active              # ready | active | blocked | done | dead
owner: codex
depends_on: []
results_ref: DISPATCH_LOG.md#A116             # -> DISPATCH_LOG.md section with the verdict prose
evidence: []                  # artifact paths that PROVE it ran (checked to exist)
updated_at: "2026-11-25T23:57:35.423184+00:00"
```

# DISPATCH — A116 · Trustworthy architecture audit and authoritative ledger reads

**Level:** 3 (agent acceptance decisions, service/read boundaries, breadth) · **Type:** code + audit + certification
**Authored:** 2026-11-16 · **Packet state:** active; application and audit-tool stages in progress
**Issue:** [#290](https://github.com/nydiokar/AI-team/issues/290)
**Depends on:** none; coordinate with A111 acceptance work and current read-path changes, do not wait for their unrelated stages.
**Branch:** fresh `feat/a116-<stage>` from current `origin/main` in an isolated worktree; PR, review, push and self-merge each coherent stage. No unrelated branch carried forward.
**Authorization:** the operator's 2026-11-16 instruction explicitly authorizes another executor to fully implement, verify, certify and ship these changes autonomously without renewed user feedback. Level-3 implementation approval is satisfied for the scope below. `ready` means available for execution, not awaiting approval. Do not invent an operator-calibration gate.

> **Read this first.** The tool demonstrably generates useful structural leads, but its run/result contract can claim success without required analysis. Two of its generic candidates exposed real current ledger-reader defects. Deliver both corrected behavior and evidence that callers can interpret zero or many findings honestly on every supported run. Publishing this packet is not implementation or certification.

## Why (intent)

The desired outcome is a working architecture-audit skill that exposes architectural mismatches with source evidence, separates semantic confidence from mechanical observations, fails honestly when analysis is unavailable, and produces reproducible results. Repair the real application mismatches that motivated the job. A lower candidate count, a green unit suite or a README alone cannot close this task.

Three underlying contracts are currently confused:

1. A bounded display/history page is treated as complete authoritative review/membership state.
2. Successful process completion is treated as successful analysis despite missing scope, parse failures or index errors.
3. Structural similarity/entity mentions are treated as semantic defect detection and scored as recall.

Fix these contracts at their existing ownership boundaries. Keep SQLite, the existing APIs, audit engine and dispatch closure integration; do not create a parallel reviewer/store/runtime.

## TASK

### P0 — Re-ground, freeze witnesses and review the plan

Read `.ai/CONTEXT.md`, `DISPATCH_LOG.md`, DOC_MAP and current git state first. Read this packet, issue #290, the audit skill's SKILL/README/MEASUREMENTS, the recorded JSON and compressed original report. Resolve symbols before opening large files. The baseline is `49c4dfa62d34e84d891c5730897d2b72cd0155d9`; audit publication is PR #289 / `244a397`. The audit scripts and application code were unchanged by that publication. Recheck current main for intervening fixes before implementing; retain equivalent proofs if the implementation changed.

Preserve original data. Add executable baseline and benign-control fixtures to the existing skill/application test locations, plus a small portable labeled corpus under the existing `benchmark/` tree. Baseline expected failures must be demonstrated at the pinned old revision, then pass against the candidate. Freeze expected witnesses/labels before tuning detectors; have an independent reviewer assess labels and plan without treating existing candidate titles as truth. Use an available review mechanism, not a paid coding-agent CLI launched by tests. Make bounded engineering decisions and continue; the operator approval above persists.

Required witness map:

| Case | Existing trigger/source | Baseline evidence and desired behavior |
|---|---|---|
| F1 latest review | `MeshDB.latest_spec_review` → `list_flow_events_of_types` → `decompose_case` in `src/control/db.py` | 5,001 scored review rows: last rejects but read returns approving row 5000; decomposition gets past review gate. Reverse last verdicts too. Select latest applicable evidence with Case/type/spec predicates BEFORE the bound; malformed latest evidence must not fall back to old approval. |
| F2 membership | `MeshDB.list_flow_links` → `src/control/routes/work.py:api_work_detail/api_work_roster` and `src/control/cost_read_model.py:assemble_case_usage` | 201 linked sessions yield 200 default rows; later roster/detail inputs disappear and roles default to `member`. Preserve complete domain computations and expose any paged display as paged. Cost totals use a separate query; do not claim they were lost without proof. |
| F3 contract interpretation | frozen `benchmark/history/A104_packet.yaml`, `src/control/agent_inbox.py`, `tests/test_a104_single_pending_source.py` | Its exclusive-SELECT rule conflates pending-state authority with lifecycle/audit/migration access. Validate responsibility-correct maintained guards without weakening frozen history. |
| T1 coverage | `scripts/contract_check.py:Contract/check/_files/scan_python` | Wrong/empty scope can return `ok=true` with zero files; parse failures/skipped required inputs can remove checks without failing. No vacuous clean gate. |
| T2 availability | `scripts/pathway_explorer.py:Engine.ensure_index/persist/state/main` | Index failure under default `--no-serve` logs an error, returns normally, writes no findings and omits error in JSON. Failure must reach CLI, report and UI consistently. |
| T3 extraction | `scripts/pyast_adapter.py:_strings/data_access` and contract SQL attribution | `FROM main.secret` evades a `secret` reader ban; docstring SQL/CTE aliases invent accesses. Supported forms must be correct; unsupported forms explicit. |
| T4 caching/counts | explorer sync/invalidation, `structure.py:analyze`, baseline handling | Python-version parse facts can survive cache reuse; kinds are capped at 40; roots/all and semantic roots differ. Reused results and counts must remain attributable and complete only for stated capabilities. |
| T5 scoring | `benchmark/run_corpus.py`, `benchmark/score.py` | A new CONVERGENCE fact touching a victim is called CAUGHT/recall 1/1. Child failures do not propagate as runner failure. Separate candidate localization, verified detection and controls. |

### P1 — Repair authoritative application readers

For F1 use a deterministic parameterized latest-applicable SQL lookup, filtered by Case/type and optional spec before descending ID order and limit 1. Preserve payload and absent/malformed semantics; inspect existing indexes/query plan before adding one. Test unrelated review types, mixed specs whose matching result lies beyond the old prefix, later rejection and later approval. Preserve the already-correct latest rework/close gate. Do not rebuild A111 or perpetuate its obsolete advancement heuristic.

For F2 enumerate and classify every consumer of `list_flow_links`: page/display, exact existence/ambiguity, full membership, aggregate or role projection. Preserve the existing generic display contract where correct. Give complete domain consumers case-filtered, batched SQL reads/joins/aggregates or bounded page iteration whose exhaustion is actually consumed; expose list page/truncation metadata and frontend traversal when needed. Follow established role/rebind/duplicate semantics. Use no arbitrary larger cap, global newest-first rewrite, unbounded materialization, N+1 session reads or second membership store. Verify roster IDs/jobs/counts, detail/timeline pointers and cost role attribution, not just the base query length. Trace frontend consumers of any changed envelope.

Test 199/200/201 and 10× session cardinality, plus historical role links and membership filters. Verify request/response size, actual concurrent executions, memory and query plans. Prefer existing bounded read/snapshot helpers; any necessary schema/index change uses existing migrations and has a rollback/recovery proof. A default display bound may remain only where the UI makes it explicit and can reach later entries; an authority computation must not silently use that page.

### P2 — Make analysis outcomes failure-honest

Extend the existing checker/explorer result path; no second engine or generic observability platform. Typed data models should carry, in consistent CLI/JSON/browser semantics:

- run/source revision or working-tree digest, exact requested/effective scope, exclusions and applicability;
- Python/parser, CBM, detector/schema/index/cache identity and invalidation reason;
- execution stage status/errors, required analysis completion and explicit capability/coverage gaps;
- facts, candidates and source-verified dispositions as distinct categories; benign/known/unreviewed status never silently hides findings;
- uncapped per-kind totals, emitted/omitted counts, root/edge/depth/query caps and how to retrieve omitted evidence without unbounded memory;
- stable source/evidence references and review provenance. Incomplete/cancelled/failed runs must not leave a prior clean artifact usable as fresh evidence.

Preserve compatible contract-checker meanings: 0 only when applicable required checks complete and the declared assertions pass; 1 for completed checks with violations; 2 for invalid input or analysis/incomplete required coverage. If additional CLI states are needed, document and test them together with `scripts/dispatch/dispatch_state.py` so every nonpassing required check refuses done. Do not let baseline acknowledgement, empty scopes, fully excluded relevant input, skips, parse errors or absent dependencies bypass closure.

For the explorer, candidate count is independent of process success. An intentionally bounded partial scan remains a useful artifact with visible coverage limits, never a clean certificate; missing/failed required stages must return nonzero. Unsupported dynamic paths stay visible even when a configured static sweep completed. Do not label static call-graph coverage as full runtime coverage. Reject or explicitly mark meaningless empty store constraints. Use atomic/invalidated artifact publication for fresh result identity and persist errors even if no graph exists.

Add cache compatibility for Python/parser/detector/CBM/source/index identity as needed. Compare normalized cold/warm finding/evidence sets, not just timings. Baselines must survive the documented identity choices, report known/new/stale counts and never suppress undisclosed unresolved/omitted results. Preserve local loopback operation; do not add public hosting/auth infrastructure.

### P3 — Correct semantic inputs, contracts and evaluation

Fix supported SQL extraction/attribution demonstrated by T3: schema/quoted names, CTE scoping, SQL docstrings, and constant-defined SQL executed by helpers. Retain provenance separating literal declaration, possible execution site and inferred ownership; do not attribute a constant to an arbitrary caller or claim unknown dynamic SQL checked. Use installed/project-managed dependencies if a parser is needed; evaluate minimal local changes first. A new global SQL parser dependency or broad rewrite requires evidence of necessity within this authorized scope, not ad-hoc installation.

Keep frozen historical A102/A104 examples and original measurements immutable. Add a maintained contract/guard that constrains actual owner modules and the actual shared pending-state read model, allows inspected lifecycle/migration/test access, and rejects an injected competing pending-state authority. Reuse `test_a104_single_pending_source.py`; do not fabricate a second inbox reader to make a validator convenient. Historical counterexamples must remain historically distinguishable from maintained contracts. Preserve narrow, reasoned negative-test waivers.

Correct the scorer: structural facts alone never count as defect catches; substring mentions and CBM project-name changes cannot substitute for matching a labeled violation witness. Commit a portable corpus containing real F1/F2 witnesses, a retired-name/cross-module-import mismatch, a real competing reader/bypass and benign controls. Retain distinctions between graph-pattern localization, contract violations and semantic validation; do not pretend all three have the same oracle. Freeze defect/control labels before detector changes and reserve held-out variations for independent review. Unsupported categories remain explicit and do not inflate denominators. Runner timeout/missing report/nonzero child/incomplete run must fail or be a reported nonpassing case, never a clean control. Report denominators, false positives, misses and unknowns.

Do not optimize score by hard-coding AI-Team symbol names, deleting candidates, raising thresholds until known cases vanish or redefining all tables as hubs. Source-aware rule refinements should generalize and preserve evidence. Review raw, not only baselined output.

### P4 — Independent verification and reproducible certification

Run only relevant offline pytest/vitest targets selected using `running-targeted-tests`; use Python 3.12+ and the existing isolated test image where appropriate. Run real CBM fixture and corpus; install/download supported CBM reproducibly with checksum and a disposable process lifecycle. Declare supported/tested versions explicitly. No full/e2e suite, paid CLI proof turn, worker restart or production history mutation.

Required certification matrix:

| ID | Proof required |
|---|---|
| C1 | F1/F2 fail on baseline and pass on candidate through real DB methods and consuming API/read-model/browser seams. Empty/single/mixed/threshold/10× cases, malformed latest payload, role/rebind semantics and intentional LIMIT-2 control included. |
| C2 | Scope typo/empty/excluded, syntax-invalid Python, oversize/binary required input, absent CBM, index/query timeout/error, partial paging and corrupt cache all yield explicit nonpassing required analysis; CLI, JSON and UI agree. |
| C3 | A clean zero-result fixture, a zero-result incomplete fixture and a >=101-result fixture are distinguishable. More than 40 findings of one kind have correct total/emitted/omitted counts and recoverable evidence. Known/new/stale baselines cannot fabricate a clean run. |
| C4 | Schema-qualified/quoted SQL is checked; docstrings and CTEs do not invent stores; constant SQL declaration/consumption and unsupported dynamic SQL are attributed honestly. No regressions in approved store ownership/retirement checks. |
| C5 | Cold and warm results are semantically equal after normalization; changed Python/parser/CBM/source definitions invalidate affected caches; injected new callers and deleted symbols do not leave stale evidence. |
| C6 | Frozen labeled corpus has exact expected outcomes; CONVERGENCE-only/proximity/substrings/project-name changes cannot count as catches. Missing/crashed/incomplete child runs propagate failure and cannot improve recall/control scores. Publish all misses and unknowns. |
| C7 | CLI → JSON → live browser route/source/expansion/failure-recovery behavior is exercised. Snapshot-only rendering is not live-server proof. No retained processes after timeout/exit. |
| C8 | Existing current packet closure integration rejects every nonpassing required outcome. Wrong-scope/failed-index/incomplete inputs cannot change status to done. Existing valid contracts/waivers still work. |
| C9 | Service-boundary checklist below completed for every touched endpoint/handler, with measured memory/cardinality/concurrency and bounded controls. |
| C10 | An independent reviewer receives baseline witnesses, candidate diff, corpus labels/results and the acceptance matrix; attempts false-green, false-catch, stale-cache and pagination attacks; blockers are fixed and affected checks repeated. |
| C11 | Rerun the tool on merged source, publish manifests and full raw report(s), compare normalized pre/post coverage/results, confirm F1/F2 behavior is repaired without requiring every structural LIMIT candidate to vanish. Explain remaining candidates. |
| C12 | Merge PRs, deploy changed gateway code through `deploying-the-gateway`, verify loaded SHA and read-only API/browser behavior. Keep large-cardinality proofs isolated. No tool-only deployment needed. Complete issue/registry/CONTEXT closure with actual evidence. |

Every scoped run should cheaply validate input/version/index/report preconditions; use the committed conformance fixtures as a repeatable validation command and CI/upgrade regression guard. Do not rerun the full expensive corpus automatically for every graph click. Certification names supported capabilities and unresolved runtime/static limits. Zero can mean no matching findings in completed declared checks, never universal absence of bugs. 101 means classified evidence items, never 101 certified defects.

## TYPE

Implementation + audit, Level 3, already approved for autonomous execution. This authoring task creates the issue/packet and preserves evidence; the future executor owns P0–P4. Keep that distinction in registry state and GitHub issue state.

## CONTEXT (reuse verbatim)

- `MEASUREMENTS.md`: “The query defaults to oldest-first and `limit=5000`. `latest_spec_review` folds only those returned rows and does not request the newest row. Its `spec_id` filter runs after that SQL limit.”
- Original tool JSON: `cf15dbf85a47` / `table::flow_events` and `3a7bd241ebdb` / `table::flow_links`, both `TRUNCATED_WINDOW`, both `level=candidate`, both `semantic_verification=not performed`.
- `SKILL.md`: “Do not claim a clean system from incomplete graph coverage.”
- Baseline: 54 real-CBM tests passed; 141 routes expanded, 766 sweep roots, 280 emitted items (80 facts, 200 candidates), 698 patterns before caps, 152.3 seconds. This is observed utility, not calibrated semantic recall.
- The full report is preserved under the skill's measurement tree. See the provenance section there for the exact hash and tool-versus-agent attribution. Transient `/tmp` artifacts are optional, not prerequisites for execution.

## ACCEPTANCE (proof, not vibes)

All C1–C12 are required. F1/F2, T1–T5 and maintained F3 contract semantics must have fixed/benign/explicit capability-limit dispositions with test evidence; a required-case unknown is not certified completion. The final source-to-observation trace covers authoritative SQL → read model/API → browser and detector/parser → report/CLI → semantic review → dispatch closure. Publish exact commands, revisions, versions, return codes, expected/actual results, reviewer outcomes and remaining limits. Use one per-run typed evidence format in the existing measurement location; keep lifecycle/burndown in this packet.

No static arch-contract is invented here: no actual symbol retirement is planned, and today's checker is part of the system under test. If implementation really replaces/retires names, add one nonvacuous `arch-contract` using those actual names and full scope before doing so. It supplements, never replaces, the independent C1–C12 certification.

## RESERVED DECISIONS (defaults authorized; no renewed feedback required)

- **R1 — Authority queries vs pages:** executor chooses minimal SQL/aggregation and pagination compatible with observed consumers. Preserve complete decision/aggregate semantics and bounded transport; no blanket limit increase.
- **R2 — Result schema/compatibility:** executor chooses exact field/type names while keeping current callers compatible or updating them in the same coherent PR. Existing contract exit 0/1/2 meanings remain useful; document any extension and adapt dispatch/tests together.
- **R3 — SQL coverage:** support the witnessed forms and report unsupported forms explicitly. Do not promise general semantic SQL execution analysis; justify dependencies through the project workflow.
- **R4 — Supported CBM:** begin with the measured 0.11.0 conformance target. Test an additional installed/desired version only with its own results. Missing unsupported versions are documented capability limits, not a request for an operator decision.
- **R5 — Shipping and deploy:** executor owns review/fixes/merge and normal gateway deployment/rollback under project policy. No worker-side change is required; worker restart, paid proof turns and destructive data repair remain outside this authorization. These exclusions do not block the scoped implementation.

## SCOPE OUT

No comprehensive runtime-correctness proof, perfect semantic detector, S1/Governor/A111 acceptance redesign, new queue/store, broad role-registry rewrite, private corpus dependency, production history rewrite, real agent turn for proof, global SQL dialect support, worker restart or unrelated lifecycle/security repair. Do not close #290 because a planning/doc PR merged. Natural production cardinality thresholds are not an acceptance dependency; isolate threshold fixtures and verify live code identity/read seams separately.

## Service-boundary integration checklist (mandatory at implementation closure)

For each touched gateway or explorer HTTP handler, record concurrency admission and cancellation ownership, memory at 100 calls, payload/response limits, timeouts including native-IO limits, malformed/truncated/adversarial input and unavailable DB/CBM behavior. Put load fixtures in isolation; never stress the running gateway. Measure app query/serialization behavior at 10× Case cardinality and audit output at >=101 items and >40 per kind. Preserve shared DB snapshots/cancellation and existing admission gates. Any concrete boundary deferral must appear under A116 in CONTEXT before closure, with rationale and scope; “later” is insufficient. No new handler is implied by authoring this packet.

## TRAIL / EVIDENCE

- Issue: [#290](https://github.com/nydiokar/AI-team/issues/290).
- Baseline experience: [MEASUREMENTS.md](../../.claude/skills/architecture-pathway-audit/MEASUREMENTS.md).
- Baseline data: [2026-11-16.json](../../.claude/skills/architecture-pathway-audit/benchmark/measurements/2026-11-16.json).
- Original tool output: [2026-11-16.findings.json.gz](../../.claude/skills/architecture-pathway-audit/benchmark/measurements/2026-11-16.findings.json.gz).
- Append stage PRs/SHAs, baseline/candidate/merged command outputs, review findings/fixes, boundary measurements and deploy/read-seam evidence here. Use `evidence/A116_*` only for necessary job-specific artifacts with backlinks. Put enduring tool experiments/data in its existing MEASUREMENTS/benchmark tree; keep README as usage docs.
- Set registry owner/status when implementation starts. `ready` remains until then. Populate evidence paths with implementation proof, then results_ref and done via dispatch_state.py only after C1–C12 pass. Close #290 with a proof-linked certification comment at that point.

## Milestone (burndown)

- [ ] P0 frozen executable baseline witnesses, benign controls, labels and independent plan review.
- [ ] P1 authoritative latest-review and complete membership consumers repaired, bounded and proven through downstream seams.
- [ ] P2 failure-honest CLI/JSON/browser/dispatch outcomes, freshness, coverage/count/cache identity.
- [ ] P3 supported SQL extraction/ownership, maintained responsibility-correct guards and honest portable evaluation.
- [ ] P4 C1–C12 passed, independent adversarial review blockers fixed, merged rerun and deployed app seams verified.
- [ ] MEASUREMENTS/data/packet/issue/registry/CONTEXT closed on evidence; no false clean or false semantic score.

### Authoring verification — 2026-11-16

Issue #290 is published; this packet, human ledger and Active Work entry identify
A116 as ready and operator-approved. Original full tool output was archived
losslessly and hash-checked; both candidate IDs and the 80-fact/200-candidate split
are verified against it. A repeated cache-backed real-CBM scan returned the same
finding payload, routes, coverage and analysis scope in 22.6 seconds; its manifest
is in the existing measurement tree. No application or detector code changed.

The strict dispatch audit already failed before A116 on one A67
CLAIMED_DONE_NO_PROOF gap (missing historical evidence in the current repository
checkout); after authoring it must show the same gap, no new gaps and A116 ready.
Do not publish private security artifacts or relabel A67's history to green this
unrelated handoff. This is an explained registry baseline, not A116 certification.

## Closure

Open. This packet and issue establish an approved, executable handoff. No implementation, corrected tool or application behavior, final semantic score or deployment certification is claimed yet.

### Authoritative ledger reads

Decomposition reads the latest applicable scored event by Case, event type and spec, with
the existing Case/event-ID index. The latest payload must explicitly pass as a JSON boolean.
The same lookup runs under SQLite write admission before the link, event and stage writes
commit together. The existing Case closure rework gate is independent.

Work detail and timeline use Case-scoped link cursors. Detail counts all links and timeline
discloses the full event count alongside its newest event window. The roster pages sessions
by their oldest link and jobs by descending creation time and ID; its Case-wide totals use
deduplicated membership in SQL. Cost usage streams per-session/model SQL rows with the first
role joined in the same query, so a session with several role links is charged once. The
current Manager lookup uses the newest Manager link and the task DAG read traverses all its
links in batches. Exact lookup and LIMIT-2 requester ambiguity paths remain distinct.
Existing best-effort interrupt/sweep and affiliation cleanup have their original caps;
changing terminal cleanup would need separate mutation/affiliation-race verification.

Work detail, timeline and roster share four synchronous read slots through JSON encoding.
Case usage has four slots of its own. These routes use the existing 10-second cooperative
SQLite read snapshot with the existing 5-second per-lock wait. Saturation/unavailability
returns 503 and bad cursors return 422. Browser paged queries cancel on navigation and have a
15-second request deadline. Incoming Work and Case Cost reads carry no request body, Case IDs
are capped at 128 characters, and link/session cursors are nonnegative SQLite integers.
Roster job cursors cap at 512 characters and each list page at 200 rows. In the independent
2,000-session/2,000-job isolated test, four actual roster reads held slots, all 96 further
calls returned 503, and all four admitted reads finished successfully. A first response was
100,272 bytes with 947,392 to 1,450,454 bytes of traced Python peak, excluding native memory
and transport. These measurements describe the fixture. Legacy payload-byte, full usage
response, lineage and native-I/O limits are recorded in CONTEXT.
