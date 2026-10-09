# -*- coding: utf-8 -*-
"""30min「顶部（减仓）」判定**精度调优**（2026-10-09，owner：尽量提升准确率、防噪音）。

问题：现行「减仓」口径 = T1顶背离 或 T2量价背离（经典定义，无加严门槛）。实测 lift 很高，但
T1 用「价创新高 + DIF 未创新高」这一**宽松**定义，会收录大量「深负区小反弹」/「同段做顶」噪声。

本脚本在同一 981只×540日 面板上，一次性评估多组**加严门槛**下「减仓」call 的精度：
  · zone  —— 顶背离须发生在 DIF>0 区（对齐 同花顺/通达信）
  · swing —— 两摆动高点间须有 ≥k×中位 bar 振幅 的逆向摆动（滤同段做顶）
指标：P(未来 H 根下跌) − 基线(=精度/lift)、z、均收益、事件数。**升沿**计事件（避免状态重复计数）。

用法：
  python t_io/validation/m30_top/tune_m30_precision.py            # 全量(~4min)
  python t_io/validation/m30_top/tune_m30_precision.py --limit 120
"""
import argparse
import glob
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[3]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from analysis import m30_features as mf  # noqa: E402

PANEL = BASE / "t_io" / "cache" / "tushare_mins"
OUT = Path(__file__).resolve().parent
HORIZONS = (4, 8, 16)


def _load_rows(fp):
    try:
        d = json.loads(Path(fp).read_text(encoding="utf-8"))
        rows = d.get("rows") or []
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
        return df if {"open", "high", "low", "close", "volume"}.issubset(df.columns) else None
    except Exception:
        return None


def _variant_masks(df):
    n = len(df)
    feats = mf.scan_top_features(df)          # 基准口径(门槛关)；逐根带 t1/t2/zone_ok/t1_swing_ratio
    t1 = np.zeros(n, bool); t2 = np.zeros(n, bool); zone = np.zeros(n, bool); swr = np.zeros(n)
    for f in feats:
        i = f["index"]
        t1[i] = f["t1"]; t2[i] = f["t2"]; zone[i] = f["zone_ok"]
        swr[i] = f.get("t1_swing_ratio") or 0.0
    V = {
        "T1": t1,
        "T1+zone": t1 & zone,
        "T1+zone+sw2.5": t1 & zone & (swr >= 2.5),
        "T1+zone+sw4": t1 & zone & (swr >= 4.0),
        "T1+zone+sw6": t1 & zone & (swr >= 6.0),
        "T2": t2,
        "T1|T2": t1 | t2,
        "(T1+zone)|T2": (t1 & zone) | t2,
        "T1+zone&T2": t1 & zone & t2,
    }
    return {k: (m & ~np.r_[False, m[:-1]]) for k, m in V.items()}   # 升沿


def _acc_step(df, acc, base_acc):
    closes = df["close"].astype(float).values
    n = len(closes)
    V = _variant_masks(df)
    start = mf.MIN_BARS
    for H in HORIZONS:
        hi = n - H
        if hi <= start:
            continue
        elig = np.zeros(n, bool); elig[start:hi] = True
        fwd = np.full(n, np.nan); fwd[start:hi] = closes[start + H:hi + H] / closes[start:hi] - 1.0
        ok = elig & ~np.isnan(fwd)
        b = base_acc[H]; b["n"] += int(ok.sum()); b["down"] += int((fwd[ok] < 0).sum()); b["ret"] += float(fwd[ok].sum())
        for k, m in V.items():
            sel = m & ok
            ne = int(sel.sum())
            if not ne:
                continue
            a = acc[k][H]; a["n"] += ne; a["down"] += int((fwd[sel] < 0).sum()); a["ret"] += float(fwd[sel].sum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    keys = ["T1", "T1+zone", "T1+zone+sw2.5", "T1+zone+sw4", "T1+zone+sw6",
            "T2", "T1|T2", "(T1+zone)|T2", "T1+zone&T2"]
    acc = {k: {H: {"n": 0, "down": 0, "ret": 0.0} for H in HORIZONS} for k in keys}
    base_acc = {H: {"n": 0, "down": 0, "ret": 0.0} for H in HORIZONS}
    files = sorted(glob.glob(str(PANEL / "*_30min_d540.json")))
    if args.limit:
        files = files[:args.limit]
    used = 0
    for i, fp in enumerate(files):
        df = _load_rows(fp)
        if df is None or len(df) < mf.MIN_BARS + max(HORIZONS):
            continue
        try:
            _acc_step(df, acc, base_acc); used += 1
        except Exception:
            continue
        if (i + 1) % 100 == 0:
            print(f"  ... {i+1}/{len(files)}")
    base = {H: {"down": base_acc[H]["down"] / max(base_acc[H]["n"], 1),
                "ret": base_acc[H]["ret"] / max(base_acc[H]["n"], 1), "n": base_acc[H]["n"]}
            for H in HORIZONS}
    L = [f"# 30min 顶部判定 精度调优（{datetime.now():%Y-%m-%d %H:%M}）", "",
         f"- 面板 {PANEL.name}（{used}/{len(files)} 只）；事件=升沿；H 为前瞻根数（8 根/日）。", ""]
    L.append("## 基线（全样本）")
    L.append("| H | 样本 | 下跌占比 | 均收益 |")
    L.append("|---|---|---|---|")
    for H in HORIZONS:
        L.append(f"| {H} | {base[H]['n']} | {base[H]['down']:.1%} | {base[H]['ret']:+.3%} |")
    L.append("")
    for k in keys:
        L.append(f"## {k}")
        L.append("| H | 事件数 | 下跌占比(精度) | 基线 | lift | z | 均收益 |")
        L.append("|---|---|---|---|---|---|---|")
        for H in HORIZONS:
            a = acc[k][H]; nb = max(base_acc[H]["n"], 1); p0 = base_acc[H]["down"] / nb; ne = a["n"]
            if not ne:
                L.append(f"| {H} | 0 | — | — | — | — | — |"); continue
            ph = a["down"] / ne
            se = math.sqrt(max(p0 * (1 - p0), 1e-9) / ne)
            L.append(f"| {H} | {ne} | {ph:.1%} | {p0:.1%} | {ph-p0:+.2%} | {(ph-p0)/se:.1f} | {a['ret']/ne:+.3%} |")
        L.append("")
    (OUT / "调优_m30_precision.md").write_text("\n".join(L), encoding="utf-8")
    print(f"完成 used={used} → {OUT/'调优_m30_precision.md'}")


if __name__ == "__main__":
    main()
