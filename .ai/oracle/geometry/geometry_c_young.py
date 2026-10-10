"""GEOMETRY_C TASK-3 (young-reach), FULL young-state coverage.

Young states (age<180s) are ~0.5% of the substrate (166k states) but span all 1686 tokens. Uniform
per-token subsampling under-represents them, so this script loads ALL young states directly and runs
the TASK-3 reads at full coverage:
  (a) matched SMD reach vs non-reach within young sub-bins (0-30,30-60,60-120,120-180s);
  (b) held-out logistic AUC (train young -> val young) per young sub-bin, with age->0 trend;
  (c) raw-H geometry excess-over-null CI restricted to the young regime (does early geometry
      separate reach beyond age x exposure?).
No neural net, no tuning, no W8/W9, holdout_looks=0. Deterministic.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

TOK_ING = Path(r"C:\Users\Cicada38\Projects\tokens_ingest")
REL = TOK_ING / "evidence" / "oracle_trajectory" / "released"
OUT = Path(r"C:\Users\Cicada38\Projects\AI-team\.ai\oracle\geometry")
SEED = 20261010

H_RAW_FEATURES = [
    "net_sol_30s", "net_sol_300s", "cum_net_sol", "buy_sol_frac_300s",
    "trail_rate_tps_300s", "price_logslope_60s", "price_logslope_300s",
    "up_tick_frac_300s", "distinct_wallets_300s", "top1_wallet_sol_frac_300s",
    "hhi_wallet_sol_300s", "realized_vol_120s", "realized_vol_600s",
    "dd_from_peak", "mult_from_entry", "trail_rate_tps",
]
TEMPORAL_FEATURES = [
    "price_logslope_60s", "price_logslope_300s", "price_logaccel_60_300",
    "net_sol_accel_30_300", "buy_sol_frac_slope_300s", "buy_sol_frac_accel_30_300",
    "distinct_wallets_slope_300s", "depth_slope_120s", "depth_slope_600s",
    "bp_accel_30_vs_120", "trail_rate_tps_30s", "trail_rate_tps_300s",
    "realized_vol_120s", "realized_vol_600s",
    "time_since_last_trade_s", "time_since_last_buy_s", "time_since_last_sell_s",
    "flipper_frac_300s", "roundtrip_sol_frac_300s",
]
EXP_QUANTILES = [0.0, 0.25, 0.5, 0.75, 1.0]
YSUB_EDGES = [30, 60, 120]  # -> bins 0-30,30-60,60-120,120-180


def winsor_z(X: np.ndarray) -> np.ndarray:
    lo = np.nanpercentile(X, 1, axis=0); hi = np.nanpercentile(X, 99, axis=0)
    Xw = np.clip(X, lo, hi)
    mu = np.nanmean(Xw, axis=0); sd = np.nanstd(Xw, axis=0); sd[sd == 0] = 1.0
    return np.nan_to_num((Xw - mu) / sd, nan=0.0)


def load_young() -> pd.DataFrame:
    split = pd.read_parquet(REL / "V4_split_assignment_clockv1_ocs2_full.parquet",
                            columns=["token_id", "fold"])
    foldmap = dict(zip(split.token_id, split.fold))
    rr = pd.read_parquet(REL / "clockv1_ocs2_full_research_rows.parquet",
                         columns=["token_id", "t_s", "age_s", "n_trades_so_far",
                                  "dd_from_peak", "mult_from_entry", "trail_rate_tps",
                                  "time_since_last_trade_s", "mfe_exec_mult_1p0",
                                  "time_to_mfe_exec_s_1p0", "retention_exec_1p0"])
    rr = rr[rr.age_s < 180].reset_index(drop=True)
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
    keys = rr[["token_id", "t_s"]]
    for fn, cols in ext_map.items():
        d = pd.read_parquet(REL / fn, columns=["token_id", "t_s"] + cols)
        # inner-join to young keys only -> cheap
        d = keys.merge(d, on=["token_id", "t_s"], how="left")
        rr = rr.merge(d, on=["token_id", "t_s"], how="left")
    rr["fold"] = rr.token_id.map(foldmap)
    y = rr["mfe_exec_mult_1p0"].to_numpy(np.float64)
    rr["reach2x"] = (np.isfinite(y) & (y >= 2.0)).astype(float)
    rr["yb"] = np.digitize(rr.age_s.to_numpy(np.float64), YSUB_EDGES)
    return rr


def matched_smd(young: pd.DataFrame) -> list:
    feats = [c for c in (H_RAW_FEATURES + TEMPORAL_FEATURES) if c in young.columns]
    feats = list(dict.fromkeys(feats))
    smd = {}
    for feat in feats:
        num, den = 0.0, 0.0
        for _, cell in young.groupby("yb"):
            pos = cell[cell.reach2x == 1.0][feat].to_numpy(np.float64)
            neg = cell[cell.reach2x == 0.0][feat].to_numpy(np.float64)
            pos = pos[np.isfinite(pos)]; neg = neg[np.isfinite(neg)]
            if len(pos) < 20 or len(neg) < 20:
                continue
            psd = np.sqrt((np.var(pos) + np.var(neg)) / 2.0)
            if psd == 0:
                continue
            num += ((pos.mean() - neg.mean()) / psd) * len(cell); den += len(cell)
        if den > 0:
            smd[feat] = round(num / den, 4)
    return sorted(smd.items(), key=lambda kv: -abs(kv[1]))


def held_out_auc(young: pd.DataFrame) -> dict:
    use = [c for c in (H_RAW_FEATURES + TEMPORAL_FEATURES) if c in young.columns]
    use = list(dict.fromkeys(use))
    out = {}
    for yb, lbl in [(0, "0-30s"), (1, "30-60s"), (2, "60-120s"), (3, "120-180s")]:
        tr = young[(young.yb == yb) & (young.fold == "train")]
        va = young[(young.yb == yb) & (young.fold == "val")]
        if tr.reach2x.nunique() < 2 or va.reach2x.nunique() < 2 or len(va) < 50:
            out[lbl] = {"n_train": int(len(tr)), "n_val": int(len(va)), "auc": None,
                        "note": "insufficient/degenerate"}
            continue
        mu = np.nanmean(tr[use].to_numpy(np.float64), axis=0)
        sd = np.nanstd(tr[use].to_numpy(np.float64), axis=0); sd[sd == 0] = 1.0
        Xtr = np.nan_to_num((tr[use].to_numpy(np.float64) - mu) / sd, nan=0.0)
        Xva = np.nan_to_num((va[use].to_numpy(np.float64) - mu) / sd, nan=0.0)
        clf = LogisticRegression(max_iter=1000, C=1.0, class_weight="balanced")
        clf.fit(Xtr, tr.reach2x.to_numpy())
        p = clf.predict_proba(Xva)[:, 1]
        # token-clustered bootstrap CI on AUC
        rng = np.random.default_rng(SEED)
        vtok = va.token_id.to_numpy(); toks = np.unique(vtok); yv = va.reach2x.to_numpy()
        boot = []
        for _ in range(500):
            samp = rng.choice(toks, len(toks), replace=True)
            sel = np.isin(vtok, samp)
            if len(np.unique(yv[sel])) < 2:
                continue
            boot.append(roc_auc_score(yv[sel], p[sel]))
        ci = ([round(float(np.percentile(boot, 2.5)), 3),
               round(float(np.percentile(boot, 97.5)), 3)] if boot else [None, None])
        out[lbl] = {"n_train": int(len(tr)), "n_val": int(len(va)),
                    "val_reach_rate": round(float(va.reach2x.mean()), 4),
                    "auc": round(float(roc_auc_score(yv, p)), 4),
                    "auc_token_clustered_ci": ci,
                    "beats_chance": bool(ci[0] is not None and ci[0] > 0.5)}
    return out


def young_geometry_excess(young: pd.DataFrame) -> dict:
    """Raw-H geometry excess-over-null CI restricted to young regime (exposure-matched; age already
    <180s). Clusters on raw-H, GMM/BIC k<=6 (young is smaller), cell-clustered CI over exposure
    cells x the 4 young sub-bins."""
    from sklearn.mixture import GaussianMixture
    have = [c for c in H_RAW_FEATURES if c in young.columns]
    Z = winsor_z(young[have].to_numpy(np.float64))
    best = None
    for k in range(1, 7):
        gm = GaussianMixture(n_components=k, covariance_type="full", n_init=8,
                             random_state=SEED, max_iter=300).fit(Z)
        b = gm.bic(Z)
        if best is None or b < best[1]:
            best = (k, b, gm)
    labels = best[2].predict(Z)
    df = young.copy(); df["motif"] = labels
    expo = df["n_trades_so_far"].to_numpy(np.float64)
    qs = np.nanquantile(expo, EXP_QUANTILES)
    df["exp_bin"] = np.clip(np.digitize(expo, qs[1:-1]), 0, 3)
    rng = np.random.default_rng(SEED)
    cells, excess, w = [], [], []
    for (yb, eb), cell in df.groupby(["yb", "exp_bin"]):
        if cell.motif.nunique() < 2 or cell.token_id.nunique() < 20:
            continue
        rates = cell.groupby("motif")["reach2x"].mean()
        spread = float(rates.max() - rates.min())
        lab = cell.motif.to_numpy(); r2 = cell.reach2x.to_numpy()
        nn = [float(pd.Series(r2).groupby(rng.permutation(lab)).mean().pipe(lambda s: s.max() - s.min()))
              for _ in range(50)]
        nm = float(np.mean(nn))
        cells.append({"yb": int(yb), "exp_bin": int(eb), "n_tokens": int(cell.token_id.nunique()),
                      "excess": round(spread - nm, 4)})
        excess.append(spread - nm); w.append(cell.token_id.nunique())
    if not cells:
        return {"k": int(best[0]), "n_cells": 0}
    excess = np.array(excess); w = np.array(w, float)
    point = float(np.average(excess, weights=w))
    bci = np.array([float(np.average(excess[ix := rng.integers(0, len(cells), len(cells))], weights=w[ix]))
                    for _ in range(2000)])
    return {"k": int(best[0]), "clustered_on": have, "n_cells": len(cells),
            "n_cells_positive": int((excess > 0).sum()),
            "token_weighted_point_excess": round(point, 4),
            "cell_clustered_ci_lo": round(float(np.percentile(bci, 2.5)), 4),
            "cell_clustered_ci_hi": round(float(np.percentile(bci, 97.5)), 4),
            "ci_excludes_zero": bool(np.percentile(bci, 2.5) > 0), "per_cell": cells}


def main() -> int:
    young = load_young()
    print(f"[young] states={len(young)} tokens={young.token_id.nunique()} "
          f"reach_rate={young.reach2x.mean():.4f}")
    smd = matched_smd(young)
    auc = held_out_auc(young)
    geo = young_geometry_excess(young)
    rec = {
        "gate": "GEOMETRY_C_TASK3_YOUNG_FULL",
        "scope": "young (age<180s) reach-recognizability on ACTUAL H_t, FULL young-state coverage.",
        "n_young_states": int(len(young)), "n_young_tokens": int(young.token_id.nunique()),
        "young_reach2x_rate": round(float(young.reach2x.mean()), 4),
        "by_young_bin_counts": {lbl: int((young.yb == yb).sum())
                                for yb, lbl in [(0, "0-30s"), (1, "30-60s"), (2, "60-120s"), (3, "120-180s")]},
        "matched_smd_reach_vs_nonreach": dict(smd),
        "top10_discriminators": smd[:10],
        "held_out_logistic_auc_by_young_bin": auc,
        "young_raw_h_geometry_excess": geo,
        "amend_a_anchor": ("AMEND-A: reach_probability is the ONLY genuine young R edge (R_BETTER 7/7); "
                           "economic top-vs-bottom R-reach separation CI>0 only from ~180s onward; "
                           "0-180s straddles 0. This tests whether raw H_t itself carries that edge young."),
    }
    (OUT / "GEOMETRY_C_YOUNG_RESULT.json").write_text(json.dumps(rec, indent=2, default=str),
                                                       encoding="utf-8")
    print("[done] wrote GEOMETRY_C_YOUNG_RESULT.json")
    print("  AUC by bin:")
    for b, d in auc.items():
        print("   ", b, "auc", d.get("auc"), "ci", d.get("auc_token_clustered_ci"), "beats_chance", d.get("beats_chance"))
    print("  young raw-H geometry excess", geo.get("token_weighted_point_excess"),
          "CI", [geo.get("cell_clustered_ci_lo"), geo.get("cell_clustered_ci_hi")],
          "excl0", geo.get("ci_excludes_zero"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
