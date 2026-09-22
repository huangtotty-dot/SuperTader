# -*- coding: utf-8 -*-
"""E0 Stage6：大面板上的「多头条件化日内漂移」——约束#2 的正面检验（2026-09-22）。

## 为什么必须换面板
39 票 × 243 日无法分辨 ~15bp 的效应（Stage4 Q0：最小可辨 0.53pp）。Stage5 证明
`t_io/cache/tushare_mins` 的 981 只 × 361 日 30min 面板把「开→收」漂移推到
**毛 t=2.75 / 费后 t=1.88**。约束#2（只在多头做T）只有在这块面板上才有资格被检验。

## 口径（全部自足，不依赖外部日线，避免价格基准错配）
  日收盘   = 当日 15:00 bar 的 close
  MA20/60  = 该股**日收盘**的滚动均值，取 **T-1** 值（严格无前视）
  开→收    = close(15:00) / open(09:30) − 1
  市场代理 = 面板内等权日收益累乘构造的指数（同上取 T-1 的 MA60）

三档多头（与 Stage2 的 mask 同语义，但此处自足计算）：
  ① 市场多头   市场代理 T-1 收盘 > 其 MA60
  ② 个股多头结构 T-1 收盘 > MA20 且 > MA60
  ③ 强多头近高  ② 且 T-1 收盘落在过去 20 日高点的 3% 以内

## 判读纪律
- 同时报「多头档」与「非多头档」，并报 Δ 与 t(Δ)；**禁止只看多头侧的绝对值为正就下结论**。
- 报最小可辨效应，明确哪些差是分辨不了的。

用法：python t_io/validation/t0_bull/e0_stage6_regime_drift.py
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


def load_panel() -> pd.DataFrame:
    files = sorted(f for f in SRC.iterdir()
                   if re.match(r'^\d{6}\.(SH|SZ)_30min_d540\.json$', f.name))
    rows = []
    for fp in files:
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
        o = df[df['hhmm'] == '09:30']
        c = df[df['hhmm'] == '15:00']
        if o.empty or c.empty:
            continue
        h = df.groupby('date')['high'].max()
        l = df.groupby('date')['low'].min()
        m = (o.set_index('date')['open'].astype(float).rename('op').to_frame()
             .join(c.set_index('date')['close'].astype(float).rename('cl'))
             .join(h.rename('hi')).join(l.rename('lo'), how='inner'))
        if m.empty:
            continue
        m = m.reset_index()
        m['code'] = code
        rows.append(m)
    D = pd.concat(rows, ignore_index=True)
    D['ret'] = D['cl'] / D['op'] - 1
    D['gap'] = D['op'] / D.groupby('code')['cl'].shift(1) - 1
    D['amp'] = (D['hi'] - D['lo']) / D['op']
    return D


def main() -> None:
    D = load_panel()
    D = D[np.isfinite(D['ret']) & (D['op'] > 0)].copy()
    D = D.sort_values(['code', 'date']).reset_index(drop=True)
    print(f'面板: 股票·日={len(D)}  标的={D["code"].nunique()}  日={D["date"].nunique()}  '
          f'{D["date"].min()} ~ {D["date"].max()}')

    # ── 因果的多头状态（全部取 T-1）──
    g = D.groupby('code', sort=False)['cl']
    D['ma20'] = g.transform(lambda s: s.rolling(20).mean()).groupby(D['code']).shift(1)
    D['ma60'] = g.transform(lambda s: s.rolling(60).mean()).groupby(D['code']).shift(1)
    prev = g.shift(1)
    D['prev_cl'] = prev
    D['hi20'] = g.transform(lambda s: s.rolling(20).max()).groupby(D['code']).shift(1)
    D['t2_multihead'] = (prev > D['ma20']) & (prev > D['ma60'])
    D['t_above60'] = prev > D['ma60']
    D['t3_near_high'] = D['t2_multihead'] & (prev >= D['hi20'] * 0.97)

    # 市场代理：面板等权日收益累乘（T-1 取 MA60）
    mkt = D.groupby('date')['ret'].mean().sort_index()
    mkt_close = (1 + mkt).cumprod()
    mkt_ma60 = mkt_close.rolling(60).mean().shift(1)
    up = (mkt_close.shift(1) > mkt_ma60).rename('t1_market')
    D = D.merge(up, left_on='date', right_index=True, how='left')

    fs, fb = fees('stock')
    D['net'] = ((1 + D['ret']) * (1 - fs) - (1 + fb)) * 100

    print('\n' + '=' * 78)
    print('约束#2 正面检验：多头档 vs 非多头档 的「开→收」费后净收益（股票成本 6.91bp）')
    print('=' * 78)
    print(f"{'档':20s}{'侧':8s}{'n':>8s}{'净均%':>9s}{'SE':>8s}{'t':>7s}"
          f"{'Δpp vs非多头':>13s}{'t(Δ)':>7s}{'最小可辨':>9s}")
    for col, name in (('t1_market', '① 市场多头(代理)'), ('t2_multihead', '② 个股多头结构'),
                      ('t3_near_high', '③ 强多头近高')):
        yes = D[D[col] == True]                                   # noqa: E712
        no = D[D[col] == False]                                   # noqa: E712
        if yes.empty or no.empty:
            continue
        xy, xn = yes['net'].values, no['net'].values
        sy, sn = cse(xy, yes['date'].values), cse(xn, no['date'].values)
        d = xy.mean() - xn.mean()
        sd = float(np.sqrt(sy ** 2 + sn ** 2))
        for lab, x, se in (('多头', xy, sy), ('非多头', xn, sn)):
            extra = (f'{d:>+13.4f}{d/sd if sd else float("nan"):>7.2f}{1.96*sd:>9.4f}'
                     if lab == '多头' else '')
            print(f'{name:20s}{lab:8s}{len(x):>8d}{x.mean():>+9.4f}{se:>8.4f}'
                  f'{x.mean()/se if se else float("nan"):>7.2f}{extra}')

    print('\n' + '=' * 78)
    print('IS/OOS × 个股多头结构（大面板第一次有分辨力）')
    print('=' * 78)
    print(f"{'期':6s}{'档':10s}{'n':>8s}{'净均%':>9s}{'SE':>8s}{'t':>7s}")
    for lab, sel in (('IS', D['date'] < OOS_START), ('OOS', D['date'] >= OOS_START)):
        for tlab, tsel in (('②多头', D['t2_multihead'] == True),               # noqa: E712
                           ('非多头', D['t2_multihead'] == False)):             # noqa: E712
            s = D[sel & tsel]
            if len(s) < 30:
                continue
            x = s['net'].values
            se = cse(x, s['date'].values)
            print(f'{lab:6s}{tlab:10s}{len(s):>8d}{x.mean():>+9.4f}{se:>8.4f}'
                  f'{x.mean()/se if se else float("nan"):>7.2f}')

    print('\n' + '=' * 78)
    print('振幅条件：费后净收益 × 当日振幅分位（⚠️ 当日振幅含前视，此处仅作「机会大小」描述）')
    print('=' * 78)
    dd = D[D['amp'].notna() & (D['amp'] > 0)].copy()
    dd['q'] = pd.qcut(dd['amp'], 5, labels=['Q1低', 'Q2', 'Q3', 'Q4', 'Q5高'])
    print(f"{'振幅分位':10s}{'n':>8s}{'振幅%':>8s}{'净均%':>9s}{'SE':>8s}{'t':>7s}"
          f"{'毛均%':>9s}{'保本线%':>9s}")
    for q, s in dd.groupby('q', observed=True):
        x = s['net'].values
        se = cse(x, s['date'].values)
        amp = s['amp'].mean() * 100
        breakeven = (fs + fb) * 100 / (amp / 100) / 100          # 往返成本/振幅
        print(f'{str(q):10s}{len(s):>8d}{amp:>8.3f}{x.mean():>+9.4f}{se:>8.4f}'
              f'{x.mean()/se if se else float("nan"):>7.2f}'
              f'{s["ret"].mean()*100:>+9.4f}{breakeven*100:>9.3f}')

    out = HERE / 'results_e0_stage6_regime_drift_2026-09-22.json'
    rec = {}
    for col, name in (('t1_market', 't1'), ('t2_multihead', 't2'), ('t3_near_high', 't3')):
        for side, sel in (('yes', D[col] == True), ('no', D[col] == False)):      # noqa: E712
            s = D[sel]
            if s.empty:
                continue
            x = s['net'].values
            rec[f'{name}_{side}'] = {'n': int(len(s)), 'net': round(float(x.mean()), 4),
                                     'se': round(cse(x, s['date'].values), 4)}
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
