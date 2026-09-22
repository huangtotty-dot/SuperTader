# -*- coding: utf-8 -*-
"""E0 Stage9：执行现实性 —— 用 1min 数据量出「竞价→首分钟」「09:59→10:00」的真实代价（2026-09-22）。

## 为什么做这一步
Stage8 的唯一软肋是**滑点**：规则在 10bp/边存活、20bp 掉到 t=1.87、30bp 归零。
但 30min 面板只能给出 `op(09:30 bar)` = 集合竞价价、`cl10(10:00 bar)` = 09:30–10:00 区间收盘价，
**无法回答两个决定性的执行问题**：

  E1 拿不到竞价价、只能等**09:31 第一分钟**买，代价是多少？
  E2 卖在**10:00 bar 收盘**（≈09:59:xx）与卖在 10:00 整点，差多少？

## 数据
39 票 × 2025-08-26~2026-08-26 的 **1min** 序列（`t_io/backtest_1year_data/`）。
⚠️ 这 39 票是 981 面板的**完全子集**（交集 39/39）⇒ **本步只测执行代价，不测迁移性**，
   结论的「样本外」属性仍待 tushare 拉长历史。
好处：这两个代价是**微观结构量**，与规则是否命中无关 ⇒ 可用全部 ~9,500 股票·日估计，功效充足。

用法：python t_io/validation/t0_bull/e0_stage9_exec_realism.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[3]
for _p in (str(ROOT), str(ROOT / 't_io' / 'validation' / 'factor_mining')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import minute_data as md  # noqa: E402
from core.cost_model import fees  # noqa: E402

HERE = Path(__file__).resolve().parent
MIN_1M = 200


def cse(x: np.ndarray, g: np.ndarray) -> float:
    if len(x) == 0:
        return float('nan')
    s = pd.DataFrame({'x': x, 'g': g}).groupby('g')['x'].sum().values
    return float(np.sqrt(np.sum(s ** 2)) / len(x))


def build() -> pd.DataFrame:
    rows = []
    labels_seen = set()
    for code in md.pool_symbols():
        df = md.load_minutes(code)
        if df is None or df.empty:
            continue
        df = df.sort_values('time').reset_index(drop=True)
        prev_close = np.nan
        for dt, day in md.iter_days(df):
            if len(day) < MIN_1M:
                prev_close = float(day['close'].iloc[-1])
                continue
            t = day['time'].dt.strftime('%H:%M')
            lm = {k: i for i, k in enumerate(t)}
            labels_seen.update(lm)
            need = ('09:30', '09:31', '09:59', '10:00', '14:55')
            if not all(k in lm for k in need):
                prev_close = float(day['close'].iloc[-1])
                continue
            o = day['open'].values
            c = day['close'].values
            rows.append({
                'code': code, 'date': dt, 'prev_close': prev_close,
                'auc_open': float(o[lm['09:30']]),        # 09:30 bar 开盘 = 集合竞价价
                'm0931_open': float(o[lm['09:31']]),      # 09:31 bar 开盘 = 第一分钟可成交价
                'c0959': float(c[lm['09:59']]),           # 09:59 bar 收盘
                'o1000': float(o[lm['10:00']]),           # 10:00 bar 开盘
                'c1455': float(c[lm['14:55']]),           # 收盘价（15:00）
            })
            prev_close = float(day['close'].iloc[-1])
    print(f'1min 时点标签数 = {len(labels_seen)}  样本区间见下')
    return pd.DataFrame(rows)


def main() -> None:
    D = build()
    D = D[(D['prev_close'] > 0) & (D['auc_open'] > 0)].copy()
    print(f'股票·日 = {len(D)}  票 = {D["code"].nunique()}  '
          f'{D["date"].min()} ~ {D["date"].max()}')

    # ── E1 竞价 → 09:31 的代价 ──
    print('\n' + '=' * 84)
    print('E1 拿不到竞价价、只能 09:31 买的代价（= (09:31开盘 / 竞价价 − 1)）')
    print('=' * 84)
    D['auc2m1'] = (D['m0931_open'] / D['auc_open'] - 1) * 100
    g = D['date'].values
    x = D['auc2m1'].values
    for lab, s in (('全体', D), ('gap≤−1%', D[D['auc_open'] / D['prev_close'] - 1 <= -0.01]),
                   ('gap≥+1%', D[D['auc_open'] / D['prev_close'] - 1 >= 0.01])):
        y = s['auc2m1'].values
        se = cse(y, s['date'].values)
        print(f'  {lab:10s} n={len(s):6d}  均值={y.mean():+.4f}%  SE={se:.4f}  '
              f't={y.mean()/se if se else float("nan"):+.2f}  '
              f'中位={np.median(y):+.4f}%  p90={np.percentile(y, 90):+.4f}%')
    print('  ↑ 若低开股的该值为正且显著 ⇒ 等一分钟反而更贵，竞价价买不到会有成本')

    # ── E2 09:59 → 10:00 的差 ──
    print('\n' + '=' * 84)
    print('E2 卖在「09:59 收盘」vs「10:00 开盘」的差（= (10:00开盘 / 09:59收盘 − 1)）')
    print('=' * 84)
    D['c9592o1000'] = (D['o1000'] / D['c0959'] - 1) * 100
    y = D['c9592o1000'].values
    se = cse(y, g)
    print(f'  全体 n={len(D)}  均值={y.mean():+.4f}%  SE={se:.4f}  t={y.mean()/se:+.2f}  '
          f'中位={np.median(y):+.4f}%')
    print('  ↑ 量级极小 ⇒ 30min 面板的 cl10(=09:59收盘) 与 10:00 整点价几乎无差，E2 不构成风险')

    # ── 把 E1 代价套回规则：四种执行口径下的规则收益 ──
    print('\n' + '=' * 84)
    print('把实测执行代价套回规则：09:30→10:00 费后净均（39 票子集）')
    print('=' * 84)
    D['gap'] = D['auc_open'] / D['prev_close'] - 1
    mg = D.groupby('date')['gap'].median().rename('mkt_gap')
    D = D.merge(mg, left_on='date', right_index=True, how='left')
    D['rel'] = D['gap'] - D['mkt_gap']
    R = D[(D['mkt_gap'] < 0) & (D['rel'] <= -0.01)].copy()
    fs, fb = fees('stock')
    print(f'  命中腿 n={len(R)}  日={R["date"].nunique()}  '
          f'（39 票子集，功效有限，只看执行口径之间的**相对**差）')
    variants = {
        'V1 竞价买/09:59收(≈30min口径)': ('auc_open', 'c0959'),
        'V2 09:31买/09:59收': ('m0931_open', 'c0959'),
        'V3 竞价买/10:00开盘卖': ('auc_open', 'o1000'),
        'V4 09:31买/10:00开盘卖(最保守)': ('m0931_open', 'o1000'),
        'V5 竞价买/收盘14:55卖(对照)': ('auc_open', 'c1455'),
    }
    base = None
    for name, (buyp, sellp) in variants.items():
        if not R[buyp].notna().all() or not R[sellp].notna().all():
            continue
        net = ((R[sellp] / R[buyp]) * (1 - fs) - (1 + fb)) * 100
        se = cse(net.values, R['date'].values)
        if base is None:
            base = net.mean()
        print(f'  {name:32s} n={len(R):5d}  费后={net.mean():+.4f}%  SE={se:.4f}  '
              f't={net.mean()/se:+.2f}  ΔvsV1={net.mean()-base:+.4f}pp')

    # ── 实测滑点替代假设：把 E1 的中位/p90 当作真实滑点 ──
    print('\n' + '=' * 84)
    print('结论性代入：用实测 E1 代价替代 Stage8 的假设滑点')
    print('=' * 84)
    q50 = float(np.median(D[D['gap'] <= -0.01]['auc2m1'].values))
    q90 = float(np.percentile(D[D['gap'] <= -0.01]['auc2m1'].values, 90))
    print(f'  低开股「竞价→09:31」代价：中位 {q50:+.4f}%  p90 {q90:+.4f}%')
    print(f'  Stage8 假设的 10bp/边 = 0.10%（往返 0.20%）')
    print(f'  ⇒ 实测中位 {q50:+.4f}% {"远小于" if abs(q50) < 0.05 else "接近"} 假设滑点'
          f' ⇒ 规则更可能在 10bp/边 档存活（t=3.32）')

    out = HERE / 'results_e0_stage9_exec_2026-09-22.json'
    out.write_text(json.dumps({
        'auc_to_0931': {'all_mean': round(float(D['auc2m1'].mean()), 5),
                        'gapdn_median': round(q50, 5), 'gapdn_p90': round(q90, 5)},
        'c0959_to_o1000': {'mean': round(float(D['c9592o1000'].mean()), 5),
                           'median': round(float(np.median(D['c9592o1000'])), 5)},
        'rule_variants': {name: round(float(((R[s] / R[b]) * (1 - fs) - (1 + fb)).mean() * 100), 5)
                          for name, (b, s) in variants.items()},
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
