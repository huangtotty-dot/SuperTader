# -*- coding: utf-8 -*-
"""E0 Stage7：振幅的**因果**替代品 —— 把 Stage6 的「Q5 高振幅才有漂移」翻译成可交易规则（2026-09-22）。

## 问题
Stage6 表3：当日振幅 Q1–Q4 费后为负（t=−1.65 ~ −4.69），只有 Q5（振幅 9.27%）
净 +1.431%（t=5.55）。但**当日振幅含前视** —— 09:30 时不知道今天会不会是高波日。

## 本脚本测三个 09:30 时刻**已知**的候选代理
  G  gap        = 今日 09:30 开盘 / 昨收 − 1          （09:30 已知，完全因果）
  A  prev_amp   = 昨日 (高−低)/开盘                    （T-1 已知）
  R  open_range = 09:30–10:00 的 (高−低)/开盘          （10:00 已知；对应「10:00 建仓→收盘」腿）
另测 G × A 交互（高 gap 且高 prev_amp）。

## 判读纪律
- 一律报「费后净均 + 日聚类 SE + t」，并与「无条件基线」对照。
- 报最小可辨效应；分不开的差异不得叙述为效应。
- 若某个代理能重现 Q5 的正收益，则它是**候选可交易规则**；若都不能，则
  Stage6 的 Q5 是「不可交易的事后分层」（与项目既有
  `2026-09-18_波动选股因子包.md` 的结论一致）。

用法：python t_io/validation/t0_bull/e0_stage7_amp_proxy.py
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


def cse(x: np.ndarray, g: np.ndarray) -> float:
    if len(x) == 0:
        return float('nan')
    s = pd.DataFrame({'x': x, 'g': g}).groupby('g')['x'].sum().values
    return float(np.sqrt(np.sum(s ** 2)) / len(x))


def line(lab: str, n: int, x: np.ndarray, g: np.ndarray, base: float, bs: float) -> str:
    if n == 0:
        return f'{lab:26s}{"0":>8s}'
    se = cse(x, g)
    d = x.mean() - base
    sd = float(np.sqrt(se ** 2 + bs ** 2))
    return (f'{lab:26s}{n:>8d}{x.mean():>+10.4f}{se:>8.4f}'
            f'{(x.mean()/se if se else float("nan")):>7.2f}'
            f'{d:>+10.4f}{(d/sd if sd else float("nan")):>7.2f}{1.96*sd:>9.4f}')


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
        o = df[df['hhmm'] == '09:30'].set_index('date')
        c = df[df['hhmm'] == '15:00'].set_index('date')
        if o.empty or c.empty:
            continue
        day_hi = df.groupby('date')['high'].max().astype(float)
        day_lo = df.groupby('date')['low'].min().astype(float)
        m = pd.DataFrame({
            'op': o['open'].astype(float),
            'op_hi': o['high'].astype(float),
            'op_lo': o['low'].astype(float),
            'cl': c['close'].astype(float),
            'day_hi': day_hi, 'day_lo': day_lo,
        }).dropna()
        if m.empty:
            continue
        # 10:00 bar（用于 10:00→收盘 腿）。⚠️ 标签是**区间结束**时刻：
        # `09:30` bar 是集合竞价（量占比仅 0.77%、振幅 0.18%），真正的开盘半小时
        # 是 `10:00` bar（量占比 25.3%）。故首 30 分钟区间 = t10 的 high/low。
        t10 = df[df['hhmm'] == '10:00'].set_index('date')
        if not t10.empty:
            m = m.join(t10['close'].astype(float).rename('cl10'), how='left')
            m = m.join(t10['high'].astype(float).rename('or_hi'), how='left')
            m = m.join(t10['low'].astype(float).rename('or_lo'), how='left')
        m = m.reset_index()
        m['code'] = code
        rows.append(m)
    D = pd.concat(rows, ignore_index=True)
    D['ret'] = D['cl'] / D['op'] - 1
    D['gap'] = D['op'] / D.groupby('code')['cl'].shift(1) - 1
    D['amp'] = (D['day_hi'] - D['day_lo']) / D['op']          # 全日振幅
    D['amp_prev'] = D.groupby('code')['amp'].shift(1)         # 昨日全日振幅（T-1 已知）
    D['open30_range'] = (D['or_hi'] - D['or_lo']) / D['op']   # 首 30 分钟区间（10:00 已知）
    return D


def main() -> None:
    D = load_panel()
    D = D[np.isfinite(D['ret']) & (D['op'] > 0)].copy()
    D = D.sort_values(['code', 'date']).reset_index(drop=True)
    print(f'面板: 股票·日={len(D)}  标的={D["code"].nunique()}  日={D["date"].nunique()}')

    fs, fb = fees('stock')
    D['net'] = ((1 + D['ret']) * (1 - fs) - (1 + fb)) * 100
    D['net10'] = np.where(D['cl10'].notna(),
                          ((D['cl'] / D['cl10']) * (1 - fs) - (1 + fb)) * 100, np.nan)

    base_x, base_g = D['net'].values, D['date'].values
    base = base_x.mean()
    bs = cse(base_x, base_g)
    print(f"\n无条件基线：n={len(D)}  费后净均={base:+.4f}%  SE={bs:.4f}  "
          f"t={base/bs:+.2f}  最小可辨={1.96*bs:.4f}pp\n")

    print('=' * 92)
    print('候选代理 G：隔夜 gap（09:30 已知，完全因果）—— 分桶后 开→收 费后收益')
    print('=' * 92)
    print(f"{'桶':26s}{'n':>8s}{'净均%':>10s}{'SE':>8s}{'t':>7s}{'Δvs基线':>10s}"
          f"{'t(Δ)':>7s}{'最小可辨':>9s}")
    edges = [-np.inf, -0.03, -0.02, -0.01, -0.005, 0.0, 0.005, 0.01, 0.02, 0.03, np.inf]
    labels = ['<-3%', '[-3,-2)%', '[-2,-1)%', '[-1,-0.5)%', '[-0.5,0)%', '[0,0.5)%',
              '[0.5,1)%', '[1,2)%', '[2,3)%', '>=3%']
    D['gb'] = pd.cut(D['gap'], edges, labels=labels)
    for lab, s in D.groupby('gb', observed=True):
        print(line(str(lab), len(s), s['net'].values, s['date'].values, base, bs))

    print('\n' + '=' * 92)
    print('候选代理 A：昨日全日振幅 amp_prev（T-1 已知）—— 五分位')
    print('=' * 92)
    print(f"{'分位':26s}{'n':>8s}{'净均%':>10s}{'SE':>8s}{'t':>7s}{'Δvs基线':>10s}"
          f"{'t(Δ)':>7s}{'最小可辨':>9s}")
    dd = D[D['amp_prev'].notna()].copy()
    dd['q'] = pd.qcut(dd['amp_prev'], 5, labels=['Q1低', 'Q2', 'Q3', 'Q4', 'Q5高'],
                      duplicates='drop')
    for lab, s in dd.groupby('q', observed=True):
        print(line(f'{lab} (amp_prev={s["amp_prev"].mean()*100:.2f}%)',
                   len(s), s['net'].values, s['date'].values, base, bs))

    print('\n' + '=' * 92)
    print('交互 G×A：高 gap(−1%以下) 且 高 amp_prev(Q4/Q5)')
    print('=' * 92)
    for cond, name in ((lambda d: d['gap'] <= -0.01, 'gap<=-1%'),
                       (lambda d: (d['gap'] <= -0.01) & (d['amp_prev'] >= dd['amp_prev'].quantile(0.6)),
                        'gap<=-1% & amp_prev>=P60'),
                       (lambda d: d['gap'] >= 0.01, 'gap>=+1%'),
                       (lambda d: d['gap'].abs() >= 0.02, '|gap|>=2%'),
                       (lambda d: pd.Series(True, index=d.index), '无条件(对照)')):
        s = D[cond(D)]
        print(line(name, len(s), s['net'].values, s['date'].values, base, bs))

    print('\n' + '=' * 92)
    print('对照臂：10:00 建仓 → 收盘（open_range 代理在 10:00 已知）')
    print('=' * 92)
    print(f"{'条件':26s}{'n':>8s}{'净均%':>10s}{'SE':>8s}{'t':>7s}{'Δvs基线':>10s}"
          f"{'t(Δ)':>7s}{'最小可辨':>9s}")
    D10 = D[D['net10'].notna()].copy()
    b10 = D10['net10'].mean()
    b10s = cse(D10['net10'].values, D10['date'].values)
    print(f'   （基线：10:00→收盘 无条件 净均={b10:+.4f}%  SE={b10s:.4f}  '
          f't={b10/b10s:+.2f}）')
    for cond, name in ((lambda d: d['gap'] <= -0.01, 'gap<=-1%'),
                       (lambda d: d['gap'] >= 0.01, 'gap>=+1%'),
                       (lambda d: pd.Series(True, index=d.index), '无条件(对照)')):
        s = D10[cond(D10)]
        x = s['net10'].values
        se = cse(x, s['date'].values)
        print(f'{name:26s}{len(s):>8d}{x.mean():>+10.4f}{se:>8.4f}'
              f'{(x.mean()/se if se else float("nan")):>7.2f}'
              f'{x.mean()-b10:>+10.4f}{((x.mean()-b10)/np.sqrt(se**2+b10s**2)):>7.2f}'
              f'{1.96*np.sqrt(se**2+b10s**2):>9.4f}')

    # ── 代理 R：首 30 分钟区间（10:00 已知）—— 当日振幅的因果替代品 ──
    print('\n' + '=' * 92)
    print('★ 唯一可执行的腿：09:25 竞价即可知的 gap → 09:30 开盘买入 → 10:00 卖出')
    print('   （出入场价全部在 09:25/10:00 已知，零前视；这是本面板上唯一非循环的因果规则）')
    print('=' * 92)
    leg = D[D['cl10'].notna()].copy()
    leg['net30'] = ((leg['cl10'] / leg['op']) * (1 - fs) - (1 + fb)) * 100
    b30 = leg['net30'].mean()
    b30s = cse(leg['net30'].values, leg['date'].values)
    print(f'   （基线：09:30→10:00 无条件 净均={b30:+.4f}%  SE={b30s:.4f}  '
          f't={b30/b30s:+.2f}  最小可辨={1.96*b30s:.4f}pp）\n')
    print(f"{'条件':30s}{'n':>8s}{'净均%':>10s}{'SE':>8s}{'t':>7s}{'Δvs基线':>10s}"
          f"{'t(Δ)':>7s}{'最小可辨':>9s}")
    for cond, name in ((lambda d: d['gap'] <= -0.01, 'gap<=-1%'),
                       (lambda d: d['gap'] <= -0.02, 'gap<=-2%'),
                       (lambda d: d['gap'] <= -0.005, 'gap<=-0.5%'),
                       (lambda d: d['gap'] >= 0.01, 'gap>=+1%'),
                       (lambda d: (d['gap'] <= -0.01) & (d['amp_prev'] >= D['amp_prev'].quantile(0.6)),
                        'gap<=-1% & 昨日振幅>=P60'),
                       (lambda d: pd.Series(True, index=d.index), '无条件(对照)')):
        s = leg[cond(leg)]
        x = s['net30'].values
        se = cse(x, s['date'].values)
        d_ = x.mean() - b30
        print(f'{name:30s}{len(s):>8d}{x.mean():>+10.4f}{se:>8.4f}'
              f'{(x.mean()/se if se else float("nan")):>7.2f}{d_:>+10.4f}'
              f'{(d_/np.sqrt(se**2+b30s**2)):>7.2f}{1.96*np.sqrt(se**2+b30s**2):>9.4f}')
    print('\n  IS/OOS 分段:')
    for lab, sel in (('IS', leg['date'] < '2026-06-01'), ('OOS', leg['date'] >= '2026-06-01')):
        s = leg[sel & (leg['gap'] <= -0.01)]
        x = s['net30'].values
        se = cse(x, s['date'].values)
        print(f'    {lab:4s} gap<=-1%  n={len(s):6d}  净均={x.mean():+.4f}%  SE={se:.4f}  '
              f't={(x.mean()/se if se else float("nan")):+.2f}')

    print('\n' + '=' * 92)
    print('候选代理 R：首 30 分钟区间 open30_range（10:00 已知）—— 五分位')
    print('=' * 92)
    print(f"{'分位':30s}{'n':>8s}{'净均%':>10s}{'SE':>8s}{'t':>7s}{'Δvs基线':>10s}"
          f"{'t(Δ)':>7s}{'最小可辨':>9s}")
    rr = D[D['open30_range'].notna() & (D['open30_range'] > 0)].copy()
    rr['q'] = pd.qcut(rr['open30_range'], 5, labels=['Q1低', 'Q2', 'Q3', 'Q4', 'Q5高'],
                      duplicates='drop')
    for lab, s in rr.groupby('q', observed=True):
        print(line(f'{lab} (rng={s["open30_range"].mean()*100:.2f}%)',
                   len(s), s['net'].values, s['date'].values, base, bs))
    # 同日振幅的相关（检验该代理是否真的预示了「当日振幅」）
    ok = rr[rr['amp'].notna()]
    print(f'\n  代理 R 与「当日全日振幅」的截面相关 = '
          f'{float(np.corrcoef(ok["open30_range"], ok["amp"])[0, 1]):+.3f}'
          f'（vs 代理 A 昨日振幅的相关 '
          f'{float(np.corrcoef(ok["amp_prev"].dropna(), ok.loc[ok["amp_prev"].notna(), "amp"])[0, 1]):+.3f}）')
    print('  10:00→收盘 臂按 open30_range 五分位:')
    print(f"  {'分位':26s}{'n':>8s}{'净均%':>10s}{'SE':>8s}{'t':>7s}{'Δvs基线':>10s}{'t(Δ)':>7s}")
    for lab, s in rr[rr['net10'].notna()].groupby('q', observed=True):
        x = s['net10'].values
        se = cse(x, s['date'].values)
        print(f'  {str(lab):26s}{len(s):>8d}{x.mean():>+10.4f}{se:>8.4f}'
              f'{(x.mean()/se if se else float("nan")):>7.2f}'
              f'{x.mean()-b10:>+10.4f}{((x.mean()-b10)/np.sqrt(se**2+b10s**2)):>7.2f}')

    out = HERE / 'results_e0_stage7_amp_proxy_2026-09-22.json'
    out.write_text(json.dumps({
        'baseline': {'n': int(len(D)), 'net': round(base, 4), 'se': round(bs, 4)},
        'gap_buckets': {str(lab): {'n': int(len(s)), 'net': round(float(s['net'].mean()), 4),
                                   'se': round(cse(s['net'].values, s['date'].values), 4)}
                        for lab, s in D.groupby('gb', observed=True)},
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
