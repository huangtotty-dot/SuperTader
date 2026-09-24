# -*- coding: utf-8 -*-
"""E0 Stage18：为 L4 修 `mkt_gap` 代理，把**固定、非篮子、确定性**的代理冻死并验证（2026-09-24）。

## 为什么
Stage14 只验证了「**随机** k 只子集」当代理可行（k=10 即够）。但 L4 实现里传进
`core/open_gap_reversal.evaluate` 的池 = `STOCKS` = **被交易的 20 只高波篮子** ⇒ **自指**，
实测与 981 面板中位**31 天里 9 天符号相反**、腿集交集仅 36/141（见 [[ogr-backtest-closed-loop]]）。
修法需要一张**写死的**代理名单（随机不可复现，不能上生产）。

## 本步做的事
对若干**确定性**代理候选，报两套数：
  §1 面板口径（Stage14 同构：交易全样本、代理=k 名）—— 稳健性，判据 净均≥+0.20% 且 t≥2
  §2 **篮子口径**（交易 20 只高波篮子、代理=该名单）—— 这是**修好后 GM 该对上的靶子**
外加代理保真度（该名单中位 与 全样本中位 的日度相关）。

## 候选（全部排除篮子自身）
  L10 / L20   —— 面板内代码**字典序最小**的 10/20 只（**完全不按业绩选**，最不易被质疑）
  LIQ20       —— 全期成交额中位最大的 20 只（有轻微选择，作对照）

用法：python e0_stage18_fixed_proxy.py
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

from e0_stage11_oos_test import load_oos, cse      # noqa: E402
from core.cost_model import fees                   # noqa: E402

PASS_NET, PASS_T = 0.20, 2.0
REL = -0.01
with (HERE / 'vol_basket_2026-04-08.csv').open(encoding='utf-8') as f:
    BASKET = {r['code'].strip() for r in csv.DictReader(f)}


def window_target(cands, win0='2026-04-08', win1='2026-07-01'):
    """§4：**本轮 GM 回测所在窗口**的篮子靶子（数据源换成 d540 面板，OOS 面板止于 2025-03）。

    这是修好 mkt_gap 代理后 GM 应当对上的数。窗口与选股口径见 Stage17（asof 2026-04-07）。
    """
    from e0_stage16_vol_conditioning import load as load_d540
    D = load_d540()
    D['c6'] = D['code'].astype(str).str.slice(0, 6)
    D['d'] = D['date'].astype(str).str.slice(0, 10)
    D = D.sort_values(['c6', 'd'])
    D['prev_close'] = D.groupby('c6')['cl_1500'].shift(1)
    D = D[np.isfinite(D['prev_close']) & (D['op_auc'] > 0) & (D['cl_1000'] > 0)].copy()
    D['gap'] = D['op_auc'] / D['prev_close'] - 1
    fs, fb = fees('stock')
    D['ret'] = ((D['cl_1000'] / D['op_auc']) * (1 - fs) - (1 + fb)) * 100
    W = D[(D['d'] >= win0) & (D['d'] <= win1)]
    full = W.groupby('d')['gap'].median()
    print('\n' + '=' * 92)
    print(f'§4 本轮窗口 {win0}~{win1} 的**篮子靶子**（交易篮子、代理=该名单）'
          f'  [d540 面板 {W["c6"].nunique()} 只 / {W["d"].nunique()} 日]')
    print(f"{'代理':28s}{'n':>8s}{'日':>6s}{'净均%':>10s}{'SE':>8s}{'t':>7s}")
    for tag, sub in cands.items():
        try:
            mg = (full if sub is None
                  else W[W['c6'].isin(sub)].groupby('d')['gap'].median())
            Q = W.merge(mg.rename('mg'), left_on='d', right_index=True, how='left')
            Q = Q[Q['c6'].isin(BASKET) & np.isfinite(Q['mg'])]
            R = Q[(Q['mg'] < 0) & ((Q['gap'] - Q['mg']) <= REL)]
            if len(R) < 10:
                print(f'{tag:28s}{len(R):>8d}  —— 过薄'); continue
            se = cse(R['ret'].values, R['d'].values)
            print(f'{tag:28s}{len(R):>8d}{R["d"].nunique():>6d}{R["ret"].mean():>+10.4f}'
                  f'{se:>8.4f}{R["ret"].mean() / se:>+7.2f}')
        except Exception as e:
            print(f'{tag:28s}  失败: {e}')


def main() -> None:
    P = load_oos()
    P['c6'] = P['code'].astype(str).str.slice(0, 6)
    if 'prev_close' not in P.columns:
        P['prev_close'] = P.groupby('code')['cl_1500'].shift(1)
    P['gap'] = P['op_auc'] / P['prev_close'] - 1
    P = P[np.isfinite(P['gap']) & (P['op_auc'] > 0) & (P['cl_1000'] > 0)].copy()
    fs, fb = fees('stock')
    P['ret'] = ((P['cl_1000'] / P['op_auc']) * (1 - fs) - (1 + fb)) * 100
    full = P.groupby('date')['gap'].median()
    out_codes = sorted(set(P['c6']) - BASKET)
    print(f'面板 {len(P)} 股票·日 / {P["c6"].nunique()} 只 / {P["date"].min()}~{P["date"].max()}'
          f'   篮子在面板内 {len(set(P["c6"]) & BASKET)} 只，可做代理的非篮子 {len(out_codes)} 只')

    liq20 = (P.groupby('c6')['amt'].median().sort_values(ascending=False)
             .head(40).index.tolist())
    liq20 = [c for c in liq20 if c not in BASKET][:20]
    cands = {
        '全样本中位(基准)': None,
        'L10(字典序最小10只)': out_codes[:10],
        'L20(字典序最小20只)': out_codes[:20],
        'LIQ20(成交额最大20只)': liq20,
        '篮子自身中位(现行 L4 的做法)': sorted(BASKET & set(P['c6'])),
    }

    def run(mg, sub=None):
        Q = P.merge(mg.rename('mg'), left_on='date', right_index=True, how='left')
        Q = Q[np.isfinite(Q['mg'])]
        if sub is not None:
            Q = Q[Q['c6'].isin(sub)]
        R = Q[(Q['mg'] < 0) & ((Q['gap'] - Q['mg']) <= REL)]
        if len(R) < 30:
            return None
        se = cse(R['ret'].values, R['date'].values)
        return {'n': int(len(R)), 'days': int(R['date'].nunique()),
                'net': float(R['ret'].mean()), 'se': se, 't': float(R['ret'].mean() / se)}

    print('\n' + '=' * 92)
    print('§1 面板口径（交易全样本、代理=该名单）')
    print(f"{'代理':28s}{'n':>8s}{'日':>6s}{'净均%':>10s}{'SE':>8s}{'t':>7s}{'判定':>8s}")
    rows1 = {}
    for tag, sub in cands.items():
        mg = (full if sub is None
              else P[P['c6'].isin(sub)].groupby('date')['gap'].median())
        r = run(mg)
        if r is None:
            print(f'{tag:28s}  —— 过薄'); continue
        ok = (r['net'] >= PASS_NET) and (abs(r['t']) >= PASS_T)
        rows1[tag] = r
        print(f'{tag:28s}{r["n"]:>8d}{r["days"]:>6d}{r["net"]:>+10.4f}'
              f'{r["se"]:>8.4f}{r["t"]:>+7.2f}{"✅" if ok else "⚠️":>8s}')

    print('\n' + '=' * 92)
    print('§2 篮子口径（交易 20 只高波篮子、代理=该名单）← 修好后 GM 该对上的靶子')
    print(f"{'代理':28s}{'n':>8s}{'日':>6s}{'净均%':>10s}{'SE':>8s}{'t':>7s}")
    rows2 = {}
    for tag, sub in cands.items():
        mg = (full if sub is None
              else P[P['c6'].isin(sub)].groupby('date')['gap'].median())
        r = run(mg, sub=BASKET)
        if r is None:
            print(f'{tag:28s}  —— 过薄'); continue
        rows2[tag] = r
        print(f'{tag:28s}{r["n"]:>8d}{r["days"]:>6d}{r["net"]:>+10.4f}'
              f'{r["se"]:>8.4f}{r["t"]:>+7.2f}')

    print('\n' + '=' * 92)
    print('§3 代理保真度：该名单中位 与 全样本中位 的日度相关 / 符号一致率')
    for tag, sub in cands.items():
        if sub is None:
            continue
        mg = P[P['c6'].isin(sub)].groupby('date')['gap'].median()
        j = pd.concat([full.rename('full'), mg.rename('sub')], axis=1).dropna()
        if len(j) < 10:
            continue
        corr = float(np.corrcoef(j['full'], j['sub'])[0, 1])
        same = float(((j['full'] < 0) == (j['sub'] < 0)).mean())
        print(f'  {tag:28s} 相关 {corr:+.3f}   符号一致 {same:.1%}')

    window_target(cands)

    (HERE / 'results_e0_stage18_fixed_proxy_2026-09-24.json').write_text(
        json.dumps({'panel': rows1, 'basket': rows2, 'candidates': {
            k: (v if v else 'full') for k, v in cands.items()}},
            ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {HERE / "results_e0_stage18_fixed_proxy_2026-09-24.json"}')


if __name__ == '__main__':
    main()
