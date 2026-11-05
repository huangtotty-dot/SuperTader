# -*- coding: utf-8 -*-
"""5min 关键均线突破 · 第二轮：固定「离均线距离」后还有没有别的特征。

第一轮结论（`ma_break_5min_20261105.py`）：
  · 走到线上后 60 分钟内**摸过去** 59~62%，**收上去过** 47~50%，但 30 分钟后**仍站在上方的只有 28~30%**；
  · 唯一强且单调的特征是 **dist（事件时收盘离均线的距离）** —— 贴着线 → 35%，离 1.2% 以上 → 17%；
  · 盘中量比（同时点累计，修正前视后）**没有**区分度；斜率高/低几乎无差。

本轮问题：dist 是**机械**的（离得近当然更容易跨过阈值）。把 dist 压到贴近带（0.1%~0.6%）
再来一遍，看量能/趋势/位置/时段这些「真特征」是否还能动概率；并做一个带时间外验证的模型，
比较「只用 dist」与「dist + 其余特征」的 AUC。

运行：`python scripts/experiment/ma_break_5min_stage2_20261105.py`
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
EV = BASE / "tmp" / "ma_break_5min_events.parquet"
MAS = (5, 10, 20, 30, 60)
TARGET = "hold_12"


def dayclust(sub: pd.DataFrame, all_: pd.DataFrame, col: str) -> tuple[float, float]:
    a = sub.groupby("date")[col].mean()
    b = all_.groupby("date")[col].mean()
    j = pd.concat([a, b], axis=1, keys=["g", "a"]).dropna()
    if len(j) < 10:
        return np.nan, np.nan
    d = j["g"] - j["a"]
    sd = d.std(ddof=1)
    t = d.mean() / (sd / np.sqrt(len(d))) if sd > 0 else np.nan
    return float(d.mean()), float(t)


def bins_for(col: str, cuts: list):
    return pd.cut


def main() -> int:
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 40)
    ev = pd.read_parquet(EV)
    print(f"事件 {len(ev):,}；窗口 {ev['date'].min()} ~ {ev['date'].max()}")

    print("\n【A】dist 细分档：走到线上后，30 分钟站稳 / 60 分钟内摸过")
    cuts = [0.0, 0.001, 0.002, 0.003, 0.004, 0.006, 0.01, 0.02, np.inf]
    rows = []
    s = ev[ev["ma"] == 20]
    b = pd.cut(s["dist"], cuts)
    for k, sub in s.groupby(b, observed=True):
        rows.append({"dist档": str(k), "n": len(sub),
                     "30分站稳(%)": round(100 * sub["hold_12"].mean(), 1),
                     "60分摸过(%)": round(100 * sub["brk_high_12"].mean(), 1),
                     "60分收上(%)": round(100 * sub["brk_close_12"].mean(), 1),
                     "事后30分收益(%)": round(100 * sub["ret_12"].mean(), 3)})
    print(pd.concat([pd.DataFrame(rows)]).to_string(index=False))

    print("\n【B】压到贴近带 dist∈(0,0.6%]，逐特征再看（目标 30 分钟站稳）")
    feats = [
        ("volratio", "盘中量比", [0.7, 1.0, 1.3, 1.8]),
        ("volr", "本根量/前24根", [0.8, 1.2, 2.0, 3.0]),
        ("ma_slope5", "均线5日斜率", [-0.01, 0.0, 0.01, 0.03]),
        ("tod", "当日第几根", [6, 12, 24, 36]),
        ("atr20", "日线波动", [0.02, 0.03, 0.045, 0.06]),
        ("dayret", "当日至今", [-0.01, 0.0, 0.01, 0.02]),
        ("ret6", "近30分钟动量", [-0.008, -0.002, 0.004, 0.012]),
        ("ret24", "近120分钟动量", [-0.02, -0.005, 0.005, 0.02]),
        ("gap", "今开相对昨收", [-0.01, -0.002, 0.004, 0.012]),
        ("touches20", "前20日摸线次数", [0, 1, 2, 4]),
        ("align", "多头排列", None),
        ("side60", "MA60 上方", None),
        ("prev_above", "昨收在线上方", None),
    ]
    for ma in MAS:
        s = ev[(ev["ma"] == ma) & (ev["dist"] > 0) & (ev["dist"] <= 0.006)].copy()
        if len(s) < 8000:
            continue
        base = 100 * s[TARGET].mean()
        print(f"\n--- MA{ma} 贴近带 n={len(s):,}  基准 {base:.1f}% ---")
        out = []
        for col, label, cc in feats:
            x = s[col]
            if x.notna().sum() < len(s) * 0.6:
                continue
            if cc is None:
                groups = s.groupby(x, observed=True)
            else:
                groups = s.groupby(pd.cut(x, [-np.inf] + cc + [np.inf]), observed=True)
            for key, sub in groups:
                if len(sub) < 400:
                    continue
                dm, t = dayclust(sub, s, TARGET)
                out.append({"特征": label, "档": str(key), "n": len(sub),
                            "站稳(%)": round(100 * sub[TARGET].mean(), 1),
                            "差(pp)": round(100 * dm, 2), "t": round(t, 1),
                            "30分收益(%)": round(100 * sub["ret_12"].mean(), 3)})
        o = pd.DataFrame(out)
        keep = o[(o["t"].abs() >= 2.5) | (o["特征"].isin(["盘中量比", "多头排列", "MA60 上方", "昨收在线上方"]))]
        print(keep.to_string(index=False) if len(keep) else "  （无 |t|≥2.5 的档）")

    print("\n【C】模型对比（时间外验证：前 60% 日期训练 / 后 40% 验证）")
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except Exception as e:
        print(f"  sklearn 不可用: {e}")
        return 0
    feats_core = ["dist"]
    feats_all = ["dist", "volratio", "volr", "ma_slope5", "tod", "atr20",
                 "dayret", "ret6", "ret24", "gap", "touches20", "align",
                 "side60", "prev_above"]
    for ma in MAS:
        s = ev[(ev["ma"] == ma) & (ev["dist"] > 0) & (ev["dist"] <= 0.006)].dropna(
            subset=[TARGET, "dist"]).copy()
        if len(s) < 20000:
            continue
        dates = sorted(s["date"].unique())
        cut = dates[int(len(dates) * 0.6)]
        tr, te = s[s["date"] < cut], s[s["date"] >= cut]
        ytr, yte = tr[TARGET].to_numpy(int), te[TARGET].to_numpy(int)
        r = {}
        for name, cols in (("dist only", feats_core), ("dist+其余", feats_all)):
            Xtr = tr[cols].astype(float).fillna(tr[cols].astype(float).median())
            Xte = te[cols].astype(float).fillna(tr[cols].astype(float).median())
            m = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
            m.fit(Xtr, ytr)
            p = m.predict_proba(Xte)[:, 1]
            r[name] = (roc_auc_score(yte, p), p)
        base_rate = 100 * yte.mean()
        print(f"  MA{ma}: 训练 {len(tr):,} / 验证 {len(te):,}，验证基准 {base_rate:.1f}%  "
              f"AUC(dist only)={r['dist only'][0]:.4f}  AUC(dist+其余)={r['dist+其余'][0]:.4f}")
        # 十分位校准
        p = r["dist+其余"][1]
        q = pd.qcut(pd.Series(p), 10, duplicates="drop")
        cal = pd.DataFrame({"p": p, "y": yte}).groupby(q.values, observed=True).agg(
            n=("y", "size"), 预测=("p", "mean"), 实际=("y", "mean"))
        cal["预测"] = (100 * cal["预测"]).round(1)
        cal["实际"] = (100 * cal["实际"]).round(1)
        print("    十分位校准:")
        print(cal.to_string())
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
