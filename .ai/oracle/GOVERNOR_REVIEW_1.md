# GOVERNOR_REVIEW_1 — Scientific Governor review of the O3→O4 milestone conclusion

**Programme:** Oracle Trajectory Inversion. **Case:** 6a4c523570f3444f90a9ea5045664ae9.
**Object under review:** `.ai/oracle/MANAGER_PROPOSED_CONCLUSION.md` (verdict **B — PARTIAL CAPABILITY**).
**Date:** 2026-10-10. **Role contract:** `docs/harness/roles/scientific_governor.md` +
`docs/harness/scientific_governor_contract.md`.
**Stance:** read-only on all evidence; no experiments; no W8/W9 opened; no training. I spot-verified
the Manager's load-bearing numbers against the actual computed tables/JSON, not the prose.

---

## STEELMAN FIRST (the strongest defensible reading of the conclusion)

Read charitably, this conclusion is a disciplined, honestly-bounded partial-capability verdict that
does the hard thing the programme's estimand lock demands: it refuses the binary collapse, keeps the
five `O_t(s)` quantities separate, and draws its economic read from a *continuous* discrimination
surface (tercile separation) rather than the forbidden `P(mfe≥2)` AUC ladder. It correctly carries
the 2026-10-10 LATE amendment (Defects A/B/C/D) forward instead of reverting to the superseded
Output-1/2/3 headlines. Crucially, it **independently reproduces AMEND-B's raw-H geometry null** on a
5×-larger substrate (test excess +0.0662 [−0.0159,+0.1533] vs AMEND-B +0.0674 [−0.0132,+0.1459]) —
i.e. it does not re-commit the prediction-space-as-causal-geometry error; it actively confirms the
null. It flags the GPU-DIAGNOSTIC_ONLY provenance of its discrimination/coverage numbers, flags the
unpredicted adverse-drawdown axis, flags sparse young denominators, and proposes a *cheap, dev-only,
preregistered* next test rather than a new training run. On the evidence I checked, the verdict is
neither a manufactured positive nor a manufactured negative. **This steelman survives.** My findings
below are refinements and one scope-precision fix — not a rebuttal of verdict B.

---

## THE SIX ANSWERS

### 1. Alignment — does it serve the milestone contract and the North Star, or has the target drifted?
**PASS (aligned).** The milestone question (how close is the recognizer to the Oracle outcomes; WHEN
does the gap matter; does it sort economically-valuable states — across components and sizes) is
answered component-by-component and age-by-age, on the canonical continuous `O_t(s)`, not a narrowed
surrogate. The North Star ("earliest causal moment where enough value still remains to trade",
`ORACLE_TRAJECTORY_PROGRAM.md` line 93) is directly engaged: the conclusion locates a ≈180–600s reach
window where recognizability and remaining value (reach-2× ~0.21, q50 upside ~1.42×) overlap, and is
explicit that magnitude skill arrives only after value is gone (a Future-B shape for magnitude). The
estimand lock (`ORACLE_ESTIMAND_LOCK_V1`) is honored — see Q2. No drift to an easier target.

### 2. Validity — was the correct object measured; is the claimed causal info actually present?
**PASS with one scope-precision MATERIAL (M1).** The canonical object `H_t → P(O_t(s)|H_t)` with
`s` a parameter is the one measured; binary `P(mfe≥2)` is used only as the realized-outcome *label*
inside a continuous tercile-separation read, never as a closure object — verified in
`discrimination_tercile_sep.csv` (reach rows carry "realized reach-2x rate" as the realized target of
a continuous-predictor tercile split, not as the predictor). The raw-H geometry work is run on the
**actual released `H_t` substrate** (`GEOMETRY_C_RESULT.json` `clustered_on` = 16 raw as-of H
features: net_sol, hhi/top1 concentration, price slope, vol, dd_from_peak, trail_rate — NOT prediction
columns), which is exactly the AMEND-B correction. Causal-legality and denominator conservation hold
(censoring_conservation_full.json: resolvable 0.9725; censored-unknown only ~2.75%, not counted
negative).
- **M1 (scope precision, decision-relevant):** the conclusion's one-liner says "0–30s reach is
  recognizable (AUC 0.682)" and the body says this "rides summary/current-state features." That AUC is
  from a plain logistic regression on **raw-H + temporal** features at 0–30s (GEOMETRY_C_YOUNG task3b,
  `auc 0.6816 [0.641,0.714]`), i.e. it demonstrates raw `H_t` carries an out-of-sample reach signal at
  0–30s — a *current-state-snapshot-ish* signal (wallet-dispersion + liveness), NOT verified trajectory
  geometry. The conclusion mostly says this correctly ("rides summary/current-state features"), but the
  word "recognizable" sitting next to "REACH-based recognizer from ~180s" invites a snapshot→history
  conflation (pattern 1). The precise, defensible statement is: *a point-in-time H_t read separates
  reach out-of-sample at 0–30s (snapshot-like), while the economic tercile sort on the history head is
  CI-clean only ≥180s, and the transferable static-motif geometry is a bounded null.* This is a
  one-sentence tightening, not a rework. See pattern-1 finding.

### 3. Evidence — what is established vs unproven; any claim broader than the data?
Established (verified against files):
- Component split on CPU system-of-record: R beats C and D* on all three excursion sizes + logtime
  (CI<0), ties reach/retention_exec/definedness, loses retention_ratio to C — matches COMPARISON_OWNER
  §2 and the routing JSON. **Established.**
- Continuous discrimination (`discrimination_tercile_sep.csv`, verified row-by-row): reach sorts ≥180s
  on the R head and at **all** ages on the routed C head (0–180s C +0.1211 [+0.0106,+0.2057] sig);
  excursion sorts only ≥3600s (s=1.0 ≥3600s +0.3568 [+0.0116,+0.606] sig; 0–180s negative/ns);
  retention sorts at all ages. **Established.**
- Raw-H static-motif geometry = bounded null, reproduces AMEND-B. **Established** (GEOMETRY_C task1).
- Adverse drawdown unpredicted; 27.4% of young exec-defined states draw <0.5× before the opportunity
  (`adverse_censoring_oracle_side.csv` exec_defined_0-180s frac_lt_0p5 = 0.2742). **Established.**
- Interval coverage: R excursion well-calibrated (cov80 0.778/0.815/0.793), C over-confident
  (~0.71) — `interval_coverage.csv`. **Established.**
- Reach heads are ~4× biased high in level (oracle 0.0239 vs R 0.1017 / C 0.1105,
  actual_vs_predicted_distribution.csv) — an honest calibration caveat the Manager states.

Not over-claimed: the conclusion nowhere claims protected confirmation, nowhere claims a clean young
economic sweet spot, nowhere claims motif geometry. One residual (R1): the young 0–30s AUC rests on
token-clustered CIs over a sparse eval population (0–30s ≈ 82 states/75 tok at the eval cap; full
young-coverage run uses n_val=5243 — the Manager cited the full-coverage 0.682, the stronger and
correct number). Named, not blocking.

### 4. Continuity — does prior accepted knowledge survive; what does accepting it do to the roadmap?
**PASS.** Nothing accepted is contradicted. O2 (`O2_ORACLE_MEANINGFUL`), O3 canonical
(`PARTIAL_RECOGNIZABILITY_SPLIT_BY_COMPONENT`), H-cert, and the final-representation release all
survive and are *reinforced*: the tercile surface independently reproduces the component split, and the
geometry null independently reproduces AMEND-B. The anti-pendulum rule (RESUME.md 2026-10-08) is
honored — no representation/feature reopening, holdout_looks=0, W8 untouched, W9 undefined (all
machine-verified claims consistent with INVENTORY_B §1.8). Accepting verdict B unlocks exactly the
owner fork the roadmap already names (O4-on-REACH vs Future-B earlier-data for the 60–180s dead band);
it invalidates no later stage and reorders nothing improperly.

### 5. Future simulation — next move under positive / negative / mixed / invalid
- **Positive (preregistered 0–30s decile test passes on VAL):** open O4 policy design on REACH; the
  milestone contract still holds. Correct next action named by the Manager.
- **Negative (decile test fails / no beat over clock):** the 0–30s snapshot edge is not an actionable
  gate → the live region is ≈180–600s reach only, and the 0–30s band folds into the Future-B earlier-
  data question. Milestone still closes at B; no re-run of the science needed.
- **Mixed (sorts but does not beat clock-only):** exactly why the preregistered test's decision rule
  is "beats the clock-only decile contrast" — the Manager built the right discriminating check in.
- **Invalid (GPU-frame artifact):** the one genuine exposure — see M2. If the discrimination surface
  did not survive re-expression on the CPU SoR, the ≥180s reach economic claim would weaken. The
  Manager flags this as residual; I raise it to MATERIAL-but-not-rework (M2) because it is the single
  seam between "development structure" and "owner-final read."

### 6. Decision
**ACCEPT-WITH-QUALIFICATIONS.** Verdict B is defensible and well-grounded; it manufactures neither a
false positive nor a false negative. Close it with two qualifications recorded on the finding (M1
scope-precision wording; M2 GPU-frame provenance as an explicitly-accepted limitation OR a cheap CPU
re-expression). Neither is a BLOCKER; neither forces rework of the science.

---

## PER-PATTERN FINDINGS (the six known scientific-failure patterns)

| # | Pattern | Verdict | Evidence |
|---|---------|---------|----------|
| 1 | Snapshot sold as causal history | **AVOIDED (with M1 wording caveat)** | The 0–30s AUC 0.682 is explicitly attributed to "summary/current-state features" (wallet-concentration/liveness) in both MANAGER_PROPOSED_CONCLUSION and GEOMETRY_C task3; the history-head economic sort is held back to ≥180s. The conflation risk is wording-only (M1), not substance. |
| 2 | Wrong/narrower target substituted | **AVOIDED** | Binary `P(mfe≥2)` appears only as a realized *label* inside continuous tercile separation (discrimination_tercile_sep.csv); closure rests on the continuous `O_t(s)` component surface. Estimand lock honored. |
| 3 | Aggregate improvement sold as early actionability | **AVOIDED** | The conclusion explicitly separates pooled CRPS wins (carried by OLD ages) from young per-case signal: excursion magnitude sorts only ≥3600s "after value is mostly gone" (discrimination CSV s=1.0/2.0 ≥3600s sig, young ns/negative). CRPS lift is not sold as timely signal. |
| 4 | Conditional timing confused with probability-opportunity-exists | **AVOIDED** | Q-TIME (logtime, R WORSE young 7/7 per AMEND-A) and Q-REACH (reach prob, the surviving young edge) are kept distinct throughout; the conclusion routes the young claim to REACH, not timing. |
| 5 | Prediction-space clusters sold as causal-history geometry | **AVOIDED (actively corrected)** | GEOMETRY_C clusters the 16 RAW H features (JSON `clustered_on`), reproduces AMEND-B's null (+0.0662 [−0.0159,+0.1533]), and states the headline raw-H motif geometry is a bounded null / OPEN. This is the AMEND-B error being honored, not repeated. |
| 6 | Development evidence sold as protected confirmation | **AVOIDED** | Every discrimination/coverage/geometry number is labelled GPU DIAGNOSTIC_ONLY or dev; W8 untouched, W9 undefined, holdout_looks=0 (COMPARISON_OWNER §0, INVENTORY_B §1.8). The conclusion's own "Honest residuals" names the GPU-frame reliance. M2 only asks this be elevated from residual to an explicitly-accepted qualification. |

---

## FINDINGS BY SEVERITY

- **BLOCKER:** none.

- **MATERIAL — M1 (scope-precision, decision-relevant to the owner fork wording):** the "0–30s reach
  is recognizable" phrasing should be tightened to "a point-in-time H_t read separates reach
  out-of-sample at 0–30s (snapshot-like, wallet-dispersion+liveness); the history-head economic sort is
  CI-clean only ≥180s; transferable static-motif geometry is a bounded null." Rests on
  `GEOMETRY_C_YOUNG_RESULT.json` (0–30s auc 0.6816) + `discrimination_tercile_sep.csv` (R-reach 0–180s
  −0.0263 ns; C-reach 0–180s +0.1211 sig). **Cheapest repair: one-sentence edit; no re-analysis.**
  Decision-relevant because the owner fork (O4-on-REACH-from-180s vs Future-B for 60–180s) hinges on not
  mistaking a 0–30s snapshot AUC for early *history/trajectory* actionability.

- **MATERIAL — M2 (provenance, bounds the owner-final read):** the discrimination surface (§4), the
  interval coverage (§4b) and the actual-vs-predicted distribution (§1) are all on the **GPU
  DIAGNOSTIC_ONLY** frame (`2cda7aa2…`), not the CPU system-of-record (`1237513b…`). The CPU SoR
  carries only the pre-registered CRPS/Brier deltas, not the tercile discrimination. **This does not
  invalidate verdict B** (the component split it reproduces IS on the CPU SoR), but the *new* economic
  discrimination claim (≥180s reach sorts) lives only on the GPU frame. **Cheapest repair: the Manager
  explicitly ACCEPTS the GPU frame as the owner-final discrimination surface in the closure note, OR
  schedules the cheap CPU re-expression as part of the preregistered next move** — not a blocker, a
  recorded qualification. Rests on COMPARISON_OWNER §0 provenance table + §7 gap #2.

- **RESIDUAL — R1 (sparse young denominators):** 0–30s claims rest on token-clustered CIs over tens–
  hundreds of tokens (eval cap 300 vs 66,223 available). Named by the Manager; densification
  (`layer_b_decision`) is an un-run reopening handle. Does not block closure.

- **RESIDUAL — R2 (internal number discrepancy, non-load-bearing):** `GEOMETRY_C_RESULT.json` task3
  reports 0–30s AUC **0.6591** on a tiny subsample (n_val=80), while `GEOMETRY_C_YOUNG_RESULT.json`
  reports **0.6816 [0.641,0.714]** on full coverage (n_val=5243). The Manager correctly cited the
  full-coverage 0.682. The two files should be cross-referenced so the subsample 0.6591 is not later
  mis-quoted as a weaker contradicting result. Record only.

- **RESIDUAL — R3 (routing-vs-discrimination tension, already flagged by the Manager):** the release
  routes retention and reach to the summary head C on CRPS, yet on realized *discrimination* the R
  retention head out-sorts C young (0–180s R +0.1485 sig vs C −0.0008 ns, discrimination CSV). The
  Manager flags this ("flagged, not resolved"). It is a legitimate open refinement, not a defect.

- **OPPORTUNITY — O1:** an adverse-excursion / drawdown prediction head is the single highest-value
  model add for an ENTER/WAIT read (27% young draw <0.5× before opportunity, unforecastable today).
  Correctly held as a next-move, not built (new training). Advisory only.

---

## DECISION + CHEAPEST USEFUL NEXT ACTION

**DECISION: ACCEPT-WITH-QUALIFICATIONS.** Verdict B (PARTIAL CAPABILITY, component-specific) is
scientifically defensible and the evidence I spot-checked matches the conclusion's claims. No BLOCKER.
The two MATERIAL findings are bounded wording/provenance qualifications that do **not** justify
re-running any science.

**Cheapest useful next action (one edit + one recorded acceptance, then close):**
1. Tighten the M1 sentence (snapshot-vs-history wording) in the conclusion — one line.
2. Record M2 as an explicit owner-facing qualification: the ≥180s reach economic-discrimination claim
   is on the GPU DIAGNOSTIC_ONLY frame; either accept it as the owner-final discrimination surface or
   fold the CPU re-expression into the already-proposed preregistered move (which is CPU, dev-only,
   <30 min, W8/W9 reserved). The Manager's proposed move-1 (0–30s decile test) already stays entirely
   on dev data and is the correct discriminating check — it needs no change from me.

The manager adjudicates; I do not close the Case. Normally one review + one verification pass; I expect
no verification pass is needed unless the manager reworks M1/M2 and asks me to confirm the edits.
