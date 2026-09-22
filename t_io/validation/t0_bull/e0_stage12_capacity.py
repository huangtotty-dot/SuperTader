# -*- coding: utf-8 -*-
"""E0 Stage12：容量估计 —— 这条规则能吃多少资金（2026-09-22）。

## 为什么这一步是「能不能真赚钱」的最后一关
规则已过样本外（+0.5245%/腿、t=8.07），但 +0.5%/腿 只有在**能按那个价成交**时才是钱。
09:30 的买价来自**集合竞价**，而竞价只有全天成交量的 ~0.8%（[[tushare-mins-panel]] 实测）
⇒ **竞价成交量就是入场侧的硬约束**，不是 ADV。

## 口径
对每条命中腿取三个量（来自 d540 完整 9 根 bar）：
  `auc_amt`  09:30 bar 成交额 = **集合竞价成交额**（入场侧的真正约束）
  `f30_amt`  10:00 bar 成交额 = 09:30–10:00 连续竞价成交额（若接受在头几分钟买入）
  `adv_amt`  当日全天成交额（行业惯用的容量尺子，此处作对照）
容量(单腿) = 参与率 p × 对应成交额。**先用 Σamount/Σvolume 反解 VWAP 做单位自检** ——
不确认单位，容量数字无意义。

## 对照
样本外 61,403 腿只有日级 `amt`（Stage10 归约未存竞价量）⇒ 用**全日成交额分布**与样本内
做跨期对照，说明两个时期的流动性量级是否可比。

## 不做的事
不假设冲击成本模型（无逐笔数据），只用「占成交量比例」这一保守代理；
**结论只给「占量 p% 时的单腿上限与可覆盖腿数」，不给精确冲击成本。**

用法：python e0_stage12_capacity.py
"""
from __future__ import annotations

import json
import re
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

from e0_stage11_oos_test import load_oos, apply_rule        # noqa: E402

SRC = ROOT / 't_io' / 'cache' / 'tushare_mins'
PARTS = (0.01, 0.05, 0.10, 0.20)          # 参与率（占对应成交额比例）
SIZES = (10, 50, 100, 300, 1000)          # 目标单笔规模（万元）
RULE_REL = -0.01


def build_legs_with_liquidity() -> tuple[pd.DataFrame, dict]:
    """样本内命中腿 + 三个流动性量。返回 (legs, 单位自检信息)。"""
    legs, ratios = [], []
    for f in sorted(SRC.iterdir()):
        if not re.match(r'^\d{6}\.(SH|SZ)_30min_d540\.json$', f.name):
            continue
        code = f.name.split('.')[0]
        d = json.loads(f.read_text(encoding='utf-8'))
        bars = d if isinstance(d, list) else (d.get('rows') or d.get('data') or [])
        if not bars:
            continue
        df = pd.DataFrame(bars)
        df['date'] = df['time'].str[:10]
        df['hhmm'] = df['time'].str[11:16]
        for c in ('open', 'close', 'volume', 'amount'):
            df[c] = pd.to_numeric(df[c], errors='coerce')

        auc = df[df['hhmm'] == '09:30']
        f30 = df[df['hhmm'] == '10:00']
        day = df.groupby('date').agg(adv_amt=('amount', 'sum'),
                                     adv_vol=('volume', 'sum'),
                                     cl_1500=('close', 'last'))
        # 单位自检：全日 amount/volume 应 ≈ 价格
        chk = day[day['adv_vol'] > 0]
        if len(chk):
            ratios.append(float((chk['adv_amt'] / chk['adv_vol']).median()))
        t = pd.DataFrame({
            'op_auc': auc.set_index('date')['open'].astype(float),
            'auc_amt': auc.set_index('date')['amount'].astype(float),
            'f30_amt': f30.set_index('date')['amount'].astype(float),
        }).join(day, how='inner').dropna(subset=['op_auc', 'adv_amt'])
        if t.empty:
            continue
        t = t.reset_index().rename(columns={'index': 'date'})
        t['code'] = code
        legs.append(t)

    L = pd.concat(legs, ignore_index=True).sort_values(['code', 'date']).reset_index(drop=True)
    L['prev_close'] = L.groupby('code')['cl_1500'].shift(1)
    L = L[np.isfinite(L['prev_close'])]
    return L, {'implied_vwap_median': float(np.median(ratios)) if ratios else float('nan')}


def main() -> None:
    L, chk = build_legs_with_liquidity()
    print(f'样本内 可用 (code,date) = {len(L)}')

    # ── 单位自检 ──
    med_price = float(L['op_auc'].median())
    print('\n' + '=' * 84)
    print('单位自检')
    print('=' * 84)
    print(f'  Σamount/Σvolume 的票级中位 = {chk["implied_vwap_median"]:.4f}')
    print(f'  同期 open 中位价          = {med_price:.4f}')
    r = chk['implied_vwap_median'] / med_price
    print(f'  比值 = {r:.4f}  →  ' + (
        '✅ amount 为元、volume 为股，两者可直接用'
        if 0.5 < r < 2 else
        f'⚠️ 比值异常，amount 单位可能不是元（ratio={r:.3g}）—— 下述金额按此比例换算后再读'))

    # ── 命中腿 + 流动性 ──
    L['gap'] = L['op_auc'] / L['prev_close'] - 1
    L = L[np.isfinite(L['gap']) & (L['op_auc'] > 0)]
    L['mkt_gap'] = L.groupby('date')['gap'].transform('median')
    L['rel'] = L['gap'] - L['mkt_gap']
    R = L[(L['mkt_gap'] < 0) & (L['rel'] <= RULE_REL)].copy()
    print(f'\n命中腿 = {len(R)}   票 = {R["code"].nunique()}   日 = {R["date"].nunique()}')

    print('\n' + '=' * 84)
    print('命中腿的三个成交额分位（万元 —— 若单位自检通过，amount 单位=元，故 /1e4）')
    print('=' * 84)
    unit = 1e4 if 0.5 < r < 2 else 1.0
    print(f"{'量':10s}{'p10':>12s}{'p25':>12s}{'中位':>12s}{'p75':>12s}{'p90':>12s}")
    for col, name in (('auc_amt', '竞价成交额'), ('f30_amt', '首半小时额'), ('adv_amt', '全日成交额')):
        v = R[col].dropna() / unit
        print(f'{name:10s}{v.quantile(.1):>12,.0f}{v.quantile(.25):>12,.0f}'
              f'{v.median():>12,.0f}{v.quantile(.75):>12,.0f}{v.quantile(.9):>12,.0f}')

    print('\n' + '=' * 84)
    print('单腿容量：给定参与率 p，能下单的金额上限（按竞价量 vs 按全日量）')
    print('=' * 84)
    print(f"{'参与率':8s}{'按竞价 p10/中位(万元)':>26s}{'按全日 p10/中位(万元)':>26s}")
    for p in PARTS:
        a = R['auc_amt'].dropna() / unit * p
        b = R['adv_amt'].dropna() / unit * p
        print(f'{p:>6.0%}  {a.quantile(.1):>12,.0f}{a.median():>14,.0f}'
              f'{b.quantile(.1):>14,.0f}{b.median():>12,.0f}')

    print('\n' + '=' * 84)
    print('可覆盖腿数：目标单笔规模 S 下，有多少命中腿容得下（按竞价量 / 按全日量）')
    print('=' * 84)
    print(f"{'单笔S(万元)':12s}{'p=5%竞价':>12s}{'p=10%竞价':>12s}{'p=20%竞价':>12s}"
          f"{'p=5%全日':>12s}{'p=10%全日':>12s}")
    for s in SIZES:
        row = []
        for p, col in ((.05, 'auc_amt'), (.10, 'auc_amt'), (.20, 'auc_amt'),
                       (.05, 'adv_amt'), (.10, 'adv_amt')):
            v = R[col].dropna() / unit * p
            row.append((v >= s).mean())
        print(f'{s:>12d}' + ''.join(f'{x:>11.1%} ' for x in row))

    print('\n' + '=' * 84)
    print('每日总容量：某日所有命中腿在参与率 p 下可吃下的总额（万元/日）')
    print('=' * 84)
    for p in PARTS:
        d1 = (R.groupby('date')['auc_amt'].sum() * p / unit)
        d2 = (R.groupby('date')['adv_amt'].sum() * p / unit)
        print(f'  p={p:>4.0%}  竞价口径: 中位 {d1.median():>12,.0f}  均值 {d1.mean():>12,.0f}  '
              f'|  全日口径: 中位 {d2.median():>12,.0f}  均值 {d2.mean():>12,.0f}')
    print('  ↑ 竞价口径 = 只有集合竞价那一瞬间的量；若接受在 09:30–09:35 连续竞价买入，'
          '可用首半小时口径作上界')

    # ── 跨期对照：样本外只有全日量 ──
    print('\n' + '=' * 84)
    print('跨期对照：全日成交额分布（样本内 2025-2026 vs 样本外 2019-2025）')
    print('=' * 84)
    oos = load_oos()
    if 'prev_close' not in oos.columns:
        oos['prev_close'] = oos.groupby('code')['cl_1500'].shift(1)
    Ro = apply_rule(oos)          # Ro 继承 oos 的 amt（日级总成交额）
    for lab, v in (('样本内 2025-03~2026-09', R['adv_amt'].dropna() / unit),
                   ('样本外 2019-01~2025-03', Ro['amt'].dropna() / unit)):
        print(f'  {lab:24s} n={len(v):6d}  p10={v.quantile(.1):>10,.0f}  '
              f'中位={v.median():>10,.0f}  p90={v.quantile(.9):>10,.0f}  (万元)')
    print('  ⚠️ 样本外无竞价量（Stage10 归约未存），故竞价口径的跨期对照缺；'
          '若需要可重拉（约 3 次调用/票）')

    # ── 用「竞价占全日比」的外推补上样本外的竞价量 ──
    print('\n' + '=' * 84)
    print('样本外可行性的外推（用样本内的「竞价/全日」比分布 × 样本外全日额）')
    print('=' * 84)
    sh = (R['auc_amt'] / R['adv_amt']).replace([np.inf, -np.inf], np.nan).dropna()
    print(f'  样本内「竞价/全日」比: 中位 {sh.median():.4%}  p10 {sh.quantile(.1):.4%}  '
          f'p90 {sh.quantile(.9):.4%}  (n={len(sh)})')
    oo = Ro.copy()
    # 保守：每腿按其全日额 × 竞价占比的经验分位（p25）折算，避免高估
    for lab, q in (('中位', .5), ('p25(保守)', .25)):
        auc_oos = oo['amt'] * sh.quantile(q)
        cov = {s: float((auc_oos * 0.10 >= s * 1e4).mean()) for s in SIZES}
        print(f'  按竞价占比 {lab} 折算 + p=10% 参与率 → 可覆盖腿数占比: ' +
              '  '.join(f'{s}万:{cov[s]:.1%}' for s in SIZES))
    print('  （外推，非实测；若要实测需重拉样本外的 09:30 bar 成交额）')

    out = HERE / 'results_e0_stage12_capacity_2026-09-22.json'
    out.write_text(json.dumps({
        'unit_check': {'implied_vwap_median': chk['implied_vwap_median'],
                       'median_price': med_price, 'ratio': r},
        'rule_hits': int(len(R)),
        'liquidity_quantiles_wan': {
            col: {q: round(float(R[col].dropna().quantile(q) / unit), 1)
                  for q in (.1, .25, .5, .75, .9)}
            for col in ('auc_amt', 'f30_amt', 'adv_amt')},
        'cover_rate': {f'S{s}wan': {f'p{p}_{c}': round(float((R[c].dropna() / unit * p >= s).mean()), 4)
                                    for p in PARTS for c in ('auc_amt', 'adv_amt')}
                       for s in SIZES},
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
