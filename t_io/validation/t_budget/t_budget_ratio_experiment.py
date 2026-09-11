# -*- coding: utf-8 -*-
"""t_budget_ratio_experiment.py — 每支股票"可T仓位比例"实验（2026-09-11）

目标：给出每票可T仓位 = qty × ρ 的建议比例 ρ，兼顾"做T空间"与"风险上限"。
模型：日内振幅 A%（近250日 median (high-low)/prev_close）→ 最坏日不利波动 ≈ ρ×A。
     ρ* = clamp(R / A, ρ_min, ρ_max) —— 使 T 仓最坏日波动 ≈ R（占仓位比）。
输出：各 R（1.5%/2%/2.5%）下的 ρ 与 T 仓股数示例；供 owner 选 R 或直接采纳 R=2%。
用法：python t_io/validation/t_budget/t_budget_ratio_experiment.py [--days 250]
"""
import argparse
import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CODES = ["588170", "600481", "002451", "000988", "600176", "603667", "300054", "002639", "300153"]


def run(days=250):
    from core.market_data import get_provider
    print(f"{'code':8} {'medianA%':>9} {'ATR14%':>8} | " +
          " ".join(f"R={r:.1%} ρ".ljust(10) for r in (0.015, 0.02, 0.025)))
    ratios = {}
    for code in CODES:
        try:
            df = get_provider().daily(code, days)
        except Exception:
            continue
        if df is None or len(df) < 60:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        h, l, c = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
        pc = c.shift(1)
        amp = ((h - l) / pc).dropna()
        medA = float(np.median(amp.tail(days)))
        tr = np.maximum(h - l, np.maximum((h - pc).abs(), (l - pc).abs()))
        atr14 = float(tr.rolling(14).mean().iloc[-1] / c.iloc[-1])
        row = []
        for R in (0.015, 0.02, 0.025):
            rho = min(0.50, max(0.15, R / medA if medA > 0 else 0.3))
            row.append(rho)
        ratios[code] = {"medianA": medA, "atr14": atr14,
                        "rho_1.5": row[0], "rho_2.0": row[1], "rho_2.5": row[2]}
        print(f"{code:8} {medA*100:9.2f} {atr14*100:8.2f} | " +
              " ".join(f"{rho:.0%}(ρA={rho*medA*100:.2f}%)".ljust(10) for rho in row))
    print("\n判读：ρ×A ≈ R 即该票 T 仓最坏日波动受控于风险预算；振幅大→ρ 小（同风险预算）。")
    print("当前惯例 ρ=30%（reconcile 手设）→ 对高振幅票风险超预算，对低振幅票空间不足。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250)
    run(ap.parse_args().days)
