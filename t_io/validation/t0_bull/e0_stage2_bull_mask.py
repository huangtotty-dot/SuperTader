# -*- coding: utf-8 -*-
"""E0 Stage2：物化「多头 / 空头」日级 mask（2026-09-22）。

## 为什么需要它
E0 的目标是「只在多头趋势日做日内T」，而 `factor_ops` 的算子层只见单日 bar
（窗口单位 = bar、不跨日），没有任何跨日状态可用 ⇒ 多头判定必须在**分钟层之外**
算好，再作为 mask 注入求值器。

## 真源复用（不重写任何趋势逻辑）
- `core.build_decision.regime_from_index_daily` — 指数 vs MA60 → trend_up/dn/range
- `core.build_decision.features_from_daily`      — 个股 MA20/MA60/回撤/多头结构
- `core.board_index.resolve_index`               — 个股 → 所属板指数（600→上证 / 688·588→科创50 / 300→创业板 / 00x→深成）

## 三档多头口径（owner 决策：三档都测，报敏感性）
  ① t1_index_up   市场层：指数 regime == 'trend_up'（close > MA60×1.005）
  ② t2_multihead  个股层：price > MA20 且 price > MA60
  ③ t3_near_high  强多头：t2 的 above_ma60 + 浅回撤 ≥ -3%
                  （口径沿用 W34「多头趋势=追强」的既有结论，非新发明）
空头侧同时记录：`mkt_regime`（指数）与 `above_ma60`（个股），供约束#2「转空头清仓」用。

## ⚠️ 前视纪律（本脚本存在的唯一理由）
日内做T在 **T 日盘中**决策 ⇒ mask 只能用 **T-1 及之前** 的日线。
故对每个 T 日，`date_str` 一律传 **前一交易日**，并断言
`features['last_bar_date'] <= prev_date`（fail-closed：陈旧日线不静默放行）。
**绝不使用** 当日 o/h/l/c 派生的 `by_day_type` 一类字段做门控 —— 那是项目
已被咬过的前视陷阱（`doc/experiment/2026-09-18_波动选股因子包.md` 按当日振幅分位那张表）。

产出：`t_io/validation/t0_bull/bull_mask.parquet`
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FM = ROOT / 't_io' / 'validation' / 'factor_mining'
if str(FM) not in sys.path:
    sys.path.insert(0, str(FM))

import minute_data as md  # noqa: E402
from core import build_decision as bd  # noqa: E402
from core.board_index import resolve_index  # noqa: E402

HERE = Path(__file__).resolve().parent
CACHE = ROOT / 't_io' / 'cache' / 'daily_kline'
OUT = HERE / 'bull_mask.parquet'

# 与 minute_data 的 A 源窗口一致（T 日候选区间）；T-1 需再往前，故左界放宽
T_START, T_END = '2025-09-01', '2026-08-31'
MIN_ROWS = 61               # features_from_daily / regime 的最低行数（与真源一致）


def load_daily_cache(code: str) -> pd.DataFrame:
    """读本地日线缓存（_DAILY_CACHE_DIR），**不走 provider/网络**。

    股票 → `{code}.json`；指数（sh000001/sz399006 等非纯数字码）→ `index_{code}.json`。
    格式不变式 `{date, saved_at, rows:[{date,open,high,low,close,volume}]}`，
    与 core/market_data/tencent_provider.py:5 声明一致。
    """
    name = code if code[0].isdigit() else f'index_{code}'
    fp = CACHE / f'{name}.json'
    if not fp.exists():
        return pd.DataFrame()
    d = json.loads(fp.read_text(encoding='utf-8'))
    df = pd.DataFrame(d.get('rows') or [])
    if df.empty:
        return df
    return df.sort_values('date').reset_index(drop=True)


def main() -> None:
    syms = md.pool_symbols()
    print(f'池内代码数: {len(syms)}')

    rows, skipped = [], []
    for i, code in enumerate(syms):
        sdf = load_daily_cache(code)
        if sdf.empty or len(sdf) < MIN_ROWS + 1:
            skipped.append((code, '日线缓存不足'))
            print(f'[{i+1}/{len(syms)}] {code} 跳过：日线不足')
            continue
        idx_code, idx_name = resolve_index(code)
        idf = load_daily_cache(idx_code)
        if idf.empty:
            skipped.append((code, f'指数 {idx_code} 缓存缺失'))
            print(f'[{i+1}/{len(syms)}] {code} 跳过：指数 {idx_code} 缺失')
            continue

        dates = [d for d in sdf['date'].astype(str) if T_START <= d <= T_END]
        n_ok = 0
        for j in range(1, len(sdf)):
            t_day = str(sdf['date'].iloc[j])
            if not (T_START <= t_day <= T_END):
                continue
            prev_day = str(sdf['date'].iloc[j - 1])
            # ⚠️ 关键：date_str 传 T-1
            f = bd.features_from_daily(sdf, prev_day)
            if not f:
                continue
            r = bd.regime_from_index_daily(idf, prev_day)
            # fail-closed：特征末 bar 不得晚于 T-1
            if str(f.get('last_bar_date', '')) > prev_day:
                skipped.append((code, f"前视! last_bar={f.get('last_bar_date')} > prev={prev_day}"))
                continue
            t2 = bool(f['trend_multihead'])
            t3 = bool(f['above_ma60'] and f['drawdown'] >= -0.03)
            rows.append({
                'code': code, 'date': t_day, 'prev_date': prev_day,
                'mkt_regime': r.get('regime'),
                'idx_ratio': r.get('ratio'),
                't1_index_up': r.get('regime') == 'trend_up',
                't2_multihead': t2,
                't3_near_high': t3,
                'above_ma60': bool(f['above_ma60']),
                'drawdown': f['drawdown'],
                'dist_ma60': f['dist_ma60'],
                'rsi': f['rsi'],
                'feat_bar_date': f['last_bar_date'],
                'idx_code': idx_code,
            })
            n_ok += 1
        print(f'[{i+1}/{len(syms)}] {code} ({idx_code} {idx_name}): {n_ok} 日  '
              f'窗口内候选 {len(dates)} 日')

    if not rows:
        print('❌ 无产出')
        return
    out = pd.DataFrame(rows)
    out.to_parquet(OUT, index=False)

    print(f'\n已写 {OUT}  行数={len(out)}  日期 {out["date"].min()}~{out["date"].max()}')
    n = len(out)
    for c in ('t1_index_up', 't2_multihead', 't3_near_high', 'above_ma60'):
        k = int(out[c].sum())
        print(f'  {c:14s} {k:6d}/{n}  ({k/n:.1%})')
    print('  三档交集 t1&t2&t3 :', int((out['t1_index_up'] & out['t2_multihead']
                                        & out['t3_near_high']).sum()))
    print('\n各票覆盖天数（前5）:')
    print(out.groupby('code')['date'].count().head().to_string())
    if skipped:
        print(f'\n⚠️ 跳过 {len(skipped)} 条: {skipped[:5]}')


if __name__ == '__main__':
    main()
