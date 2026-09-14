# -*- coding: utf-8 -*-
"""auto 做T 重构回测（2026-09-14）— regime 切正T/反T + 反T 低点因子探索。

预注册：C:\\Users\\Lenovo\\.claude\\plans\\mighty-sniffing-feigenbaum.md（Part B）

## 背景（owner 2026-09-14 三条）
1. manual 做T 全删（另见 Part A）；
2. auto 做T 深度排查 → 结论：信号不弱，**执行链掐死**（600176 三次 SELL_HIGH 被
   `floor_protection` 拦下）+ **回补是"等"不是"低吸"**（`buyback_delayed` 溢价时拒绝买回，
   最长挂 3 个交易日）→ 今日收盘挂 5 笔未回补；
3. 硬约束 **数量不变 + 高抛低吸** 未被遵守。

## 设计（owner 追加：震荡/单边上涨尽量做正T，防反T 卖飞）
| regime | 模式 |
|---|---|
| range / trend_up | **正T**：低吸加仓 → 高抛还原（反T 在这种市况必卖飞） |
| trend_dn | **反T**：高抛底仓 → 低点因子买回（顺势，且低点当天不来的概率低） |

## 四臂
A1 仅正T（≈现状口径，含 14:55 强平） / A2 仅反T（不看 regime） /
**A3 regime 切换（主臂）** / baseline 同格随机回补

## 口径声明
- 每笔卖出的回补一律落到"因子触发价；因子未触发则**收盘价**"——保证每笔都闭环，
  使各臂可比；因子真实的"自然闭环率"单列（`natural_close_rate`），供判断是否要靠收盘兜底。
- 成本：卖出 0.00121 / 买入 0.00015
- 无前视：所有因子只用 `<=t` 数据；Renko 砖方向按逐根推进得到
"""
import argparse
import glob
import json
import os
import random
import sys

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_MD = os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')
if _MD not in sys.path:
    sys.path.insert(0, _MD)

import run_experiment_v2 as v2  # noqa: E402  仅复用 1min 数据层 merge_days

OUT = HERE
FEE_SELL, FEE_BUY = 0.00121, 0.00015
BRICK_PCT = 0.003          # 与生产 core/t_decision.DEFAULT_T_PARAMS 一致
TP = 0.005                 # 正T 目标止盈 +0.5%（生产 swing_take_profit_pct）
FORCE_T = '14:55'
NO_NEW_AFTER = '14:30'
K_SWING = 5
MIN_1M = 100
MAX_PAIRS = 3              # 每日每票最多交易对
F2_VWAP_DROP = 0.003
F3_DROPS = (0.003, 0.005, 0.008)
N_MC = 400
N_BOOT = 2000
MC_SEED, BOOT_SEED = 20260914, 20260914
SUBSETS = ['A1_positive_only', 'A2_reverse_only', 'A3_regime_switch']


# ---------------- 指标 ----------------
def ema(arr, span):
    a = np.empty_like(arr)
    a[0] = arr[0]
    al = 2.0 / (span + 1.0)
    for i in range(1, len(arr)):
        a[i] = al * arr[i] + (1 - al) * a[i - 1]
    return a


def macd_dif_hist(c):
    dif = ema(c, 12) - ema(c, 26)
    dea = ema(dif, 9)
    return dif, (dif - dea) * 2.0


def m15_hist(c, t):
    """1min → 15min 桶（钟点 ceil 对齐），返回与 1min 等长的 **因果** 15min MACD 柱。

    桶内任一根只能看到**上一根已完成桶**的值（`hist[j-1]`）——若填本桶值，
    等于把该桶收盘价泄漏给桶内更早的 1min 根（前视）。首桶无历史 → 0。
    """
    buckets, labels = {}, []
    for i, tt in enumerate(t):
        m = int(tt[:2]) * 60 + int(tt[3:])
        k = ((m + 14) // 15) * 15 if tt != '09:30' else 570
        if k not in buckets:
            buckets[k] = []
            labels.append(k)
        buckets[k].append(i)
    closes = [c[buckets[k][-1]] for k in labels]
    out = np.zeros(len(c))
    if len(closes) < 3:
        return out
    _dif, hist = macd_dif_hist(np.array(closes, float))
    for j, k in enumerate(labels):
        v = hist[j - 1] if j > 0 else 0.0      # 只用已完成桶
        for i in buckets[k]:
            out[i] = v
    return out


def renko_dirs(c):
    """逐根喂 1min 收盘，返回与 c 等长的砖方向序列（None 表首砖前）。"""
    from core.t_decision import RenkoBuilder
    b = RenkoBuilder(brick_size_pct=BRICK_PCT)
    dirs = []
    for i in range(len(c)):
        b.update(i, float(c[i]), float(c[i]), float(c[i]))
        dirs.append(b.brick_direction)
    return dirs


def swing_lows(c):
    """局部低点确认根（滞后 K_SWING，无前视）。"""
    return {i + K_SWING for i in range(K_SWING, len(c) - K_SWING)
            if c[i] < c[i - K_SWING:i].min() and c[i] <= c[i + 1:i + K_SWING + 1].min()}


# ---------------- 单日 ----------------
def prep_day(code, dt, bars, regime):
    if len(bars) < MIN_1M:
        return None
    t = [b['t'] for b in bars]
    c = np.array([b['c'] for b in bars], float)
    v = np.array([b['v'] for b in bars], float)
    amt = np.array([b['amt'] for b in bars], float)
    cv, ca = np.cumsum(v), np.cumsum(amt)
    with np.errstate(divide='ignore', invalid='ignore'):
        vwap = np.where(cv > 0, ca / np.where(cv > 0, cv, 1), c)
    dif, hist = macd_dif_hist(c)
    return {'code': code, 'date': dt, 'n': len(c), 't': t, 'c': c, 'vwap': vwap,
            'dif': dif, 'hist1': hist, 'hist15': m15_hist(c, t),
            'dirs': renko_dirs(c), 'lows': swing_lows(c), 'regime': regime,
            'idx_1430': max([i for i, x in enumerate(t) if x <= NO_NEW_AFTER], default=-1),
            'idx_1455': max([i for i, x in enumerate(t) if x <= FORCE_T], default=len(c) - 1)}


def reverse_sell_bar(d):
    """反T 卖出触发：Renko 砖方向**由非 down 翻为 down**（上涨结构破坏）。"""
    for i in range(1, d['n']):
        if d['dirs'][i] == 'down' and d['dirs'][i - 1] != 'down' and i <= d['idx_1430']:
            return i
    return None


def buyback_factor(d, sb, name):
    """返回首个触发的回补根索引（> sb），未触发→None。全部只用 <=t 数据。"""
    c, t, dif, dirs, lows = d['c'], d['t'], d['dif'], d['dirs'], d['lows']
    sell_px = c[sb]
    for j in range(sb + 1, d['n']):
        if name == 'F1_renko_up':
            if dirs[j] == 'up' and dirs[j - 1] != 'up':
                return j
        elif name == 'F2_vwap_drop':
            if c[j] <= d['vwap'][j] * (1 - F2_VWAP_DROP):
                return j
        elif name.startswith('F3_drop'):
            x = float(name.split('_')[-1]) / 1000.0
            if c[j] <= sell_px * (1 - x):
                return j
        elif name == 'F4_swing_low':
            if j in lows:
                return j
        elif name == 'F5_dif_turnup':
            if dif[j] < 0 and dif[j] > dif[j - 1]:
                return j
        elif name == 'F6_drop5_low':
            if (c[j] <= sell_px * 0.995) and (j in lows):
                return j
    return None


def reverse_roundtrip(d, factor):
    """反T 单对：卖在 Renko 向下砖转向 → 因子回补（未触发则收盘兜底）。返回 dict 或 None。"""
    sb = reverse_sell_bar(d)
    if sb is None:
        return None
    cb = buyback_factor(d, sb, factor)
    natural = cb is not None
    if cb is None:
        cb = d['n'] - 1
    sell_px, buy_px = float(d['c'][sb]), float(d['c'][cb])
    net = 100 * (sell_px * (1 - FEE_SELL) - buy_px * (1 + FEE_BUY)) / sell_px
    never_below = bool(d['c'][sb + 1:].min() >= sell_px) if sb + 1 < d['n'] else False
    return {'code': d['code'], 'date': d['date'], 'arm': 'reverse', 'factor': factor,
            'sell_bar': sb, 'sell_time': d['t'][sb], 'sell_px': round(sell_px, 3),
            'buy_bar': cb, 'buy_time': d['t'][cb], 'buy_px': round(buy_px, 3),
            'net_pct': round(net, 4), 'win': bool(net > 0), 'natural_close': natural,
            'sold_flew': never_below, 'regime': d['regime']}


def positive_roundtrip(d):
    """正T 单对：Renko 向下砖 + 15min MACD 柱>0 低吸 → +0.5% 止盈或 14:55 强平。"""
    c, t, dirs, h15 = d['c'], d['t'], d['dirs'], d['hist15']
    entry = None
    for i in range(1, d['idx_1455'] + 1):
        if entry is None:
            if dirs[i] == 'down' and h15[i] > 0 and i <= d['idx_1430'] and i > 0:
                entry = i
        else:
            px = float(c[i])
            if px >= c[entry] * (1 + TP) or i >= d['idx_1455']:
                buy_px, sell_px = float(c[entry]), px
                net = 100 * (sell_px * (1 - FEE_SELL) - buy_px * (1 + FEE_BUY)) / buy_px
                return {'code': d['code'], 'date': d['date'], 'arm': 'positive', 'factor': 'tp0.5',
                        'sell_bar': i, 'sell_time': t[i], 'sell_px': round(sell_px, 3),
                        'buy_bar': entry, 'buy_time': t[entry], 'buy_px': round(buy_px, 3),
                        'net_pct': round(net, 4), 'win': bool(net > 0), 'natural_close': True,
                        'sold_flew': False, 'regime': d['regime']}
    return None


# ---------------- 收集 ----------------
def load_index_regime():
    from core.build_decision import regime_from_index_daily
    import time as _t
    out = {}
    for sym in ('sh000001', 'sh000688', 'sz399001', 'sz399006'):
        fp = os.path.join(ROOT, 't_io', 'cache', 'daily_kline', f'index_{sym}.json')
        if not os.path.exists(fp):
            continue
        for _i in range(8):          # 实盘进程会并发刷新该缓存：退避重试
            try:
                with open(fp, encoding='utf-8') as f:
                    rows = json.load(f)['rows']
                break
            except (json.JSONDecodeError, OSError):
                _t.sleep(0.25 * 2 ** min(_i, 3))
        else:
            continue
        df = pd.DataFrame(rows)
        df['date'] = df['date'].astype(str)
        out[sym] = {d: regime_from_index_daily(df, d)['regime'] for d in df['date']}
    return out


def discover_codes():
    return sorted({os.path.basename(fp).replace('_1year_1min.csv', '').split('.')[0]
                   for fp in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})


def collect(codes, start, end):
    idx = load_index_regime()
    from core.board_index import resolve_index
    days, skipped = [], []
    for code in codes:
        sym = resolve_index(code)[0]
        dates, merged, _src = v2.merge_days(code)
        for dt in dates:
            if dt < start or dt > end:
                continue
            d = prep_day(code, dt, merged[dt], (idx.get(sym) or {}).get(dt, 'unknown'))
            if d is None:
                skipped.append({'code': code, 'date': dt})
                continue
            days.append(d)
    return days, skipped


# ---------------- 评估 ----------------
def agg(rows, key='net_pct'):
    if not rows:
        return {'n': 0}
    v = np.array([r[key] for r in rows], float)
    sv = np.sort(v)
    k = int(len(sv) * 0.05)
    core = sv[k:len(sv) - k] if (k > 0 and len(sv) - 2 * k >= 1) else sv
    pos = {}
    for r in rows:
        pos.setdefault(r['code'], []).append(r[key])
    return {'n': len(v), 'avg': round(float(v.mean()), 4), 'median': round(float(np.median(v)), 4),
            'trimmed': round(float(core.mean()), 4), 'win_rate': round(float((v > 0).mean()), 4),
            'avg_win': round(float(v[v > 0].mean()), 4) if (v > 0).any() else None,
            'avg_loss': round(float(v[v <= 0].mean()), 4) if (v <= 0).any() else None,
            'n_stocks': len(pos), 'n_stocks_pos': sum(1 for x in pos.values() if np.mean(x) > 0)}


def by_date(rows):
    acc = {}
    for r in rows:
        acc.setdefault(r['date'], []).append(r['net_pct'])
    return {d: float(np.mean(v)) for d, v in acc.items()}


def block_bootstrap(deltas, n_boot=N_BOOT, seed=BOOT_SEED):
    if len(deltas) < 5:
        return None
    rng = random.Random(seed)
    a = np.array(deltas, float)
    n = len(a)
    ms = np.array([float(np.mean(a[np.random.RandomState(rng.randint(0, 2**31 - 1)).randint(0, n, n)]))
                   for _ in range(n_boot)])
    return {'mean': round(float(a.mean()), 4), 'ci_lo': round(float(np.percentile(ms, 2.5)), 4),
            'ci_hi': round(float(np.percentile(ms, 97.5)), 4),
            'p_gt_0': round(float((ms > 0).mean()), 4), 'n_dates': n}


def random_baseline(reverses, days_by_key, n_mc=N_MC, seed=MC_SEED):
    """同格随机回补时点（> sell_bar，均匀到收盘），同费率。"""
    rng = random.Random(seed)
    acc = {}
    for r in reverses:
        d = days_by_key[(r['code'], r['date'])]
        sb, n = r['sell_bar'], d['n']
        if n - 1 <= sb:
            continue
        for _ in range(n_mc):
            cb = rng.randint(sb + 1, n - 1)
            sell_px, buy_px = float(d['c'][sb]), float(d['c'][cb])
            acc.setdefault(r['date'], []).append(
                100 * (sell_px * (1 - FEE_SELL) - buy_px * (1 + FEE_BUY)) / sell_px)
    return {d: float(np.mean(v)) for d, v in acc.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--start', default='2025-09-14')
    ap.add_argument('--end', default='2026-08-26')
    ap.add_argument('--mc', type=int, default=N_MC)
    ap.add_argument('--out', default=os.path.join(OUT, 'results_2026-09-14.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else discover_codes()
    v2.END = args.end
    print(f'[rt] codes={len(codes)} {args.start}..{args.end} mc={args.mc}')

    days, skipped = collect(codes, args.start, args.end)
    print(f'[rt] days={len(days)} skipped={len(skipped)}')
    days_by_key = {(d['code'], d['date']): d for d in days}

    out = {'meta': {'n_codes': len(codes), 'stock_days': len(days), 'start': args.start,
                    'end': args.end, 'brick_pct': BRICK_PCT, 'tp': TP, 'n_mc': args.mc},
           'skipped': skipped[:20], 'arms': {}}

    # --- 正T（A1）---
    pos_rows = [r for d in days for r in [positive_roundtrip(d)] if r]
    out['arms']['A1_positive_only'] = {'summary': agg(pos_rows), 'n_days_with_trade': len(
        {(r['code'], r['date']) for r in pos_rows})}
    print(f"  A1 正T n={len(pos_rows)} avg={agg(pos_rows).get('avg')} win={agg(pos_rows).get('win_rate')}")

    # --- 反T（A2：不论 regime）---
    factor_names = ['F1_renko_up', 'F2_vwap_drop', 'F3_drop_03', 'F3_drop_05',
                    'F3_drop_08', 'F4_swing_low', 'F5_dif_turnup', 'F6_drop5_low']
    rev_all = {f: [] for f in factor_names}
    for d in days:
        sb = reverse_sell_bar(d)
        if sb is None:
            continue
        for f in factor_names:
            cb = buyback_factor(d, sb, f)
            natural = cb is not None
            if cb is None:
                cb = d['n'] - 1
            sell_px, buy_px = float(d['c'][sb]), float(d['c'][cb])
            net = 100 * (sell_px * (1 - FEE_SELL) - buy_px * (1 + FEE_BUY)) / sell_px
            rev_all[f].append({
                'code': d['code'], 'date': d['date'], 'factor': f, 'regime': d['regime'],
                'sell_bar': sb, 'sell_px': round(sell_px, 3), 'buy_bar': cb,
                'buy_px': round(buy_px, 3), 'net_pct': round(net, 4), 'win': bool(net > 0),
                'natural_close': natural,
                'sold_flew': bool(sb + 1 < d['n'] and d['c'][sb + 1:].min() >= sell_px)})
    out['arms']['A2_reverse_only'] = {
        f: {'summary': agg(rev_all[f]),
            'natural_close_rate': round(float(np.mean([r['natural_close'] for r in rev_all[f]])), 4),
            'sold_flew_rate': round(float(np.mean([r['sold_flew'] for r in rev_all[f]])), 4)}
        for f in factor_names}
    for f in factor_names:
        s = out['arms']['A2_reverse_only'][f]
        print(f"  A2 {f:16} n={s['summary'].get('n')} avg={s['summary'].get('avg')} "
              f"win={s['summary'].get('win_rate')} 自然闭环={s['natural_close_rate']} "
              f"卖飞={s['sold_flew_rate']}")

    # --- A3：regime 切换（trend_dn 走反T，其余走正T）---
    a3, a3_detail = [], {}
    for f in factor_names:
        rows = []
        for d in days:
            if d['regime'] == 'trend_dn':
                sb = reverse_sell_bar(d)
                if sb is None:
                    continue
                cb = buyback_factor(d, sb, f)
                natural = cb is not None
                if cb is None:
                    cb = d['n'] - 1
                sp, bp = float(d['c'][sb]), float(d['c'][cb])
                net = 100 * (sp * (1 - FEE_SELL) - bp * (1 + FEE_BUY)) / sp
                rows.append({'code': d['code'], 'date': d['date'], 'net_pct': round(net, 4),
                             'win': net > 0, 'natural_close': natural,
                             'sold_flew': bool(sb + 1 < d['n'] and d['c'][sb + 1:].min() >= sp)})
            else:
                r = positive_roundtrip(d)
                if r:
                    rows.append({'code': d['code'], 'date': d['date'], 'net_pct': r['net_pct'],
                                 'win': r['win'], 'natural_close': True, 'sold_flew': False})
        a3_detail[f] = {'summary': agg(rows),
                        'natural_close_rate': round(float(np.mean([r['natural_close'] for r in rows])), 4)
                        if rows else None,
                        'sold_flew_rate': round(float(np.mean([r['sold_flew'] for r in rows])), 4)
                        if rows else None,
                        'reverse_days': sum(1 for d in days if d['regime'] == 'trend_dn'),
                        'non_dn_days': sum(1 for d in days if d['regime'] != 'trend_dn')}
        print(f"  A3 {f:16} n={a3_detail[f]['summary'].get('n')} avg={a3_detail[f]['summary'].get('avg')}")
    out['arms']['A3_regime_switch'] = a3_detail

    # --- baseline：同格随机回补（仅反T 场景）---
    rev_a2 = [r for f in factor_names for r in rev_all[f] if f == 'F3_drop_05'] or rev_all['F1_renko_up']
    base = random_baseline(rev_a2, days_by_key, n_mc=args.mc)
    out['baseline_random_buyback'] = {'mean': round(float(np.mean(list(base.values()))), 4) if base else None}
    picked = a3_detail['F3_drop_05'] if a3_detail['F3_drop_05']['summary'].get('n') else a3_detail['F1_renko_up']
    out['baseline_note'] = 'baseline 为反T 场景的同格随机回补时点'
    print(f"  baseline 随机回补 avg={out['baseline_random_buyback']['mean']}")

    # --- Δ 与 bootstrap（主臂 A3，取样本最足的因子）---
    main_f = max(factor_names, key=lambda f: a3_detail[f]['summary'].get('n') or 0)
    rows = []
    for d in days:
        if d['regime'] != 'trend_dn':
            continue
        sb = reverse_sell_bar(d)
        if sb is None:
            continue
        cb = buyback_factor(d, sb, main_f)
        sp, bp = float(d['c'][sb]), float(d['c'][cb] if cb is not None else d['n'] - 1)
        rows.append({'date': d['date'],
                     'net_pct': 100 * (sp * (1 - FEE_SELL) - bp * (1 + FEE_BUY)) / sp})
    s_by, common = by_date(rows), None
    if base:
        common = sorted(set(s_by) & set(base))
        deltas = [s_by[d] - base[d] for d in common]
        out['main_delta'] = {'factor': main_f, 'delta_pp': round(
            float(np.mean([s_by[d] for d in common])) - float(np.mean([base[d] for d in common])), 4),
            'bootstrap': block_bootstrap(deltas), 'n_common_dates': len(common)}

    out['judge'] = judge(out, a3_detail, main_f)
    json.dump(out, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
    print(f'[rt] -> {args.out}')
    print('[rt] judge:', json.dumps(out['judge'], ensure_ascii=False))


def judge(out, a3_detail, main_f):
    s = a3_detail[main_f]['summary']
    bt = (out.get('main_delta') or {}).get('bootstrap') or {}
    d = (out.get('main_delta') or {}).get('delta_pp')
    a1 = out['arms']['A1_positive_only']['summary']
    a2 = out['arms']['A2_reverse_only']
    a3_avg, a1_avg = s.get('avg'), a1.get('avg')
    flew_a3, flew_a2 = a3_detail[main_f]['sold_flew_rate'], a2['F3_drop_05']['sold_flew_rate']
    lines = {
        'A3_avg>0': bool(a3_avg is not None and a3_avg > 0),
        'A3_ci_lo>0': bool(bt.get('ci_lo', -1) > 0),
        'A3_win>=55%': bool((s.get('win_rate') or 0) >= 0.55),
        'A3_delta>=0.10pp': bool(d is not None and d >= 0.10),
        'A3_close_rate>=60%': bool((a3_detail[main_f]['natural_close_rate'] or 0) >= 0.60),
        'A3_stocks>=1/3': bool(s.get('n_stocks') and s.get('n_stocks_pos', 0) >= s['n_stocks'] / 3),
        'A3> A1(现状)': bool(a3_avg is not None and a1_avg is not None and a3_avg > a1_avg),
        'A3_flew< A2': bool(flew_a3 is not None and flew_a2 is not None and flew_a3 < flew_a2),
    }
    return {'main_factor': main_f, 'lines': lines, 'pass_all': all(lines.values()),
            'A1_avg': a1_avg, 'A3_avg': a3_avg}


if __name__ == '__main__':
    main()
