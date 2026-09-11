# -*- coding: utf-8 -*-
"""rsi_sensitivity.py — RSI 阈值敏感性实验（2026-09-11）

回答"RSI 阈值该定多少"：对 9 持仓 ×N 日，扫描 RSI>65/68/70/72/75/80 及组合
（+趋势下行代理 close<MA20、+日线顶背离≥1）的 T+5 前瞻，选"真正偏弱且样本够"的档。

用法：python t_io/validation/ob_calib/rsi_sensitivity.py [--days 500]
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


def _div(high, rsi, dif):
    out = np.zeros(len(high), dtype=bool)
    for i in range(62, len(high)):
        idxs = list(range(i - 59, i + 1))
        peaks = [j for j in range(2, len(idxs) - 2)
                 if high[idxs[j]] >= high[idxs[j] - 1] and high[idxs[j]] >= high[idxs[j] - 2]
                 and high[idxs[j]] >= high[idxs[j] + 1] and high[idxs[j]] >= high[idxs[j] + 2]]
        if len(peaks) < 2:
            continue
        p2, p1 = idxs[peaks[-2]], idxs[peaks[-1]]
        if high[p1] > high[p2] and (pd.notna(rsi[p1]) and pd.notna(rsi[p2]) and rsi[p1] < rsi[p2]
                                    or pd.notna(dif[p1]) and pd.notna(dif[p2]) and dif[p1] < dif[p2]):
            out[i] = True
    return out


def run(days=500):
    frames = []
    for code in CODES:
        try:
            from core.market_data import get_provider
            df = get_provider().daily(code, days)
        except Exception:
            continue
        if df is None or len(df) < 120:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        c, h, v = df["close"], df["high"], df["volume"]
        rsi = _rsi(c)
        e12 = c.ewm(span=12, adjust=False).mean(); e26 = c.ewm(span=26, adjust=False).mean()
        dif = (e12 - e26)
        ma20 = c.rolling(20).mean()
        d_ = _div(h.values, rsi.values, dif.values)
        fwd = (c.shift(-FWD) / c - 1).values
        frames.append(pd.DataFrame({"rsi": rsi.values, "fwd": fwd, "div": d_,
                                    "below_ma20": (c < ma20).values}).dropna())
    al = pd.concat(frames, ignore_index=True)
    base = al["fwd"]
    print(f"=== 基线 n={len(base)} T+5 mean={base.mean()*100:+.2f}% down={(base<0).mean()*100:.1f}% ===\n")
    rows = []
    for th in (65, 68, 70, 72, 75, 80):
        for name, mask in (
            (f"RSI>{th}", al["rsi"] > th),
            (f"RSI>{th} & 趋势下行(close<MA20)", (al["rsi"] > th) & al["below_ma20"]),
            (f"RSI>{th} & 顶背离", (al["rsi"] > th) & al["div"]),
        ):
            m = mask.values
            n = int(m.sum())
            if n == 0:
                rows.append({"条件": name, "n": 0, "fwd5%": None, "down%": None}); continue
            rows.append({"条件": name, "n": n,
                         "fwd5%": round(al["fwd"][m].mean() * 100, 2),
                         "down%": round((al["fwd"][m] < 0).mean() * 100, 1)})
    t = pd.DataFrame(rows)
    print(t.to_string(index=False))
    print(f"\n基线: fwd5={base.mean()*100:+.2f}% down={(base<0).mean()*100:.1f}%")
    print("判读：n≥100 且 fwd5 明显<基线、down% 明显>基线 → 该档有效；n 太小(样本不足)不可用。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=500)
    run(ap.parse_args().days)
