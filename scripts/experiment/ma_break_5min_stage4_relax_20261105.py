# -*- coding: utf-8 -*-
"""5min 关键均线 · 第四轮（2026-11-05 owner 反馈后）：贴近带口径的概率模型。

第一版事件要求「本根盘中摸到线且收盘仍在线下」，结果只在**摸线那一刻**成立
（科创半导离 MA20 还有 1.4% 时什么也不显示）。owner 的诉求是「走到关键位置」⇒
把事件改成 **走进均线下方 2% 的贴近带**（从带外进入的第一根），重新拟合三特征模型。

本脚本：读 `tmp/ma_break_5min_events_relax.parquet`，做时间外验证 + 校准，
并与第一版（摸线口径）系数在同一批样本上对比，决定生产端用哪一组。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
EV = BASE / "tmp" / "ma_break_5min_events_relax.parquet"
TARGET = "hold_12"
OLD = (-0.868638, -0.971440, 0.112518, -0.792758)   # 第一版（摸线口径）


def main() -> int:
    pd.set_option("display.width", 200)
    ev = pd.read_parquet(EV)
    d = ev[(ev["dist"] > 0) & (ev["dist"] <= 0.02)].dropna(
        subset=[TARGET, "dist", "atr20", "tod"]).copy()
    d["f_dist"] = d["dist"] * 100
    d["f_atr"] = d["atr20"] * 100
    d["f_tod"] = (d["tod"] / 48.0).clip(upper=0.85)   # 收盘后不外推（训练里没有样本）
    print(f"贴近带样本 {len(d):,}  票 {d['code'].nunique()}  日 {d['date'].nunique()}  "
          f"{d['date'].min()} ~ {d['date'].max()}")

    print("\n【1】基准：走到均线下方 2% 以内之后（30 分钟）")
    rows = []
    for ma in (5, 10, 20, 30, 60):
        s = d[d["ma"] == ma]
        rows.append({"MA": f"MA{ma}", "n": len(s),
                     "摸过": round(100 * s["brk_high_12"].mean(), 1),
                     "收上": round(100 * s["brk_close_12"].mean(), 1),
                     "站稳": round(100 * s[TARGET].mean(), 1),
                     "30分收益%": round(100 * s["ret_12"].mean(), 3)})
    print(pd.DataFrame(rows).to_string(index=False))

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    cols = ["f_dist", "f_atr", "f_tod"]
    dates = sorted(d["date"].unique())
    cut = dates[int(len(dates) * 0.6)]
    tr, te = d[d["date"] < cut], d[d["date"] >= cut]
    m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))
    m.fit(tr[cols], tr[TARGET])
    p = m.predict_proba(te[cols])[:, 1]
    print(f"\n【2】时间外（训练 {len(tr):,} / 验证 {len(te):,}，基准 {100*te[TARGET].mean():.1f}%）")
    print(f"  新模型（贴近带拟合）AUC = {roc_auc_score(te[TARGET], p):.4f}")

    zo = (OLD[0] + OLD[1] * te["f_dist"] + OLD[2] * te["f_atr"] + OLD[3] * te["f_tod"])
    po = 1 / (1 + np.exp(-zo))
    print(f"  旧模型（摸线口径，直接套）AUC = {roc_auc_score(te[TARGET], po):.4f}")

    lr = m.named_steps["logisticregression"]
    sc = m.named_steps["standardscaler"]
    raw = lr.coef_[0] / sc.scale_
    itc = lr.intercept_[0] - float(np.sum(lr.coef_[0] * sc.mean_ / sc.scale_))
    print(f"\n【3】新系数（原始尺度）: intercept={itc:.6f}"
          f"  dist={raw[0]:.6f}  atr={raw[1]:.6f}  tod={raw[2]:.6f}")

    print("\n【4】校准（验证段十分位）")
    q = pd.qcut(pd.Series(p), 10, duplicates="drop")
    cal = pd.DataFrame({"p": p, "y": te[TARGET].to_numpy()}).groupby(q.values, observed=True).agg(
        n=("y", "size"), 新预测=("p", "mean"), 实际=("y", "mean"))
    cal["新预测"] = (100 * cal["新预测"]).round(1)
    cal["实际"] = (100 * cal["实际"]).round(1)
    cal["旧预测"] = (100 * pd.Series(po, index=te.index).groupby(q.values, observed=True).mean()).round(1)
    print(cal.to_string())

    print("\n【5】分均线（验证段）")
    for ma in (5, 10, 20, 30, 60):
        s = te[te["ma"] == ma]
        ps = m.predict_proba(s[cols])[:, 1]
        print(f"  MA{ma}: n={len(s):,}  基准={100*s[TARGET].mean():.1f}%  "
              f"AUC={roc_auc_score(s[TARGET], ps):.4f}  "
              f"最好10%={100*s[TARGET][ps >= np.quantile(ps, 0.9)].mean():.1f}%  "
              f"最差10%={100*s[TARGET][ps <= np.quantile(ps, 0.1)].mean():.1f}%")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
