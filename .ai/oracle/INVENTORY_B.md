# INVENTORY_B — Oracle Trajectory Inversion: O3→O4 evidence inventory + gap analysis

Deterministic, read-only inventory for the O3→O4 milestone. Every claim is cited to an actual
file. Dates absolute. **No experiments run, no models trained, no protected data opened.** Where
bundle prose disagrees with the files, it is flagged `[PROSE≠FILES]`.

Authored 2026-10-10. Scope checked against:
- Spec: `C:\Users\Cicada38\Projects\t1r-owner-evidence-bundle\ORACLE_TRAJECTORY_PROGRAM.md`
- Bundle: `C:\Users\Cicada38\Projects\t1r-owner-evidence-bundle\request-13-oracle-o3-o4-bridge.zip`
  (latest, 2026-10-10) + `request-12-oracle-final-representation-release\` (unzipped)
- Live source tree: `C:\Users\Cicada38\Projects\tokens_ingest` on branch
  `jev-oracle-feasibility-lane` — the `evidence/oracle_trajectory/released/` substrate + the
  programme control docs under `programmes/oracle_trajectory_inversion/` are PRESENT on disk.

`[PROSE≠FILES] #1` — The task briefing calls the source branch `programme-control-layer`; the
checked-out branch is actually `jev-oracle-feasibility-lane`, and request-13's own
`OWNER_EVIDENCE_INDEX.md` names that branch at commit `d3e1026b…`. request-12 cites a different
commit `cf92f28a…` on the same branch. Use the branch, not the briefing label.

`[PROSE≠FILES] #2` — The bundle README's "latest = request-9" pointer is STALE for the Oracle line,
as the briefing warned. Confirmed: request-12 (final representation) and request-13 (O3→O4 bridge)
supersede it. The true current head of the Oracle narrative is the **2026-10-10 LATE owner amendment
inside `programmes/oracle_trajectory_inversion/RESUME.md`** (Defects A/B/C), which is NEWER than, and
corrects, the request-13 zip itself. The zip is NOT the final word.

---

## 1. PROGRAMME RECOVERY (past)

### 1.1 Fundamental X / H / O objective
From the spec (lines 19-95) and the estimand lock
(`request-13/.../specs/ORACLE_ESTIMAND_LOCK_V1.md`, mirror of
`tokens_ingest/.ai/specs/ORACLE_ESTIMAND_LOCK_V1.yaml`):

- `X_t` = all legally observable feature values at causal time `t`.
- `H_t` = {X_0 … X_t}, the full causal history through `t` (multiscale, order-bearing).
- `O_t(s)` = **continuous / multicomponent** remaining executable opportunity if capital of size
  `s` enters at `t`. Size `s ∈ {0.5, 1.0, 2.0} SOL` is a **parameter**, not a semantics change
  (depth is carried per crossing so any `s` is recomputable — O0 Contract V2 §U1).
- The scientific object is `H_t → P(O_t(s) | H_t)`, estimated repeatedly across a token's life.
- North Star: the **earliest causal moment** where `H_t` identifies still-capturable opportunity
  ("when does it become predictable while enough value still remains to trade", spec line 93).

**The estimand is LOCKED** (`ORACLE_ESTIMAND_LOCK_V1.md`, incident 2026-10-04): a thresholded /
binary / scalar collapse (e.g. `P(mfe_exec_1p0 ≥ 2.0)`) is a **permitted DIAGNOSTIC projection
only** and is mechanically ineligible to close a phase or mint a null. Enforced by
`research_os/oracle_estimand_gate.py` (ExperimentContract + OBJECT_FIDELITY gate, selftest 5/5).

### 1.2 The canonical `O_t(s)` component vector (the FIVE distinct quantities — MUST NOT conflate)
Backed by real columns in `clockv1_ocs2_full_research_rows.parquet` (137 cols), verified
2026-10-04 (`ORACLE_ESTIMAND_LOCK_V1.md` §"maps to REAL released columns"):

| # | Quantity | Column family | NOT the same as |
|---|---|---|---|
| Q-EXC | **max future upside** — size-aware executable favorable excursion | `mfe_exec_mult_{0p5,1p0,2p0}` | reach prob |
| Q-REACH | **probability of reaching opportunity** (e.g. reach ≥τ) | `reach_prob` / thresholded `mfe` | magnitude |
| Q-TIME | **conditional time-to-opportunity** (reached set) | `time_to_mfe_exec_s_*` / `logtime` | reach prob |
| Q-RET | **remaining value after costs** — retention / giveback | `retention_exec_{s}`, `retention_ratio`, `terminal_exec_mult_*` | upside |
| Q-DEF | **opportunity exists / is executable at all** — definedness | `exec_depth_null`, `entry_unpriceable`, `no_conf_crossing` | any of the above |
| (aux) | persistence duration | `fwd_horizon_s`, `censor_bound_s` | — |

The canonical executable estimand is **candidate C: size-conditional volume-confirmed executable
fill** (A2 constant-product haircut), FROZEN in `ORACLE_TRAJECTORY_O0_CONTRACT_V2.md` §U1. Raw-touch
(A) = diagnostic-only; confirmed-touch (B) = size-free envelope; measured-live-fill (D) =
calibration overlay only (population non-coverage makes it fatal as a label).

### 1.3 SCIENTIFICALLY ESTABLISHED (accepted) vs DEVELOPMENT EVIDENCE
Everything below is **W7 (MODERN_DEV) development evidence** — NONE is protected-validated. Within
dev, these are the accepted phase closures:

- **O2 `O2_ORACLE_MEANINGFUL`** (`request-13/.../oracle_trajectory/O2_ANATOMY_REPORT.md`;
  `validation/O2_ANATOMY_clockv1_ocs2_full.json`; human audit PASS 0 defects
  `O2_HUMAN_AUDIT_clockv1_ocs2_full.json`): executable opportunity is economically REAL, broad
  (exec-defined in 1609/1686 tokens = 95.4%; per-token best mfe median 2.09×; 827 tokens reach
  exec ≥2×), opens EARLY (first ≥2×-exec at token-age median **47s**), remains ~2.5h median,
  survives round-trip 1 SOL execution (median 91% of confirmed envelope), and is **NOT
  artifact-dominated** (zero-tape/silence/raw-wick/censoring all quarantined). Accepted.
- **O3 `PARTIAL_RECOGNIZABILITY_SPLIT_BY_COMPONENT`** (the canonical-object closure,
  `O3_CANONICAL_INVERSION_REPORT.md`; `validation/O3_CANONICAL_INVERSION_clockv1_ocs2_full.json`;
  OBJECT_FIDELITY_PASS). On the HistGBT quantile/hazard envelope, `H_t` beats time-only (A) on
  **time-to-opportunity, retention, and definedness** (CI entirely favouring history) but **NOT on
  excursion magnitude** (time/age already captures the magnitude distribution ≥ as well). This is
  component-specific, not a flat negative. Accepted as the O3 verdict of record.
- **H_t completeness** `H_CERTIFIED_AFTER_MATERIAL_REPAIR_O3_UPDATED`
  (`request-13/.../h_cert/FINAL_VERDICT.md`): `H_t` = **45 causal feature columns** (carried 11 +
  ext1 20 + ext2 6 + ext3 8) certified complete against the 88-object Feature Conveyor universe
  (0 MISSING). The component split survived the ext3 re-eval unchanged.
- **Final representation** `FINAL_REPRESENTATION_RELEASED_COMPONENT_SPECIFIC`
  (`request-12/.../01_FINAL_VERDICT.md`): see §1.5. The representation layer is PERMANENTLY CLOSED
  under a binding anti-pendulum rule.

**DEVELOPMENT EVIDENCE ONLY (structure, not certified):** the three O3→O4 bridge Outputs 1/2/3
(§2) and ALL of their numbers are computed on the **GPU `U5_PREDICTION_FRAME` =
`DIAGNOSTIC_ONLY`**. They characterize structure; they do NOT re-certify anything.

### 1.4 NEGATIVE findings — bounded, and to exactly what
Two negatives exist and must not be confused:

- **BOUNDED DIAGNOSTIC NEGATIVE** (reclassified, NOT a phase closure): the earlier
  `O3_CONVERGENT_NEGATIVE` across 4 representations {time, current-state, flat-multiscale GBT,
  ordered-sequence windowed-MLP}, documented in `O3_BASELINE_LADDER_REPORT.md`. On 2026-10-04 the
  owner ruled this a **governance incident** — it tested only the thresholded binary diagnostic
  `P(mfe_exec_1p0 ≥ 2.0)`, NOT the canonical continuous `O_t(s)`. It was RESCINDED as a phase
  verdict and reclassified to a bounded diagnostic negative; O3 reopened
  (`ORACLE_ESTIMAND_LOCK_V1.md` §incident; RESUME.md STATE 2026-10-04). Scope it kills: **that one
  projection × that substrate × that estimator capacity — NEVER "trajectory information
  exhausted"** (owner-only per spec §Evidence Discipline line 226).
- **The excursion-MAGNITUDE null is REPRESENTATION-BOUNDED, later PARTIALLY RECOVERED.** The
  flat-summary magnitude null was NOT information exhaustion: the ordered full-prefix GRU
  (R_HYBRID) RECOVERED magnitude information the flat panel could not
  (`request-12/.../00_OWNER_README.md` §"Scientific correction recorded"; U5 Q1 = RETAINS).

### 1.5 Frozen CPU vs GPU evidence distinction (where documented)
Documented in `request-13/.../OWNER_EVIDENCE_INDEX.md` §2 & §11, the
`notes/2026-10-10-oracle-o3-o4-bridge-outputs-complete.md` "Watch out for (a)", and RESUME.md
AMEND-D:

- **System-of-record = frozen CPU** `80_FINAL_COMPARISON_RESULT.json`, content-digest
  **`1237513b…`**. This is the one pre-registered scored comparison (C vs D* vs R_HYBRID).
- **GPU re-inference = DIAGNOSTIC_ONLY**, device-tagged **`2cda7aa2…`** (the amendment MD also
  quotes the frame sha `c714657e…`). It produced the 101,100-row prediction frame; persistence
  audit 31/31 PASS; CRPS means recompute bit-exactly from the frame. All three Outputs and the
  amendment read the GPU frame → therefore all three are DIAGNOSTIC_ONLY.

### 1.6 The "released component-specific routing" (what components, what routing)
`request-12/.../41_FINAL_COMPONENT_ROUTING/R_FINAL_COMPONENT_ROUTING_V1.json` (mirror in
request-13 `final_rep/`). The release ROUTES each `O_t(s)` component to whichever already-scored
U5 branch wins its paired per-token CI — **no retrain, no tuning**:

- **→ `H_t` ordered full-prefix (R_HYBRID temporal GRU, hidden=96):** excursion magnitude
  (s=0.5/1/2), time-to-opportunity logtime, persistence duration. (R beats both frozen C and the
  K=64 D* — all CI entirely < 0.)
- **→ `X*_t` summary (FROZEN C, HistGBT row-only heads):** reach probability, retention_exec_1p0,
  retention_ratio, execution definedness.

Honest latent decomposition (routing JSON `latent_source_note_2026_10_09`): persistence is in the
temporal *bucket* because R_HYBRID wins it, but R_HYBRID's persist head actually reads the SUMMARY
latent `s_t`, not the temporal latent `h_t`. So temporal-latent `h_t` = {excursion magnitude +
logtime}; R_HYBRID summary-latent `s_t` = {persistence}; frozen-C summary = {reach, retention×2,
definedness}. Stream widths: canonical per-step `X_t` = 53 dims; temporal STREAM `F_STEP` = 55
(53 + dt_s + silence_flag); summary `D_SUMMARY` = 53; "45" is the LEGACY H_t registry count only.

### 1.7 Current OPEN QUESTIONS (owner forks — NONE auto-dispatch)
From RESUME.md top STATE + `OWNER_EVIDENCE_INDEX.md` §10:
1. Is young-age **reach-probability** recognizability (the one surviving young R edge after the
   amendment) + its ≥180s economic separation ACTIONABLE for an ENTER/WAIT policy → O4 policy
   design opens **on REACH, not magnitude/timing young**?
2. Or is excursion MAGNITUDE the decisive quantity → **Future-B / U8** (earlier-than-fire data,
   owner-only new-data acquisition — the ONLY genuinely unavailable axis)?
3. Register the three outputs as atlas findings (ORACLE-O3O4-OUTPUT1/2/3 — done as findings in
   RESUME.md).

Other standing forks (RESUME.md "INHERITED EVIDENCE LAYER", HARD RULES): Challenger-D escalation
budget is reserved (D justified-not-built); a larger Jev ordered-trace replication; the 10
GENUINELY_UNAVAILABLE + 4 ILLEGAL feature axes (owner-only). U2 WIN/LOSE collapse is closed-by-default.

### 1.8 PROTECTED datasets (W8/W9) and their exact untouched status
Spec §Dataset Roles (lines 228-244): **W8 = protected validation, W9 = final untouched
confirmation**; do NOT spend during construction / representation / arbitration / model search /
distillation. HARD RULES (RESUME.md line 611): "No … W8 / W9 … Zero holdout looks until O2+ under a
new contract."

- **holdout_looks_consumed = 0 (ZERO), machine-verified.** `OWNER_EVIDENCE_INDEX.md` §9 and
  `80_FINAL_COMPARISON_RESULT.json` carry `holdout_looks_consumed=0, zero_verified=True`. The
  search ledger (`evidence/oracle_trajectory/search_ledger.parquet`, 50 moves) tracks DEV search
  moves and rejects any nonzero holdout look; `research_os/holdout_ledger.py` (present on disk) is
  the SEPARATE instrument that would track consumed W8/W9 looks — it records none for this programme.
- **W8 data files exist on disk but were NOT opened by me** (read-only constraint honored). Paths
  (recorded, untouched): `tokens_ingest/evidence/research-v4/cdfe_state_w8.parquet`,
  `.../cdfe_labels_w8.parquet`, `.../evidence/atlas/token_grain_synthesis/token_grain_substrate_w8.parquet`
  (+ two w7∪w8 union files). Status: **PROTECTED, untouched by the Oracle programme** — the V4 split
  gate empirically proved the holdout firewall `W7 ∩ W8 == 0` over 10,495 W8-era tokens
  (RESUME.md STATE 2026-10-03, V4 gate).
- **W9 is UNDEFINED.** `[PROSE≠FILES] #3`: the V4 gate records `W7 ∩ W9 == 0` only as
  **EMPTY-BY-DEFINITION — W9 is not defined in `vintages.yaml`** (U6 owner-only). There is a
  recorded GAP: "re-run V4 once W9 is defined to re-prove against a real set." So W9 is not merely
  untouched — **it does not yet exist as a materialized set.** No W9 parquet was found on disk.

---

## 2. ARTIFACT INVENTORY — Oracle ↔ model evaluation

All paths below are inside `request-13-oracle-o3-o4-bridge.zip` unless marked `[SRC]` (lives on the
`tokens_ingest` working tree, branch `jev-oracle-feasibility-lane`). `dev` = W6/W7 development
evidence; **everything here is dev — nothing is protected.**

### 2.1 W7 Oracle labels (the TRUTH / `O_t(s)`)
- `[SRC] evidence/oracle_trajectory/released/clockv1_ocs2_full_oracle_vector.parquet` (537 MB,
  sha `20b444bf…`) — the Y labels + metadata, continuous multicomponent, oracle `oracle_vec_v1`,
  exec model `a2cp_roundtrip_v1`. Population: 30,464,958 states / 1686 tokens.
- `[SRC] .../clockv1_ocs2_full_research_rows.parquet` (804 MB, sha `645c051d…`) — `H_t` features +
  oracle joined (137 cols). Denominators (O2 anatomy): exec_defined 3,972,060 / 1609 tok;
  no_conf_crossing 26,489,036; entry_unpriceable 838,618; zero-forward 70.8%; 77/1686 tokens
  entirely censored. Measures ALL FIVE quantities (Q-EXC/REACH/TIME/RET/DEF) as separate columns.
  **dev.**

### 2.2 Frozen model + baseline predictions (the PREDICTIONS)
- `80_FINAL_COMPARISON/80_FINAL_COMPARISON_RESULT.json` — **system-of-record, CPU `1237513b…`**.
  The ONE pre-registered scored comparison. Paired per-token CRPS/Brier deltas, token-clustered
  95% CI, for C (frozen HistGBT summary), D* (K=64 GRU refit), R_HYBRID. **dev / system-of-record.**
- `80_FINAL_COMPARISON/U5_PREDICTION_FRAME_test.parquet` (69 MB, 101,100 rows × 115 cols, 337 test
  tokens) — the GPU per-state prediction frame. **CONTAINS ONLY:** keys `token_id, t_s, age_s,
  fold` + three model prediction blocks `R_*`, `C_*`, `Dstar_*` (quantiles q10/25/50/75/90 for exc
  s=0.5/1/2, logtime, retention_exec_1p0, retention_ratio, persist; plus reach_prob, defn_prob).
  **It carries NO truth columns and NO H_t feature columns** (schema read directly). **dev /
  DIAGNOSTIC_ONLY.** This is the source frame for ALL three Outputs.
- `80_FINAL_COMPARISON/u5_r_hybrid_gpu.best_model.pt` (225 KB) + `.ckpt.pt` (910 KB) — the GPU
  R_HYBRID checkpoint (persistence 31/31 PASS, `90_PERSISTENCE_AUDIT.json`). **dev.**
- Model source: `request-12/.../70_U5_FINAL_MODEL/src/{oracle_r_hybrid_v1.py,
  oracle_history_prefix_v1.py, oracle_final_rep_u5_comparison.py}` + `MODEL_CARD.md`.

### 2.3 Per-token / per-state prediction frames
- The U5 prediction frame above is the per-state frame (300-state/token eval cap; 337 test tokens).
- Smoke variants: `80_FINAL_COMPARISON/pilot/U5_PREDICTION_FRAME_test_smoke16.parquet`;
  validations `U5_FINAL_COMPARISON_clockv1_ocs2_full{,_smoke12,_smoke16}.json`.

### 2.4 Component-specific model comparisons
- `final_rep/R_FINAL_COMPONENT_ROUTING_V1.json` — per-component R-C and R-D* paired CIs, the
  routing decision (§1.6). Covers all 5 quantities + persistence. **dev.**
- `final_rep/80_FINAL_COMPARISON/80_FINAL_COMPARISON_RESULT.md` — the five-questions read (Q1
  RETAINS magnitude; Q2 DEGRADES_SOME; Q3 INFERIOR_ON_SOME_STRONG_CELL; Q4 first recognizable age
  >3600s; Q5 economic VOI reserved to owner).

### 2.5 Age-specific analyses (Output 1 — fine causal-age RECOGNITION × REMAINING-VALUE surface)
- `final_rep/82_OUTPUT1_AGE_VALUE_SURFACE/OUTPUT1_AGE_VALUE_SURFACE_RESULT.{json,md}`. Age grid
  0..86401s; per-bin n_tokens, powered flag, R_vs_C and R_vs_A paired CRPS deltas [CI], reach2x
  rate, remaining-exec q50 — **for EACH of the 5 quantities separately** (excursion_0p5/1p0/2p0,
  logtime, retention_exec_1p0, retention_ratio, persistence, reach_prob, definedness). This is the
  single richest per-quantity × per-age artifact. **dev / DIAGNOSTIC_ONLY.** See §3 for which
  quantity each column is.

### 2.6 O-side phenotypes (Output 2)
- `final_rep/85_O_PHENOTYPE_DISCOVERY/O_PHENOTYPE_DISCOVERY_RESULT.{json,md}` + frozen prereg
  `final_rep/O_PHENOTYPE_PREREG_V1.{json,md}`. GMM over 5 oracle-side axes (reach_magnitude,
  retention_giveback, timing, persistence, definedness). k=6 families (raw economic units table:
  mfe 1.18×–7.28×, retention 0.09–0.93, timing 42s–3119s). **H-INDEPENDENT** (clusters on O_t
  outcomes, NOT on H_t). Eligible exec-defined tokens 1609 (train 956/val 332/test 321). **dev.**

### 2.7 O4 trajectory geometry (Output 3)
- `final_rep/84_OUTPUT3_H_GEOMETRY/OUTPUT3_H_GEOMETRY_RESULT.json`. k=8 motifs clustered on the
  **6 R-PREDICTED channels** (reach, logtime_q50, retention×2, persist, defn), age×exposure-matched
  reach-2x separation, excess-over-null +0.0488 (per-cell table of 19 cells). **dev /
  DIAGNOSTIC_ONLY — and the headline is WITHDRAWN (see §2.9).**
- O3 recognition surface `[SRC] validation/O3_RECOGNITION_SURFACE_clockv1_ocs2_full.json`
  (per-component, per-age).

### 2.8 Baseline ladder + canonical inversion (the O3 of-record)
- `oracle_trajectory/O3_BASELINE_LADDER_REPORT.md` + `validation/O3_BASELINE_LADDER_clockv1_ocs2_full.json`
  — A_time / B_current_state / C_multiscale / D_sequence token-AUC on the BINARY DIAGNOSTIC target
  `P(mfe_exec_1p0 ≥ 2.0)`. **DIAGNOSTIC — reclassified, does not close O3.**
- `oracle_trajectory/O3_CANONICAL_INVERSION_REPORT.md` +
  `validation/O3_CANONICAL_INVERSION_clockv1_ocs2_full.json` — the CANONICAL multicomponent O3
  (the split verdict). **dev / phase closure of record.**

### 2.9 The scientific AMENDMENTS correcting overstated conclusions (THE CURRENT SoT)
`[SRC] evidence/oracle_trajectory/final_rep/86_REQUEST13_AMENDMENT/REQUEST13_AMENDMENT_RESULT.{json,md}`
(result_sha `7a948a05…`, reproduced bit-for-bit, selftest 5/5; module
`research_os/oracle_o3_o4_request13_amendment.py`). Summarized in RESUME.md top STATE
(2026-10-10 LATE). **This supersedes the request-13 zip's Output headlines on three points:**

- **AMEND-A (Output 1): the three young-age channels are DISTINCT, not one "arrival/timing skill".**
  Powered young-bin sign tally (R vs frozen-C, CI-excl-0): **reach_probability R_BETTER 7/7;
  conditional_timing R_WORSE 7/7; excursion_magnitude R_WORSE 6/7.** Only REACH-PROBABILITY is a
  genuine young R edge. Economically, top-vs-bottom R-reach tercile separates oracle reach-2x
  **only from ~180s onward** (0–180s CIs all straddle 0). Finding
  `ORACLE-O3O4-OUTPUT1-AGE-VALUE-01` (validated-with-negative).
- **AMEND-B (Output 3): `H_GEOMETRY_REAL_AND_RECOGNIZED` WITHDRAWN.** Output 3 clustered
  PREDICTION-space channels with no aggregate CI. Corrected: prediction-space excess +0.0488
  [+0.0087, +0.0818] (excl 0) BUT the **actual RAW causal-history H-feature clustering gives +0.0674
  [−0.0132, +0.1459] — STRADDLES 0.** Verdict
  `PREDICTION_SPACE_SEPARATES_BUT_RAW_H_GEOMETRY_NOT_ESTABLISHED` (null). Prediction-space
  association is NOT proof of causal-history motifs.
- **AMEND-C (Output 2): six phenotypes are DEVELOPMENT-SUPPORTED CANDIDATES, not confirmed types.**
  k=7 failed TRAIN bootstrap; only k=6 passed. Held-out occupancy VAL 4/6, TEST 5/6. Finding
  `ORACLE-O3O4-OUTPUT2-PHENOTYPES-01` (null).
- **AMEND-D: provenance** — all three outputs read the GPU `DIAGNOSTIC_ONLY` frame; that tag was
  missing and is now asserted. Component-specific routing PRESERVED (routing JSON untouched).

### 2.10 Supporting validators / H-cert / inspection
`[SRC] validation/`: O3_OBJECT_FIDELITY, O3_CHALLENGER_D, O3_HT_COVERAGE_MAP, O3_REEVAL_EXT3,
MODEL_INPUT_FIDELITY_GATE_V1, SUMMARY_EQUIVALENCE_GATE_V1, U5_TRAINING_INPUT_TRANSFORM_GATE_V1,
V4_split_materialization, O2_HUMAN_AUDIT. `h_cert/` (30+ files: 88→H crosswalk, column inventory,
temporal basis yaml, semantic recoverability). `inspection/` 7 case JSONs (positive_high_conf_mfe,
negative_deep_adverse, awkward_opportunity_rich_destructive, early_censor, dead_short_lived, etc.).

---

## 3. GAP ANALYSIS — toward the owner-readable comparison

Milestone question: **how close is the causal recognizer to the Oracle's opportunity outcomes;
when does the gap matter; does it discriminate economically valuable opportunities from
unattractive states — across components and execution sizes.**

### 3.1 What the 5 quantities map to in existing artifacts (NO conflation)

| Quantity | Predicted-distribution artifact | Oracle-truth artifact | Covered? |
|---|---|---|---|
| Q-EXC max upside (per size) | `U5_PREDICTION_FRAME` `R/C/Dstar_exc_{0p5,1p0,2p0}_q*`; routing CIs; Output 1 excursion_* rows | `..._research_rows.parquet` `mfe_exec_mult_{s}` | **YES** (per size, per age) |
| Q-REACH reach probability | frame `*_reach_prob`; Output 1 `reach_prob` rows; AMEND-A | thresholded `mfe` reach2x | **YES** |
| Q-TIME conditional time-to-opp | frame `*_logtime_q*`; Output 1 `logtime`; routing CI | `time_to_mfe_exec_s_*` | **YES** (but R WORSE young — AMEND-A) |
| Q-RET remaining value after costs | frame `*_ret_retention_{exec_1p0,ratio}_q*`; Output 1 | `retention_exec_{s}`, `retention_ratio`, `terminal_exec_mult_*` | **YES** |
| Q-DEF opportunity exists | frame `*_defn_prob`; Output 1 `definedness` | `exec_depth_null/entry_unpriceable/no_conf_crossing` | **YES** |

### 3.2 What the existing artifacts ALREADY answer
- **Oracle outcome distribution** — YES. O2 anatomy (per-token + per-state, all 5 quantities,
  denominators conserved); `oracle_vector.parquet` is the full truth.
- **Predicted distribution** — YES, as QUANTILES (q10..q90) per component per model in the U5 frame.
- **Prediction error / calibration (component)** — PARTIAL. Paired CRPS (quantile heads) / Brier
  (prob heads) with token-clustered CIs exist for every component (routing JSON; final-comparison
  JSON; Output 1 by-age). The binary-target ladder has ECE + coverage (cov80). **Gap:** there is NO
  single component-and-size calibration/coverage surface (reliability curves, PIT, interval
  coverage) for the CANONICAL continuous heads assembled in one owner-readable place — the frame has
  the raw material (quantiles + ... ) but the reliability aggregation is not produced.
- **Lift vs baselines** — YES for R-vs-C (frozen-summary) and R-vs-A (time/clock) at every age bin
  and size (Output 1). R-vs-B (current-state) exists in the O3 canonical/ladder but is NOT carried
  into the by-age Output-1 surface for the continuous components.
- **Earliest reliable recognition region + remaining value there** — PARTIAL, and this is the
  crux. Output 1 gives reach2x rate + remaining-exec q50 per age bin (remaining value IS present).
  BUT: (i) on the BINARY ladder, "earliest reliable recognition (AUC CI-lo≥0.60 ∧ ECE≤0.10) = NONE
  at any age" (`O3_BASELINE_LADDER_REPORT.md`); (ii) on the continuous side, AMEND-A shows the only
  young R edge is REACH-PROBABILITY, and its ECONOMIC separation (does a high-R-reach state actually
  sort to higher oracle reach2x) only turns CI-positive **from ~180s onward** — young-young (0–180s)
  straddles 0. So the sweet-spot region is characterized but not cleanly established young.
- **Adverse movement (MAE) exists as a label** (`mae_total_mult`, `mae_before_mfe_mult` in the
  oracle vector) — but NO model HEAD predicts it; the frame has no adverse-excursion prediction.

### 3.3 GENUINELY MISSING to produce the owner-readable comparison (the real gaps)
1. **Discrimination of attractive-vs-unattractive states, and FP/FN/coverage, is NOT produced for
   the canonical continuous object.** The U5 frame scores distributional ACCURACY (CRPS/Brier), not
   DECISION discrimination. The only discrimination numbers (token-AUC, within-age-bin AUC, FP/FN
   implied by a threshold) live on the RECLASSIFIED binary diagnostic ladder, which the estimand
   lock forbids as a closure object. AMEND-A's top-vs-bottom-tercile reach2x separation is the
   ONLY economic-discrimination read on the continuous side, and it is single-component (reach) and
   only ≥180s. There is no attractive-vs-unattractive confusion surface (FP/FN/coverage) over the
   canonical components × sizes. **This is the #1 blocker.**
2. **No assembled calibration + interval-coverage surface for the continuous heads, per component
   × per size × per age.** The quantile predictions and the truth both exist, but the owner-readable
   object — "for each (component, size, age region): predicted vs realized, calibration, coverage,
   lift over clock AND over current-state" — is not materialized as one comparison. The by-age
   Output 1 is close but reports only R-vs-C and R-vs-A deltas, omits B, omits coverage/reliability,
   and is DIAGNOSTIC_ONLY (GPU).
3. **Adverse-movement / execution-feasibility / censoring are LABELS but not PREDICTED, and not
   folded into the comparison.** Q-RET (retention) is predicted and censoring is conserved in the
   denominators, but adverse excursion (MAE-before-opp) has no prediction head, and execution
   feasibility beyond the definedness flag (depth/fill at decision time) is not surfaced per state
   in the comparison. Without the adverse + feasibility axes, an ENTER/WAIT owner read cannot net
   upside against drawdown/fill risk.

Secondary gaps: the owner comparison is on **337 TEST tokens / GPU DIAGNOSTIC frame** — to be
owner-final it must be re-expressed on the CPU system-of-record (or the gap explicitly accepted);
and young-age denominators are sparse (0–30s = 82 states/75 tokens, EVAL cap 300 vs 66,223
available — `OUTPUT1…RESULT.md` denominator audit), so any young sweet-spot claim rests on
token-clustered CIs over ~75–265 tokens with ~1 state/token (governed densification is a scoped
reopening handle, `layer_b_decision`, NOT yet run).

---

## 4. Objective-3 handoff — ACTUAL `H_t` vs model-PREDICTION artifacts

**Critical distinction for the geometry work: use real `H_t`, not prediction clusters.** This is
exactly the error AMEND-B caught (Output 3 clustered prediction-space, not raw H).

### 4.1 ACTUAL causal-history `H_t` artifacts (real trajectories / substrate)
All `[SRC]` on `tokens_ingest` branch `jev-oracle-feasibility-lane`,
`evidence/oracle_trajectory/released/` (present on disk; NOT in the zip — too large):
- `clockv1_ocs2_full_causal_states.parquet` (332 MB, sha `67609c0e…`) — the carried causal state
  features at all 30,464,958 states.
- `clockv1_ocs2_full_causal_ext1.parquet` (356 MB, `8d9af07d…`) — 20 flow/breadth/concentration
  slope cols (the AVAILABLE_BUT_OMITTED repair).
- `clockv1_ocs2_full_causal_ext2.parquet` (325 MB, `4296eac5…`) — 6 vol/depth-slope/recency cols.
- `clockv1_ocs2_full_causal_ext3.parquet` (205 MB, `2f54bca4…`) — 8 flow-accel + per-wallet
  round-trip (flipper/roundtrip) cols.
- `clockv1_ocs2_full_causal_ext4.parquet` (174 MB, `b5795988…`) — U1 ext4 canonical-X ADDs.
- `clockv1_ocs2_full_research_rows.parquet` (804 MB) — `H_t` (the 45-col registry) joined to oracle.
- `V4_split_assignment_clockv1_ocs2_full.parquet` (116 KB, `cad6874d…`) — token-grouped
  chronological split (train 1012 / val 337 / test 337).
- Frozen H contract/basis (in-zip): `request-13/.../h_cert/{H_REPRESENTATION_FREEZE_V1.json,
  H_TEMPORAL_BASIS_V1.yaml, H_COLUMN_INVENTORY_V1.{csv,json}}`; builders
  `research_os/oracle_causal_{ext1,ext2,ext3}.py`, `oracle_causal_sampler_v1.py` (the authority on
  causal-state eligibility). **These are the real `H_t` the O4 geometry MUST cluster.**

The per-step ordered STREAM the GRU folds is `F_STEP = 55` (53 canonical X + dt_s + silence_flag);
canonical per-step `X_t = 53`. These are reconstructable from the released parquets + the frozen
contract; they are the genuine causal history.

### 4.2 Model-PREDICTION artifacts (DO NOT treat as H_t)
- `U5_PREDICTION_FRAME_test.parquet` — **predictions only** (R_/C_/Dstar_ quantiles + probs + 4
  keys). No H_t columns, no truth. Confirmed by direct schema read.
- Output 3 `OUTPUT3_H_GEOMETRY_RESULT.json` clusters the **6 R-PREDICTED channels** — this is
  prediction-space, and its "real geometry" headline is WITHDRAWN (AMEND-B). The RAW-H clustering
  that AMEND-B ran (excess +0.0674 [−0.0132, +0.1459], straddles 0) is the correct method but is
  NOT YET ESTABLISHED — O4 geometry on real `H_t` is genuinely OPEN.

### 4.3 Where temporal-model vs summary-head outputs are separable
Separable by column prefix in the frame AND by latent in the model
(`R_FINAL_COMPONENT_ROUTING_V1.json` `latent_source_note`):
- **Temporal latent `h_t` (ordered full-prefix GRU):** excursion magnitude `R_exc_*` + logtime
  `R_logtime_*`.
- **R_HYBRID summary latent `s_t`:** persistence `R_persist_*`.
- **Frozen-C summary (HistGBT, row-only X*_t):** reach `C_reach_prob`, retention `C_ret_*`,
  definedness `C_defn_prob`.
The `C_*` block is the pure summary/current-state model; the `R_*` block mixes temporal + summary
latents by component. D* (`Dstar_*`) is the K=64 refit GRU comparator. So Objective-3 geometry can
cleanly isolate what the TEMPORAL (order-bearing) path contributes vs the SUMMARY path — but it must
do so on the raw `H_t` substrate (§4.1), not on these prediction columns.

---

## Appendix — provenance one-liners
- Source branch (verified `git branch --show-current`): `jev-oracle-feasibility-lane`.
- holdout_looks consumed: 0 (zero_verified). W8 files present-but-untouched; W9 undefined.
- System-of-record = CPU `1237513b…`; every Output + the amendment = GPU `DIAGNOSTIC_ONLY`.
- Current SoT for the O3→O4 read = RESUME.md 2026-10-10 LATE amendment, NOT the request-13 zip.
