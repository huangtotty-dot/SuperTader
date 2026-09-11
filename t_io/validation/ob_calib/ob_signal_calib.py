# -*- coding: utf-8 -*-
"""ob_signal_calib.py — 持仓体检信号前瞻校准（2026-09-11）

目的：量化 RSI>70 / KDJ-J>100 / CCI>100 / 收盘破BOLL上轨 / 日线顶背离(≥1,≥2) 各自对
未来 5 日收益的预测力，判断"该不该报、报多高"，给 GUI 风险口径定阈值。

口径：日线（前复权），信号日 T 满足条件 → 统计 T+5 收益均值/下跌占比，对比全样本基线。
用法：python t_io/validation/ob_calib/ob_signal_calib.py [--days 500]
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CODES = ["588170", "600481", "002451", "000988", "600176", "603667", "300054", "002639", "300153"]
FWD = 5


def _rsi(c, n=14):
    d = c.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def _kdj(h, l, c, n=9):
    ll = l.rolling(n).min()
    hh = h.rolling(n).max()
    rsv = (c - ll) / (hh - ll).replace(0, np.nan) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return 3 * k - 2 * d


def _cci(h, l, c, n=14):
    tp = (h + l + c) / 3
    ma = tp.rolling(n).mean()
    md = (tp - ma).abs().rolling(n).mean()
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def _divergence(high, rsi, dif):
    """近60日两个局部高点：价新高但 RSI 或 DIF 未新高 → 顶背离(返回布尔序列)。"""
    out = np.zeros(len(high), dtype=bool)
    for i in range(62, len(high)):
        win = range(i - 59, i + 1)
        peaks = [j for j in range(2, len(list(win)) - 2)
                 if high[list(win)[j]] >= high[list(win)[j] - 1] and high[list(win)[j]] >= high[list(win)[j] - 2]
                 and high[list(win)[j]] >= high[list(win)[j] + 1] and high[list(win)[j]] >= high[list(win)[j] + 2]]
        if len(peaks) < 2:
            continue
        p2, p1 = list(win)[peaks[-2]], list(win)[peaks[-1]]
        if high[p1] > high[p2]:
            dv_rsi = pd.notna(rsi[p1]) and pd.notna(rsi[p2]) and rsi[p1] < rsi[p2]
            dv_dif = pd.notna(dif[p1]) and pd.notna(dif[p2]) and dif[p1] < dif[p2]
            out[i] = bool(dv_rsi or dv_dif)
    return out


def _stats(mask, fwd):
    n = int(mask.sum())
    if n == 0:
        return (0, None, None)
    r = fwd[mask]
    return (n, float(np.nanmean(r) * 100), float(np.nanmean(r < 0) * 100))


def run(days=500):
    rows, all_fwd = [], []
    for code in CODES:
        try:
            from core.market_data import get_provider
            df = get_provider().daily(code, days)
        except Exception as e:
            print(f"{code} 数据失败: {e}")
            continue
        if df is None or len(df) < 120:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
        rsi = _rsi(c); kdj = _kdj(h, l, c); cci = _cci(h, l, c)
        e12 = c.ewm(span=12, adjust=False).mean(); e26 = c.ewm(span=26, adjust=False).mean()
        dif = e12 - e26
        boll_up = c.rolling(20).mean() + 2 * c.rolling(20).std()
        div = _divergence(h.values, rsi.values, dif.values)
        fwd = c.shift(-FWD) / c - 1
        base = fwd.notna().values
        all_fwd.append(fwd[base].values)
        flags = {
            "RSI>70": (rsi > 70).values,
            "KDJ>100": (kdj > 100).values,
            "CCI>100": (cci > 100).values,
            "BOLL上轨": (c > boll_up).values,
            "顶背离≥1": div,
        }
        flags["顶背离≥2"] = div  # 单指标版这里先用同日；下方整体再做组合
        for k, m in flags.items():
            m = m & base
            n, mean, down = _stats(m, fwd.values)
            rows.append({"code": code, "flag": k, "n": n, "fwd5_mean%": mean, "down%": down})
    base_all = np.concatenate([x for x in all_fwd if len(x)]) if all_fwd else np.array([])
    print(f"\n=== 基线（全样本 T+5）: n={len(base_all)} mean={np.nanmean(base_all)*100:+.2f}% "
          f"down={np.nanmean(base_all<0)*100:.1f}% ===\n")
    if rows:
        t = pd.DataFrame(rows)
        agg = t.groupby("flag").agg(n=("n", "sum"),
                                    fwd5_mean=("fwd5_mean%", "mean"),
                                    down=("down%", "mean")).sort_values("fwd5_mean")
        print(agg.to_string(float_format=lambda x: f"{x:.2f}"))
    print("\n判读：fwd5_mean 明显低于基线且 down% 明显高于基线 → 该信号有前瞻意义（值得报）；"
          "接近/高于基线 → 噪声（不该报或仅提示）。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=500)
    a = ap.parse_args()
    run(a.days)
