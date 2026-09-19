# -*- coding: utf-8 -*-
"""背离「过滤因子」挖掘（2026-09-20 owner 要求：提升 30/60 分钟背离判定准确率）。

## 问题

540 天同总体 A/B 已证：**裸背离对"同总体非背离摆动"没有可测出的优势**
（Δ +1.7~2.6pp，CI 全含对照）。owner 要求继续挖 —— 那么问题就变成：

  **「在背离事件内部，哪些事前可知的特征能把好信号和坏信号分开？」**

这是**条件化**问题，不是"背离有没有用"的问题。基线因此是
**「同一个周期、同一个方向的全部背离事件」**（不是对照摆动谷）——
比的是"加了这道条件之后，命中率比裸背离高多少"。

## 纪律（本项目反复吃亏的地方，必须先立规矩）

1. **特征必须因果**：全部只用事件当根及之前的数据（含 `_local_extrema` 自带的
   3 根确认滞后，与生产一致）。
2. **同总体基线**：基线 = 同周期同方向的**全部背离事件**命中率。
3. **子区间稳定性是唯一裁判**：540 天切 3 段，**三段符号必须一致**才留下；
   否则就是多重比较挑出来的噪声（本轮要筛 ~14 个特征 × 2 方向 × 2 周期）。
4. **样本下限**：任何单元格 n<25 不出结论。

## 标签

主标签 = 驻顶/驻底（K=3 交易日、3% 反向，与 `validate_divergence.py` 同口径）。
副标签 = 事件后 K 日收益（顶为负=好）。

用法：python t_io/validation/w35_divergence/mine_divergence_filters.py --days 540
"""
import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent.parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from analysis import divergence  # noqa: E402
from t_io.validation.w35_divergence.validate_divergence import (  # noqa: E402
    K_DAYS, WARMUP, BARS_PER_DAY, _load_watchlist, _top_bottom_flags)

MIN_CELL = 25           # 单元格样本下限
EPS = 1e-9


def _features_at(df, freq, kind, i, j, dif, closes, highs, lows, vols, peaks, troughs):
    """事件当根（+历史）可算的因果特征。i=事件极值根, j=前一个同类极值根。

    返回 {特征名: 值}；取不到的返回 None（后续按缺失剔除）。"""
    f = {}
    n = len(closes)
    win = 20
    if kind == "顶":
        f["dif_level"] = float(dif[i])                       # DIF 绝对高度（离零轴多远）
        f["dif_drop"] = float((dif[j] - dif[i]) / (abs(dif[j]) + EPS))   # 相对衰减
        f["price_excess"] = float(highs[i] / highs[j] - 1)   # 新高幅度
        f["gap_bars"] = int(i - j)
        f["swing_depth"] = float((highs[i] - lows[j:i + 1].min()) / highs[i])  # 中间回落深度
        pre = 40
        if i - pre >= 0:
            f["run_up"] = float(highs[i] / lows[i - pre:i].min() - 1)        # 峰值前的涨幅
        f["is_new_high_60"] = bool(i >= 60 and highs[i] >= highs[i - 60:i].max())
        ma20 = closes[i - win:i].mean() if i >= win else None
        f["ma20_dev"] = float(closes[i] / ma20 - 1) if ma20 else None
        f["vol_runup"] = float(vols[j:i + 1].mean() / (vols[j:i + 1][0] + EPS))  # 量能递增度
    else:
        f["dif_level"] = float(dif[i])
        f["dif_rise"] = float((dif[i] - dif[j]) / (abs(dif[j]) + EPS))
        f["price_excess"] = float(1 - lows[i] / lows[j])      # 新低幅度
        f["gap_bars"] = int(i - j)
        f["swing_depth"] = float((highs[j:i + 1].max() - lows[i]) / lows[i])
        pre = 40
        if i - pre >= 0:
            f["run_down"] = float(1 - lows[i] / highs[i - pre:i].max())
        f["is_new_low_60"] = bool(i >= 60 and lows[i] <= lows[i - 60:i].min())
        ma20 = closes[i - win:i].mean() if i >= win else None
        f["ma20_dev"] = float(closes[i] / ma20 - 1) if ma20 else None
        f["vol_runup"] = float(vols[j:i + 1].mean() / (vols[j:i + 1][0] + EPS))
    if i >= 20:
        f["vol_vs20"] = float(vols[i] / (vols[i - 20:i].mean() + EPS))
    # 摆动幅度（相对该票自身波动）
    med = float(np.median((highs - lows) / closes))
    f["swing_ratio"] = float(f["swing_depth"] / (med + EPS)) if med > 0 else None
    f["atr_ratio"] = med
    return f


def build_events(df, freq, code, other_df=None):
    """产出该 (票,周期) 的全部背离事件（含特征、标签、日期）。"""
    closes = df["close"].astype(float).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    vols = df["volume"].astype(float).values if "volume" in df.columns else np.ones(len(closes))
    times = [str(x)[:10] for x in df["time"].values]
    dif = divergence._macd_dif(closes)
    peaks, troughs = divergence._local_extrema(highs, lows)
    top_set, bottom_set, unconfirmed = _top_bottom_flags(df, peaks, troughs, freq)
    events = divergence.detect_divergence_events(df)

    la = K_DAYS * BARS_PER_DAY[freq]
    pos_pk = {p: k for k, p in enumerate(peaks)}
    pos_tr = {t: k for k, t in enumerate(troughs)}
    out = []
    for e in events:
        i = e["index"]
        if i in unconfirmed or i < WARMUP or i + la >= len(closes):
            continue
        if e["type"] == "顶":
            k = pos_pk.get(i)
            if k is None or k < 1:
                continue
            j = peaks[k - 1]
            hit = i in top_set
            fwd = float(closes[i + la] / highs[i] - 1)
        else:
            k = pos_tr.get(i)
            if k is None or k < 1:
                continue
            j = troughs[k - 1]
            hit = i in bottom_set
            fwd = float(closes[i + la] / lows[i] - 1)
        f = _features_at(df, freq, e["type"], i, j, dif, closes, highs, lows, vols, peaks, troughs)
        rec = {"code": code, "freq": freq, "kind": e["type"], "date": times[i],
               "hit": bool(hit), "fwd": fwd, "consec": bool(e.get("consec")),
               "dif": float(e["dif"]), "price": float(e["price"])}
        rec.update(f)
        out.append(rec)
    return out


def _fwd_ok(rec):
    """副标签：顶背离后应跌（fwd<0）、底背离后应涨（fwd>0）。"""
    return rec["fwd"] < 0 if rec["kind"] == "顶" else rec["fwd"] > 0


def _wilson(h, n, z=1.96):
    if not n:
        return (float("nan"), float("nan"))
    p = h / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(max(p * (1 - p), 0) / n + z * z / (4 * n * n)) / d
    return (c - half, c + half)


FEATURES = ["dif_level", "dif_drop", "dif_rise", "price_excess", "gap_bars", "swing_depth",
            "swing_ratio", "run_up", "run_down", "is_new_high_60", "is_new_low_60",
            "ma20_dev", "vol_runup", "vol_vs20", "consec"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument("--seg", type=int, default=3, help="子区间段数（稳定性检验）")
    args = ap.parse_args()
    stocks = _load_watchlist()
    events = []
    cache = {}
    for code in stocks:
        for freq in ("30min", "60min"):
            try:
                df = divergence.fetch_freq_kline(code, freq, days=args.days)
            except Exception:
                continue
            if df is None or df.empty or len(df) < WARMUP + 40:
                continue
            cache[(code, freq)] = df
            events.extend(build_events(df, freq, code))
    print(f"事件总数 {len(events)}（{len(stocks)} 只 × 2 周期）")

    # 子区间划分
    ds = sorted({e["date"] for e in events})
    import datetime as _dt
    d0, d1 = _dt.date.fromisoformat(ds[0]), _dt.date.fromisoformat(ds[-1])
    span = (d1 - d0).days
    bounds = [d0 + _dt.timedelta(days=int(span * k / args.seg)) for k in range(args.seg + 1)]
    for e in events:
        d = _dt.date.fromisoformat(e["date"])
        e["seg"] = next((k for k in range(args.seg) if bounds[k] <= d < bounds[k + 1]), args.seg - 1)
    print(f"区间 {ds[0]} ~ {ds[-1]}  分 {args.seg} 段")

    report = {}
    for freq in ("30min", "60min"):
        for kind in ("顶", "底"):
            grp = [e for e in events if e["freq"] == freq and e["kind"] == kind]
            if len(grp) < MIN_CELL:
                continue
            base = sum(1 for e in grp if e["hit"]) / len(grp)
            print(f"\n{'='*100}\n{freq} {kind}背离  基线命中率 {base*100:.1f}%  n={len(grp)}"
                  f"   副标签(后市方向对) {sum(1 for e in grp if _fwd_ok(e))/len(grp)*100:.1f}%")
            print(f"{'特征':<16}{'方向':>8}{'分位':>10}{'命中率':>9}{'n':>6}{'Δ':>9}"
                  f"{'95%CI':>18}{'三段符号':>16}")
            rows = []
            for feat in FEATURES:
                vals = [(e[feat], e) for e in grp if e.get(feat) is not None]
                if isinstance(vals[0][0], bool) if vals else False:
                    pass
                if len(vals) < MIN_CELL:
                    continue
                if isinstance(vals[0][0], bool):
                    # 二值特征：True 组 vs False 组
                    hi_ = [e for v, e in vals if v]
                    lo_ = [e for v, e in vals if not v]
                    cells = [("是", hi_), ("否", lo_)]
                else:
                    arr = np.array([v for v, _ in vals], float)
                    q1, q3 = np.percentile(arr, [30, 70])
                    cells = [("高30%", [e for v, e in vals if v >= q3]),
                             ("低30%", [e for v, e in vals if v <= q1])]
                for lab, cell in cells:
                    if len(cell) < MIN_CELL:
                        continue
                    h = sum(1 for e in cell if e["hit"])
                    r = h / len(cell)
                    lo_ci, hi_ci = _wilson(h, len(cell))
                    signs = []
                    for s in range(args.seg):
                        sub = [e for e in cell if e["seg"] == s]
                        if len(sub) >= 8:
                            rr = sum(1 for e in sub if e["hit"]) / len(sub)
                            signs.append("+" if rr > base else "-")
                    stable = "".join(signs)
                    flag = "  ★一致" if len(signs) == args.seg and len(set(signs)) == 1 else ""
                    print(f"{feat:<16}{'':>8}{lab:>10}{r*100:>8.1f}%{len(cell):>6}"
                          f"{(r-base)*100:>+8.1f}pp   [{lo_ci*100:>5.1f},{hi_ci*100:>5.1f}]{stable:>12}{flag}")
                    rows.append({"feat": feat, "cell": lab, "rate": r, "n": len(cell),
                                 "lift": r - base, "seg_signs": stable,
                                 "stable": len(signs) == args.seg and len(set(signs)) == 1})
            report[f"{freq}:{kind}"] = {"base": base, "n": len(grp), "rows": rows}

    (BASE / "t_io" / "validation" / "w35_divergence" / "summary_filters.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n→ summary_filters.json")


if __name__ == "__main__":
    main()
