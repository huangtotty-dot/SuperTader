# -*- coding: utf-8 -*-
"""分时 MACD 背离做T 实验 v4（2026-09-14）— owner 口径：「高点递降 + DIF 腰斩」1min 版。

预注册：doc/solutions/2026-09-14_分时MACD背离做T实验_plan.md（v4 段）
报告：doc/experiment/2026-09-14_分时MACD背离做T实验.md §十一

## 为什么还有 v4（v3 结论的适用范围被推翻）

owner 2026-09-14 用当日 000988 分时图指出一个 v1/v2/v3 **都没测到**的形态：
  10:02 高点 102.85，DIF 峰 0.402；10:26 高点 102.49，DIF 仅 0.075（衰减 81%）→ 高抛。
v3 的检测器看不见它，两条独立原因（均已逐行验证）：
  ① v2/v3 用 **5min 金叉/死叉锚定**，当日只切出**一个** up 区间（H=102.99, DIF峰=0.495,
     确认根 10:35）——顶背离规则比较的是**相邻** up 区间，单个区间无从比较 → 零信号。
     10:02 与 10:26 两个高点落在同一区间内。
  ② v1/v2/v3 的价格条件都要求「价格不弱于前高」（v1 `>=×0.998`、v2 `>=×(1-0.003)`），
     而本例是 **低高点**（102.49/102.85 = 0.9965）→ 被 0.2%/0.3% 缓冲拒掉。

## owner 拍板的口径（AskUserQuestion 确认）

- 价格：**允许低高点**（`c2 <= c1`，不做缓冲）
- 动能：**`dif2 < dif1 * 0.5`**（DIF 腰斩，衰减 >50%）
- 回补：**三种同时跑**（E1 DIF负区上拐 / E2 MACD柱由负转正 / E3 14:50强制）

## 预注册附加条件（v4 自定，跑前声明）

- 周期 **1min**（owner 看的是分时图；v1 也是 1min，v2/v3 的 5min 已证看不见）
- 摆动点 k=5、确认滞后 5 根、两高点间隔 ≥15 根（沿用 v1 常量）
- **`dif1 > 0`**：第一高点动能必须为正——否则 `dif2 < 0.5*dif1` 会退化成"下跌加速"，
  与 owner 描述的"第一高点强、第二高点弱"不是同一形态。此为 v4 新增，特此声明。
- 新信号截止 14:30，warmup 30 根
"""
import argparse
import json
import os
import random
import sys

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import run_experiment_v2 as v2  # noqa: E402  仅复用数据层（load_csv_days/merge_days）

OUT = HERE
K_SWING = 5
MIN_GAP = 15
WARMUP = 30
NO_NEW_AFTER = '14:30'
FLAT_TIME = '14:50'
FEE_SELL = 0.00121
FEE_BUY = 0.00015
DIF_RATIO = 0.5          # dif2 < dif1 * DIF_RATIO
MIN_1M = 100
MAX_SIG_PER_DAY = 2      # 沿用 v1 MAX_SIG_PER_DIR，防单日主导
IDEAL_ENTRY = False      # True = 上界诊断（卖在第二高点当根，含未来函数，不可交易）
N_MC = 500
N_BOOT = 2000
MC_SEED = 20260914
BOOT_SEED = 20260914
ARMS = ['E1_dif_turnup', 'E2_hist_neg2pos', 'E3_forced_1450']


def ema(arr, span):
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def macd_1m(c):
    dif = ema(c, 12) - ema(c, 26)
    dea = ema(dif, 9)
    return dif, dea, (dif - dea) * 2.0


def detect_highs(c):
    """1min 局部高点 → [(pivot_idx, confirm_idx)]，确认滞后 K_SWING（无未来）。"""
    out = []
    n = len(c)
    for i in range(K_SWING, n - K_SWING):
        left = c[i - K_SWING:i]
        right = c[i + 1:i + K_SWING + 1]
        if c[i] > left.max() and c[i] >= right.max():
            out.append((i, i + K_SWING))
    return out


def gen_signals(day, dif):
    """高点递降 + DIF 腰斩。返回信号（触发根 = 第二高点确认根）。

    注意：比较的是**任意两个满足间隔的高点**，而非仅相邻高点——因为 owner 的例子
    (10:02, 10:26) 中间还夹着一个 10:15 小高点，相邻配对会被 MIN_GAP 拆成两段 13/11 分钟
    的短间隔而全部拒掉（v4 初版即栽在这，已实测复现）。每根触发根只保留衰减最强的一对，
    每日上限 2（沿用 v1 MAX_SIG_PER_DIR）。"""
    c, t = day['c'], day['t']
    idx_1430 = max([i for i, tt in enumerate(t) if tt <= NO_NEW_AFTER], default=-1)
    highs = detect_highs(c)
    cand = {}
    for (i1, _), (i2, cf2) in ((a, b) for a in highs for b in highs if b[0] > a[0]):
        if i2 - i1 < MIN_GAP or cf2 < WARMUP or cf2 > idx_1430:
            continue
        d1, d2 = float(dif[i1]), float(dif[i2])
        if not (c[i2] <= c[i1]):                   # owner: 允许低高点
            continue
        if not (d1 > 0 and d2 < d1 * DIF_RATIO):   # owner: DIF 腰斩 + 首高动能为正
            continue
        decay = (d1 - d2) / d1
        # IDEAL_ENTRY：诊断用**上界**——卖在第二高点当根（需未来信息，实盘不可执行），
        # 用来回答"是不是仅仅被 5 根确认滞后拖累"。若上界仍不成立，任何实现都救不回来。
        _eb = i2 if IDEAL_ENTRY else cf2
        if _eb not in cand or decay > cand[_eb]['decay']:
            cand[_eb] = {'type': 'top', 'dir': 'sell', 'bar': _eb, 'time': t[_eb],
                         'px': float(c[_eb]), 'p1': float(c[i1]), 'p2': float(c[i2]),
                         'pi1': i1, 'pi2': i2, 'dif1': d1, 'dif2': d2, 'decay': round(decay, 4)}
    return sorted(cand.values(), key=lambda s: s['bar'])[:MAX_SIG_PER_DAY]


def exits(day, dif, hist, trig):
    """三种回补规则，全部只用 >trig 的数据。返回 {arm: (cover_bar, cover_time, cover_px, fired)}。

    E1/E2 若当日从未触发，则退回 14:50 强制回补（仓位不能挂着过夜），fired=False 标注。
    """
    c, t, n = day['c'], day['t'], day['n']
    j1450 = max([i for i, tt in enumerate(t) if tt <= FLAT_TIME], default=n - 1)
    if j1450 <= trig:
        j1450 = n - 1
    fallback = (j1450, t[j1450], float(c[j1450]))
    out = {}
    for j in range(trig + 1, n):
        if 'E1_dif_turnup' not in out and dif[j] < 0 and dif[j] > dif[j - 1]:
            out['E1_dif_turnup'] = (j, t[j], float(c[j]), True)
        if 'E2_hist_neg2pos' not in out and hist[j] > 0 and hist[j - 1] <= 0:
            out['E2_hist_neg2pos'] = (j, t[j], float(c[j]), True)
        if len(out) == 2:
            break
    out.setdefault('E1_dif_turnup', fallback + (False,))
    out.setdefault('E2_hist_neg2pos', fallback + (False,))
    out['E3_forced_1450'] = fallback + (True,)
    return out


def pair_net(sell_px, cover_px):
    return 100 * (sell_px * (1 - FEE_SELL) - cover_px * (1 + FEE_BUY)) / sell_px


def load_day(code, dt, bars):
    if len(bars) < MIN_1M:
        return None
    c = np.array([b['c'] for b in bars], float)
    return {'code': code, 'date': dt, 'n': len(bars),
            't': [b['t'] for b in bars], 'c': c,
            'h': np.array([b['h'] for b in bars], float),
            'l': np.array([b['l'] for b in bars], float)}


def run_day(code, dt, bars):
    day = load_day(code, dt, bars)
    if day is None:
        return None, None, []
    dif, _dea, hist = macd_1m(day['c'])
    sigs = gen_signals(day, dif)
    idx_1430 = max([i for i, tt in enumerate(day['t']) if tt <= NO_NEW_AFTER], default=-1)
    pairs = []
    for s in sigs:
        ex = exits(day, dif, hist, s['bar'])
        for arm in ARMS:
            cb, ct, cp, fired = ex[arm]
            net = pair_net(s['px'], cp)
            pairs.append({'code': code, 'date': dt, 'arm': arm, 'fired': fired,
                          'sell_bar': s['bar'], 'sell_time': s['time'], 'sell_px': s['px'],
                          'p1': s['p1'], 'p2': s['p2'], 'dif1': s['dif1'], 'dif2': s['dif2'],
                          'decay': s['decay'],
                          'cover_bar': cb, 'cover_time': ct, 'cover_px': cp,
                          'net_pct': round(net, 4), 'win': bool(net > 0),
                          'fake_cover': bool(cp > s['px']),
                          'day_chg_pct': round(100 * (day['c'][-1] / day['c'][0] - 1), 3)})
            v2.check('cost_nonnegative_pair', net <= 100 * (s['px'] - cp) / s['px'] + 1e-12)
    day_rec = {'code': code, 'date': dt, 't': day['t'], 'c': day['c'], 'n': day['n'],
               'dif': dif, 'hist': hist, 'idx_1430': idx_1430,
               'idx_1450': max([i for i, tt in enumerate(day['t']) if tt <= FLAT_TIME],
                               default=day['n'] - 1),
               'high_confirms': [cf for _p, cf in detect_highs(day['c'])
                                 if WARMUP <= cf <= idx_1430],
               'high_pivots': [p for p, _cf in detect_highs(day['c'])
                               if WARMUP <= p <= idx_1430]}
    return sigs, pairs, day_rec


def strat_by_date(pairs):
    acc = {}
    for p in pairs:
        acc.setdefault(p['date'], []).append(p['net_pct'])
    return {d: float(np.mean(v)) for d, v in acc.items()}


def _cover_for(rec, sb, arm):
    """同回补规则。idx_1450 已预计算（原先每次重建是 O(n)，1.8M 次调用下是主要瓶颈）。"""
    dif, hist, c = rec['dif'], rec['hist'], rec['c']
    if arm == 'E1_dif_turnup':
        for j in range(sb + 1, rec['n']):
            if dif[j] < 0 and dif[j] > dif[j - 1]:
                return float(c[j])
    elif arm == 'E2_hist_neg2pos':
        for j in range(sb + 1, rec['n']):
            if hist[j] > 0 and hist[j - 1] <= 0:
                return float(c[j])
    j1450 = rec['idx_1450']
    if j1450 <= sb:
        j1450 = rec['n'] - 1
    return float(c[j1450])


def matched_baseline(cells, arm, mode, n_mc=N_MC, seed=MC_SEED):
    """同格随机入场，同回补规则、同费率。
    mode='random'         : 任意 bar ∈ [WARMUP, idx_1430]
    mode='any_high'       : 该日**已确认局部高点**的确认根（= 可执行的"卖在任意高点"）
    mode='any_high_ideal' : 该日**局部高点当根**（含未来函数；与上界臂同延迟，用来把
                            "延迟收益" 与 "DIF 选择收益" 分开）"""
    rng = random.Random(seed)
    elig = {}
    for rec, npairs in cells:
        if mode == 'any_high':
            bars = list(rec['high_confirms'])
        elif mode == 'any_high_ideal':
            bars = list(rec['high_pivots'])
        else:
            bars = list(range(WARMUP, rec['idx_1430'] + 1))
        if bars:
            elig[(rec['code'], rec['date'])] = (rec, bars, max(1, int(npairs)))
    if not elig:
        return {}, 0
    acc = {}
    for _ in range(n_mc):
        for (code, date), (rec, bars, npairs) in elig.items():
            for _j in range(npairs):
                sb = rng.choice(bars)
                acc.setdefault(date, []).append(pair_net(float(rec['c'][sb]), _cover_for(rec, sb, arm)))
    return {d: float(np.mean(v)) for d, v in acc.items()}, len(elig)


def block_bootstrap(deltas, n_boot=N_BOOT, seed=BOOT_SEED):
    if len(deltas) < 5:
        return None
    rng = random.Random(seed)
    arr = np.array(deltas, float)
    n = len(arr)
    means = np.array([float(np.mean(arr[np.random.RandomState(rng.randint(0, 2**31 - 1)).randint(0, n, n)]))
                      for _ in range(n_boot)])
    return {'mean': round(float(arr.mean()), 4), 'ci_lo': round(float(np.percentile(means, 2.5)), 4),
            'ci_hi': round(float(np.percentile(means, 97.5)), 4),
            'p_gt_0': round(float((means > 0).mean()), 4), 'n_dates': n}


def agg(pairs):
    if not pairs:
        return {'n': 0}
    v = np.array([p['net_pct'] for p in pairs], float)
    sv = np.sort(v)
    k = int(len(sv) * 0.05)
    core = sv[k:len(sv) - k] if (k > 0 and len(sv) - 2 * k >= 1) else sv
    pos = {}
    for p in pairs:
        pos.setdefault(p['code'], []).append(p['net_pct'])
    return {'n': len(v), 'avg_net_pct': round(float(v.mean()), 4),
            'median_net_pct': round(float(np.median(v)), 4),
            'trimmed_mean_pct': round(float(core.mean()), 4),
            'win_rate': round(float((v > 0).mean()), 4),
            'fake_cover_rate': round(float(np.mean([p['fake_cover'] for p in pairs])), 4),
            'n_stocks': len(pos), 'n_stocks_pos': sum(1 for x in pos.values() if np.mean(x) > 0)}


def collect(codes, start, end):
    all_pairs, day_recs, per_code, skipped = [], [], {}, []
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        for dt in dates:
            if dt < start or dt > end:
                continue
            bars = merged[dt]
            _s, pairs, rec = run_day(code, dt, bars)
            if rec is None:
                skipped.append({'code': code, 'date': dt, 'bars': len(bars)})
                continue
            all_pairs.extend(pairs)
            day_recs.append(rec)
            per_code[code] = per_code.get(code, 0) + 1
    return all_pairs, day_recs, per_code, skipped


def evaluate(all_pairs, day_recs, n_mc=N_MC):
    by_cell = {(r['code'], r['date']): r for r in day_recs}
    res = {}
    for arm in ARMS:
        sub = [p for p in all_pairs if p['arm'] == arm]
        per_cell = {}
        for p in sub:
            per_cell[(p['code'], p['date'])] = per_cell.get((p['code'], p['date']), 0) + 1
        cells = [(by_cell[k], n) for k, n in per_cell.items() if k in by_cell]
        b_rand, nc1 = matched_baseline(cells, arm, 'random', n_mc=n_mc) if cells else ({}, 0)
        b_high, nc2 = matched_baseline(cells, arm, 'any_high', n_mc=n_mc) if cells else ({}, 0)
        if IDEAL_ENTRY:
            b_ideal, _ = matched_baseline(cells, arm, 'any_high_ideal', n_mc=n_mc) if cells else ({}, 0)
        else:
            b_ideal = {}
        s_by = strat_by_date(sub)
        out = {'strategy': agg(sub), 'n_cells': len(cells),
               'baseline_random': round(float(np.mean(list(b_rand.values()))), 4) if b_rand else None,
               'baseline_any_high': round(float(np.mean(list(b_high.values()))), 4) if b_high else None,
               'baseline_any_high_ideal': (round(float(np.mean(list(b_ideal.values()))), 4)
                                           if b_ideal else None)}
        for tag, base in (('vs_random', b_rand), ('vs_any_high', b_high),
                          ('vs_any_high_ideal', b_ideal)):
            common = sorted(set(s_by) & set(base))
            deltas = [s_by[d] - base[d] for d in common]
            out[tag] = {'delta_pp': (round(float(np.mean([s_by[d] for d in common]))
                                          - float(np.mean([base[d] for d in common])), 4)
                                     if common else None),
                        'bootstrap': block_bootstrap(deltas)}
        res[arm] = out
    return res


def judge(res):
    """预注册：以三臂中 **Δ vs any_high 最优** 臂为判定臂（any_high 是最严对照）。"""
    best, best_d = None, None
    for arm in ARMS:
        d = (res[arm].get('vs_any_high') or {}).get('delta_pp')
        if d is not None and (best_d is None or d > best_d):
            best, best_d = arm, d
    r = res[best]
    s, bt = r['strategy'], (r['vs_any_high']['bootstrap'] or {})
    lines = {
        'n>=100': s.get('n', 0) >= 100,
        'delta_vs_any_high>=0.10pp': (best_d is not None and best_d >= 0.10),
        'ci_lo>0': bool(bt.get('ci_lo', -1) > 0),
        'win>=55%': s.get('win_rate', 0) >= 0.55,
        'median>0': s.get('median_net_pct', -1) > 0,
        'stocks>=1/3': bool(s.get('n_stocks') and s.get('n_stocks_pos', 0) >= s['n_stocks'] / 3),
        'avg_net>0': s.get('avg_net_pct', -1) > 0,
    }
    return {'best_arm': best, 'lines': lines, 'pass_all': all(lines.values())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--start', default='2025-09-14')
    ap.add_argument('--end', default='2026-08-26')
    ap.add_argument('--mc', type=int, default=N_MC)
    ap.add_argument('--out', default=os.path.join(OUT, 'results_v4_2026-09-14.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else v2_universe()
    v2.END = args.end
    print(f'[v4] codes={len(codes)} start={args.start} end={args.end} mc={args.mc}')

    all_pairs, day_recs, per_code, skipped = collect(codes, args.start, args.end)
    n_sig = len({(p['code'], p['date'], p['sell_time']) for p in all_pairs})
    print(f'[v4] 信号(去重)={n_sig} pairs={len(all_pairs)} days={len(day_recs)} skipped={len(skipped)}')

    res = evaluate(all_pairs, day_recs, n_mc=args.mc)
    out = {'meta': {'version': 'v4', 'n_codes': len(codes), 'start': args.start, 'end': args.end,
                    'n_mc': args.mc, 'dif_ratio': DIF_RATIO, 'k_swing': K_SWING,
                    'min_gap': MIN_GAP, 'stock_days': sum(per_code.values()),
                    'rule': 'c2<=c1 (低高点) & dif1>0 & dif2<dif1*0.5; 1min; 确认滞后5根'},
           'signal_stats': {'n_signals_dedup': n_sig, 'n_pairs': len(all_pairs)},
           'per_code_days': per_code, 'skipped': skipped[:20],
           'results': res, 'judge': judge(res), 'assertions': v2.ASSERTS}
    json.dump(out, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'[v4] -> {args.out}')
    for arm in ARMS:
        r = res[arm]
        s = r['strategy']
        vh = r['vs_any_high']
        print(f"  {arm:18} n={s.get('n',0):>4} avg={s.get('avg_net_pct')} med={s.get('median_net_pct')} "
              f"win={s.get('win_rate')} | rand={r['baseline_random']} Δr={r['vs_random']['delta_pp']} "
              f"| anyHigh={r['baseline_any_high']} Δh={vh['delta_pp']} "
              f"CI=[{vh['bootstrap']['ci_lo']},{vh['bootstrap']['ci_hi']}]")
    print('[v4] judge:', json.dumps(out['judge'], ensure_ascii=False))


def v2_universe():
    import glob as _g
    return sorted({os.path.basename(fp).replace('_1year_1min.csv', '').split('.')[0]
                   for fp in _g.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})


if __name__ == '__main__':
    main()
