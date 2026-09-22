# -*- coding: utf-8 -*-
"""E0 Stage17：构造**高波篮子**并给出掘金回测的**期望值**（2026-09-22）。

## 选股规则（冻结，事后不得更改）
  Universe  = d540 30min 面板内的**股票**（剔除 3 只 ETF）
  波动率    = 截至 **2026-04-07**（窗口前一交易日）的**过去 20 个交易日**
              日均振幅 mean[(H−L)/prev_close]            ← 因果，绝不用窗口内数据
  流动性闸  = 同窗口的 20 日**日均成交额中位 ≥ 1 亿**        ← 容量可行性（Stage8）
  排序取前 N = 20                                            ← ≥10 只以满足截面中位数（Stage14）

## 产出的两个用途
  §1 篮子清单 + 每只的 vol（给 `backtest_holdings.py` 用）
  §2 **期望值**：在同一窗口上直接算规则在「篮子」与「全样本」上的费后净均 ——
     这是掘金回测要比对的靶子。**若回测结果与 §2 显著不符 ⇒ 实现/前视有问题。**

用法：python e0_stage17_vol_basket.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from e0_stage16_vol_conditioning import load, W0, W1, VOL_WIN, REJ   # noqa: E402
from e0_stage11_oos_test import cse                                  # noqa: E402
from core.cost_model import fees                                     # noqa: E402

N_TOP = 20
MIN_AMT = 1.0e8          # 20 日日均成交额中位下限（元）
ETF_PREFIX = ('51', '56', '58', '15', '16')


def main() -> None:
    D = load()
    D['prev_close'] = D.groupby('code')['cl_1500'].shift(1)
    D = D[np.isfinite(D['prev_close']) & (D['op_auc'] > 0)].copy()
    D['amp'] = (D['hi'] - D['lo']) / D['prev_close']
    D = D.sort_values(['code', 'date'])

    # ── 选股：只用 <= 2026-04-07 的数据 ──
    from e0_stage11_oos_test import load_oos
    A = load_oos()          # ts_oos 有日级 amt（成交额）
    # ⚠️ ts_oos 的 code 是 ts_code（000001.SZ），本文件其余部分用 6 位码 ⇒ 必须归一
    A = A.copy()
    A['code'] = A['code'].astype(str).str.slice(0, 6)
    A = A[(A['date'] <= '2026-04-07') & (~A['code'].str.startswith(ETF_PREFIX))]
    amt_med = (A.sort_values('date').groupby('code')['amt']
               .apply(lambda s: s.tail(20).median()).rename('amt_med'))
    pre = D[(D['date'] <= '2026-04-07') & (~D['code'].str.startswith(ETF_PREFIX))]
    vol = (pre.sort_values('date').groupby('code')['amp']
           .apply(lambda s: s.tail(VOL_WIN).mean()).rename('vol'))
    sel = pd.concat([vol, amt_med], axis=1)
    sel['vol'] = pd.to_numeric(sel['vol'], errors='coerce')

    ok = sel[(sel['vol'].notna()) & (sel['amt_med'] >= MIN_AMT)].sort_values(
        'vol', ascending=False)
    basket = list(ok.index[:N_TOP])
    print(f'候选 {len(sel)} 只 → 过流动性闸 {len(ok)} 只 → 取前 {len(basket)} 只\n')
    print(f"{'#':>4s} {'代码':8s}{'vol% (T-1 20日)':>18s}{'20日成交额中位(亿)':>20s}")
    for i, c in enumerate(basket, 1):
        print(f'{i:>4d} {c:8s}{sel.loc[c,"vol"]*100:>18.3f}{sel.loc[c,"amt_med"]/1e8:>20.2f}')

    # 窗口起点参考价（底仓播种用；取 <= 2026-04-07 的最后一日收盘）
    ref = (D[D['date'] <= '2026-04-07'].sort_values('date')
           .groupby('code')['cl_1500'].last())
    p = HERE / 'vol_basket_2026-04-08.csv'
    with open(p, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['code', 'vol_20d', 'amt_med_20d', 'ref_px'])
        for c in basket:
            w.writerow([c, round(float(sel.loc[c, 'vol']), 6),
                        round(float(sel.loc[c, 'amt_med']), 1),
                        round(float(ref.get(c, 0) or 0), 4)])
    print(f'\n→ 篮子清单 {p}（含 ref_px，供底仓播种）')

    # ── §2 期望值：同一窗口上，规则在篮子 vs 全样本 ──
    D['gap'] = D['op_auc'] / D['prev_close'] - 1
    D['mkt_gap'] = D.groupby('date')['gap'].transform('median')
    D['rel'] = D['gap'] - D['mkt_gap']
    fs, fb = fees('stock')
    D['net'] = ((D['cl_1000'] / D['op_auc']) * (1 - fs) - (1 + fb)) * 100
    Win = D[(D['date'] >= W0) & (D['date'] <= W1)]
    print(f'\n{"组":16s}{"n":>7s}{"日":>5s}{"净均%":>10s}{"SE":>8s}{"t":>7s}{"胜率":>7s}')
    for lab, S in (('全样本', Win),
                   ('高波篮子', Win[Win['code'].isin(basket)])):
        R = S[(S['mkt_gap'] < 0) & (S['rel'] <= REJ)]
        if len(R) < 30:
            print(f'{lab:16s}{len(R):>7d}  —— 过薄'); continue
        x = R['net'].values
        se = cse(x, R['date'].values)
        print(f'{lab:16s}{len(R):>7d}{R["date"].nunique():>5d}{x.mean():>+10.4f}'
              f'{se:>8.4f}{x.mean()/se if se else float("nan"):>7.2f}{(x > 0).mean():>7.3f}')

    out = HERE / 'results_e0_stage17_vol_basket_2026-09-22.json'
    out.write_text(json.dumps({
        'rule': {'vol_win': VOL_WIN, 'asof': '2026-04-07', 'min_amt_20d': MIN_AMT,
                 'n_top': N_TOP, 'universe': 'd540 面板内股票（剔 ETF）', 'rel': REJ},
        'basket': basket,
        'window': [W0, W1],
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
