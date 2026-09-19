# -*- coding: utf-8 -*-
"""同口径基线复核（2026-09-20）—— 回答「加了摆动门槛后，背离还有增量吗」。

## 为什么必须另做这一步

`validate_divergence.py` 的基线是
    base_peaks = [p for p in peaks if p not in unconfirmed and p not in div_peak]
即 **「全部峰 − 背离峰」**。加了 `SWING_MULT` 摆动门槛后，背离事件只从**显著性摆动**里出，
而基线仍包含大量**噪声级小极值** ⇒ **两者不是同一总体**，命中率之差会被
"峰本身的显著性"污染，而不是背离的贡献。

（本项目已有同类前车之鉴：子集结论必须与「同子集、同过滤条件」的基线比，
否则几乎必然造假优势——见 memory `project-divergence-validation`。）

## 本脚本的口径：同一批"可比配对"内部做 A/B

  总体 = 全部**通过摆动门槛**的相邻峰对 (p2,p1)（结构上可比）
  处理组 = 其中**同时满足** price_excess（新高幅度）+ dif_zone（DIF 同价区）的配对 → 背离
  对照组 = 其余配对
两边用**同一套**驻顶/驻底判定（`_top_bottom_flags`）与同一个确认窗口。
⇒ 差值才干净归因于「背离」本身。

用法：python t_io/validation/w35_divergence/baseline_samepop.py --days 90
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
    K_DAYS, R_PCT, WARMUP, BARS_PER_DAY, _load_watchlist, _top_bottom_flags)


def _pairs_ab(df, freq):
    """返回同一总体内的 A/B：[(kind, idx, group, hit, date), ...]。group ∈ {'div','ctrl'}。"""
    closes = df["close"].astype(float).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    dif = divergence._macd_dif(closes)
    peaks, troughs = divergence._local_extrema(highs, lows)
    top_set, bottom_set, unconfirmed = _top_bottom_flags(df, peaks, troughs, freq)

    med = float(np.median((highs - lows) / closes))
    # 顶/底用**不同**摆动门槛（与生产一致），否则"对照总体"会和生产口径漂移
    swing_min_top = divergence.SWING_MULT_TOP * med
    swing_min = divergence.SWING_MULT * med
    ex = divergence.PRICE_EXCESS
    zone = divergence.REQUIRE_DIF_ZONE
    times = [str(x)[:10] for x in df["time"].values]

    rows = []
    for i in range(1, len(peaks)):
        p2, p1 = peaks[i - 1], peaks[i]
        if p1 in unconfirmed:
            continue
        # 结构可比性：中间必须有真实回落（摆动门槛）
        if swing_min_top > 0 and lows[p2:p1 + 1].min() > highs[p1] * (1 - swing_min_top):
            continue
        is_div = (highs[p1] > highs[p2] * (1 + ex) and dif[p1] < dif[p2]
                  and (not zone or dif[p1] > 0))
        rows.append(("顶", p1, "div" if is_div else "ctrl", p1 in top_set, times[p1]))
    for i in range(1, len(troughs)):
        t2, t1 = troughs[i - 1], troughs[i]
        if t1 in unconfirmed:
            continue
        if swing_min > 0 and highs[t2:t1 + 1].max() < lows[t1] * (1 + swing_min):
            continue
        is_div = (lows[t1] < lows[t2] * (1 - ex) and dif[t1] > dif[t2]
                  and (not zone or dif[t1] < 0))
        rows.append(("底", t1, "div" if is_div else "ctrl", t1 in bottom_set, times[t1]))
    return rows


def _wilson(h, n, z=1.96):
    """命中率的 Wilson 区间（小样本比正态近似稳）。"""
    if not n:
        return (float("nan"), float("nan"))
    p = h / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(max(p * (1 - p), 0) / n + z * z / (4 * n * n)) / d
    return (c - half, c + half)


def _split_report(agg, periods, min_div=20):
    """按子区间输出 Δ 及其 CI —— 判断"两个窗口都为正"是不是只是运气。"""
    print(f"\n{'子区间':<26}{'周期':>7}{'方向':>5}"
          f"{'背离':>9}{'n':>5}{'对照':>9}{'n':>6}{'Δ':>9}{'95%CI(背离)':>18}")
    for lo, hi in periods:
        for freq in ("30min", "60min"):
            for kind in ("顶", "底"):
                hd, nd = agg[(lo, hi, freq, kind, "div")]
                hc, nc = agg[(lo, hi, freq, kind, "ctrl")]
                if nd < min_div or not nc:
                    continue
                rd, rc = hd / nd, hc / nc
                clo, chi = _wilson(hd, nd)
                flag = "" if clo <= rc <= chi else "  ← 显著"
                print(f"{lo}~{hi}".ljust(26) + f"{freq:>7}{kind:>5}"
                      f"{rd*100:>8.1f}%{nd:>5}{rc*100:>8.1f}%{nc:>6}"
                      f"{(rd-rc)*100:>+8.1f}pp   [{clo*100:>5.1f},{chi*100:>5.1f}]{flag}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--split", type=int, default=0,
                    help=">0 时把窗口等分成 N 段，逐段算 Δ + CI（稳定性检验）")
    args = ap.parse_args()
    stocks = _load_watchlist()
    print(f"候选股 {len(stocks)} 只 | 历史 {args.days} 天 | 摆动门槛 {divergence.SWING_MULT}x")
    agg = defaultdict(lambda: [0, 0])          # (freq,kind,group) -> [hit,n]
    raw = defaultdict(list)                    # (freq,kind,group) -> [(day,hit)]
    buckets = defaultdict(lambda: [0, 0])      # (lo,hi,freq,kind,group) -> [hit,n]
    dates, periods = [], []

    for code in stocks:
        for freq in ("30min", "60min"):
            try:
                df = divergence.fetch_freq_kline(code, freq, days=args.days)
            except Exception:
                continue
            if df is None or df.empty or len(df) < WARMUP + 20:
                continue
            dates.append(str(df["time"].iloc[0])[:10])
            dates.append(str(df["time"].iloc[-1])[:10])
            for kind, _idx, group, hit, day in _pairs_ab(df, freq):
                cell = agg[(freq, kind, group)]
                cell[1] += 1
                cell[0] += 1 if hit else 0
                raw[(freq, kind, group)].append((day, hit))

    if args.split > 0 and dates:
        periods = _make_periods(min(dates), max(dates), args.split)
        for (freq, kind, group), items in raw.items():
            for day, hit in items:
                for (lo, hi) in periods:
                    if lo <= day <= hi:
                        c = buckets[(lo, hi, freq, kind, group)]
                        c[1] += 1
                        c[0] += 1 if hit else 0
                        break

    print(f"\n{'周期':>7}{'方向':>5}{'组':>7}{'命中':>7}{'样本':>7}{'命中率':>9}")
    out = {}
    for freq in ("30min", "60min"):
        for kind in ("顶", "底"):
            rates = {}
            for group in ("div", "ctrl"):
                h, n = agg[(freq, kind, group)]
                rates[group] = (h / n) if n else None
                print(f"{freq:>7}{kind:>5}{group:>7}{h:>7}{n:>7}"
                      f"{(h/n*100 if n else float('nan')):>8.1f}%")
            d, c = rates.get("div"), rates.get("ctrl")
            if d is not None and c is not None:
                print(f"{'':>7}{'':>5}{'Δ(div-ctrl)':>19}{'':>7}{(d-c)*100:>+8.1f}pp")
            out[f"{freq}:{kind}"] = {"div": d, "ctrl": c,
                                     "n_div": agg[(freq, kind, 'div')][1],
                                     "n_ctrl": agg[(freq, kind, 'ctrl')][1]}

    if periods:
        _split_report(buckets, periods)
        out["periods"] = [f"{lo}~{hi}" for lo, hi in periods]

    (BASE / "t_io" / "validation" / "w35_divergence" / "summary_samepop.json").write_text(
        json.dumps({"days": args.days, "split": args.split,
                    "swing_mult": divergence.SWING_MULT,
                    "price_excess": divergence.PRICE_EXCESS,
                    "require_dif_zone": divergence.REQUIRE_DIF_ZONE,
                    "result": out}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n→ summary_samepop.json")


def _make_periods(lo, hi, n):
    """把 [lo,hi] 按**交易日序**（而非自然日）等分成 n 段。"""
    import datetime as _dt
    d0 = _dt.date.fromisoformat(lo)
    d1 = _dt.date.fromisoformat(hi)
    total = (d1 - d0).days
    out = []
    for i in range(n):
        a = d0 + _dt.timedelta(days=int(total * i / n))
        b = d0 + _dt.timedelta(days=int(total * (i + 1) / n))
        out.append((a.isoformat(), b.isoformat()))
    return out


if __name__ == "__main__":
    main()
