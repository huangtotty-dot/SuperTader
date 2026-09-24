# -*- coding: utf-8 -*-
"""只读重算 Stage17 §2 的**离线期望值**（这个篮子 × 这个窗口），供 GM 回测比对（2026-09-23）。

## 为什么
`e0_stage17_vol_basket.py` 的 §2 就是为「掘金回测要比对的靶子」而写，但它**只 print、不落盘**
（落盘的 json 里只有篮子清单与窗口）。本脚本用**同一套 load/口径**把那个数重算出来，
**不写任何既有产物**（不改 vol_basket_*.csv、不改 results_*.json）。

## ⚠️ 两个必须对齐的口径
1. **窗口**：选股 asof 2026-04-07 ⇒ 期望窗口是 `W0..W1`(=2026-04-08~2026-07-01)，
   GM 回测窗口 03-30 起 ⇒ 比对时必须用 `bt_ogr_fills.py --since 2026-04-08`。
2. **入场价**：离线 `net` 用的是 **集合竞价价 op_auc**；GM 那一轮跑的是 **09:31 开盘价**
   （`--ogr-limit` 把成交钉在 ref×1.0001）。stage9 实测这两者差 **18.688bp**
   （V1 竞价买 +0.76169% vs V4 09:31买 +0.57481%）⇒ 比对 GM 数字时要 **减去 0.187%**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
T0 = HERE                       # 与 stage11/16/17 同目录
sys.path.insert(0, str(T0))
sys.path.insert(0, str(T0.parents[2]))

from e0_stage16_vol_conditioning import load, W0, W1, VOL_WIN, REJ   # noqa: E402
from e0_stage11_oos_test import cse                                  # noqa: E402
from core.cost_model import fees                                     # noqa: E402

STAGE9_V1_MINUS_V4 = 0.76169 - 0.57481      # 竞价买 vs 09:31买（%/腿）
import csv as _csv                          # noqa: E402
with (T0 / 'vol_basket_2026-04-08.csv').open(encoding='utf-8') as _f:
    BASKET = [r['code'].strip() for r in _csv.DictReader(_f)]


def main() -> None:
    D = load()
    D['prev_close'] = D.groupby('code')['cl_1500'].shift(1)
    D = D[np.isfinite(D['prev_close']) & (D['op_auc'] > 0)].copy()
    D['gap'] = D['op_auc'] / D['prev_close'] - 1
    D['mkt_gap'] = D.groupby('date')['gap'].transform('median')
    D['rel'] = D['gap'] - D['mkt_gap']
    fs, fb = fees('stock')
    D['net'] = ((D['cl_1000'] / D['op_auc']) * (1 - fs) - (1 + fb)) * 100

    Win = D[(D['date'] >= W0) & (D['date'] <= W1)]
    print(f'窗口 {W0} ~ {W1}   篮子 {len(BASKET)} 只（{BASKET}）\n')
    print(f'{"组":10s}{"n":>7s}{"日":>5s}{"净均%":>10s}{"SE":>8s}{"t":>7s}{"胜率":>7s}'
          f'{"扣09:31折价后":>16s}')
    for lab, S in (('全样本', Win),
                   ('高波篮子', Win[Win['code'].isin(BASKET)])):
        R = S[(S['mkt_gap'] < 0) & (S['rel'] <= REJ)]
        if len(R) < 30:
            print(f'{lab:10s}{len(R):>7d}  —— 过薄'); continue
        x = R['net'].values
        se = cse(x, R['date'].values)
        print(f'{lab:10s}{len(R):>7d}{R["date"].nunique():>5d}{x.mean():>+10.4f}'
              f'{se:>8.4f}{x.mean() / se if se else float("nan"):>7.2f}'
              f'{(x > 0).mean():>7.3f}{x.mean() - STAGE9_V1_MINUS_V4:>+16.4f}')

    print(f'\n⇒ 与 GM(成交口径, 09:31 限价入场) 比对时应看最后那一列'
          f'（= 离线竞价口径 − {STAGE9_V1_MINUS_V4:.3f}pp）。')


if __name__ == '__main__':
    main()
