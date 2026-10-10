# Manager's Proposed Milestone Conclusion — Oracle↔Model O3→O4 (for Scientific Governor review)

Authored 2026-10-10 by the persistent Opus Manager, Case 6a4c523570f3444f90a9ea5045664ae9.
This is the conclusion I propose to CLOSE the O3→O4 evaluation milestone. It is the object the
Scientific Governor must review. Every claim is backed by a computed artifact under `.ai/oracle/`.

## The milestone question
How close is the current causal recognizer to the Oracle's actual opportunity outcomes; WHEN does
the gap matter; and DOES the recognizer distinguish economically valuable opportunities from
unattractive states? Across canonical components and execution sizes. Protected W8/W9 untouched.

## Proposed verdict: **B — PARTIAL CAPABILITY** (development-stage, component-specific)

### What the Oracle could observe (established, O2, accepted)
Executable opportunity is economically REAL and not artifact-dominated: exec-defined in 1609/1686
tokens (95.4%), per-token best mfe median 2.09×, first ≥2×-exec at token-age median 47s, remains
~2.5h median, survives 1-SOL round-trip at ~91% retention. (INVENTORY_B §1.3.)

### What the frozen model predicts without seeing the future (established, O3 of record)
`PARTIAL_RECOGNIZABILITY_SPLIT_BY_COMPONENT`: on the canonical continuous O_t(s), H_t beats the
clock/time baseline on time-to-opportunity, retention, and definedness (CI entirely favouring
history) but NOT on excursion magnitude. Released component routing: magnitude+logtime+persistence
→ temporal GRU (R_HYBRID); reach+retention×2+definedness → summary HistGBT (frozen C).

### Where the model adds genuine information beyond simpler baselines
- CPU system-of-record CRPS/Brier: R beats C and D* on all three excursion sizes + logtime (CI<0);
  ties on reach/retention_exec/definedness; loses to C only on retention_ratio (the split that
  forced routing). (COMPARISON_OWNER; routing JSON.)
- Discrimination (continuous, no binary collapse; discrimination_tercile_sep.csv): predicted-tercile
  → realized-outcome separation with token-clustered CIs. Reach sorts significantly from ≥180s
  (summary head C_sep even sorts young 0–180s: +0.121 [+0.011,+0.206]); retention sorts at ALL ages;
  excursion sorts only ≥3600s.

### Where it fails or arrives too late
- Excursion magnitude skill arrives only ≥3600s, by which age reach-2x base rate has fallen to
  ~0.10 and remaining upside to ~1.22× — magnitude skill arrives after value is mostly gone.
- Raw causal-history static-motif GEOMETRY for reach is a BOUNDED NULL: age×exposure-matched raw-H
  excess over null +0.0662 [−0.016,+0.153] (straddles 0), independently reproducing AMEND-B
  (+0.0674 [−0.013,+0.146]) now across all 1686 tokens. H_t carries reach info but it is DISTRIBUTED,
  not organized as transferable static motifs (dev in-sample +0.122 CI-positive, test null →
  token-level non-generalization). (GEOMETRY_C task1.)
- Adverse movement (MAE-before-opportunity) has NO prediction head: 27% of young exec-defined states
  draw down >50% before the opportunity, unforecastable by the current recognizer.
  (adverse_censoring_oracle_side.csv.)

### Does the useful signal identify materially better executable opportunities?
Partially, and component-specifically. The earliest ECONOMIC discrimination is on REACH from ~180s
(remaining upside ~1.42× median, q90 ~3.8–4.1×, retention ~0.15–0.17 at 180–600s).

**Precise statement (tightened per Governor M1 — do not read this as early trajectory actionability):**
a *point-in-time* H_t read separates reach OUT-OF-SAMPLE at 0–30s (AUC 0.682 [0.641,0.714], full
young-coverage n_val=5243 — the subsample 0.6591 in GEOMETRY_C_RESULT.json task3 is a tiny-n_val=80
cross-reference, NOT a weaker contradicting result), and this signal is SNAPSHOT-LIKE — it rides
low wallet-concentration (hhi/top1) + trade liveness, i.e. summary/current-state features, NOT
verified trajectory geometry. That snapshot recognizability COLLAPSES to chance at 60–180s. The
economic tercile-separation on the HISTORY head is not CI-clean until ≥180s (the summary head C does
sort reach young: 0–180s +0.121 [+0.011,+0.206]). Transferable static-motif causal-history GEOMETRY
is a bounded NULL. So: a snapshot-like reach signal exists young, the history-head economic sort is
CI-clean only ≥180s, and trajectory-geometry is not established — three distinct statements that must
not be collapsed into "early trajectory recognizability."

### Candidate worth advancing?
Yes, bounded: a REACH-based recognizer operating from ~180s is a defensible development-stage
candidate; the owner fork is whether to open O4 policy design on REACH (not magnitude/timing young).
The 60–180s band is genuinely under-informative from current data (a Future-B earlier-data argument
applies there, NOT whole-programme).

## Exact protected-evidence consumption
W8 untouched, W9 undefined, holdout_looks_consumed = 0 (machine-verified). Nothing proposed here
spends either.

## Proposed next two programme moves
1. Run the preregistered cheap-decisive test (Worker C): does the 0–30s reach-score top-vs-bottom
   decile separate oracle reach-2x on held-out VAL and beat a clock-only baseline — the cheapest
   test of whether 0–30s recognizability is an ACTIONABLE ENTER gate (plain sklearn, CPU, <30min,
   TEST/W8/W9 reserved). Resolves the recognizability-vs-economics gap.
2. Owner decision on the O4 fork: REACH-based policy design (if move 1 is positive) vs Future-B
   earlier-data acquisition for the 60–180s dead band. An adverse-excursion prediction head is the
   highest-value model add but is NEW training — recorded as a next-move, not built.

## Governor-adjudicated qualifications (recorded at closure)
- **M2 (ACCEPTED as explicit owner-facing qualification):** the component split (R beats C/D* on
  excursion+logtime) IS on the CPU system-of-record (80_FINAL_COMPARISON, digest 1237513b). But the
  NEW economic-discrimination claim — ≥180s reach tercile sorts realized reach-2x — lives ONLY on the
  GPU DIAGNOSTIC_ONLY frame (2cda7aa2), not the CPU SoR. I accept the GPU frame as the development-stage
  owner discrimination surface AND fold the CPU re-expression of the tercile discrimination into
  next-move 1 (it is CPU, dev-only, <30 min, W8/W9 reserved). This bounds the ≥180s reach economic
  claim as GPU-diagnostic-grade until re-expressed; it does NOT weaken verdict B.
- **M1 (ACCEPTED, applied above):** the 0–30s reach wording tightened to prevent a snapshot→trajectory
  conflation.

## Honest residuals (NOT closure blockers)
- All O3→O4 Output numbers are on the GPU DIAGNOSTIC_ONLY frame (337 test tokens); the CPU
  system-of-record (80_FINAL_COMPARISON) carries the pre-registered scored comparison. (See M2.)
- R3 (routing-vs-discrimination tension): the release routes retention to the summary head C on CRPS,
  yet the R retention head out-SORTS C young on realized discrimination (0–180s R +0.149 sig vs C ns).
  A legitimate open refinement for O4, not a defect.
- R-vs-B (current-state baseline) for continuous components is uncomputable from released artifacts
  (no B head released) — bounded as binary-ladder-only.
- Young denominators are sparse (0–30s ≈ 82 states/75 tokens at the eval cap); young claims rest on
  token-clustered CIs over tens–hundreds of tokens.
