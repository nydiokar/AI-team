# COMPARISON_OWNER — Oracle outcomes vs causal-recognizer predictions (O3→O4)

**Purpose.** One owner-readable surface consolidating how close the causal recognizer (`H_t`
model, released as component-specific routing) comes to the Oracle's realized opportunity
outcomes `O_t(s)`, per component × execution size × causal age — and where the gap matters.
Built from EXISTING frozen artifacts plus development-only CPU analysis over the released
parquets. **No model trained. No W8/W9 opened. holdout_looks_consumed = 0.**

Ground truth for the inventory: `.ai/oracle/INVENTORY_B.md`. SoT for the O3→O4 read:
`tokens_ingest/programmes/oracle_trajectory_inversion/RESUME.md` (2026-10-10 LATE amendment).

---

## 0. Provenance, populations, denominators (READ FIRST — every table below is labelled)

| Provenance tag | What it means | Which numbers |
|---|---|---|
| **CPU SYSTEM-OF-RECORD** | the one pre-registered scored comparison, `80_FINAL_COMPARISON_RESULT.json` content-digest `1237513b…` | the per-component paired CRPS/Brier deltas quoted from `R_FINAL_COMPONENT_ROUTING_V1.json` §component_routing |
| **GPU DIAGNOSTIC_ONLY** | GPU re-inference frame `U5_PREDICTION_FRAME_test.parquet` (device-tagged `2cda7aa2…`; the on-disk `80` JSON is this device tag) — characterizes structure, does NOT re-certify | Output-1 by-age surface; my §3 discrimination; my §1 actual-vs-predicted; my §4 coverage |
| **TRUTH (released)** | `clockv1_ocs2_full_research_rows.parquet` (sha `645c051d…`) + `…_oracle_vector.parquet` (sha `20b444bf…`); continuous multicomponent `O_t(s)` | all "oracle actual" columns; §5 adverse/censoring |

**Populations / denominators used below (stated once, referenced per table):**
- **FULL oracle population** = 30,464,958 states / 1686 tokens (oracle_vector).
- **TEST join (this doc's compute pop)** = the 337-test-token GPU frame joined to truth on
  `(token_id, t_s)` — **101,100 pred rows, join = 101,100 / 101,100 (100%)**. This is the only
  population on which predictions and truth coexist, and it is **GPU DIAGNOSTIC_ONLY**.
- **exec_defined** = `exec_depth_null==0 ∧ entry_unpriceable==0 ∧ no_conf_crossing==0` →
  **11,981 states / 305 tokens** in the TEST join (opportunity is defined & priceable). Used for
  excursion/retention (those quantities are only meaningful where opportunity exists).
- **resolvable** (reach) = exec_defined ∪ (observed-no-opportunity: `no_conf_crossing==1 ∧
  entry_unpriceable==0`) → **96,868 states / 323 tokens**. Matches the baseline-ladder "resolvable"
  population; censored-unknown/unpriceable states are NOT counted as negatives.

**The 5 quantities are kept strictly separate throughout** (Q-EXC magnitude, Q-REACH reach prob,
Q-TIME time-to-opp, Q-RET retention, Q-DEF definedness; + persistence aux). A 2× reach probability
is NOT a prediction of a strategy earning 2×; a forecast CRPS is NOT capital captured.

**Released component routing (the thing being evaluated), from `R_FINAL_COMPONENT_ROUTING_V1.json`:**
- **→ `H_t` ordered full-prefix (R_HYBRID temporal GRU, hidden=96):** excursion magnitude
  (s=0.5/1/2), time-to-opportunity logtime, persistence (persist head reads the *summary* latent).
- **→ `X*_t` summary (FROZEN C, HistGBT row-only):** reach probability, retention_exec_1p0,
  retention_ratio, execution definedness.

---

## 1. Actual Oracle distribution vs Predicted distribution (per component × size)

Table: `tables/actual_vs_predicted_distribution.csv`. Population = **TEST join, exec_defined**
(11,981 states / 305 tok) for excursion & retention; **resolvable** (96,868 / 323) for reach.
"oracle_actual" = empirical quantile of the realized label over the population; "*_pred_head_mean"
= mean of each model's quantile head over the same states. **GPU DIAGNOSTIC_ONLY.**

**Excursion `mfe_exec_mult` (Q-EXC) — multiples (×):**

| size | q | oracle actual | R head | C head |
|---|---|---|---|---|
| 1.0 | q10 | 0.968 | 0.925 | 0.999 |
| 1.0 | q50 | 1.205 | 1.199 | 1.329 |
| 1.0 | q90 | 2.928 | 2.166 | 3.250 |
| 0.5 | q90 | 3.061 | 2.282 | 3.623 |
| 2.0 | q90 | 2.669 | 1.939 | 2.827 |

Read: at the **median** R is near-exact (1.199 vs 1.205 at s=1.0); in the **upper tail (q90)** R
*under*-shoots (2.17 vs 2.93) while C *over*-shoots (3.25). R is the tighter, better-centred
excursion distribution (confirmed by the lower CRPS, §2), C is more dispersed. Plain-unit gap: at
s=1.0, R's 90th-pct excursion call is ~0.76× low; the Oracle's realized upside tail is heavier
than R admits.

**Retention `retention_exec_1p0` (Q-RET) — fraction of favorable excursion kept:**

| q | oracle actual | R head | C head |
|---|---|---|---|
| q10 | 0.080 | 0.313 | 0.319 |
| q50 | 0.525 | 0.541 | 0.499 |
| q90 | 0.939 | 0.834 | 0.784 |

Read: **both** models badly miss the LOW tail — oracle q10 retention is 0.08 (you can keep only 8%
after costs/giveback in the worst decile) but both heads floor near 0.31. Retention giveback risk
in the bottom decile is systematically under-predicted by both.

**Reach probability (Q-REACH):** oracle realized reach-2× rate over resolvable = **0.0239**; R head
mean 0.102, C head mean 0.111. Both heads are ~4× too high on average — a calibration bias (they
over-state reach), though they still *rank* correctly where it matters (§3).

---

## 2. Calibration + prediction error per component (CPU SYSTEM-OF-RECORD deltas)

Consolidated from `R_FINAL_COMPONENT_ROUTING_V1.json` (quotes the CPU `1237513b…` per-component
paired deltas) and `80_FINAL_COMPARISON_RESULT.json`. Metric = paired per-token **CRPS** (quantile
heads) or **Brier** (prob heads); **NEG = R better**; token-clustered 95% CI; n_tokens per cell.

| Component (size) | metric | R−C mean [CI] | R−D* mean [CI] | routing |
|---|---|---|---|---|
| excursion 0.5 | CRPS | −0.0413 [−0.054,−0.029] | −0.0148 [−0.023,−0.007] | **R (H_t)** |
| excursion 1.0 | CRPS | −0.0333 [−0.044,−0.022] | −0.0146 [−0.022,−0.007] | **R (H_t)** |
| excursion 2.0 | CRPS | −0.0276 [−0.037,−0.019] | −0.0140 [−0.021,−0.007] | **R (H_t)** |
| time-to-opp logtime | CRPS | −0.0376 [−0.060,−0.015] | −0.0429 [−0.063,−0.022] | **R (H_t)** |
| persistence | CRPS | −0.0164 [−0.031,−0.003] | — | **R (model, summary latent)** |
| reach prob | Brier | +0.0006 [−0.001,+0.002] (TIE) | −0.0008 [−0.002,+0.001] | **C (summary)** |
| retention_exec_1p0 | CRPS | +0.0004 [−0.002,+0.003] (TIE) | −0.0106 [−0.014,−0.007] | **C (summary)** |
| retention_ratio | CRPS | **+0.0070 [+0.0065,+0.0075] (C better)** | +0.0008 [−0.001,+0.002] | **C (summary)** |
| definedness | Brier | +0.00005 [−0.0015,+0.0016] (TIE) | −0.0015 [−0.003,−0.00003] | **C (summary)** |

Established and preserved: R (full-prefix history) **beats both C and D\* on all three excursion
sizes + logtime** (CI entirely < 0). On reach / retention_exec / definedness R only **ties** C and
on retention_ratio R is **worse** than C — this is the FUTURE-C degraded cell that forced
component-specific routing rather than one shared hybrid. The routing sends each component to its
CI winner. This is the O3 `PARTIAL_RECOGNIZABILITY_SPLIT_BY_COMPONENT` verdict, intact.

---

## 3. Lift vs baselines — R-vs-A (clock), R-vs-C (summary), and the R-vs-B gap

**By-age lift, consolidated from Output-1** (`82_OUTPUT1_AGE_VALUE_SURFACE`, GPU DIAGNOSTIC_ONLY;
sign = paired per-token delta R−X, **NEG = R better**). Full per-component × per-age tables are in
Output-1's MD; the decisive shape, per the amendment (AMEND-A powered-young sign tally R vs C):

| young channel | sign tally (7 powered young bins) | meaning |
|---|---|---|
| **reach_probability** | **R_BETTER 7/7** | the one genuine young R edge |
| conditional timing (logtime) | **R_WORSE 7/7** (e.g. +0.87 @0-30s) | clock/summary already time arrival better young |
| excursion magnitude | **R_WORSE 6/7, 1 tie** | R's magnitude edge is NEGATIVE young, turns positive only ≥3600s (where reach has collapsed) |

So R's excursion-CRPS win (§2, pooled) is carried by **old** ages; young, R is worse than the clock
on magnitude and timing and better only on reach. This is why "early arrival/timing skill" was
withdrawn (AMEND-A) and only reach survives as a young edge.

**R-vs-B (current-state) — the inventory's flagged gap.** B (current_state) predictions **do NOT
exist in released form for the continuous components.** The released frame carries only
`R_`, `C_`, `Dstar_` blocks (115 cols, schema read directly) — there is no B block. B exists ONLY
on the **reclassified binary-diagnostic ladder** (`O3_BASELINE_LADDER…json`), where it is a token-
AUC on `P(mfe_exec_1p0≥2.0)`: B_current_state TEST token-AUC = **0.697 [0.658, 0.737]**, and the
ladder's own `B_vs_A_test Δ-AUC = −0.102` (B *below* clock) and `C_vs_B_test Δ-AUC = +0.070` (the
multiscale/history layer beats current-state). **Bound on the claim:** the current-state baseline is
beaten by the history layer *on the binary diagnostic only*; this cannot be carried into the
canonical continuous estimand because no continuous B head was scored — computing it would require
training a head, which is out of scope (recorded as a next-move, §7). **Note on C:** in the
final-rep layer, frozen-C is the *row-only X\*_t summary* — i.e. a current-state summary model — so
R-vs-C is the closest released continuous proxy for "lift over current-state", and §2 shows R only
ties/loses to it outside magnitude+logtime+persistence.

---

## 4. Discrimination — does a high-predicted state sort to higher realized Oracle outcome? (GAP #1)

This is the inventory's **#1 blocker**: the only discrimination numbers that existed were on the
forbidden binary ladder. Here is a **legitimate discrimination read on the canonical continuous
object, without collapsing to the locked-out binary**: for each component × size × age region,
split exec_defined states into **predicted terciles** and measure the **realized-outcome
separation** (top-tercile realized mean − bottom-tercile realized mean), with **token-clustered
bootstrap 95% CI** (500 resamples over tokens). This is a **development-only diagnostic**; the
tercile split is an *analytical* selection budget (fixed 1/3 cut), explicitly **NOT** an optimized
trading policy, and I do **not** convert it to "% of Oracle profit captured" (no decision/execution
rule is specified). Table: `tables/discrimination_tercile_sep.csv`. **GPU DIAGNOSTIC_ONLY.**

Separation in realized units (× for excursion, rate for reach, fraction for retention); R = the
routed/released predictor where applicable; sig = CI excludes 0.

| component (size) | 0-180s | 180-900s | 900-3600s | ≥3600s |
|---|---|---|---|---|
| **reach (R head)** | −0.026 [−0.110,+0.077] **ns** | +0.108 [+0.022,+0.189] **sig** | +0.157 [+0.101,+0.225] **sig** | +0.040 [+0.027,+0.055] **sig** |
| **reach (C head = routed)** | +0.121 [+0.011,+0.206] **sig** | +0.139 [+0.061,+0.216] **sig** | +0.196 [+0.143,+0.255] **sig** | +0.040 [+0.028,+0.055] **sig** |
| excursion 1.0 (R) | −0.41 [−1.14,+0.32] ns | −0.04 [−0.47,+0.29] ns | +0.13 [−0.34,+0.57] ns | +0.36 [+0.01,+0.61] sig |
| excursion 2.0 (R) | −0.01 [−0.61,+0.60] ns | +0.09 [−0.19,+0.37] ns | +0.08 [−0.29,+0.46] ns | +0.43 [+0.20,+0.67] sig |
| retention 1.0 (R head) | +0.149 [+0.058,+0.258] **sig** | +0.225 [+0.121,+0.342] **sig** | +0.316 [+0.251,+0.380] **sig** | +0.371 [+0.276,+0.438] **sig** |
| retention 1.0 (C = routed) | −0.001 [−0.169,+0.125] ns | +0.160 [+0.078,+0.246] sig | +0.267 [+0.098,+0.373] sig | +0.310 [+0.221,+0.388] sig |

**Reads (these do NOT contradict the established record; they independently reproduce and sharpen it):**
1. **Reach discrimination is the clean positive, and the ≥180s boundary reproduces** — the *routed*
   predictor for reach is **C** (frozen summary), and C-reach sorts attractive from "attractive"
   states at **every** age region including 0-180s (+0.121 sig). The **R** reach head straddles 0
   at 0-180s and only turns significant ≥180s — exactly AMEND-A's economic-separation boundary.
   So: the *component* reach is discriminable young, but the *history* model buys nothing young over
   the summary; the summary head is what carries young reach. This is a refinement worth the owner's
   attention — AMEND-A's "R-reach ≥180s" is a statement about the *history* head, not about reach
   being undiscriminable young (the summary head discriminates reach young).
2. **Excursion magnitude does NOT discriminate young** on realized multiples — no significant sort
   until ≥3600s (where remaining value is nearly gone, §5). Realized `mfe` is heavy-tailed so the
   CIs are wide; still, the young cells straddle (and 0-180s s=1.0 is even negative). Magnitude is
   not an early decision signal.
3. **Retention sorts at every age** — and the R retention head out-sorts the routed C head **young**
   (0-180s: R +0.149 sig vs C −0.001 ns). The release routes retention to C on CRPS; on realized
   *discrimination* R's retention head is the better young sorter. Flagged, not resolved.

**FP/FN/coverage:** a confusion surface (FP/FN) only exists against a binary cutoff, which the
estimand lock forbids as a closure object. The legitimate continuous analogue is the tercile
separation above (does high-predicted sort high-realized) + interval coverage (§4b). No binary
decision threshold is minted.

### 4b. Interval coverage of the continuous heads (calibration, GAP #2 partial)

Table: `tables/interval_coverage.csv`, exec_defined TEST join, GPU DIAGNOSTIC_ONLY. Empirical
coverage of each model's central interval vs nominal:

| component (size) | model | cov80 (nom 0.80) | cov50 (nom 0.50) |
|---|---|---|---|
| excursion 1.0 | R | **0.815** | 0.503 |
| excursion 1.0 | C | 0.708 | 0.395 |
| excursion 2.0 | R | 0.793 | 0.579 |
| excursion 2.0 | C | 0.713 | 0.396 |
| retention 1.0 | R | 0.737 | — |
| retention 1.0 | C | 0.660 | — |

R's excursion intervals are **well-calibrated** (80%/50% near nominal); C's are **over-confident**
(80% interval only covers ~71%). This is consistent with §1 (C's q90 over-shoots yet its central
band is too tight). On the continuous object R is the better-calibrated excursion predictor.

---

## 5. Earliest reliable recognition region + remaining executable value there

Consolidated from Output-1 (reach2x rate + remaining-exec-q50 per age bin) and my §4.

**The honest region (the crux):**
- On the **binary ladder**, earliest reliable recognition (AUC CI-lo≥0.60 ∧ ECE≤0.10) = **NONE at
  any age** (`O3_BASELINE_LADDER…`, `earliest_reliable_recognition.found=False`). Diagnostic-only.
- On the **canonical continuous object**, the only component with a young recognizability edge is
  **reach**, and its *economic separation* (does predicted-high reach sort to higher realized
  reach-2×) is **CI-positive only from ≈180s onward** (§4: 0-180s straddles 0 for the history head;
  the summary head is positive earlier). So the earliest cleanly-established recognition region is
  **≈180-900s on REACH**, not 0-180s, and not on magnitude/timing.

**Remaining executable value at that region (Output-1, per age bin, exec_defined denominators):**

| age bin | n_tok | exec_defined frac | reach-2× rate | remaining `mfe_exec_1p0` q50 | q90 | retention median |
|---|---|---|---|---|---|---|
| 0-30s | 75 | 0.524 | 0.146 | 1.613 | 3.509 | 0.122 |
| 30-60s | 98 | 0.805 | 0.212 | 1.446 | 3.681 | 0.150 |
| 120-180s | 156 | 0.700 | 0.233 | 1.436 | 4.506 | 0.147 |
| **180-300s** | **218** | **0.713** | **0.224** | **1.421** | **3.806** | **0.145** |
| **300-600s** | **293** | **0.695** | **0.210** | **1.428** | **4.080** | **0.166** |
| 600-900s | 281 | 0.640 | 0.181 | 1.449 | 3.968 | 0.199 |
| 1800-3600s | 337 | 0.535 | 0.117 | 1.240 | 3.307 | 0.321 |
| ≥3600s (first R-magnitude edge) | 337 | 0.438→0.011 | 0.098→0.0004 | 1.22→1.00 | 3.12→1.61 | 0.51→0.81 |

**Read:** the sweet spot where recognizability (reach, ≥180s) and remaining value overlap is
**≈180-600s**: there, reach-2× base rate is still ~0.21-0.22, median remaining executable upside is
~1.42× with a q90 of ~3.8-4.1× (Q-EXC) and median retention ~0.15-0.17 (Q-RET). By the time R's
*magnitude* edge turns positive (≥3600s), reach-2× has fallen to ~0.10 and q50 remaining upside to
~1.22× — i.e. the magnitude skill arrives after most capturable value is gone (a Future-B shape for
magnitude specifically). The region is **characterized but young-young (0-180s) is NOT cleanly
established** — denominators there are sparse (0-30s = 82 states / 75 tok, ~1 state/token; eval cap
300 vs 66,223 states available), so the young claim rests on token-clustered CIs over ~75-260
tokens. Densification (`layer_b_decision`) is a scoped reopening handle, not run.

---

## 6. Adverse movement + execution feasibility + censoring (GAP #3 — UNPREDICTED)

Adverse excursion (MAE-before-opportunity) is a **label with no prediction head** — the frame has no
adverse-excursion block (schema confirmed). So the owner can see drawdown/fill risk only from the
Oracle truth side; it is **not something the recognizer forecasts**. Table:
`tables/adverse_censoring_oracle_side.csv` (TEST join exec_defined), plus
`tables/censoring_conservation_full.json` (FULL population).

**Oracle-side adverse distribution (exec_defined TEST join; `mae_*_mult` = worst multiple-of-entry
reached; <1.0 = drawdown):**

| region | `mae_before_mfe` q10 | q50 | frac <0.5 (>50% drawdown before opp) | `mae_total` q50 |
|---|---|---|---|---|
| 0-180s | 0.226 | 0.735 | 0.274 | 0.176 |
| 180-900s | 0.228 | 0.787 | 0.231 | ~0.20-0.32 |
| ≥3600s | 0.664 | 0.953 | 0.050 | 0.831 |
| ALL | 0.485 | 0.919 | 0.107 | 0.661 |

**Read:** entering **young (0-180s)** carries real pre-opportunity drawdown — median trough is
~0.74× entry and **27% of exec-defined young states draw down below 0.5× (lose >50%) before the
opportunity arrives**. The recognizer does NOT predict this; an ENTER/WAIT read at 180-600s cannot
net upside against this drawdown risk from released artifacts alone. `mae_total` median 0.18 young
shows how destructive the full path can be. **This is the sharpest unfilled gap for policy.**

**Censoring conservation (FULL oracle_vector, 30,464,958 states — matches O2 anatomy denominators):**
- `no_conf_crossing` = 26,489,036 (**0.8695** — the dominant "no opportunity observed" mass; matches
  INVENTORY §2.1), `entry_unpriceable` = 838,618 (0.0275), `exec_depth_null` = 3,862 (0.0001).
- **resolvable** = 29,626,258 (0.9725) — censored-unknown is only ~2.75%, so the reach denominator
  is honest.
- `tape_end_censored` = 1.0 of states carry a finite censor bound (every token's tape ends);
  `censor_bound_s` median ~16,545s (TEST exec_defined) — the forward horizon is bounded and must be
  read as a right-censored survival quantity, not an infinite look-ahead.
- reach-2× among exec_defined (full pop) = 793,485 states.

Execution feasibility beyond the `definedness` flag (depth/fill at the decision instant) is **not
surfaced per-state** in any released prediction — only the binary definedness head exists, and it
ties C=R (§2). So fill risk at decision time is unpredicted too.

---

## 7. Milestone verdict + top remaining gaps

**Verdict: FUTURE B — PARTIAL CAPABILITY (component-specific recognizability; young edge on reach
only).** Not A (no clean early recognizability with value still richly remaining across components),
not C (histories DO distinguish outcomes on several components — reach/retention/definedness/time),
not E (O2 already established the executable oracle is economically real). The evidence is a
**split**: 
- The causal recognizer **genuinely improves** excursion magnitude (all sizes), time-to-opportunity,
  and persistence over both the clock and the summary (CPU system-of-record, CI-clean), and is
  **better-calibrated** on excursion intervals than the summary.
- But its improvements are concentrated at **OLD ages**; **young**, the only component with a real
  edge is **reach**, whose economic separation is CI-positive only **≥≈180s** (history head), and
  even there the *summary* head already carries most of the young reach discrimination.
- Remaining executable value at the cleanest recognition region (≈180-600s) is still meaningful
  (reach-2× ~0.21, q50 upside ~1.42×, q90 ~3.8-4.1×), but **adverse drawdown there is real and
  unpredicted** (27% of young exec-defined states lose >50% before the opportunity).

So recognizability and remaining value DO overlap in a ≈180-600s window on **reach**, which is a
legitimate basis to open O4 policy design **on reach, not on magnitude/timing young** — but only if
the operator accepts (a) the result rests on the GPU DIAGNOSTIC_ONLY frame, and (b) the drawdown
axis is uncharacterized by any predictor.

**Top remaining gaps (mapped to INVENTORY §3.3):**
1. **GAP #1 (discrimination) — partially closed here.** I produced the first legitimate continuous
   discrimination surface (§4): reach sorts attractive states (≥180s for the history head, all ages
   for the summary head), retention sorts at all ages, magnitude does not sort young. What remains
   open: there is still **no agreed decision rule**, so "% of Oracle value captured" is deliberately
   NOT computed. **Recommend:** owner specifies a frozen ENTER/WAIT rule before any capture claim.
2. **GAP #2 (calibration/coverage surface) — partially closed here** (§4b coverage: R well-calibrated
   on excursion, C over-confident). Still DIAGNOSTIC_ONLY (GPU) and missing a per-component reliability
   curve on the CPU system-of-record. **Recommend:** re-express on the CPU `1237513b…` population, or
   explicitly accept the GPU frame as the owner-final discrimination surface.
3. **GAP #3 (adverse + feasibility unpredicted) — surfaced, not closed** (§6). Adverse MAE and
   decision-time fill feasibility have **no prediction head**. **Recommend (do NOT build without
   owner sign-off):** a scoped adverse-excursion head would be the single highest-value add for an
   ENTER/WAIT read — but it is a new model head, so it STOPS here as a recommendation, not a build
   (no-optimization-treadmill rule honored).

**Secondary:** R-vs-B for continuous components cannot be computed (no B head released) — bound as
binary-ladder-only (B below clock, history beats current-state on the binary diagnostic). Young-age
denominators are sparse; densification (`layer_b_decision`) is a reopening handle, not run.

---

## Appendix — artifact paths (all absolute)

Truth / predictions:
- `C:\Users\Cicada38\Projects\tokens_ingest\evidence\oracle_trajectory\released\clockv1_ocs2_full_research_rows.parquet` (sha `645c051d…`)
- `…\released\clockv1_ocs2_full_oracle_vector.parquet` (sha `20b444bf…`)
- `…\final_rep\80_FINAL_COMPARISON\U5_PREDICTION_FRAME_test.parquet` (GPU DIAGNOSTIC_ONLY, `2cda7aa2…`)

Frozen results consolidated:
- `…\final_rep\80_FINAL_COMPARISON\80_FINAL_COMPARISON_RESULT.json` (on-disk = GPU tag; CPU SoR digest `1237513b…`)
- `…\final_rep\R_FINAL_COMPONENT_ROUTING_V1.json`
- `…\final_rep\82_OUTPUT1_AGE_VALUE_SURFACE\OUTPUT1_AGE_VALUE_SURFACE_RESULT.{json,md}`
- `…\final_rep\86_REQUEST13_AMENDMENT\REQUEST13_AMENDMENT_RESULT.{json,md}` (SoT)
- `…\validation\O3_BASELINE_LADDER_clockv1_ocs2_full.json` (binary diagnostic; B baseline lives here)

Computed by this doc (development-only; `.ai\oracle\tables\`):
- `discrimination_tercile_sep.csv` — §4 (GAP #1)
- `actual_vs_predicted_distribution.csv` — §1
- `interval_coverage.csv` — §4b (GAP #2)
- `adverse_censoring_oracle_side.csv` — §6 (GAP #3)
- `censoring_conservation_full.json` — §6 (FULL population)

**Discipline statement.** No model trained; no W8/W9 file opened (confirmed absent from all reads);
holdout_looks_consumed = 0. Every quantity labelled by population, denominator and
CPU-SoR-vs-GPU-DIAGNOSTIC provenance. The 5 quantities were kept separate; no binary collapse was
used to close anything or mint a null; no "% of profit captured" was invented.
