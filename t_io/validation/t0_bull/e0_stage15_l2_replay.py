# -*- coding: utf-8 -*-
"""E0 Stage15：L2 离线回放 —— `core/open_gap_reversal` 与离线结论逐腿对拍（2026-09-22）。

规格 §9 的 L2 层：用**既有面板**重跑模块的决策路径，比对逐腿一致性。

## 为什么有两种口径
离线检验（Stage11/13）的 `apply_rule` 是**先剔掉 `cl_1000` 缺失的行、再取中位数** ——
因为检验要算收益，没出场价的行本来就没用。但**生产在 09:30 时并不知道 10:00 价**，
中位数只能由 (prev_close, open) 决定。二者在「10:00 bar 缺失」的少数行上会有差别。

故本脚本跑两遍：
  **口径 A（复刻）**：先剔 `cl_1000` 缺失再取中位 ⇒ 必须与 Stage11/13 **逐腿 diff=0**，
                      用来证明模块的**判定逻辑**与离线完全一致。
  **口径 B（生产忠实）**：不剔 ⇒ 量化那个**有意为之**的差异有多大。
                       （若 B 与 A 差得大，说明该差异需要在实现里显式处理。）

用法：python e0_stage15_l2_replay.py
"""
from __future__ import annotations

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

from e0_stage11_oos_test import load_oos, apply_rule, cse      # noqa: E402
from core import open_gap_reversal as ogr                      # noqa: E402
from core.cost_model import fees                               # noqa: E402

FS, FB = fees('stock')


def legs_via_module(P: pd.DataFrame, mode: str) -> pd.DataFrame:
    """用 `ogr.evaluate` 逐日重跑，返回与 apply_rule 同构的腿表。"""
    out = []
    for date, day in P.groupby('date', sort=True):
        if mode == 'A':
            d = day[day['cl_1000'] > 0]
        else:
            d = day
        pc = dict(zip(d['code'], d['prev_close']))
        op = dict(zip(d['code'], d['op_auc']))
        r = ogr.evaluate(pc, op)
        if not r['tradable']:
            continue
        pick = day[day['code'].isin(r['tradable'])]
        for _, x in pick.iterrows():
            out.append({'code': x['code'], 'date': date,
                        'net': (x['cl_1000'] / x['op_auc']) * (1 - FS) - (1 + FB)})
    L = pd.DataFrame(out)
    if not L.empty:
        L['net'] = L['net'] * 100
    return L


def summarize(tag: str, L: pd.DataFrame) -> dict:
    if L.empty:
        print(f'  {tag:34s} 空')
        return {'tag': tag, 'n': 0}
    x, g = L['net'].values, L['date'].values
    se = cse(x, g)
    print(f'  {tag:34s} n={len(L):6d} 日={L["date"].nunique():4d} '
          f'净均={x.mean():+.4f}%  t={(x.mean()/se if se else float("nan")):+.2f}')
    return {'tag': tag, 'n': int(len(L)), 'days': int(L['date'].nunique()),
            'net': round(float(x.mean()), 4),
            't': round(float(x.mean() / se), 2) if se else None}


def compare(tag: str, ref: pd.DataFrame, got: pd.DataFrame) -> dict:
    a = set(zip(ref['code'], ref['date']))
    b = set(zip(got['code'], got['date']))
    print(f'  {tag:34s} 离线 {len(a):6d}  模块 {len(b):6d}  '
          f'交集 {len(a & b):6d}  仅在离线 {len(a - b):5d}  仅在模块 {len(b - a):5d}')
    return {'ref': len(a), 'got': len(b), 'inter': len(a & b),
            'only_ref': len(a - b), 'only_got': len(b - a)}


def main() -> None:
    print('=' * 92)
    print('§1 主面板样本外（973 只 / 2019-01~2025-03）')
    print('=' * 92)
    P = load_oos()
    if 'prev_close' not in P.columns:
        P['prev_close'] = P.groupby('code')['cl_1500'].shift(1)
    P['gap'] = P['op_auc'] / P['prev_close'] - 1
    P = P[np.isfinite(P['gap']) & (P['op_auc'] > 0) & (P['cl_1000'] > 0)].copy()
    print(f'  面板 {len(P)} 股票·日  {P["date"].nunique()} 日')

    ref = apply_rule(P)
    sRef = summarize('离线 apply_rule（基准）', ref)
    A = legs_via_module(P, 'A')
    sA = summarize('模块 · 口径A（复刻）', A)
    cA = compare('A vs 离线', ref, A)
    B = legs_via_module(P, 'B')
    sB = summarize('模块 · 口径B（生产忠实）', B)
    cB = compare('B vs 离线', ref, B)
    print(f'  ⇒ 口径A {"✅ 逐腿完全一致" if cA["only_ref"] == cA["only_got"] == 0 else "⚠️ 有差异"}'
          f'；口径B 与离线差 {cB["only_got"] + cB["only_ref"]} 腿'
          f'（净均 {sB.get("net")} vs {sA.get("net")}）')

    print('\n' + '=' * 92)
    print('§2 独立 400 只池（Stage13 的同一份数据与样本）')
    print('=' * 92)
    try:
        from e0_stage13_indep_universe import load_indep
        I = load_indep()
        if I.empty:
            print('  ts_indep 为空，跳过')
        else:
            if 'prev_close' not in I.columns:
                I['prev_close'] = I.groupby('code')['cl_1500'].shift(1)
            I['gap'] = I['op_auc'] / I['prev_close'] - 1
            I = I[np.isfinite(I['gap']) & (I['op_auc'] > 0) & (I['cl_1000'] > 0)].copy()
            rI = apply_rule(I)
            summarize('离线 apply_rule（基准）', rI)
            AI = legs_via_module(I, 'A')
            summarize('模块 · 口径A（复刻）', AI)
            cI = compare('A vs 离线', rI, AI)
            BI = legs_via_module(I, 'B')
            summarize('模块 · 口径B（生产忠实）', BI)
            compare('B vs 离线', rI, BI)
            print(f'  ⇒ 口径A {"✅ 逐腿完全一致" if cI["only_ref"] == cI["only_got"] == 0 else "⚠️ 有差异"}')
    except Exception as e:
        print(f'  跳过（{type(e).__name__}: {str(e)[:80]}）')

    out = HERE / 'results_e0_stage15_l2_2026-09-22.json'
    out.write_text(json.dumps({
        'panel_973': {'ref': sRef, 'A': sA, 'cmpA': cA, 'B': sB, 'cmpB': cB},
        'verdict_L2': ('PASS: 模块决策路径与离线结论逐腿一致'
                       if cA['only_ref'] == cA['only_got'] == 0 else 'DIFF'),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
