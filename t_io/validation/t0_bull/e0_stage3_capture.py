# -*- coding: utf-8 -*-
"""E0 Stage3：新成本 × 三档多头 mask 下的「日内波动捕获」标定（2026-09-22）。

## 为什么能免重跑
`results_vol_adaptive_exit.json` 的逐腿 `rows` 存了**未扣费的毛价差 `gross`**，
而净收益与成本的关系是精确的（已逐腿验证 max|Δ|=3e-14）：

    net(fs, fb) = gross − 100 × ( fs×(1+gross/100) + fb )

⇒ 换任何成本口径都能**精确重算**，无需重跑 6,868 腿的回放。这是本项目
「成本可事后重算」的第一次显式利用，也是本轮降成本能立刻出结论的原因。

## 复用
- 入场与出场臂定义全部来自 `run_vol_adaptive_exit.py`（不改动，只读取其产物）：
  `CEIL_mfe`=完美出场上界（不可实现）、`CEIL_hold`=持到14:55不做T、
  `RAND`=无技巧基线、`E0_tp05`=现行生产 +0.5% 止盈。
- 多头 mask 来自 `bull_mask.parquet`（Stage2，T-1 日线算出，无前视）。

## 统计口径
均值标准误按**日聚类**（同日 39 只在横截面上相关，按腿独立性算 SE 会严重低估）
    se_cluster = sqrt( Σ_g (Σ_{i∈g} x_i)² ) / N

用法：python t_io/validation/t0_bull/e0_stage3_capture.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cost_model import fees, round_trip  # noqa: E402

HERE = Path(__file__).resolve().parent
SRC = ROOT / 't_io' / 'validation' / 't0_schemes' / 'results_vol_adaptive_exit.json'
MASK = HERE / 'bull_mask.parquet'

CAUSAL = ('E0_tp05', 'VA_tp20', 'VA_tp30', 'VA_tp40', 'VA_tp50', 'VA_tr25', 'VA_tr40')
OOS_START = '2026-06-01'      # 与项目既有 IS/OOS 边界一致
TIERS = (('t1_index_up', '① 指数多头'), ('t2_multihead', '② 个股多头结构'),
         ('t3_near_high', '③ 强多头近高'))


def net_of(gross: np.ndarray, venue: str) -> np.ndarray:
    """由毛价差精确重算净收益（%，venue 取 stock/etf/legacy）。"""
    fs, fb = fees(venue)
    return gross - 100.0 * (fs * (1.0 + gross / 100.0) + fb)


def cluster_se(x: np.ndarray, groups: np.ndarray) -> float:
    """日聚类标准误。"""
    n = len(x)
    if n == 0:
        return float('nan')
    df = pd.DataFrame({'x': x, 'g': groups})
    s = df.groupby('g')['x'].sum().values
    return float(np.sqrt(np.sum(s ** 2)) / n)


def stat(x: np.ndarray, groups: np.ndarray) -> dict:
    n = len(x)
    if n == 0:
        return {'n': 0, 'mean': float('nan'), 'se': float('nan'), 't': float('nan')}
    m = float(np.mean(x))
    se = cluster_se(x, groups)
    return {'n': n, 'mean': m, 'se': se, 't': (m / se if se and se > 0 else float('nan'))}


def main() -> None:
    d = json.loads(SRC.read_text(encoding='utf-8'))
    rows, meta = d['rows'], d['meta']
    print(f"源: {SRC.name}  票={meta['codes']}  股票日={meta['days']}  腿={meta['entries']}"
          f"  旧成本={meta['cost_pct']:.3f}%")

    # ── 长表：每腿 arm/code/date/gross/amp ──
    long = []
    for arm, rs in rows.items():
        for r in rs:
            long.append({'arm': arm, 'code': r['code'], 'date': r['date'],
                         'gross': r['gross'], 'amp_pct': r['amp_pct'],
                         'k_capture': r['k_capture']})
    L = pd.DataFrame(long)

    mk = pd.read_parquet(MASK)
    L = L.merge(mk, on=['code', 'date'], how='left')
    n_all, n_matched = len(L), int(L['t1_index_up'].notna().sum())
    print(f'mask 命中 {n_matched}/{n_all} 腿 ({n_matched/n_all:.1%})'
          f'  —— 未命中者无日线/mask，报告时按各 tier 可用腿计')

    for v in ('legacy', 'stock', 'etf'):
        L[f'net_{v}'] = net_of(L['gross'].values, v)

    # ── 表A：样本厚度 ──
    print('\n' + '=' * 78)
    print('表A  样本厚度（每档可判定腿数；预注册闸门：<300 只报不判）')
    print('=' * 78)
    base = L[L['arm'] == 'CEIL_hold']
    for col, name in TIERS:
        sub = base[base[col] == True]                       # noqa: E712
        bear = base[base[col] == False]                      # noqa: E712
        print(f'  {name:14s} 多头腿 {len(sub):5d} ({len(sub)/len(base):5.1%})'
              f'   空头腿 {len(bear):5d}  {"⚠️样本不足" if len(sub) < 300 else "✅可判定"}')

    # ── 表B：捕获率天花板（#3 的核心）──
    print('\n' + '=' * 78)
    print('表B  捕获率 k 与净收益（k = 毛价差 / 当日振幅；保本线 k* = 往返成本/振幅）')
    print('=' * 78)
    arms = ['CEIL_mfe', 'CEIL_hold', 'RAND'] + list(CAUSAL)
    hdr = f"{'arm':10s}{'n':>6s}{'k':>9s}{'gross%':>9s}{'net@旧':>9s}{'net@股':>9s}{'net@ETF':>9s}"
    for col, name in TIERS:
        print(f'\n【{name}】')
        print(hdr)
        for arm in arms:
            s = L[(L['arm'] == arm) & (L[col] == True)]      # noqa: E712
            if s.empty:
                continue
            k = s['k_capture'].mean()
            g = s['gross'].mean()
            print(f"{arm:10s}{len(s):>6d}{k:>+9.4f}{g:>+9.4f}"
                  f"{s['net_legacy'].mean():>+9.4f}{s['net_stock'].mean():>+9.4f}"
                  f"{s['net_etf'].mean():>+9.4f}")
        amp = base[base[col] == True]['amp_pct'].mean()      # noqa: E712
        print(f"  └ 该档日均振幅 {amp:.3f}%  保本捕获线 k*: 股票 {round_trip('stock')*100/amp*100:.3f}%"
              f"  ETF {round_trip('etf')*100/amp*100:.3f}%")

    # ── 表C：多头 vs 空头（约束#2 的直接检验）──
    print('\n' + '=' * 78)
    print('表C  多头日 vs 空头日：漂移与振幅（约束#2「只在多头做T」是否本来就更有利）')
    print('=' * 78)
    print(f"{'tier':16s}{'侧':6s}{'n':>6s}{'振幅%':>8s}{'漂移gross%':>12s}"
          f"{'net@股%':>10s}{'SE':>8s}{'t':>7s}")
    for col, name in TIERS:
        for side, lab in ((True, '多头'), (False, '空头')):
            s = L[(L['arm'] == 'CEIL_hold') & (L[col] == side)]   # noqa: E712
            if s.empty:
                continue
            st = stat(s['net_stock'].values, s['date'].values)
            print(f'{name:16s}{lab:6s}{st["n"]:>6d}{s["amp_pct"].mean():>8.3f}'
                  f'{s["gross"].mean():>12.4f}{st["mean"]:>10.4f}{st["se"]:>8.4f}{st["t"]:>7.2f}')

    # ── 表D：现行生产出场 vs 去截断（可行动作）──
    print('\n' + '=' * 78)
    print('表D  现行生产(+0.5% 止盈) vs 去截断：被固定止盈吃掉的价差')
    print('=' * 78)
    print(f"{'tier':16s}{'E0_tp05@股':>12s}{'CEIL_hold@股':>13s}{'差额pp':>9s}"
          f"{'胜率tp05':>9s}{'胜率hold':>9s}")
    for col, name in [('__ALL__', '全体')] + list(TIERS):
        sub = L if col == '__ALL__' else L[L[col] == True]        # noqa: E712
        a = sub[sub['arm'] == 'E0_tp05']
        b = sub[sub['arm'] == 'CEIL_hold']
        if a.empty or b.empty:
            continue
        print(f'{name:16s}{a["net_stock"].mean():>+12.4f}{b["net_stock"].mean():>+13.4f}'
              f'{b["net_stock"].mean()-a["net_stock"].mean():>+9.4f}'
              f'{(a["gross"]>0).mean():>9.3f}{(b["gross"]>0).mean():>9.3f}')

    # ── 表E：IS/OOS 分段（入场 Renko+MACD15 本身在本段数据上开发，不分段即自证）──
    print('\n' + '=' * 78)
    print(f'表E  IS/OOS 分段（边界 {OOS_START}）—— 入场规则是在 IS 期开发的，OOS 才是检验')
    print('=' * 78)
    print(f"{'tier':16s}{'期':6s}{'arm':10s}{'n':>6s}{'gross%':>9s}{'net@股%':>10s}"
          f"{'SE':>8s}{'t':>7s}")
    for col, name in [('__ALL__', '全体')] + list(TIERS):
        sub0 = L if col == '__ALL__' else L[L[col] == True]        # noqa: E712
        for seg, lab in (('__IS__', 'IS'), ('__OOS__', 'OOS')):
            seg_df = (sub0[sub0['date'] < OOS_START] if seg == '__IS__'
                      else sub0[sub0['date'] >= OOS_START])
            for arm in ('CEIL_hold', 'RAND'):
                s = seg_df[seg_df['arm'] == arm]
                if s.empty:
                    continue
                st = stat(s['net_stock'].values, s['date'].values)
                print(f'{name:16s}{lab:6s}{arm:10s}{st["n"]:>6d}{s["gross"].mean():>+9.4f}'
                      f'{st["mean"]:>+10.4f}{st["se"]:>8.4f}{st["t"]:>7.2f}')

    out = HERE / 'results_e0_stage3_2026-09-22.json'
    out.write_text(json.dumps({
        'cost_round_trip': {v: round_trip(v) for v in ('stock', 'etf', 'legacy')},
        'tableB': {name: {arm: {
            'n': int(len(s)),
            'k': round(float(s['k_capture'].mean()), 5),
            'gross': round(float(s['gross'].mean()), 5),
            'net_stock': round(float(s['net_stock'].mean()), 5),
            'net_etf': round(float(s['net_etf'].mean()), 5),
            'net_legacy': round(float(s['net_legacy'].mean()), 5),
        } for arm in arms
            if not (s := L[(L['arm'] == arm) & (L[col] == True)]).empty}   # noqa: E712
            for col, name in TIERS},
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
