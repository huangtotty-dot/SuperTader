# -*- coding: utf-8 -*-
"""E0 Stage8：§4.5 规则的可交易性闸门 —— 「这规则能不能真下单」（2026-09-22）。

## 为什么必须先做这一步
§4.5 的规则（大盘低开 & 个股相对低开≤−1% → 09:30 买 / 10:00 卖）费后 +0.64%/腿、
t=4.61、18/18 月为正，**但它是在全样本上发现、未做事前预注册**。
在花 tushare 额度拉长历史做样本外之前，先回答一个更便宜、且能**一票否决**的问题：

  **这 22,225 腿落在什么票上？真能按 09:30 开盘价买入吗？**

## 四道闸（任一不过 ⇒ 规则不可用，无需再拉历史）
  T1 流动性   —— 按「前日成交额」分位分层；若收益集中在低流动性分位 ⇒ 容量不可行
                 （只用**分位/次序**统计量，故对 amount 的单位不敏感）
  T2 规模     —— 按价格水平分层（低价股/仙股风险）+ 报告价格分位
  T3 涨跌停   —— 开盘即跌停（买得到但可能继续跌）、10:00 涨停（卖不掉）等事件的频率
  T4 滑点     —— 低开股开盘是一天中流动性最差的时点，用 10/20bp/边压力测试
                 （§4.5 只测到 4bp/边，明显偏乐观）

用法：python t_io/validation/t0_bull/e0_stage8_tradability.py
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
for _p in (str(ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from core.cost_model import fees  # noqa: E402

SRC = ROOT / 't_io' / 'cache' / 'tushare_mins'
HERE = Path(__file__).resolve().parent
OOS_START = '2026-06-01'


def cse(x: np.ndarray, g: np.ndarray) -> float:
    if len(x) == 0:
        return float('nan')
    s = pd.DataFrame({'x': x, 'g': g}).groupby('g')['x'].sum().values
    return float(np.sqrt(np.sum(s ** 2)) / len(x))


def load() -> pd.DataFrame:
    """复用 Stage7 的加载范式，另带 amount（做流动性分层）。"""
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
        o = df[df['hhmm'] == '09:30'].set_index('date')     # 集合竞价
        c = df[df['hhmm'] == '15:00'].set_index('date')
        t10 = df[df['hhmm'] == '10:00'].set_index('date')
        if o.empty or c.empty or t10.empty:
            continue
        amt = df.groupby('date')['amount'].sum()
        m = pd.DataFrame({
            'op': o['open'].astype(float),
            'op_hi': o['high'].astype(float),
            'op_lo': o['low'].astype(float),
            'cl': c['close'].astype(float),
            'cl10': t10['close'].astype(float),
            'hi10': t10['high'].astype(float),
            'lo10': t10['low'].astype(float),
            'amt': amt.astype(float),
        }).dropna()
        if m.empty:
            continue
        m = m.reset_index()
        m['code'] = code
        rows.append(m)
    D = pd.concat(rows, ignore_index=True).sort_values(['code', 'date']).reset_index(drop=True)
    D['gap'] = D['op'] / D.groupby('code')['cl'].shift(1) - 1
    D['amt_prev'] = D.groupby('code')['amt'].shift(1)
    return D


def main() -> None:
    D = load()
    D = D[np.isfinite(D['ret'] if 'ret' in D else D['gap']) | True]
    D = D[np.isfinite(D['gap']) & (D['op'] > 0)].copy()
    mg = D.groupby('date')['gap'].median().rename('mkt_gap')
    D = D.merge(mg, left_on='date', right_index=True, how='left')
    D['rel'] = D['gap'] - D['mkt_gap']

    fs, fb = fees('stock')
    D['net30'] = ((D['cl10'] / D['op']) * (1 - fs) - (1 + fb)) * 100
    R = D[(D['mkt_gap'] < 0) & (D['rel'] <= -0.01)].copy()
    print(f'规则命中腿 = {len(R)}  标的 = {R["code"].nunique()}  交易日 = {R["date"].nunique()}')
    print(f'全体基线：费后 {R["net30"].mean():+.4f}%  t={R["net30"].mean()/cse(R["net30"].values, R["date"].values):+.2f}')

    # 单位自检：amount/volume 隐含的 VWAP 是否与价格量级相符
    print(f'\n[单位自检] op 中位={R["op"].median():.2f}  amt 中位={R["amt"].median():.3e}  '
          f'（amt 单位未假设，下面只用**分位**，故与单位无关）')

    # ── T1 流动性：按前日成交额分位 ──
    print('\n' + '=' * 88)
    print('T1 流动性闸门：按「前日成交额」五分位（单位无关，只用次序）')
    print('=' * 88)
    q = D[D['amt_prev'].notna() & (D['amt_prev'] > 0)].copy()
    q['aq'] = pd.qcut(q['amt_prev'], 5, labels=['L1最低', 'L2', 'L3', 'L4', 'L5最高'],
                      duplicates='drop')
    # 先看规则命中腿落在哪个流动性分位
    print(f"{'流动性分位':12s}{'该分位腿数':>12s}{'规则命中':>10s}{'命中率':>9s}"
          f"{'命中腿费后%':>12s}{'t':>7s}")
    for lab, g in q.groupby('aq', observed=True):
        hit = g[(g['mkt_gap'] < 0) & (g['rel'] <= -0.01)]
        x = hit['net30'].values
        print(f'{str(lab):12s}{len(g):>12d}{len(hit):>10d}{len(hit)/len(g):>9.2%}'
              f'{(x.mean() if len(x) else float("nan")):>+12.4f}'
              f'{(x.mean()/cse(x, hit["date"].values) if len(x) > 30 else float("nan")):>7.2f}')

    # ── T2 规模：价格水平 ──
    print('\n' + '=' * 88)
    print('T2 规模闸门：按开盘价五分位（低价股/仙股风险）')
    print('=' * 88)
    q2 = D[D['op'] > 0].copy()
    q2['pq'] = pd.qcut(q2['op'], 5, labels=['P1最低', 'P2', 'P3', 'P4', 'P5最高'],
                       duplicates='drop')
    print(f"{'价格分位':12s}{'价格中位':>10s}{'命中腿数':>10s}{'费后%':>10s}{'t':>7s}")
    for lab, g in q2.groupby('pq', observed=True):
        hit = g[(g['mkt_gap'] < 0) & (g['rel'] <= -0.01)]
        x = hit['net30'].values
        print(f'{str(lab):12s}{g["op"].median():>10.2f}{len(hit):>10d}'
              f'{(x.mean() if len(x) else float("nan")):>+10.4f}'
              f'{(x.mean()/cse(x, hit["date"].values) if len(x) > 30 else float("nan")):>7.2f}')

    # ── T3 涨跌停事件 ──
    print('\n' + '=' * 88)
    print('T3 涨跌停/极端事件频率（规则命中腿内）')
    print('=' * 88)
    # 板块涨跌幅限制：创业板/科创 20%，其余 10%
    R['limit'] = np.where(R['code'].str[:2].isin(['30', '68']), 0.20, 0.10)
    R['gap_hit_dn'] = R['gap'] <= -(R['limit'] - 0.005)      # 开盘接近跌停
    day_ret = R['cl'] / (R['op'] / (1 + R['gap'])) - 1       # 当日相对前收的涨跌
    R['day_ret'] = day_ret
    R['hit_up'] = day_ret >= (R['limit'] - 0.005)
    R['hit_dn'] = day_ret <= -(R['limit'] - 0.005)
    print(f"  开盘即近跌停: {R['gap_hit_dn'].mean():.2%}  ({int(R['gap_hit_dn'].sum())} 腿)")
    print(f"  当日近涨停  : {R['hit_up'].mean():.2%}  ({int(R['hit_up'].sum())} 腿)"
          f"  ← 10:00 若已涨停则卖不掉")
    print(f"  当日近跌停  : {R['hit_dn'].mean():.2%}  ({int(R['hit_dn'].sum())} 腿)")
    print(f"  rel 分位: p10={R['rel'].quantile(.1):.4f} 中位={R['rel'].median():.4f} "
          f"p90={R['rel'].quantile(.9):.4f}")
    # 剔除涨停不可卖腿后的收益
    ok = R[~R['hit_up']]
    x = ok['net30'].values
    print(f"  剔除「10:00 可能涨停」腿后：n={len(ok)}  费后={x.mean():+.4f}%  "
          f"t={x.mean()/cse(x, ok['date'].values):+.2f}")

    # ── T4 滑点压力（低开股开盘是最差流动性时点）──
    print('\n' + '=' * 88)
    print('T4 滑点压力测试（每边 bp，加在买卖价上）')
    print('=' * 88)
    print(f"{'滑点/边':10s}{'费后净均%':>12s}{'t':>7s}{'年化(按0.064次/日/票)':>22s}")
    for bp in (0, 4, 10, 20, 30):
        sl = bp / 10000.0
        x = ((R['cl10'] / R['op']) * (1 - fs - sl) - (1 + fb + sl)) * 100
        t = x.mean() / cse(x.values, R['date'].values)
        print(f'{bp:>6d}bp  {x.mean():>+12.4f}{t:>7.2f}'
              f'{x.mean()*0.064*243:>+22.1f}%')
    print('  （年化只按「单票口径」粗估：0.064 次/日/票 × 243 日 × 每腿收益，未复利）')

    # ── 汇总：加流动性/涨停闸后的稳健版本 ──
    print('\n' + '=' * 88)
    print('汇总：叠加「流动性≥中位 + 剔除涨停不可卖」后的规则')
    print('=' * 88)
    med = D[D['amt_prev'].notna()].groupby('date')['amt_prev'].transform('median')
    D['amt_prev_med'] = med
    S = D[(D['mkt_gap'] < 0) & (D['rel'] <= -0.01)
          & (D['amt_prev'] >= D['amt_prev_med'])].copy()
    S['limit'] = np.where(S['code'].str[:2].isin(['30', '68']), 0.20, 0.10)
    S['hit_up'] = (S['cl'] / (S['op'] / (1 + S['gap'])) - 1) >= (S['limit'] - 0.005)
    S = S[~S['hit_up']]
    for lab, sel in (('IS', S['date'] < OOS_START), ('OOS', S['date'] >= OOS_START)):
        x = S.loc[sel, 'net30'].values
        g = S.loc[sel, 'date'].values
        print(f'  {lab:4s} n={len(x):6d} 日={S.loc[sel,"date"].nunique():3d}  '
              f'费后={x.mean():+.4f}%  SE={cse(x,g):.4f}  t={x.mean()/cse(x,g):+.2f}')
    x = S['net30'].values
    print(f'  全体 n={len(x)} 费后={x.mean():+.4f}% t={x.mean()/cse(x, S["date"].values):+.2f} '
          f'（原规则 +0.6404% / t=4.61）')

    out = HERE / 'results_e0_stage8_tradability_2026-09-22.json'
    out.write_text(json.dumps({
        'rule_hits': int(len(R)),
        'by_liquidity': {str(lab): {'n': int(len(g)),
                                    'hits': int(((g['mkt_gap'] < 0) & (g['rel'] <= -0.01)).sum()),
                                    'hit_net': round(float(g[(g['mkt_gap'] < 0)
                                                             & (g['rel'] <= -0.01)]['net30'].mean()), 4)}
                         for lab, g in q.groupby('aq', observed=True)},
        'slippage_bp_per_side': {str(bp): round(float(
            (((R['cl10'] / R['op']) * (1 - fs - bp / 10000.0) - (1 + fb + bp / 10000.0)) * 100).mean()), 4)
            for bp in (0, 4, 10, 20, 30)},
        'gated_rule': {'n': int(len(S)), 'net': round(float(S['net30'].mean()), 4),
                       't': round(float(S['net30'].mean() / cse(S['net30'].values, S['date'].values)), 2)},
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
