# -*- coding: utf-8 -*-
"""E0 Stage14：市场代理需要多宽？—— 决定这条规则能否落地（2026-09-22）。

## 为什么必须先做这一步（先于写设计规格）
§4.5 起所有检验的 `mkt_gap` 都取**全样本约千只的截面中位数**。
但生产环境（掘金自动盘）只订阅**自己的池子**（几十只），**算不出全市场中位数**；
且本仓库无 `index_daily` 权限（`stock_basic` 同样无权限）。
⇒ 若市场代理必须很宽，这条规则就**不可实现**；若几十只够，就能落地。

## 设计
对样本外面板（973 只 / 2019-01~2025-03），把 `mkt_gap` 换成**随机 k 只子集的 gap 中位数**，
k ∈ {10, 20, 50, 100, 300}，每个 k 跑 20 个随机种子（子集抽取有无放回、去重），
报「规则净均」的均值/范围，与全样本基准（+0.5245%）对照。
**判据：某个 k 若其 20 个种子的净均下沿仍 ≥ +0.20%/腿 且中位 t ≥ 2 ⇒ 该宽度可落地。**

同时报「池子自身 gap 中位数」与「全样本 gap 中位数」的**截面相关**，
说明代理的保真度（相关系数低则代理不可靠，与效应存亡是两回事）。

用法：python e0_stage14_mktproxy.py
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

from e0_stage11_oos_test import load_oos, cse      # noqa: E402
from core.cost_model import fees                   # noqa: E402

KS = (10, 20, 50, 100, 300)
N_SEED = 20
RULE_REL, PASS_NET, PASS_T = -0.01, 0.20, 2.0
REF = 0.5245                                        # 全样本代理的样本外基准


def main() -> None:
    P = load_oos()
    if 'prev_close' not in P.columns:
        P['prev_close'] = P.groupby('code')['cl_1500'].shift(1)
    P['gap'] = P['op_auc'] / P['prev_close'] - 1
    P = P[np.isfinite(P['gap']) & (P['op_auc'] > 0) & (P['cl_1000'] > 0)].copy()
    codes = sorted(P['code'].unique())
    print(f'样本外面板: {len(P)} 股票·日  {len(codes)} 只  '
          f'{P["date"].min()} ~ {P["date"].max()}')

    fs, fb = fees('stock')
    P['ret'] = (P['cl_1000'] / P['op_auc']) * (1 - fs) - (1 + fb)
    full = P.groupby('date')['gap'].median().rename('mg')
    P = P.merge(full, left_on='date', right_index=True, how='left')

    def run(mg_series: pd.Series, tag: str) -> dict:
        Q = P.drop(columns=['mg']).merge(mg_series.rename('mg'),
                                         left_on='date', right_index=True, how='left')
        Q = Q[np.isfinite(Q['mg'])]
        R = Q[(Q['mg'] < 0) & ((Q['gap'] - Q['mg']) <= RULE_REL)]
        if len(R) < 100:
            return {'tag': tag, 'n': len(R)}
        x, g = R['ret'].values * 100, R['date'].values
        se = cse(x, g)
        return {'tag': tag, 'n': int(len(R)), 'days': int(R['date'].nunique()),
                'net': float(x.mean()), 'se': se, 't': float(x.mean() / se)}

    base = run(full, '全样本中位(基准)')
    print(f"\n基准（全样本 {len(codes)} 只的 gap 中位）：n={base['n']}  "
          f"净均={base['net']:+.4f}%  t={base['t']:+.2f}   （§4.7 记 +{REF}）")

    print('\n' + '=' * 88)
    print('市场代理宽度敏感性：随机 k 只子集的 gap 中位（每个 k 跑 20 个种子）')
    print('=' * 88)
    print(f"{'k':>6s}{'净均 均值':>12s}{'净均 p5':>11s}{'净均 p95':>11s}"
          f"{'t 均值':>9s}{'t 最小':>9s}{'腿数 中位':>11s}{'可落地':>9s}")
    rows = []
    for k in KS:
        nets, ts, ns = [], [], []
        for s in range(N_SEED):
            rng = np.random.default_rng(1000 + s)
            sub = rng.choice(codes, size=min(k, len(codes)), replace=False)
            mg = P[P['code'].isin(sub)].groupby('date')['gap'].median().rename('mg')
            r = run(mg, f'k={k}')
            if 'net' in r:
                nets.append(r['net']); ts.append(r['t']); ns.append(r['n'])
        if not nets:
            continue
        nets, ts = np.array(nets), np.array(ts)
        ok = (np.percentile(nets, 5) >= PASS_NET) and (np.median(ts) >= PASS_T)
        print(f'{k:>6d}{nets.mean():>+12.4f}{np.percentile(nets,5):>+11.4f}'
              f'{np.percentile(nets,95):>+11.4f}{ts.mean():>9.2f}{ts.min():>9.2f}'
              f'{int(np.median(ns)):>11d}{"✅" if ok else "⚠️":>9s}')
        rows.append({'k': k, 'net_mean': float(nets.mean()),
                     'net_p5': float(np.percentile(nets, 5)),
                     'net_p95': float(np.percentile(nets, 95)),
                     't_mean': float(ts.mean()), 't_min': float(ts.min()),
                     'n_median': int(np.median(ns)), 'feasible': bool(ok)})

    print('\n' + '=' * 88)
    print('代理保真度：子集 gap 中位 与 全样本 gap 中位 的日度相关')
    print('=' * 88)
    for k in (10, 20, 50, 100, 300):
        cs = []
        for s in range(5):
            rng = np.random.default_rng(1000 + s)
            sub = rng.choice(codes, size=min(k, len(codes)), replace=False)
            mg = P[P['code'].isin(sub)].groupby('date')['gap'].median()
            j = pd.concat([full, mg.rename('sub')], axis=1).dropna()
            cs.append(float(np.corrcoef(j['mg'], j['sub'])[0, 1]))
        print(f'  k={k:>4d}  相关 = {np.mean(cs):+.3f}  (5 种子的范围 '
              f'{min(cs):+.3f}~{max(cs):+.3f})')
    mgn = full[full < 0].mean()
    print(f'\n  （全样本 mkt_gap 为负的日占比 = {(full < 0).mean():.1%}，'
          f'负日均值 = {mgn:.4f}）')

    p = HERE / 'results_e0_stage14_mktproxy_2026-09-22.json'
    p.write_text(json.dumps({'baseline': base, 'by_k': rows,
                             'ref_full_proxy': REF}, ensure_ascii=False, indent=2),
                 encoding='utf-8')
    print(f'\n→ {p}')


if __name__ == '__main__':
    main()
