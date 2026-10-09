# -*- coding: utf-8 -*-
"""
S0 账户级纯选股基线 · 数据准备（步骤1/3）
=========================================
输入（只读，不修改 F3 任何产物）：
  - F3 因子快照  results/daily_selection_screen_2026-10-09/factors_filtered.parquet
  - F3 summary.csv（取 ICIR h5 绝对值做 ICIR 加权对照臂）
  - 截面面板     t_io/validation/xsection/panel/（open/high/low/close）

输出（s0_account/results/）：
  - scores.parquet   长表 [symbol, date, score_eq, score_icir, rev10_z]
                     四因子截面 z-score（winsorize 1%）合成；完备截面
                     （四因子全非 NaN 才入样，即 F3 过滤后宇宙）。
  - pivots.pkl       {open, high, low, close, score_eq, score_icir, rev10_z}
                     dates × symbols 透视表（float32），供十分位与账户仿真复用。

因子方向（与 F3 IC 符号对齐，值大=看好）：
  REV10 原样(+IC) / GAP 原样(+IC) / AMOUNT_CHG 取负 / PRICE_POS60 取负
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
FM_DIR = HERE.parent
WORKSPACE = FM_DIR.parents[2]
sys.path.insert(0, str(FM_DIR))

from ic_layer import load_panel  # noqa: E402

F3_DIR = FM_DIR / "results" / "daily_selection_screen_2026-10-09"
PANEL_DIR = WORKSPACE / "t_io" / "validation" / "xsection" / "panel"
OUT_DIR = HERE / "results"
SINCE = "2023-09-01"

# 值大=看好 的方向映射：src -> (新列名, 符号)
FACTOR_MAP = {
    "REV10": ("z_REV10", +1.0),
    "GAP": ("z_GAP", +1.0),
    "AMOUNT_CHG": ("z_AMT_INV", -1.0),
    "PRICE_POS60": ("z_POS_INV", -1.0),
}
WINSOR_Q = 0.01


def _winsorize_z(x: np.ndarray) -> np.ndarray:
    """单截面：1% 缩尾后 z-score。NaN 保留。"""
    out = np.full_like(x, np.nan, dtype=np.float64)
    ok = np.isfinite(x)
    if ok.sum() < 30:
        return out
    v = x[ok]
    lo, hi = np.quantile(v, [WINSOR_Q, 1.0 - WINSOR_Q])
    v = np.clip(v, lo, hi)
    mu, sd = v.mean(), v.std()
    if sd <= 0 or not np.isfinite(sd):
        return out
    out[ok] = (v - mu) / sd
    return out


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. ICIR 权重（F3 summary.csv，|ICIR h5| 归一）──
    summ = pd.read_csv(F3_DIR / "summary.csv")
    summ = summ.set_index("factor")
    icir = {
        "REV10": abs(float(summ.loc["REV10", "icir_h5"])),
        "GAP": abs(float(summ.loc["GAP", "icir_h5"])),
        "AMOUNT_CHG": abs(float(summ.loc["AMOUNT_CHG", "icir_h5"])),
        "PRICE_POS60": abs(float(summ.loc["PRICE_POS60", "icir_h5"])),
    }
    wsum = sum(icir.values())
    wts = {k: v / wsum for k, v in icir.items()}
    print(f"[icir_w] { {k: round(v, 4) for k, v in wts.items()} }", flush=True)

    # ── 2. 因子快照 → 截面 z-score 合成 ──
    fac = pd.read_parquet(F3_DIR / "factors_filtered.parquet")
    fac["date"] = pd.to_datetime(fac["date"]).dt.normalize()
    fac["symbol"] = fac["symbol"].astype(str)
    src_cols = list(FACTOR_MAP)
    fac = fac[["symbol", "date"] + src_cols]
    # 完备截面：四因子全非 NaN（= F3 过滤后宇宙）
    cc = fac.dropna(subset=src_cols).reset_index(drop=True)
    print(f"[snap] {len(fac)} 行 -> 完备 {len(cc)} 行, "
          f"{cc['date'].nunique()} 日, {cc['symbol'].nunique()} 只", flush=True)

    zframes = {}
    for src, (zcol, sign) in FACTOR_MAP.items():
        v = (cc[src].to_numpy(np.float64) * sign)
        zframes[zcol] = v
    zdf = cc[["symbol", "date"]].copy()
    for zcol in zframes:
        zdf[zcol] = zframes[zcol]

    zdf = zdf.sort_values(["date", "symbol"], kind="mergesort").reset_index(drop=True)
    gid = pd.factorize(zdf["date"], sort=True)[0]
    # 按日截面 winsorize+z（617 日循环，每日 ~4k 行，向量化分块）
    boundaries = np.flatnonzero(np.r_[True, gid[1:] != gid[:-1]])
    boundaries = np.r_[boundaries, len(zdf)]
    for zcol in zframes:
        arr = zdf[zcol].to_numpy(np.float64)
        out = np.empty_like(arr)
        for s, e in zip(boundaries[:-1], boundaries[1:]):
            out[s:e] = _winsorize_z(arr[s:e])
        zdf[zcol] = out
    print(f"[z] winsorize+z done {time.time()-t0:.1f}s", flush=True)

    zdf["score_eq"] = zdf[[c for c, _ in FACTOR_MAP.values()]].mean(axis=1)
    zdf["score_icir"] = (
        zdf["z_REV10"] * wts["REV10"]
        + zdf["z_GAP"] * wts["GAP"]
        + zdf["z_AMT_INV"] * wts["AMOUNT_CHG"]
        + zdf["z_POS_INV"] * wts["PRICE_POS60"]
    )
    zdf["rev10_z"] = zdf["z_REV10"]

    scores = zdf[["symbol", "date", "score_eq", "score_icir", "rev10_z"]].copy()
    scores.to_parquet(OUT_DIR / "scores.parquet", index=False)
    print(f"[save] scores.parquet {len(scores)} 行", flush=True)

    # ── 3. 面板 → 价格/分数透视表 ──
    panel = load_panel(str(PANEL_DIR))
    panel = panel[panel["date"] >= pd.Timestamp(SINCE)].reset_index(drop=True)
    print(f"[panel] {panel['symbol'].nunique()} 只, {len(panel)} 行, "
          f"{panel['date'].min().date()} ~ {panel['date'].max().date()} "
          f"({time.time()-t0:.1f}s)", flush=True)

    pivots = {}
    for col in ["open", "high", "low", "close"]:
        pivots[col] = panel.pivot(index="date", columns="symbol", values=col)
    del panel
    for col in ["score_eq", "score_icir", "rev10_z"]:
        pivots[col] = scores.pivot(index="date", columns="symbol", values=col)
    # 对齐：所有 pivot 同一日期轴（并集，面板日期 ⊇ 分数日期）
    dates = pivots["close"].index
    for k in pivots:
        pivots[k] = pivots[k].reindex(dates).astype(np.float32)
    with open(OUT_DIR / "pivots.pkl", "wb") as f:
        pickle.dump(pivots, f, protocol=4)
    print(f"[save] pivots.pkl dates={len(dates)} symbols={pivots['close'].shape[1]} "
          f"总耗时 {time.time()-t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
