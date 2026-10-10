# GEOMETRY_C — Objective-3: causal-history geometry on ACTUAL H_t + model-failure read

Authored 2026-10-10. Analysis over EXISTING frozen artifacts. **No model training, no W8/W9,
holdout_looks=0.** All numbers computed on `tokens_ingest` branch `jev-oracle-feasibility-lane`,
`evidence/oracle_trajectory/released/`. Work on ACTUAL H_t (released causal_states + ext1-4 +
research_rows), NOT model-prediction columns. This is the error AMEND-B caught and this report does
NOT repeat it.

Scripts (deterministic, seed=20261010), reproducible:
- `.ai/oracle/geometry/geometry_c_analysis.py` — TASK 1 + TASK 2 (full substrate, per-token
  subsample, respects the V4 split: dev=train+val, characterize on test).
- `.ai/oracle/geometry/geometry_c_young.py` — TASK 3 (young regime, FULL young-state coverage).
- Results: `.ai/oracle/geometry/GEOMETRY_C_RESULT.json`, `GEOMETRY_C_YOUNG_RESULT.json`.

Method is byte-faithful to AMEND-B (`research_os/oracle_o3_o4_request13_amendment.py`): winsor-z →
`GaussianMixture` BIC(k=1..8) → age×exposure matched cells → token-weighted excess-over-null
reach-2x spread → **cell-clustered** bootstrap CI. So every excess CI here is directly comparable to
AMEND-B's raw-H `+0.0674 [−0.0132, +0.1459]`. (`fit_motifs` uses `n_init=4` vs AMEND-B's 10 for the
larger-N full-substrate fit — a compute pragmatic reduction; BIC k-selection is unchanged.)

Sampling (stated plainly): full substrate is ~18k states/token across 1686 tokens (30.46M states).
AMEND-B ran on the GPU prediction frame which is EVAL-capped at ~300 states/token on **337 test
tokens only** — that cap is why its raw-H CI was so wide. TASK 1/2 here uniformly subsample 250
states/token (matching AMEND-B's per-token density) but across **all 1686 tokens** and respecting the
split. TASK 3 loads **all 166,158 young states** (age<180s) — no subsample — because young states are
only 0.5% of the substrate and uniform sampling starves them.

---

## TASK 1 — raw-H causal-history geometry vs oracle reach-2x (reproduce/extend AMEND-B honestly)

Clustered on AMEND-B's exact 16 raw as-of H features (net_sol flows, wallet concentration, price
slope, vol, dd_from_peak, mult_from_entry, trail_rate). Age×exposure-matched reach-2x separation,
cell-clustered CI.

| split | k (BIC) | token-weighted excess | cell-clustered 95% CI | CI excl 0? | matched cells (pos) |
|---|---|---|---|---|---|
| **test (held-out, characterize)** | 7 | **+0.0662** | **[−0.0159, +0.1533]** | **NO (straddles 0)** | 19 (15+) |
| dev (train+val, in-sample) | 6 | +0.1216 | [+0.0861, +0.1523] | yes | 19 (16+) |
| AMEND-B raw-H (337 test tok, ~300/tok) | 8 | +0.0674 | [−0.0132, +0.1459] | NO | — |

**ESTABLISHED (bounded NULL, reproduced).** On held-out TEST tokens the raw-H excess is
`+0.0662 [−0.0159, +0.1533]` — it **straddles 0**, and it reproduces AMEND-B's
`+0.0674 [−0.0132, +0.1459]` almost exactly, independently, with ~5× the tokens' worth of substrate
access at the same per-token density. AMEND-B's verdict
`PREDICTION_SPACE_SEPARATES_BUT_RAW_H_GEOMETRY_NOT_ESTABLISHED` is **robust, not a sampling
artifact.** Densifying beyond ~250 states/token does not rescue it (confirmed at per_tok=60 → +0.0314
and per_tok=250 → +0.0662, both straddle 0 on test).

**The diagnosis of WHY (the model-failure read).** Dev is CI-positive (`+0.1216 [+0.0861,+0.1523]`)
while test straddles 0. The gap is not noise — it is **token-level non-generalization**: GMM motifs
fit on tokens the clustering saw DO separate reach-2x within matched age×exposure cells, but the same
motif boundaries do not transfer to held-out tokens. So "global static H-geometry motifs → reach"
is an **in-sample** structure. That is the precise failure mode: raw H_t carries reach-relevant
structure, but it is NOT organized as a small set of transferable static trajectory motifs. (This is
fully consistent with the released routing sending reach to the frozen-C SUMMARY head, not a motif
model — reach recognizability is distributed, not geometric.)

A representation can carry useful DISTRIBUTED information with no clean static motif geometry — that
is a valid finding, and it is what the test CI + the dev/test gap jointly say. **OPEN remains OPEN:
there is no established clean static raw-H motif geometry for reach-2x on held-out tokens.**

---

## TASK 2 — what TEMPORAL structure explains the time/retention/definedness component edge

The O3 verdict: H_t adds info on time/retention/definedness but NOT magnitude. Matched SMD (per
temporal feature, outcome-positive vs outcome-negative, WITHIN age×exposure cells, token-weighted;
sign + = feature higher for the positive outcome). This is the mechanism behind the component split.
Full tables in `GEOMETRY_C_RESULT.json`; top discriminators:

**Executable-definedness (Q-DEF)** — dominated by **trade recency + rate**, NOT by price/magnitude:
- `time_since_last_buy_s −0.76`, `time_since_last_trade_s −0.65`, `time_since_last_sell_s −0.63`
  (shorter gap → defined); `trail_rate_tps_300s +0.33`, `trail_rate_tps_30s +0.25` (faster tape →
  defined). Price slope/accel/vol are near-zero. Definedness is a **liveness** property of the
  history — exactly a temporal (recency/rate) signal, not a level/magnitude one.

**Reach-2x (Q-REACH)** — same liveness core:
- `time_since_last_buy_s −0.59`, `…_trade_s −0.50`, `…_sell_s −0.49`; `trail_rate_tps_300s +0.25`.
  Price slopes and net-flow accel are all |SMD|<0.06. Reach is carried by **recent, sustained order
  flow**, not by how steep the price is.

**Conditional time-to-opportunity / EARLY reach (Q-TIME)** — the one place slope/accel/churn matter:
- `trail_rate_tps_300s +0.30`, `roundtrip_sol_frac_300s +0.29`, `flipper_frac_300s +0.27`,
  `trail_rate_tps_30s +0.25`, `distinct_wallets_slope_300s +0.24`, `realized_vol_600s +0.21`.
  Histories that reach SOONER have faster, churnier, wallet-growing, more-volatile recent flow. This
  is genuinely multivariate temporal structure (rate + slope + churn), and it is the mechanism for
  why the temporal GRU branch carries logtime.

**Retention after costs (Q-RET)** — a distinct wallet-behavior motif, opposite sign on churn:
- `roundtrip_sol_frac_300s −0.35`, `flipper_frac_300s −0.34` (fewer flippers/round-trips → value
  retained); `time_since_last_buy_s +0.30` (less frenetic → retained); `trail_rate_tps_300s −0.17`,
  `realized_vol_120s −0.11`. High retention is a **holder, low-churn, calmer-tape** temporal
  signature — the inverse of the early-reach churn signature.

**ESTABLISHED.** The component edge O3 split out is mechanistically a **recency/rate/churn** temporal
signature, not a price-magnitude one: definedness+reach ride trade liveness; early-reach rides
fast/churny/growing flow; retention rides low-churn holder behavior. Magnitude features
(`mult_from_entry`, `price_logslope_*`, `price_logaccel`) are weak discriminators for all of these —
consistent with magnitude routing to the GRU and NOT improving on the clock young (AMEND-A).

---

## TASK 3 — the young-reach edge (age<180s): recognizable early geometry, or under-informative?

Full young coverage: 166,158 states / 1686 tokens, young reach-2x rate 0.214. Two reads.

**(b) Held-out logistic separability (train young → VAL young, plain LogisticRegression on raw-H +
temporal, no tuning, no NN), token-clustered AUC CI per young sub-bin:**

| young sub-bin | n_val | val reach rate | held-out AUC | token-clustered 95% CI | beats chance (CI>0.5)? |
|---|---|---|---|---|---|
| **0–30s** | 5243 | 0.154 | **0.682** | **[0.641, 0.714]** | **YES** |
| 30–60s | 6688 | 0.205 | 0.560 | [0.518, 0.602] | yes (weak) |
| 60–120s | 13139 | 0.210 | 0.476 | [0.429, 0.528] | NO |
| 120–180s | 12596 | 0.229 | 0.501 | [0.451, 0.548] | NO |

**(c) Young raw-H geometry excess (GMM motifs, exposure-matched within young, cell-clustered CI):**
`+0.1407 [+0.0563, +0.2328]` — **CI excludes 0** (15 cells, 12 positive, k=6).

**(a) Matched reach-vs-non-reach SMD within young sub-bins — top discriminators:** wallet
dispersion and recency: `hhi_wallet_sol_300s −0.14`, `top1_wallet_sol_frac_300s −0.13` (LESS
concentrated → reach), `time_since_last_*_s +0.11..+0.13`, `realized_vol_120s/600s −0.08`.

**FINDING — recognizable early geometry EXISTS, and it is sharply front-loaded, then collapses.**
The youngest regime is NOT uniformly under-informative. At **0–30s** real H_t separates reach
out-of-sample at **AUC 0.68 (CI clear of chance)**; it decays to a weak edge by 30–60s and vanishes
(CI straddles 0.5) by 60–180s. The young-restricted raw-H geometry excess is **CI-positive**
(`+0.141 [+0.056, +0.233]`) — note this is stronger and CI-clean precisely because restricting to
young + full coverage removes the age-confounded, token-non-generalizing noise that drowns the
whole-life TEST test in TASK 1. The signal is **wallet-dispersion + liveness** (broad, active, early
accumulation reaches; concentrated/quiet does not) — this is the raw-H substrate of AMEND-A's
"reach_probability is the only genuine young R edge."

**Implication for the owner fork.** This supports the **REACH-based O4 policy** branch over
Future-B/new-data for the 0–30s window: there is genuine, held-out, recognizable early reach geometry
on EXISTING H_t at the very youngest ages, carried by interpretable wallet-breadth + liveness
features. It does NOT support a reach edge in 60–180s from current data — that mid-young band IS
under-informative and is where a Future-B earlier-data argument would apply if the owner wants reach
recognizability there. Note AMEND-A's **economic** separation only turned CI-positive ≥180s; this
report shows the **recognizability** (AUC) is strongest at 0–30s — the gap between "can recognize"
(0–30s) and "economically separates reach-2x terciles" (≥180s) is itself the key owner question.

---

## STATUS SUMMARY

- **ESTABLISHED:** (1) Raw-H static-motif geometry does NOT separate reach-2x on held-out tokens:
  TEST excess `+0.0662 [−0.0159,+0.1533]`, reproducing AMEND-B `+0.0674 [−0.0132,+0.1459]` — a
  robust **bounded NULL**. The dev/test gap shows the mechanism = token-level non-generalization, not
  absence of signal. (2) The time/retention/definedness edge is mechanistically a recency/rate/churn
  temporal signature (not magnitude): definedness+reach ride trade liveness, early-reach rides
  fast/churny/growing flow, retention rides low-churn holder behavior.
- **NEW FINDING (held-out, CI-clean):** recognizable early reach geometry exists on real H_t at
  **0–30s (AUC 0.68 [0.64,0.71])** and in the young-restricted geometry excess
  `+0.141 [+0.056,+0.233]`, carried by wallet-breadth + liveness; it collapses to chance by 60–180s.
- **OPEN:** whether a clean transferable static raw-H motif atlas exists for reach whole-life (TASK 1
  says not as static motifs); whether 0–30s recognizability is economically actionable (AMEND-A's
  economic separation is ≥180s only — a recognizability-vs-economics gap for the owner).
- **BOUNDED NULL:** raw-H static-motif geometry for reach-2x on held-out tokens, whole-life.

## Preregistered targeted experiment (held for resource review — NOT launched)

**Hypothesis.** The 0–30s held-out reach recognizability (AUC 0.68) is driven by a low
wallet-concentration + high-liveness early signature and is **monotone-usable as an ENTER gate**:
states in the top decile of a frozen 0–30s reach-score carry materially higher oracle reach-2x than
bottom decile, on held-out VAL, at a decision age ≤30s.
- **Estimand.** Held-out (VAL) difference in oracle reach-2x rate, top-decile vs bottom-decile of a
  LogisticRegression reach-score fit on TRAIN 0–30s states, token-clustered 95% CI; plus the implied
  remaining-exec value (median `mfe_exec_mult_1p0`, `retention_exec_1p0`) in each decile.
- **Baseline.** The clock-only (age) predictor at 0–30s (A-baseline), same decile contrast.
- **Metric / decision rule.** POSITIVE iff the top-vs-bottom reach-2x CI excludes 0 AND beats the
  clock-only decile contrast CI; this would move the owner fork toward REACH-based O4 at 0–30s.
- **Split frozen before running.** TRAIN fit, VAL evaluate, TEST untouched (reserved for a single
  confirmatory look if VAL passes); W8/W9 untouched; holdout_looks stays 0.
- **Cost.** Plain sklearn, CPU, minutes. No NN, no tuning sweep, no new features beyond the released
  raw-H + temporal set. Well under 30 min.

I recommend running it — it is the cheapest decisive test of whether the 0–30s recognizability edge
is an actionable ENTER gate (the owner's REACH-vs-Future-B fork), and it stays entirely on dev data.
Returned for your resource review before execution.
