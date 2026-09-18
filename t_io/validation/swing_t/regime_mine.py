# -*- coding: utf-8 -*-
"""状态识别（震荡/单边）基线因子库 —— 波段做T 的入场决策环。

## 为什么是"状态识别"而不是"择时信号"

owner 2026-09-18 澄清：底仓全程不动；**活动仓在震荡段低吸高抛、单边上涨段只加不减（防卖飞）**。
所以要挖的不是"买点/卖点"，而是**当前处于哪种状态**。

## 天花板（实测，见 overlay_sim --control，39票×1年，活动仓30%）

  完美前视叠加 = 收益 **+10.08pp** / 回撤改善 **+2.29pp**
  ⇒ 判据用「占天花板的比」，而不是绝对值。

## 与日内机器复用

`factor_ops` 的算子**与 bar 周期无关**（只看序列），故日线可直接喂进去。
本模块的因子 = 日线 ctx 上的因果算子组合。

## 用法

  python regime_mine.py [--codes ...] [--act 0.3] [--grid 0.02]
  python regime_mine.py --control        # 跑阳性对照，复核天花板
"""
import argparse
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
for _p in (ROOT, os.path.join(ROOT, 't_io', 'validation', 't0_schemes'),
           os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t'),
           os.path.join(ROOT, 't_io', 'validation', 'factor_mining')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import factor_ops as fo  # noqa: E402
import overlay_sim as OS  # noqa: E402

CEIL_RET = 10.08      # pp，实测天花板（39票×1年，act=30%）
CEIL_DD = 2.29        # pp


def daily_ctx(bars):
    """把日线 bars 包成 factor_ops 的 ctx（算子与周期无关，可直接复用）。"""
    o = np.array([b['o'] for b in bars], float)
    h = np.array([b['h'] for b in bars], float)
    l = np.array([b['l'] for b in bars], float)
    c = np.array([b['c'] for b in bars], float)
    v = np.ones_like(c)                       # 本面板无日成交量，占位（量类因子不参与）
    ret = np.zeros_like(c)
    ret[1:] = c[1:] / np.where(c[:-1] > 0, c[:-1], np.nan) - 1
    return {'t': [b['d'] for b in bars], 'o': o, 'h': h, 'l': l, 'c': c, 'v': v,
            'amt': c * v, 'vwap': np.cumsum(c) / np.arange(1, len(c) + 1),
            'ret': ret, 'hi_so_far': np.maximum.accumulate(h),
            'lo_so_far': np.minimum.accumulate(l), 'prev_close': c[0],
            'daily_atr': 0.0, 'n': len(c)}


# ── 基线状态因子（日线）──────────────────────────────────────────────────
def r_ma_slope(ctx, n=20):
    """MA 斜率 / 波动 —— 趋势强度的经典度量。"""
    ma = fo.ts_mean(ctx['c'], n)
    sl = fo.delta(ma, 5)
    vol = fo.ts_std(ctx['ret'], n)
    return fo.div(sl, fo.mul(vol, ctx['c']))

def r_trend_eff(ctx, n=20):
    """效率比 = |n 日净变动| / Σ|单日变动|（1=完美单边，0=纯震荡）。"""
    net = fo.abs_(fo.delta(ctx['c'], n))
    path = fo.ts_sum(fo.abs_(ctx['ret']), n)
    return fo.div(net, fo.mul(path, np.maximum(ctx['c'], 1e-9)))

def r_dist_ma(ctx, n=20):
    """收盘对 MA 的标准化偏离。"""
    ma = fo.ts_mean(ctx['c'], n)
    vol = fo.ts_std(ctx['c'], n)
    return fo.div(fo.sub(ctx['c'], ma), vol)

def r_donchian(ctx, n=20):
    """N 日通道内位置（0=最低，1=最高）。"""
    hi, lo = fo.ts_max(ctx['h'], n), fo.ts_min(ctx['l'], n)
    return fo.div(fo.sub(ctx['c'], lo), fo.add(fo.sub(hi, lo), 1e-9))

def r_vol_ratio(ctx, s=5, n=20):
    """短波动/长波动 —— 波动扩张 vs 收敛。"""
    return fo.div(fo.ts_std(ctx['ret'], s), fo.add(fo.ts_std(ctx['ret'], n), 1e-9))

def r_macd_d(ctx):
    """日线 MACD 柱（owner 点名的那条风险线）。"""
    dif = fo.sub(fo.ema(ctx['c'], 12), fo.ema(ctx['c'], 26))
    dea = fo.ema(np.nan_to_num(dif), 9)
    return fo.div(fo.sub(dif, dea), np.maximum(ctx['c'], 1e-9))

def r_ret_n(ctx, n=20):
    """N 日动量。"""
    return fo.div(fo.delta(ctx['c'], n), np.maximum(np.roll(ctx['c'], n), 1e-9))

def r_amp_ratio(ctx, s=5, n=20):
    """短振幅/长振幅。"""
    amp = fo.div(fo.sub(ctx['h'], ctx['l']), np.maximum(ctx['c'], 1e-9))
    return fo.div(fo.ts_mean(amp, s), fo.add(fo.ts_mean(amp, n), 1e-9))


REGIME_FACTORS = {
    'ma_slope': r_ma_slope, 'trend_eff': r_trend_eff, 'dist_ma': r_dist_ma,
    'donchian': r_donchian, 'vol_ratio5_20': r_vol_ratio, 'macd_d': r_macd_d,
    'ret20': r_ret_n, 'amp_ratio': r_amp_ratio,
}


def make_regime_fn(fvals, q_hi=0.7, q_lo=0.3, win=120):
    """把因子值变成状态函数：**用因子自身历史的因果分位**做阈值（不预设绝对水平）。"""
    def _fn(closes, i):
        w = fvals[max(0, i - win):i + 1]
        w = w[np.isfinite(w)]
        if len(w) < 30 or not np.isfinite(fvals[i]):
            return 'range'
        hi = np.quantile(w, q_hi)
        lo = np.quantile(w, q_lo)
        v = fvals[i]
        if v >= hi:
            return 'up'
        if v <= lo:
            return 'down'
        return 'range'
    return _fn


def eval_codes(codes, act=0.3, grid=0.02, control=False):
    out = {}
    for c in codes:
        bars = OS.daily_bars(c)
        if len(bars) < 80:
            continue
        if control:
            no, nb = OS.simulate_control(bars, act_frac=act)
        else:
            no, nb, _t = OS.simulate(bars, act_frac=act, grid=grid)
        so, sb = OS.stats(no), OS.stats(nb)
        out[c] = (so, sb)
    return out


def summarize(per, label):
    if not per:
        return None
    agg = lambda k: float(np.mean([v[0][k] for v in per.values()]))
    bh = lambda k: float(np.mean([v[1][k] for v in per.values()]))
    d_ret = agg('total') - bh('total')
    d_dd = agg('maxdd') - bh('maxdd')
    return {'label': label, 'n': len(per), 'overlay_ret': agg('total'), 'bh_ret': bh('total'),
            'd_ret_pp': d_ret * 100, 'd_dd_pp': d_dd * 100,
            'ret_capture': d_ret * 100 / CEIL_RET, 'dd_capture': d_dd * 100 / CEIL_DD,
            'shallower': sum(1 for v in per.values() if v[0]['maxdd'] > v[1]['maxdd']),
            'higher': sum(1 for v in per.values() if v[0]['total'] > v[1]['total'])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--act', type=float, default=0.3)
    ap.add_argument('--grid', type=float, default=0.02)
    ap.add_argument('--control', action='store_true')
    ap.add_argument('--out', default=os.path.join(HERE, 'regime_baseline.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else sorted(
        {os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
         for f in glob.glob(os.path.join(OS.v2.CSV_DIR, '*_1year_1min.csv'))})

    if args.control:
        per = eval_codes(codes, act=args.act, grid=args.grid, control=True)
        r = summarize(per, 'CONTROL(前视)')
        print(f"对照 n={r['n']}  收益增量 {r['d_ret_pp']:+.2f}pp  回撤改善 {r['d_dd_pp']:+.2f}pp "
              f"(天花板 {CEIL_RET}/{CEIL_DD})")
        return

    rows = []
    for name, fn in REGIME_FACTORS.items():
        per = {}
        for c in codes:
            bars = OS.daily_bars(c)
            if len(bars) < 80:
                continue
            ctx = daily_ctx(bars)
            try:
                fv = np.asarray(fn(ctx), float)
            except Exception:
                fv = np.full(len(bars), np.nan)
            no, nb, _t = OS.simulate(bars, act_frac=args.act, grid=args.grid,
                                     regime_fn=make_regime_fn(fv))
            per[c] = (OS.stats(no), OS.stats(nb))
        r = summarize(per, name)
        if r:
            rows.append(r)
    rows.sort(key=lambda x: -x['d_ret_pp'])
    print(f"{'状态因子':14}{'收益增量pp':>11}{'回撤改善pp':>11}{'收益捕获':>9}{'回撤捕获':>9}"
          f"{'更浅':>6}{'更高':>6}")
    for r in rows:
        print(f"{r['label']:14}{r['d_ret_pp']:>+11.2f}{r['d_dd_pp']:>+11.2f}"
              f"{r['ret_capture']:>8.1%}{r['dd_capture']:>9.1%}"
              f"{r['shallower']:>4}/{r['n']:<3}{r['higher']:>3}/{r['n']:<3}")
    print(f"\n  天花板(实测前视): 收益 +{CEIL_RET}pp / 回撤 +{CEIL_DD}pp")
    json.dump(rows, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
    print(f'  -> {args.out}')


if __name__ == '__main__':
    main()
