# -*- coding: utf-8 -*-
"""E0 Stage4：诊断「OOS 崩塌」到底是效应死了、还是样本太薄（2026-09-22）。

## 起因
Stage3 发现：新成本下「开盘→14:55 持到收盘」在 IS 期净 +11.1bp/腿（t=1.32），
**OOS 期（2026-06-01 起）却为 −5.0bp（t=−0.24）**；且三档多头口径的 OOS 排序
反直觉（大盘多头最差、个股多头结构最好）。在给这件事编叙事之前，必须先回答：

  **Q0（功效）** IS 与 OOS 的差，在统计上分得开吗？还是三个月样本下本就该差这么多？

只有 Q0 答「分得开」，后面的归因才有意义。

## 三个候选机制
  M1 效应衰减   —— 日内漂移本身变弱/转负
  M2 效应迁移   —— 收益从「日内(开→收)」搬到「隔夜(昨收→开)」；若是，B7（隔夜腿）
                   的月度形态应与日内腿**反相**
  M3 机会收缩   —— 振幅变小 ⇒ 可捕获空间变小（不是信号没了，是鱼小了）

## 口径（严格沿用既有 intraday_drift 实验，便于对照）
  gap      = 09:30 bar 开盘 / 前收 − 1            （隔夜腿）
  日内臂    = close(09:31) → close(14:55)，`net_long` 同构，成本取 core/cost_model
  amp      = (高−低)/开盘
标定锚：全样本 09:31→14:55 旧口径净均应 ≈ **+0.128%**（项目记录），对不上即口径错。

用法：python t_io/validation/t0_bull/e0_stage4_oos_diag.py
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
OOS_START = '2026-06-01'
TIN, TOUT = '09:31', '14:55'
MIN_1M = 100


def net_long(buy: float, sell: float, fs: float, fb: float) -> float:
    """与 intraday_drift.run_experiment.net_long 同构（费后，%）。"""
    return (sell * (1 - fs) - buy * (1 + fb)) / buy * 100


def cse(x: np.ndarray, g: np.ndarray) -> float:
    """日聚类标准误（同日 39 只在横截面相关，按腿独立算会低估）。"""
    if len(x) == 0:
        return float('nan')
    s = pd.DataFrame({'x': x, 'g': g}).groupby('g')['x'].sum().values
    return float(np.sqrt(np.sum(s ** 2)) / len(x))


def main() -> None:
    codes = md.pool_symbols()
    rows = []
    for code in codes:
        df = md.load_minutes(code)
        if df is None or df.empty:
            continue
        df = df.sort_values('time').reset_index(drop=True)
        prev_close = np.nan
        for dt, day in md.iter_days(df):
            if len(day) < MIN_1M:
                prev_close = float(day['close'].iloc[-1])
                continue
            lm = {str(t)[11:16]: i for i, t in enumerate(day['time'])}
            i, j = lm.get(TIN), lm.get(TOUT)
            op = float(day['open'].iloc[0])
            rec = {'code': code, 'date': dt,
                   'gap': (op / prev_close - 1) if prev_close > 0 else np.nan,
                   'amp': ((day['high'].max() - day['low'].min()) / op) if op > 0 else np.nan,
                   'buy': float(day['close'].iloc[i]) if i is not None else np.nan,
                   'sell': float(day['close'].iloc[j]) if j is not None else np.nan}
            rows.append(rec)
            prev_close = float(day['close'].iloc[-1])

    D = pd.DataFrame(rows)
    D = D[D['buy'].notna() & D['sell'].notna() & (D['buy'] > 0)].copy()
    for v in ('legacy', 'stock', 'etf'):
        fs, fb = fees(v)
        D[f'net_{v}'] = net_long(D['buy'].values, D['sell'].values, fs, fb)
    print(f'股票·日 = {len(D)}   票 = {D["code"].nunique()}   '
          f'区间 {D["date"].min()} ~ {D["date"].max()}')

    # ── 标定锚 ──
    m = D['net_legacy'].mean()
    print(f'\n【标定锚】全样本 09:31→14:55 旧口径净均 = {m:+.4f}%  '
          f'(项目记录 +0.128%)  {"✅一致" if abs(m - 0.128) < 0.05 else "⚠️偏离"}')

    # ── Q0 功效检验 ──
    print('\n' + '=' * 78)
    print('Q0 功效：IS 与 OOS 的差，在统计上分得开吗？')
    print('=' * 78)
    print(f"{'venue':8s}{'IS净均':>10s}{'SE':>8s}{'OOS净均':>10s}{'SE':>8s}"
          f"{'Δ(OOS−IS)':>11s}{'se(Δ)':>8s}{'t(Δ)':>7s}{'最小可辨':>9s}")
    for v in ('legacy', 'stock', 'etf'):
        a = D[D['date'] < OOS_START]
        b = D[D['date'] >= OOS_START]
        xa, xb = a[f'net_{v}'].values, b[f'net_{v}'].values
        sa, sb = cse(xa, a['date'].values), cse(xb, b['date'].values)
        d = xb.mean() - xa.mean()
        sd = float(np.sqrt(sa ** 2 + sb ** 2))
        # 最小可辨效应 ≈ 1.96×se(Δ)（α=0.05 双侧）
        print(f'{v:8s}{xa.mean():>+10.4f}{sa:>8.4f}{xb.mean():>+10.4f}{sb:>8.4f}'
              f'{d:>+11.4f}{sd:>8.4f}{(d/sd if sd else float("nan")):>7.2f}'
              f'{1.96*sd:>9.4f}')
    print('  ↑ 「最小可辨」= 1.96×se(Δ)。若 |Δ| 远小于它 ⇒ OOS 差异是噪声，'
          '「崩塌」这一说法本身不成立。')

    # ── 按月分解：M1 衰减 / M2 迁移 / M3 机会 ──
    print('\n' + '=' * 78)
    print('按月分解：日内腿(费后) / 隔夜腿 / 振幅 —— 检验 M1 衰减、M2 迁移、M3 机会收缩')
    print('=' * 78)
    D['ym'] = D['date'].str[:7]
    print(f"{'月':9s}{'n':>6s}{'日内@旧':>10s}{'日内@股':>10s}{'隔夜gap':>10s}"
          f"{'振幅%':>8s}{'日内SE':>8s}{'t':>7s}")
    for ym, g in D.groupby('ym'):
        x = g['net_stock'].values
        se = cse(x, g['date'].values)
        mark = '  ← OOS' if ym >= OOS_START[:7] else ''
        print(f'{ym:9s}{len(g):>6d}{g["net_legacy"].mean():>+10.4f}{x.mean():>+10.4f}'
              f'{g["gap"].mean()*100:>+10.4f}{g["amp"].mean()*100:>8.3f}{se:>8.4f}'
              f'{(x.mean()/se if se else float("nan")):>7.2f}{mark}')

    # ── M2 迁移的正面检验：日内 与 隔夜 的月度相关 ──
    mo = D.groupby('ym').agg(intra=('net_stock', 'mean'), over=('gap', 'mean'),
                             amp=('amp', 'mean'), n=('date', 'size'))
    mo['over'] *= 100
    if len(mo) >= 4:
        r = float(np.corrcoef(mo['intra'], mo['over'])[0, 1])
        print(f'\nM2 迁移检验：月「日内费后」与月「隔夜 gap」的相关 = {r:+.3f}'
              f'（n月={len(mo)}）')
        print('  负相关 ⇒ 收益从日内搬到隔夜（迁移）；近零 ⇒ 两条腿独立。')
    r2 = float(np.corrcoef(mo['intra'], mo['amp'])[0, 1]) if len(mo) >= 4 else float('nan')
    print(f'M3 机会检验：月「日内费后」与月「振幅」的相关 = {r2:+.3f}')

    # ── 多头档位构成的时段变化（防「OOS 崩」其实是成分变化）──
    mk = pd.read_parquet(HERE / 'bull_mask.parquet')
    J = D.merge(mk, on=['code', 'date'], how='left')
    print('\n各档月度占位率（检验 OOS 是否只是多头日变少/变多）:')
    t = J.groupby('ym')[['t1_index_up', 't2_multihead', 't3_near_high']].mean() * 100
    print(t.round(1).to_string())

    print('\n各档 IS/OOS 费后净均（股票成本）:')
    for col, name in (('t1_index_up', '① 指数多头'), ('t2_multihead', '② 个股多头结构'),
                      ('t3_near_high', '③ 强多头近高')):
        for lab, sel in (('IS', J['date'] < OOS_START), ('OOS', J['date'] >= OOS_START)):
            s = J[sel & (J[col] == True)]                      # noqa: E712
            if s.empty:
                continue
            x = s['net_stock'].values
            se = cse(x, s['date'].values)
            print(f'  {name:14s}{lab:4s} n={len(s):5d}  净均={x.mean():+.4f}%  '
                  f'SE={se:.4f}  t={x.mean()/se if se else float("nan"):+.2f}')

    out = HERE / 'results_e0_stage4_2026-09-22.json'
    out.write_text(json.dumps({
        'monthly': {k: {kk: round(float(vv), 5) for kk, vv in v.items()}
                    for k, v in mo.to_dict('index').items()},
        'monthly_tier_share': t.round(3).to_dict('index'),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n→ {out}')


if __name__ == '__main__':
    main()
