# -*- coding: utf-8 -*-
"""波段做T · 8 年全市场 OOS 检验。

## 为什么这个检验是决定性的

LLM 在 **8 只票 × 1 年（牛市，B&H 均 +63.9%）** 上提出
`vol_compression_breakout_early`，收益 +8.32pp（82.5% 天花板）。
但两个理由不能采信：
  ① 15 条候选里挑最好 = 多重比较
  ② 该规则在"上涨段只加不减" ⇒ **牛市里天然累积超额暴露**，
     赢的可能是"上涨时仓位更重"，不是"状态识别更准"

本检验：**5,674 只 × 8 年**（2018-2026），**按年拆开**，因子**零调参**直接上。
  · 多数年份为正、且不集中 → 可能是真的
  · 只在少数年份（尤其最近一年）为正 → 是牛市暴露，不是能力

对照：每年都跑**前视对照**，得到**该年的天花板**，用于看"捕获率"是否稳定。

用法：python oos_test.py [--max-symbols N] [--score vol_compression_breakout_early|macd|control]
"""
import argparse
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
for _p in (ROOT, os.path.join(ROOT, 't_io', 'validation', 'factor_mining'),
           os.path.join(ROOT, 't_io', 'validation', 't0_schemes'),
           os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t'), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import overlay_sim as OS  # noqa: E402
import regime_mine as RM  # noqa: E402
import factor_ops as fo  # noqa: E402

SHARDS = os.path.join(HERE, '..', 'xsection', 'panel', 'shards')
MIN_BARS = 200          # 年内至少这么多交易日才纳入该年（一年约 242 日）


# ── 候选评分函数（从 regime_llm_ledger 原样抄回，**零调参**）──
def score_vcbe(ctx):
    c = ctx['c']
    tr = fo.ts_std(c, 5)
    base = fo.ts_std(c, 20)
    comp = fo.div(base, fo.add(tr, 1e-9))
    mx = fo.ts_max(c, 20)
    brk = fo.div(fo.sub(c, mx), fo.add(mx, 1e-9))
    return fo.mul(comp, fo.where_(brk > -0.01, 1.0, 0.0))


def score_macd(ctx):
    return RM.r_macd_d(ctx)


def bars_from_df(g):
    """面板 DataFrame（已按日期升序）→ overlay_sim 要的 bars 列表。"""
    return [{'d': str(t)[:10], 'o': float(o), 'h': float(h), 'l': float(lo),
             'c': float(c), 'v': float(v or 0)} for t, o, h, lo, c, v
            in zip(g['eob'], g['open'], g['high'], g['low'], g['close'], g['volume'])]


def run_symbol(bars, score_fn, act=0.3, grid=0.02):
    """按年切分，逐年算 overlay vs B&H。返回 {year: (d_ret_pp, d_dd_pp, bh_ret)}。"""
    ctx = RM.daily_ctx(bars)
    fv = np.asarray(score_fn(ctx), float).ravel() if score_fn else None
    years = np.array([b['d'][:4] for b in bars])
    out = {}
    for y in sorted(set(years)):
        idx = np.where(years == y)[0]
        if len(idx) < MIN_BARS:
            continue
        sub = [bars[i] for i in idx]
        if score_fn is None:
            no, nb = OS.simulate_control(sub, act_frac=act)
        else:
            no, nb, _t = OS.simulate(sub, act_frac=act, grid=grid,
                                     regime_fn=RM.make_regime_fn(fv[idx]))
        so, sb = OS.stats(no), OS.stats(nb)
        out[y] = ((so['total'] - sb['total']) * 100, (so['maxdd'] - sb['maxdd']) * 100,
                  sb['total'])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-symbols', type=int, default=800)
    ap.add_argument('--act', type=float, default=0.3)
    ap.add_argument('--grid', type=float, default=0.02)
    ap.add_argument('--out', default=os.path.join(HERE, 'oos_8y.json'))
    args = ap.parse_args()
    files = sorted(glob.glob(os.path.join(SHARDS, '*.parquet')))
    print(f'[oos] 面板分片 {len(files)} 个；最多取 {args.max_symbols} 只')

    SCORES = {'control(前视)': None, 'vol_compression_breakout_early': score_vcbe,
              'macd_d': score_macd}
    acc = {k: {} for k in SCORES}          # 名 -> {year: [(d_ret,d_dd,bh)]}
    n_sym = 0
    for fp in files:
        df = pd.read_parquet(fp)
        for sym, g in df.groupby('symbol', sort=False):
            if n_sym >= args.max_symbols:
                break
            g = g.sort_values('eob')
            if len(g) < MIN_BARS:
                continue
            bars = bars_from_df(g)
            for name, fn in SCORES.items():
                try:
                    r = run_symbol(bars, fn, act=args.act, grid=args.grid)
                except Exception:
                    continue
                for y, v in r.items():
                    acc[name].setdefault(y, []).append(v)
            n_sym += 1
        if n_sym >= args.max_symbols:
            break
        print(f'  已处理 {n_sym} 只', flush=True)

    print(f'\n[oos] 样本 {n_sym} 只，逐年拆解（活动仓 {args.act:.0%}）')
    for name in SCORES:
        yrs = sorted(acc[name])
        if not yrs:
            continue
        print(f'\n=== {name} ===')
        print(f"{'年':6}{'n':>6}{'收益增量pp':>11}{'回撤改善pp':>11}{'B&H收益':>10}")
        for y in yrs:
            v = np.array(acc[name][y], float)
            print(f'{y:6}{len(v):>6}{v[:, 0].mean():>+11.2f}{v[:, 1].mean():>+11.2f}'
                  f'{v[:, 2].mean():>10.1%}')
        allv = np.concatenate([np.array(acc[name][y], float) for y in yrs])
        pos = sum(1 for y in yrs if np.mean([x[0] for x in acc[name][y]]) > 0)
        print(f'  合计 收益 {allv[:, 0].mean():+.2f}pp  回撤 {allv[:, 1].mean():+.2f}pp  '
              f'正年份 {pos}/{len(yrs)}')
    json.dump({k: {y: np.array(v).mean(axis=0).tolist() for y, v in d.items()}
               for k, d in acc.items()},
              open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'\n  -> {args.out}')


if __name__ == '__main__':
    main()
