# -*- coding: utf-8 -*-
"""E0 Stage5：功效量化 —— 「补样本」到底能把 t 值提到多少（2026-09-22）。

## 起因
Stage4 的 Q0 结论：IS vs OOS 的差 t=0.08、最小可辨效应 0.53pp，而效应本身只有
0.023pp ⇒ **「OOS 崩塌」是误读，真实约束是统计功效**。连项目头号发现
「日内漂移 +0.128%」在 39 票 × 243 日上也只有 t=1.17（日聚类）。

## 本脚本要回答
仓库 `t_io/cache/tushare_mins/` 里已缓存 **1000+ 只 × 361 日 30min K线**
（起始 2025-03-31）。若用它测同一个「开→收」漂移，功效能提到多少？

## 口径
  开→收 = close(15:00 bar) / open(09:30 bar) − 1     （30min 粒度）
  隔夜   = open(09:30 bar) / 前一日 close(15:00 bar) − 1
日聚类标准误 se = sqrt( Σ_day (Σ_{i∈day} x_i)² ) / N
对比锚：39 票 1min 口径的「09:31→14:55」= +0.1279%、se 0.1095、t 1.17
        （两者定义略有差：14:55 vs 15:00，实测只值 0.02pp）

用法：python t_io/validation/t0_bull/e0_stage5_power_check.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.cost_model import fees  # noqa: E402

SRC = ROOT / 't_io' / 'cache' / 'tushare_mins'
HERE = Path(__file__).resolve().parent
OOS_START = '2026-06-01'


def cse(x: np.ndarray, g: np.ndarray) -> float:
    if len(x) == 0:
        return float('nan')
    s = pd.DataFrame({'x': x, 'g': g}).groupby('g')['x'].sum().values
    return float(np.sqrt(np.sum(s ** 2)) / len(x))


def main() -> None:
    files = sorted(f for f in SRC.iterdir()
                   if re.match(r'^\d{6}\.(SH|SZ)_30min_d540\.json$', f.name))
    print(f'd540 文件数 = {len(files)}')

    rows = []
    for i, fp in enumerate(files):
        code = fp.name.split('.')[0]
        try:
            d = json.loads(fp.read_text(encoding='utf-8'))
        except Exception:
            continue
        bars = d if isinstance(d, list) else (d.get('rows') or d.get('data') or [])
        if not bars:
            continue
        df = pd.DataFrame(bars)
        df['date'] = df['time'].str[:10]
        df['hhmm'] = df['time'].str[11:16]
        first = df[df['hhmm'] == '09:30']
        last = df[df['hhmm'] == '15:00']
        if first.empty or last.empty:
            continue
        m = (first.set_index('date')['open'].rename('op').astype(float)
             .to_frame().join(last.set_index('date')['close'].rename('cl').astype(float),
                              how='inner'))
        if m.empty:
            continue
        m = m.reset_index()
        m['code'] = code
        m['ret'] = m['cl'] / m['op'] - 1
        m['gap'] = m['op'] / m['cl'].shift(1) - 1
        rows.append(m)
        if (i + 1) % 250 == 0:
            print(f'  [{i+1}/{len(files)}] 累计 {sum(len(r) for r in rows)} 股票·日')

    D = pd.concat(rows, ignore_index=True)
    D = D[D['ret'].notna() & np.isfinite(D['ret'])]
    print(f'\n股票·日 = {len(D)}   标的 = {D["code"].nunique()}   '
          f'交易日 = {D["date"].nunique()}   {D["date"].min()} ~ {D["date"].max()}')

    D['net'] = (1 + D['ret']) * 1.0
    fs, fb = fees('stock')
    # 费后：开买→收卖
    D['net_stock'] = ((1 + D['ret']) * (1 - fs) - (1 + fb)) * 100

    print('\n' + '=' * 78)
    print('开→收 漂移：30min 面板（1032 只 × 361 日） vs 1min 面板（39 只 × 243 日）')
    print('=' * 78)
    print(f"{'面板':22s}{'股票·日':>9s}{'标的':>6s}{'日':>5s}{'毛均%':>9s}{'SE':>8s}{'t':>7s}")
    for lab, sub in (('30min 全样本', D),
                     ('30min 33票同窗口', D[(D['date'] >= '2025-09-14') & (D['date'] <= '2026-08-26')])):
        x = sub['ret'].values * 100
        se = cse(x, sub['date'].values)
        print(f'{lab:22s}{len(sub):>9d}{sub["code"].nunique():>6d}{sub["date"].nunique():>5d}'
              f'{x.mean():>+9.4f}{se:>8.4f}{(x.mean()/se if se else float("nan")):>7.2f}')
    print(f'{"1min 39票(锚)":22s}{8848:>9d}{39:>6d}{243:>5d}{0.1279:>+9.4f}{0.1095:>8.4f}{1.17:>7.2f}')

    print('\n费后（股票成本 6.91bp）：')
    x = D['net_stock'].values
    se = cse(x, D['date'].values)
    print(f'  全样本 净均={x.mean():+.4f}%  SE={se:.4f}  t={x.mean()/se if se else float("nan"):+.2f}'
          f'  最小可辨={1.96*se:.4f}pp')
    oos = D[D['date'] >= OOS_START]
    iso = D[D['date'] < OOS_START]
    print(f'  IS(<{OOS_START}) n={len(iso)} 净均={iso["net_stock"].mean():+.4f}% '
          f'SE={cse(iso["net_stock"].values, iso["date"].values):.4f}')
    print(f'  OOS           n={len(oos)} 净均={oos["net_stock"].mean():+.4f}% '
          f'SE={cse(oos["net_stock"].values, oos["date"].values):.4f}')
    da = (oos['net_stock'].mean() - iso['net_stock'].mean())
    sa = np.sqrt(cse(oos['net_stock'].values, oos['date'].values) ** 2
                 + cse(iso['net_stock'].values, iso['date'].values) ** 2)
    print(f'  Δ(OOS−IS) = {da:+.4f}pp  se(Δ)={sa:.4f}  t={da/sa if sa else float("nan"):+.2f}'
          f'  ← 换大面板后 IS/OOS 才第一次具备分辨力')

    print('\n隔夜腿（同一面板）：')
    xg = D['gap'].dropna().values * 100
    gd = D.loc[D['gap'].notna(), 'date'].values
    seg = cse(xg, gd)
    print(f'  隔夜 gap 净均={xg.mean():+.4f}%  SE={seg:.4f}  t={xg.mean()/seg if seg else float("nan"):+.2f}')

    # 逐月
    print('\n按月：')
    D['ym'] = D['date'].str[:7]
    print(f"{'月':9s}{'n':>7s}{'开→收毛%':>10s}{'费后%':>9s}{'隔夜%':>9s}{'SE':>8s}{'t':>7s}")
    for ym, g in D.groupby('ym'):
        x = g['net_stock'].values
        se = cse(x, g['date'].values)
        print(f'{ym:9s}{len(g):>7d}{g["ret"].mean()*100:>+10.4f}{x.mean():>+9.4f}'
              f'{g["gap"].mean()*100:>+9.4f}{se:>8.4f}'
              f'{(x.mean()/se if se else float("nan")):>7.2f}')

    out = HERE / 'results_e0_stage5_power_2026-09-22.json'
    out.write_text(json.dumps({
        'panel': {'stocks': int(D['code'].nunique()), 'days': int(D['date'].nunique()),
                  'stock_days': int(len(D)), 'window': [D['date'].min(), D['date'].max()]},
        'open_to_close_gross_pct': round(float(D['ret'].mean() * 100), 5),
        'open_to_close_net_pct': round(float(D['net_stock'].mean()), 5),
        'cluster_se': round(cse(D['net_stock'].values, D['date'].values), 5),
        'anchor_1min_39': {'mean': 0.1279, 'se': 0.1095, 't': 1.17, 'n': 8848, 'days': 243},
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
