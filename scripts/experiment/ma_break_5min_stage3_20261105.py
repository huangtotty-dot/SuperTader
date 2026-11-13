# -*- coding: utf-8 -*-
"""5min 关键均线突破 · 第三轮：可移植的概率模型（时间外验证 + 按月稳定性）。

第二轮结论：
  · dist（贴线距离）是主因，但压到 dist≤0.6% 后仍有三个稳定特征：
      - atr20 日线波动：高波动 34~37% vs 低波动 22~26%（t −9~−4）
      - tod 时段：09:30~10:00 33~34% vs 13:00~14:35 27~28%（t −5~−3）
      - 趋势背景：MA60 上方 / 多头排列 / 均线上行 各 +1~2pp（弱）
      - 昨收已在线下（今天从下方靠近）比昨收在上方再回踩略高
  · 盘中量比只在 MA5 有一点（+3pp, t=3.0），MA10~60 基本无效，时间外不稳定 ⇒ 不进模型。

本轮：把特征写成一个**可移植的线性模型**（权重可硬编码进 gui），只做时间外验证，
并按月看 AUC 是否稳定；输出各特征的系数与最终概率分档。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
EV = BASE / "tmp" / "ma_break_5min_events.parquet"
TARGET = "hold_12"

FEATS = [
    ("dist", "贴线距离(%)", lambda d: d["dist"] * 100),
    ("atr20", "日线波动(%)", lambda d: d["atr20"] * 100),
    ("tod", "当日第几根/49", lambda d: d["tod"] / 48.0),
    ("slope5", "均线5日斜率(%)", lambda d: d["ma_slope5"] * 100),
    ("prev_above", "昨收在线上方", lambda d: d["prev_above"].astype(float)),
    ("side60", "价在MA60上方", lambda d: d["side60"].astype(float)),
    ("align", "多头排列", lambda d: d["align"].astype(float)),
    ("dayret", "当日至今(%)", lambda d: d["dayret"] * 100),
    ("ret6", "近30分钟动量(%)", lambda d: d["ret6"] * 100),
]


def prep(ev: pd.DataFrame) -> pd.DataFrame:
    d = ev[(ev["dist"] > 0) & (ev["dist"] <= 0.02)].copy()
    d = d.dropna(subset=[TARGET, "dist", "atr20", "ma_slope5", "prev_above", "side60", "align"])
    for name, _, fn in FEATS:
        d["f_" + name] = fn(d)
    d = d.dropna(subset=["f_" + n for n, _, _ in FEATS])
    return d


def main() -> int:
    pd.set_option("display.width", 200)
    ev = pd.read_parquet(EV)
    d = prep(ev)
    print(f"样本 {len(d):,}（dist∈(0,2%]）  窗口 {d['date'].min()} ~ {d['date'].max()}")

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    cols = ["f_" + n for n, _, _ in FEATS]
    dates = sorted(d["date"].unique())
    cut = dates[int(len(dates) * 0.6)]
    tr, te = d[d["date"] < cut], d[d["date"] >= cut]
    m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))
    m.fit(tr[cols], tr[TARGET])
    p = m.predict_proba(te[cols])[:, 1]
    print(f"\n训练 {tr['date'].min()}~{tr['date'].max()}（{len(tr):,}） | "
          f"验证 {te['date'].min()}~{te['date'].max()}（{len(te):,}）  验证基准 {100*te[TARGET].mean():.1f}%")
    print(f"时间外 AUC = {roc_auc_score(te[TARGET], p):.4f}")

    # 只用 dist 的对照
    m0 = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000))
    m0.fit(tr[["f_dist"]], tr[TARGET])
    p0 = m0.predict_proba(te[["f_dist"]])[:, 1]
    print(f"对照（只用 dist）AUC = {roc_auc_score(te[TARGET], p0):.4f}")

    # 系数（标准化后）
    lr = m.named_steps["logisticregression"]
    sc = m.named_steps["standardscaler"]
    coef = lr.coef_[0]
    print("\n标准化系数（正=更容易翻过去）:")
    for (n, label, _), c in sorted(zip(FEATS, coef), key=lambda x: -abs(x[1])):
        print(f"  {label:<14} {c:+.3f}")

    # 原始尺度系数（截距 + 每单位特征），便于硬编码
    raw = coef / sc.scale_
    intercept = lr.intercept_[0] - float(np.sum(coef * sc.mean_ / sc.scale_))
    print(f"\n原始尺度: intercept={intercept:+.4f}")
    for (n, label, _), c in zip(FEATS, raw):
        print(f"  {label:<14} {c:+.5f} /单位")

    # 按月 AUC
    print("\n按月 AUC（验证段，样本≥3000 的月份）:")
    for month, sub in te.groupby(te["date"].str[:7]):
        if len(sub) < 3000:
            continue
        pm = m.predict_proba(sub[cols])[:, 1]
        print(f"  {month}: n={len(sub):,}  基准={100*sub[TARGET].mean():.1f}%  "
              f"AUC={roc_auc_score(sub[TARGET], pm):.4f}")

    # 概率分档校准（验证段）
    print("\n验证段十分位校准:")
    q = pd.qcut(pd.Series(p), 10, duplicates="drop")
    cal = pd.DataFrame({"p": p, "y": te[TARGET].to_numpy()}).groupby(q.values, observed=True).agg(
        n=("y", "size"), 预测=("p", "mean"), 实际=("y", "mean"))
    cal["预测"] = (100 * cal["预测"]).round(1)
    cal["实际"] = (100 * cal["实际"]).round(1)
    print(cal.to_string())

    # 组合规则示例：好/差两头
    print("\n组合规则（验证段）:")
    te2 = te.copy()
    te2["p"] = p
    for lo, hi, label in ((0.0, 0.22, "最差档(预测<22%)"), (0.22, 0.30, "22~30%"),
                          (0.30, 0.38, "30~38%"), (0.38, 1.0, "最好档(预测≥38%)")):
        s = te2[(te2["p"] >= lo) & (te2["p"] < hi)]
        if len(s) == 0:
            continue
        print(f"  {label:<16} n={len(s):>6,}  实际站稳={100*s[TARGET].mean():5.1f}%  "
              f"30分钟收益均值={100*s['ret_12'].mean():+.3f}%")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
