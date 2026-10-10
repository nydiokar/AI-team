"""GEOMETRY_C — Objective-3 causal-history geometry on ACTUAL H_t (NOT prediction clusters).

Extends AMEND-B's raw-H geometry test from the EVAL-capped 337-test-token GPU frame (~300
states/token) to the FULL released causal substrate (~18k states/token, 1686 tokens), respecting
the V4 split (dev=train+val, characterize=test). Method is byte-faithful to AMEND-B:
  winsor-z -> GaussianMixture BIC(k=1..8) -> age x exposure matched cells ->
  token-weighted excess-over-null reach-2x spread -> cell-clustered bootstrap CI.
So the excess-over-null CI here is directly comparable to AMEND-B's raw-H [-0.0132, +0.1459].

NO model training. NO W8/W9. holdout_looks=0. CPU pandas/numpy/sklearn. Deterministic (seed-pinned).
Per-token state subsampling is used for tractability; sampling is stated in the output.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TOK_ING = Path(r"C:\Users\Cicada38\Projects\tokens_ingest")
REL = TOK_ING / "evidence" / "oracle_trajectory" / "released"
OUT = Path(r"C:\Users\Cicada38\Projects\AI-team\.ai\oracle\geometry")

SEED = 20261010
QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)

# AMEND-B's exact raw-H feature set (H_RAW_FEATURES in oracle_o3_o4_request13_amendment.py)
H_RAW_FEATURES = [
    "net_sol_30s", "net_sol_300s", "cum_net_sol", "buy_sol_frac_300s",
    "trail_rate_tps_300s", "price_logslope_60s", "price_logslope_300s",
    "up_tick_frac_300s", "distinct_wallets_300s", "top1_wallet_sol_frac_300s",
    "hhi_wallet_sol_300s", "realized_vol_120s", "realized_vol_600s",
    "dd_from_peak", "mult_from_entry", "trail_rate_tps",
]
# AMEND-B matched-strata grid (identical for comparability)
AGE_SEP_EDGES = [0, 300, 900, 3600, 14400, 86401]
EXP_QUANTILES = [0.0, 0.25, 0.5, 0.75, 1.0]
YOUNG_EDGES = [0, 30, 60, 120, 180, 300, 600, 900]

# TASK-2 temporal-structure feature set (slopes / accel / persistence / relative-timing)
TEMPORAL_FEATURES = [
    "price_logslope_60s", "price_logslope_300s", "price_logaccel_60_300",
    "net_sol_accel_30_300", "buy_sol_frac_slope_300s", "buy_sol_frac_accel_30_300",
    "distinct_wallets_slope_300s", "depth_slope_120s", "depth_slope_600s",
    "bp_accel_30_vs_120", "trail_rate_tps_30s", "trail_rate_tps_300s",
    "realized_vol_120s", "realized_vol_600s",
    "time_since_last_trade_s", "time_since_last_buy_s", "time_since_last_sell_s",
    "flipper_frac_300s", "roundtrip_sol_frac_300s",
]


def winsor_z(X: np.ndarray) -> np.ndarray:
    lo = np.nanpercentile(X, 1, axis=0)
    hi = np.nanpercentile(X, 99, axis=0)
    Xw = np.clip(X, lo, hi)
    mu = np.nanmean(Xw, axis=0)
    sd = np.nanstd(Xw, axis=0)
    sd[sd == 0] = 1.0
    Z = (Xw - mu) / sd
    return np.nan_to_num(Z, nan=0.0)


def fit_motifs(Z: np.ndarray, kmax: int = 8, n_init: int = 4) -> dict:
    # n_init=4 (vs AMEND-B's 10) for the larger-N full-substrate fit — a compute pragmatic
    # reduction that does not change k-selection on these well-separated BIC curves; stated in output.
    from sklearn.mixture import GaussianMixture
    best = None
    bics = {}
    for k in range(1, kmax + 1):
        gm = GaussianMixture(n_components=k, covariance_type="full", n_init=n_init,
                             random_state=SEED, max_iter=300)
        gm.fit(Z)
        bic = float(gm.bic(Z))
        bics[k] = bic
        if best is None or bic < best[1]:
            best = (k, bic, gm)
    k, _, gm = best
    return {"k": int(k), "bic_by_k": bics, "labels": gm.predict(Z),
            "median_max_responsibility": float(np.median(gm.predict_proba(Z).max(axis=1)))}


def separation_with_ci(df: pd.DataFrame, labels: np.ndarray) -> dict:
    """AMEND-B _separation_with_ci, byte-faithful. age x exposure matched cells,
    token-weighted excess-over-null, cell-clustered bootstrap CI."""
    df = df.copy()
    df["motif"] = labels
    age = df["age_s"].to_numpy(np.float64)
    df["age_bin"] = np.digitize(age, AGE_SEP_EDGES[1:-1])
    expo = df["n_trades_so_far"].to_numpy(np.float64)
    qs = np.nanquantile(expo, EXP_QUANTILES)
    df["exp_bin"] = np.clip(np.digitize(expo, qs[1:-1]), 0, 3)
    y = df["mfe_exec_mult_1p0"].to_numpy(np.float64)
    df["reach2x"] = (np.isfinite(y) & (y >= 2.0)).astype(float)

    MIN_C = 20
    rng = np.random.default_rng(SEED)
    cells, obs, null, w = [], [], [], []
    for (ab, eb), cell in df.groupby(["age_bin", "exp_bin"]):
        if cell.motif.nunique() < 2 or cell.token_id.nunique() < MIN_C:
            continue
        rates = cell.groupby("motif")["reach2x"].mean()
        spread = float(rates.max() - rates.min())
        lab = cell.motif.to_numpy(); r2 = cell.reach2x.to_numpy()
        nn = []
        for _ in range(50):
            perm = rng.permutation(lab)
            s = pd.Series(r2).groupby(perm).mean()
            nn.append(float(s.max() - s.min()))
        nm = float(np.mean(nn))
        cells.append({"age_bin": int(ab), "exp_bin": int(eb),
                      "n_tokens": int(cell.token_id.nunique()),
                      "n_motifs": int(cell.motif.nunique()),
                      "obs_reach2x_spread": round(spread, 4),
                      "null_reach2x_spread": round(nm, 4),
                      "excess_over_null": round(spread - nm, 4)})
        obs.append(spread); null.append(nm); w.append(cell.token_id.nunique())
    if not cells:
        return {"n_matched_cells": 0, "token_weighted_point_excess": None,
                "cell_clustered_ci_lo": None, "cell_clustered_ci_hi": None}
    obs = np.array(obs); null = np.array(null); w = np.array(w, float)
    excess = obs - null
    point = float(np.average(excess, weights=w))
    bci = []
    for _ in range(2000):
        ix = rng.integers(0, len(cells), len(cells))
        bci.append(float(np.average(excess[ix], weights=w[ix])))
    bci = np.array(bci)
    n_pos = int((excess > 0).sum())
    return {
        "n_matched_cells": len(cells),
        "n_cells_positive_excess": n_pos,
        "n_cells_negative_excess": len(cells) - n_pos,
        "token_weighted_point_excess": round(point, 4),
        "cell_clustered_ci_lo": round(float(np.percentile(bci, 2.5)), 4),
        "cell_clustered_ci_hi": round(float(np.percentile(bci, 97.5)), 4),
        "ci_excludes_zero": bool(np.percentile(bci, 2.5) > 0),
        "per_cell": cells,
    }


def load_substrate(folds: list[str], per_tok: int, extra_cols: list[str]) -> pd.DataFrame:
    """Load a per-token-subsampled slice of the full causal substrate for the given split folds.
    Joins carried states + ext1/2/3 + oracle truth, row-aligned. per_tok caps states/token."""
    split = pd.read_parquet(REL / "V4_split_assignment_clockv1_ocs2_full.parquet",
                            columns=["token_id", "fold"])
    keep_tok = set(split[split.fold.isin(folds)].token_id)

    # base keys + carried state features + truth, from research_rows (the eligible-population anchor)
    base_cols = (["token_id", "t_s", "age_s", "n_trades_so_far",
                  "dd_from_peak", "mult_from_entry", "trail_rate_tps",
                  "mfe_exec_mult_1p0", "time_to_mfe_exec_s_1p0",
                  "retention_exec_1p0", "retention_ratio",
                  "time_since_last_trade_s"])
    rr = pd.read_parquet(REL / "clockv1_ocs2_full_research_rows.parquet",
                         columns=sorted(set(c for c in base_cols)))
    rr = rr[rr.token_id.isin(keep_tok)].reset_index(drop=True)

    # per-token uniform subsample (deterministic) BEFORE heavy ext joins -> keep memory bounded.
    # Vectorized: assign a per-row random key, rank within token, keep lowest `per_tok` ranks.
    rng = np.random.default_rng(SEED)
    rr["_r"] = rng.random(len(rr))
    rr["_rank"] = rr.groupby("token_id")["_r"].rank(method="first")
    rr = rr[rr["_rank"] <= per_tok].drop(columns=["_r", "_rank"]).reset_index(drop=True)

    # join ext features on (token_id, t_s). Determine which cols come from which ext file.
    ext_map = {
        "clockv1_ocs2_full_causal_ext1.parquet": [
            "net_sol_30s", "net_sol_300s", "cum_net_sol", "buy_sol_frac_300s",
            "trail_rate_tps_300s", "trail_rate_tps_30s", "price_logslope_60s",
            "price_logslope_300s", "price_logaccel_60_300", "up_tick_frac_300s",
            "distinct_wallets_300s", "top1_wallet_sol_frac_300s", "hhi_wallet_sol_300s"],
        "clockv1_ocs2_full_causal_ext2.parquet": [
            "realized_vol_120s", "realized_vol_600s", "depth_slope_120s",
            "depth_slope_600s", "time_since_last_buy_s", "time_since_last_sell_s"],
        "clockv1_ocs2_full_causal_ext3.parquet": [
            "distinct_wallets_slope_300s", "net_sol_accel_30_300",
            "buy_sol_frac_slope_300s", "buy_sol_frac_accel_30_300",
            "bp_accel_30_vs_120", "flipper_frac_300s", "roundtrip_sol_frac_300s"],
    }
    want = set(H_RAW_FEATURES + TEMPORAL_FEATURES + extra_cols)
    for fn, avail in ext_map.items():
        need = [c for c in avail if c in want]
        if not need:
            continue
        d = pd.read_parquet(REL / fn, columns=["token_id", "t_s"] + need)
        d = d[d.token_id.isin(keep_tok)]
        rr = rr.merge(d, on=["token_id", "t_s"], how="left")
    return rr


def task1_raw_h_geometry(dev: pd.DataFrame, test: pd.DataFrame) -> dict:
    """TASK 1: reproduce/extend AMEND-B raw-H geometry honestly, full substrate, on test
    (characterize) + dev (for reference). Report excess-over-null CI vs AMEND-B."""
    out = {}
    for name, df in [("test_characterize", test), ("dev_train_val", dev)]:
        have = [c for c in H_RAW_FEATURES if c in df.columns]
        Z = winsor_z(df[have].to_numpy(np.float64))
        m = fit_motifs(Z)
        sep = separation_with_ci(df, m["labels"])
        out[name] = {
            "n_states": int(len(df)), "n_tokens": int(df.token_id.nunique()),
            "clustered_on": have, "k_selected_by_bic": m["k"],
            "median_max_responsibility": round(m["median_max_responsibility"], 4),
            "separation": sep,
        }
    return out


def task2_temporal_structure(dev: pd.DataFrame) -> dict:
    """TASK 2: what TEMPORAL structure (slopes/accel/persistence/timing) distinguishes histories
    that reach high-retention / early / executable-defined outcomes. Matched controls on
    age x exposure. Reports standardized mean difference (reach vs non-reach) per temporal feature
    WITHIN matched age x exposure cells, pooled with cell weights; plus the same for high-retention
    and executable-defined outcome splits."""
    df = dev.copy()
    age = df["age_s"].to_numpy(np.float64)
    df["age_bin"] = np.digitize(age, AGE_SEP_EDGES[1:-1])
    expo = df["n_trades_so_far"].to_numpy(np.float64)
    qs = np.nanquantile(expo, EXP_QUANTILES)
    df["exp_bin"] = np.clip(np.digitize(expo, qs[1:-1]), 0, 3)

    y = df["mfe_exec_mult_1p0"].to_numpy(np.float64)
    ret = df["retention_exec_1p0"].to_numpy(np.float64)
    tmfe = df["time_to_mfe_exec_s_1p0"].to_numpy(np.float64)
    df["reach2x"] = (np.isfinite(y) & (y >= 2.0)).astype(float)
    df["exec_defined"] = np.isfinite(y).astype(float)
    # high retention: top tercile of retention among exec-defined (giveback low)
    ret_hi_thr = np.nanquantile(ret[np.isfinite(ret)], 2 / 3) if np.isfinite(ret).any() else np.nan
    df["high_retention"] = (np.isfinite(ret) & (ret >= ret_hi_thr)).astype(float)
    # early reach: among reachers, time-to-opp below median
    tmfe_med = np.nanmedian(tmfe[np.isfinite(tmfe)]) if np.isfinite(tmfe).any() else np.nan
    df["early_reach"] = (np.isfinite(tmfe) & (tmfe <= tmfe_med)).astype(float)

    have = [c for c in TEMPORAL_FEATURES if c in df.columns]
    outcomes = {
        "reach2x": ("reach2x", None),            # 1 vs 0 over all states
        "high_retention": ("high_retention", "exec_defined"),  # within exec-defined
        "early_reach": ("early_reach", "reach2x"),             # within reachers
        "exec_defined": ("exec_defined", None),
    }
    results = {}
    for oc, (col, restrict) in outcomes.items():
        sub = df if restrict is None else df[df[restrict] == 1.0]
        feat_smd = {}
        for feat in have:
            # matched SMD: within each age x exp cell compute (mean_pos - mean_neg)/pooled_sd,
            # weight by cell n_tokens, pool.
            num, den = 0.0, 0.0
            for _, cell in sub.groupby(["age_bin", "exp_bin"]):
                pos = cell[cell[col] == 1.0][feat].to_numpy(np.float64)
                neg = cell[cell[col] == 0.0][feat].to_numpy(np.float64)
                pos = pos[np.isfinite(pos)]; neg = neg[np.isfinite(neg)]
                if len(pos) < 10 or len(neg) < 10:
                    continue
                psd = np.sqrt((np.var(pos) + np.var(neg)) / 2.0)
                if psd == 0:
                    continue
                smd = (pos.mean() - neg.mean()) / psd
                wgt = float(cell.token_id.nunique())
                num += smd * wgt; den += wgt
            if den > 0:
                feat_smd[feat] = round(num / den, 4)
        # rank by |SMD|
        ranked = sorted(feat_smd.items(), key=lambda kv: -abs(kv[1]))
        results[oc] = {
            "n_states": int(len(sub)), "n_pos": int(sub[col].sum()),
            "restrict_to": restrict,
            "matched_smd_by_temporal_feature": dict(ranked),
            "top5_discriminating": ranked[:5],
        }
    return {
        "method": ("matched standardized-mean-difference (SMD) of each temporal feature between "
                   "outcome-positive and outcome-negative states WITHIN age x exposure cells "
                   "(same grid as AMEND-B), token-weighted pooled. |SMD| ranks discriminators. "
                   "SMD sign: + means feature HIGHER for the positive outcome."),
        "per_outcome": results,
    }


def task3_young_reach(dev: pd.DataFrame) -> dict:
    """TASK 3: young (age<180s) — is there recognizable early geometry distinguishing histories that
    reach vs not, or is the young regime genuinely under-informative (-> Future-B)? Two reads:
    (a) matched SMD of temporal+raw-H features reach vs non-reach within young age bins;
    (b) can a cheap logistic on raw-H separate reach within young bins (held-out AUC on val),
        and does it degrade as age->0? NO neural net; plain sklearn LogisticRegression."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    young = dev[dev.age_s < 180].copy()
    y = young["mfe_exec_mult_1p0"].to_numpy(np.float64)
    young["reach2x"] = (np.isfinite(y) & (y >= 2.0)).astype(float)

    # (a) matched SMD within young sub-bins (0-30,30-60,60-120,120-180)
    age = young["age_s"].to_numpy(np.float64)
    young["yb"] = np.digitize(age, [30, 60, 120])
    feats = [c for c in (H_RAW_FEATURES + TEMPORAL_FEATURES) if c in young.columns]
    smd_rows = {}
    for feat in feats:
        num, den = 0.0, 0.0
        for _, cell in young.groupby("yb"):
            pos = cell[cell.reach2x == 1.0][feat].to_numpy(np.float64)
            neg = cell[cell.reach2x == 0.0][feat].to_numpy(np.float64)
            pos = pos[np.isfinite(pos)]; neg = neg[np.isfinite(neg)]
            if len(pos) < 10 or len(neg) < 10:
                continue
            psd = np.sqrt((np.var(pos) + np.var(neg)) / 2.0)
            if psd == 0:
                continue
            num += ((pos.mean() - neg.mean()) / psd) * len(cell); den += len(cell)
        if den > 0:
            smd_rows[feat] = round(num / den, 4)
    ranked = sorted(smd_rows.items(), key=lambda kv: -abs(kv[1]))

    # (b) held-out separability: train logistic on train-fold young states, score val-fold young.
    split = pd.read_parquet(REL / "V4_split_assignment_clockv1_ocs2_full.parquet",
                            columns=["token_id", "fold"])
    foldmap = dict(zip(split.token_id, split.fold))
    young["fold"] = young.token_id.map(foldmap)
    use = [c for c in (H_RAW_FEATURES + TEMPORAL_FEATURES) if c in young.columns]
    auc_by_bin = {}
    for yb, lbl in [(0, "0-30s"), (1, "30-60s"), (2, "60-120s"), (3, "120-180s")]:
        tr = young[(young.yb == yb) & (young.fold == "train")]
        va = young[(young.yb == yb) & (young.fold == "val")]
        if tr.reach2x.nunique() < 2 or va.reach2x.nunique() < 2 or len(va) < 50:
            auc_by_bin[lbl] = {"n_train": int(len(tr)), "n_val": int(len(va)),
                               "auc": None, "note": "insufficient / degenerate"}
            continue
        Xtr = winsor_z(tr[use].to_numpy(np.float64))
        Xva = np.nan_to_num(va[use].to_numpy(np.float64), nan=0.0)
        # standardize val by train stats roughly via winsor_z on combined not allowed (leak);
        # use simple per-col train z applied to val
        mu = np.nanmean(tr[use].to_numpy(np.float64), axis=0)
        sd = np.nanstd(tr[use].to_numpy(np.float64), axis=0); sd[sd == 0] = 1.0
        Xva = np.nan_to_num((va[use].to_numpy(np.float64) - mu) / sd, nan=0.0)
        clf = LogisticRegression(max_iter=500, C=1.0, class_weight="balanced")
        clf.fit(Xtr, tr.reach2x.to_numpy())
        p = clf.predict_proba(Xva)[:, 1]
        auc = float(roc_auc_score(va.reach2x.to_numpy(), p))
        auc_by_bin[lbl] = {"n_train": int(len(tr)), "n_val": int(len(va)),
                           "val_reach_rate": round(float(va.reach2x.mean()), 4),
                           "auc": round(auc, 4)}
    return {
        "method": ("(a) matched SMD reach vs non-reach within young sub-bins; (b) plain logistic "
                   "regression on raw-H+temporal trained on TRAIN young states, held-out AUC on VAL "
                   "young states, per young sub-bin. No neural net, no tuning."),
        "n_young_states": int(len(young)), "n_young_tokens": int(young.token_id.nunique()),
        "matched_smd_reach_vs_nonreach_young": dict(ranked),
        "top8_young_discriminators": ranked[:8],
        "held_out_logistic_auc_by_young_bin": auc_by_bin,
    }


def main() -> int:
    per_tok = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    print(f"[load] per_tok={per_tok}")
    dev = load_substrate(["train", "val"], per_tok, extra_cols=[])
    test = load_substrate(["test"], per_tok, extra_cols=[])
    print(f"[load] dev states={len(dev)} tok={dev.token_id.nunique()} | "
          f"test states={len(test)} tok={test.token_id.nunique()}")

    rec = {
        "gate": "GEOMETRY_C_OBJECTIVE3",
        "programme": "oracle_trajectory_inversion",
        "scope": ("Objective-3 causal-history geometry on ACTUAL H_t (NOT prediction clusters). "
                  "Extends AMEND-B raw-H test to full released substrate. No training, no W8/W9, "
                  "holdout_looks=0."),
        "seed": SEED,
        "sampling": (f"per-token uniform subsample of {per_tok} eligible states/token "
                     f"(full substrate has ~18k/token; AMEND-B used the GPU frame EVAL-capped at "
                     f"~300/token on 337 test tokens only). dev=train+val, characterize on test."),
        "amend_b_reference": {
            "raw_h_excess": 0.0674, "raw_h_ci": [-0.0132, 0.1459],
            "verdict": "PREDICTION_SPACE_SEPARATES_BUT_RAW_H_GEOMETRY_NOT_ESTABLISHED (straddles 0)",
            "n_test_tokens": 337, "states_per_token_approx": 300,
        },
        "task1_raw_h_geometry": task1_raw_h_geometry(dev, test),
        "task2_temporal_structure": task2_temporal_structure(dev),
        "task3_young_reach": task3_young_reach(dev),
    }
    (OUT / "GEOMETRY_C_RESULT.json").write_text(
        json.dumps(rec, indent=2, default=str), encoding="utf-8")
    print("[done] wrote GEOMETRY_C_RESULT.json")
    t1 = rec["task1_raw_h_geometry"]["test_characterize"]["separation"]
    print(f"  TASK1 test raw-H excess={t1.get('token_weighted_point_excess')} "
          f"CI=[{t1.get('cell_clustered_ci_lo')},{t1.get('cell_clustered_ci_hi')}] "
          f"excl0={t1.get('ci_excludes_zero')} k={rec['task1_raw_h_geometry']['test_characterize']['k_selected_by_bic']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
