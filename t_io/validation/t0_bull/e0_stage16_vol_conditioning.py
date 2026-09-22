# -*- coding: utf-8 -*-
"""E0 Stage16：高波标的对「低开反转」信号是否更强？（2026-09-22）

## 缘起
owner 建议「挑选波动比较大的股票进行测试」。但记忆里有一条相邻结论
（[[high-vol-adaptive-exit-finding]]）：**「选高波标的」在因果分组后不成立** ——
不过那条针对的是**出场规则**；本条问的是**入场信号（低开反转）在高波票上是否更强**，
是可分开证伪的新命题。

## 因果纪律（这是本脚本的关键）
波动率**一律用 T−1 及之前**的数据：`vol_prev20` = 该票截至 T−1 的**过去 20 个交易日
日均振幅**。**禁止**用当日振幅或整段窗口的分位来选股 —— 那正是项目被咬过的前视陷阱
（`doc/experiment/2026-09-18_波动选股因子包.md` 按当日振幅分位那张表）。

## 三段输出
  §1 全样本（2025-03~2026-09，981 只）：按 `vol_prev20` 五分别看规则每腿费后净收益
  §2 仅 owner 指定窗口（2026-04-08 ~ 2026-07-01）：同上（这正是要用掘金回测的那段）
  §3 高波 vs 低波的 Δ 与 t(Δ)，含日聚类 SE 与最小可辨效应

用法：python e0_stage16_vol_conditioning.py
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

from e0_stage11_oos_test import cse                     # noqa: E402
from core.cost_model import fees                        # noqa: E402

SRC = ROOT / 't_io' / 'cache' / 'tushare_mins'
W0, W1 = '2026-04-08', '2026-07-01'                     # owner 指定窗口
VOL_WIN = 20
REJ = -0.010


def load() -> pd.DataFrame:
    """日级：code/date/op_auc/cl_1000/cl_1500/prev_close/amp。"""
    rows = []
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
        for c in ('open', 'high', 'low', 'close', 'volume', 'amount'):
            df[c] = pd.to_numeric(df[c], errors='coerce')
        auc = df[df['hhmm'] == '09:30'].set_index('date')['open']
        t10 = df[df['hhmm'] == '10:00'].set_index('date')['close']
        c15 = df[df['hhmm'] == '15:00'].set_index('date')['close']
        hi = df[df['hhmm'] != '09:30'].groupby('date')['high'].max()
        lo = df[df['hhmm'] != '09:30'].groupby('date')['low'].min()
        t = pd.DataFrame({'op_auc': auc, 'cl_1000': t10, 'cl_1500': c15,
                          'hi': hi, 'lo': lo}).dropna(subset=['op_auc', 'cl_1500'])
        if t.empty:
            continue
        t = t.reset_index()[['date', 'op_auc', 'cl_1000', 'cl_1500', 'hi', 'lo']]
        t['code'] = code
        rows.append(t)
    return pd.concat(rows, ignore_index=True).sort_values(['code', 'date']).reset_index(drop=True)


def main() -> None:
    D = load()
    D['prev_close'] = D.groupby('code')['cl_1500'].shift(1)
    D = D[np.isfinite(D['prev_close']) & (D['op_auc'] > 0)]
    # 日振幅（当日 hi/lo 为盘后统计，此处只用于**构造 T-1 的历史量**，不直接进信号）
    D['amp'] = (D['hi'] - D['lo']) / D['prev_close']
    # ⚠️ 因果：取 T-1 及之前 20 日的均值，再整体 shift(1)
    D['vol'] = (D.groupby('code')['amp']
                .transform(lambda s: s.rolling(VOL_WIN).mean()).groupby(D['code']).shift(1))
    D = D[np.isfinite(D['vol'])]

    # 规则腿
    D['gap'] = D['op_auc'] / D['prev_close'] - 1
    D['mkt_gap'] = D.groupby('date')['gap'].transform('median')
    D['rel'] = D['gap'] - D['mkt_gap']
    fs, fb = fees('stock')
    R = D[(D['mkt_gap'] < 0) & (D['rel'] <= REJ)].copy()
    R['net'] = ((R['cl_1000'] / R['op_auc']) * (1 - fs) - (1 + fb)) * 100
    print(f'面板 {len(D)} 股票·日 / {D["code"].nunique()} 只；规则命中 {len(R)} 腿')

    def seg(tag: str, S: pd.DataFrame) -> None:
        if len(S) < 200:
            print(f'\n【{tag}】腿数 {len(S)} —— 过薄，只报不判')
            return
        print(f'\n【{tag}】n={len(S)}  日={S["date"].nunique()}  '
              f'净均={S["net"].mean():+.4f}%  t={S["net"].mean()/cse(S["net"].values, S["date"].values):+.2f}')
        S = S.copy()
        S['q'] = pd.qcut(S['vol'], 5, labels=['Q1低波', 'Q2', 'Q3', 'Q4', 'Q5高波'],
                         duplicates='drop')
        print(f"{'分位':10s}{'n':>7s}{'日':>5s}{'vol中位%':>10s}{'净均%':>10s}"
              f"{'SE':>8s}{'t':>7s}{'胜率':>7s}")
        for q, g in S.groupby('q', observed=True):
            x = g['net'].values
            se = cse(x, g['date'].values)
            print(f'{str(q):10s}{len(g):>7d}{g["date"].nunique():>5d}'
                  f'{g["vol"].median()*100:>10.3f}{x.mean():>+10.4f}{se:>8.4f}'
                  f'{x.mean()/se if se else float("nan"):>7.2f}{(x > 0).mean():>7.3f}')
        lo = S[S['q'] == 'Q1低波']['net'].values
        hi = S[S['q'] == 'Q5高波']['net'].values
        d = hi.mean() - lo.mean()
        sd = float(np.sqrt(cse(hi, S[S['q'] == 'Q5高波']['date'].values) ** 2
                           + cse(lo, S[S['q'] == 'Q1低波']['date'].values) ** 2))
        print(f'  Δ(Q5−Q1) = {d:+.4f}pp   se(Δ)={sd:.4f}   t(Δ)={d/sd if sd else float("nan"):+.2f}'
              f'   最小可辨={1.96*sd:.4f}pp')
        print(f'  ⇒ {"高波显著更好" if d > 1.96*sd else ("高波显著更差" if d < -1.96*sd else "**分不开**（Δ 在噪声内）")}')

    seg('§1 全样本 2025-03~2026-09（981 只）', R)
    seg(f'§2 owner 指定窗口 {W0} ~ {W1}', R[(R['date'] >= W0) & (R['date'] <= W1)])

    out = HERE / 'results_e0_stage16_vol_2026-09-22.json'
    out.write_text(json.dumps({
        'window': [W0, W1], 'vol_win': VOL_WIN, 'rule_rel': REJ,
        'full': {'n': int(len(R)), 'net': round(float(R['net'].mean()), 4)},
        'win_only': {'n': int(((R['date'] >= W0) & (R['date'] <= W1)).sum()),
                     'net': round(float(R[(R['date'] >= W0) & (R['date'] <= W1)]['net'].mean()), 4)},
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
