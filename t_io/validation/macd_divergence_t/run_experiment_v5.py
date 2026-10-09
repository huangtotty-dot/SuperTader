# -*- coding: utf-8 -*-
"""分时 MACD 背离做T 实验 v5（2026-10-09）— MA5 门控版（克隆 v4）。

owner 新假设：日内做T必须「站上5日线」。v4 基线全军覆没（E1 胜率 26.1%、avg_net -0.172%、
0/39 票为正），v5 验证「站上5日线」硬约束能否救回收益。

相对 v4 的唯一变化：新增动态 MA5 门控。信号/回补规则/常量/种子/匹配基线结构全部沿用 v4。

## 动态 MA5 定义（owner 口径）

对每票由 merge_days 的 1min 数据聚合每日收盘。信号根 bar j（日期 d）处：
  MA5_dyn = mean(最近4个已完成交易日收盘 + 当日当前价 c[j])；站上 = c[j] > MA5_dyn。
（代数上等价于 c[j] > mean(前4日收盘)，代码按字面定义逐根计算。）
历史不足 4 个已完成交易日 → MA5 不可用：G0 保留（ma5_above=None），G1/G2 整笔跳过。

## 三个门控臂（每臂内仍跑 E1/E2/E3 三个回补臂）

- G0_none                  ：无门控（= v4 基线重跑，数学口径一致，用于快照对齐验证）
- G1_signal_gate           【核心】：信号根 c <= MA5_dyn → 整笔跳过（不站上5日线不做T）
- G2_signal_and_cover_gate ：信号门控 + 回补门控——回补根 c <= MA5_dyn 则顺延到之后首个
                             c > MA5_dyn 的 bar 回补；到 14:50 仍不满足则 14:50 强制回补，
                             标 forced_ma5=True（E3 本就 14:50 强制，不受回补门控影响）。

## 双费率口径

- legacy：FEE_SELL=0.00121 / FEE_BUY=0.00015（与 v4 可比，judge 主口径）
- live  ：owner 实盘——买 0.0001054，卖 0.0001054 + 0.0005 印花税 = 0.0006054

## 匹配基线说明（与 v4 同种子同结构）

G0 数学口径与 v4 完全一致（MC 基线换用同种子 numpy 随机流，统计等价而非逐位相同）。G1/G2 的 random / any_high 基线
同样只从「站上 MA5」的合格 bar 中抽取，G2 基线回补走同一套 MA5 顺延规则——这样 Δ 度量的是
「DIF 腰斩信号在 MA5 过滤之上」的增量，而非 MA5 过滤本身的贡献。
"""
import argparse
import json
import os
import pickle
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

import run_experiment_v2 as v2  # noqa: E402  仅复用数据层（load_csv_days/merge_days/check）

OUT = HERE
K_SWING = 5
MIN_GAP = 15
WARMUP = 30
NO_NEW_AFTER = '14:30'
FLAT_TIME = '14:50'
from core.cost_model import fees as _cost_fees  # noqa: E402
# 成本单一真源 core/cost_model.py（2026-10-09 任务COST，本文件为当日提交实验）：
# 默认 legacy 保持与 v4/已发布结果可比（judge 主口径），ST_COST_VENUE=stock/etf 可切新口径。
# 旧字面量 卖0.00121/买0.00015 已废止（内含 2023-08-28 已废止的 0.1% 印花税）。
FEE_SELL, FEE_BUY = _cost_fees(os.environ.get('ST_COST_VENUE', 'legacy') or 'legacy')
# 注意：以下 LIVE 字面量（owner 实盘口径历史值）与 cost_model stock 口径相差 0.00001/腿
# （0.0006054/0.0001054 vs 0.0005954/0.0000954，疑多计一次过户费）——为保持与已发布
# v5 live 列严格可比，保留原值不改；新实验请用 ST_COST_VENUE=stock。
FEE_SELL_LIVE = 0.0006054     # owner 实盘：0.0001054 + 0.0005 印花税
FEE_BUY_LIVE = 0.0001054
DIF_RATIO = 0.5
MIN_1M = 100
MAX_SIG_PER_DAY = 2
N_MC = 500
N_BOOT = 2000
MC_SEED = 20260914
BOOT_SEED = 20260914
ARMS = ['E1_dif_turnup', 'E2_hist_neg2pos', 'E3_forced_1450']
GATES = ['G0_none', 'G1_signal_gate', 'G2_signal_and_cover_gate']
V4_N_SIGNALS = 16861          # results_v4_2026-09-14.json signal_stats.n_signals_dedup
DATA_CACHE = os.path.join(HERE, 'v5_data_cache.pkl')


# ---------------- v4 原样克隆：MACD / 摆动高点 / 信号 ----------------

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
    """高点递降 + DIF 腰斩（与 v4 逐行一致）。"""
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
        _eb = cf2
        if _eb not in cand or decay > cand[_eb]['decay']:
            cand[_eb] = {'type': 'top', 'dir': 'sell', 'bar': _eb, 'time': t[_eb],
                         'px': float(c[_eb]), 'p1': float(c[i1]), 'p2': float(c[i2]),
                         'pi1': i1, 'pi2': i2, 'dif1': d1, 'dif2': d2, 'decay': round(decay, 4)}
    return sorted(cand.values(), key=lambda s: s['bar'])[:MAX_SIG_PER_DAY]


def pair_net(sell_px, cover_px, fee_sell, fee_buy):
    return 100 * (sell_px * (1 - fee_sell) - cover_px * (1 + fee_buy)) / sell_px


def load_day(code, dt, bars):
    if len(bars) < MIN_1M:
        return None
    c = np.array([b['c'] for b in bars], float)
    return {'code': code, 'date': dt, 'n': len(bars),
            't': [b['t'] for b in bars], 'c': c,
            'h': np.array([b['h'] for b in bars], float),
            'l': np.array([b['l'] for b in bars], float)}


# ---------------- v5 新增：MA5 门控与回补 ----------------

def _next_idx(cond):
    """nxt[i] = 最小的 j>=i 且 cond[j] 为真；无则 -1。O(n) 逆推（int32 数组）。"""
    n = len(cond)
    nxt = np.full(n, -1, dtype=np.int32)
    last = -1
    for i in range(n - 1, -1, -1):
        if cond[i]:
            last = i
        nxt[i] = last
    return nxt


def _cover_idx_arrays(n, idx_1450, e1_next, e2_next, next_above, above):
    """预计算每个假想卖出根 sb 的回补根索引（基线 MC 向量化用）。

    返回 {(arm, gated): int32 数组}。G0/G1 用 gated=False（与 v4 回补逐位等价），
    G2 用 gated=True（回补根未站上 MA5 → 顺延首个站上 bar，最晚 14:50）。"""
    cov = {}
    e3 = np.empty(n, dtype=np.int32)
    for sb in range(n):
        e3[sb] = idx_1450 if idx_1450 > sb else n - 1
    cov[('E3_forced_1450', False)] = e3
    cov[('E3_forced_1450', True)] = e3
    for arm, nxt in (('E1_dif_turnup', e1_next), ('E2_hist_neg2pos', e2_next)):
        plain = np.empty(n, dtype=np.int32)
        g2 = np.empty(n, dtype=np.int32)
        for sb in range(n):
            j1450 = idx_1450 if idx_1450 > sb else n - 1
            j = int(nxt[sb + 1]) if sb + 1 < n else -1
            plain[sb] = j1450 if j < 0 else j
            if j < 0 or above[j]:
                g2[sb] = plain[sb]
            else:
                k = int(next_above[j + 1]) if j + 1 < n else -1
                g2[sb] = k if (0 <= k <= j1450) else j1450
        cov[(arm, False)] = plain
        cov[(arm, True)] = g2
    return cov


def run_day(code, dt, bars, ma5_sum4):
    """单日全流程。ma5_sum4 = 最近 4 个已完成交易日收盘之和（不足 4 日为 None）。"""
    day = load_day(code, dt, bars)
    if day is None:
        return None, None, [], {}
    dif, _dea, hist = macd_1m(day['c'])
    c, n = day['c'], day['n']
    sigs = gen_signals(day, dif)
    idx_1430 = max([i for i, tt in enumerate(day['t']) if tt <= NO_NEW_AFTER], default=-1)
    idx_1450 = max([i for i, tt in enumerate(day['t']) if tt <= FLAT_TIME], default=n - 1)

    # MA5 站上判定（字面定义：mean(前4日收盘 + 当根价)）
    above = np.zeros(n, dtype=bool)
    if ma5_sum4 is not None:
        for j in range(n):
            above[j] = bool(c[j] > (ma5_sum4 + float(c[j])) / 5.0)
    e1_cond = np.zeros(n, dtype=bool)
    e2_cond = np.zeros(n, dtype=bool)
    for j in range(1, n):
        e1_cond[j] = bool(dif[j] < 0 and dif[j] > dif[j - 1])
        e2_cond[j] = bool(hist[j] > 0 and hist[j - 1] <= 0)
    e1_next, e2_next = _next_idx(e1_cond), _next_idx(e2_cond)
    next_above = _next_idx(above)
    cov = _cover_idx_arrays(n, idx_1450, e1_next, e2_next, next_above, above)

    def cover(sb, arm, gate):
        """回补定位。返回 (cover_bar, fired, forced_ma5, deferred)。"""
        j1450 = idx_1450 if idx_1450 > sb else n - 1
        if arm == 'E3_forced_1450':
            return j1450, True, False, False
        nxt = e1_next if arm == 'E1_dif_turnup' else e2_next
        j = int(nxt[sb + 1]) if sb + 1 < n else -1
        if j < 0:
            return j1450, False, False, False
        if gate != 'G2_signal_and_cover_gate':
            return j, True, False, False
        if above[j]:
            return j, True, False, False
        k = int(next_above[j + 1]) if j + 1 < n else -1
        if 0 <= k <= j1450:
            return k, True, False, True
        return j1450, True, True, False

    day_rec = {'code': code, 'date': dt, 'c': c, 'n': n,
               'idx_1430': idx_1430, 'idx_1450': idx_1450,
               'ma5_sum4': ma5_sum4, 'above': above, 'cov': cov,
               'high_confirms': [cf for _p, cf in detect_highs(c)
                                 if WARMUP <= cf <= idx_1430]}
    day_chg = round(100 * (float(c[-1]) / float(c[0]) - 1), 3)

    pairs = []
    stats = {'n_sig': len(sigs), 'n_above': 0, 'n_below': 0, 'n_no_ma5': 0}
    for s in sigs:
        sig_above = bool(above[s['bar']]) if ma5_sum4 is not None else None
        if sig_above is True:
            stats['n_above'] += 1
        elif sig_above is False:
            stats['n_below'] += 1
        else:
            stats['n_no_ma5'] += 1
        for gate in GATES:
            if gate != 'G0_none' and sig_above is not True:
                continue  # G1/G2：不站上（或 MA5 不可用）整笔跳过
            for arm in ARMS:
                cb, fired, forced_ma5, deferred = cover(s['bar'], arm, gate)
                # 一致性自检：实盘路径与基线预计算表必须同根
                v2.check('cover_map_consistent',
                         cb == int(cov[(arm, gate == 'G2_signal_and_cover_gate')][s['bar']]),
                         f'{code} {dt} {arm} {gate} sb={s["bar"]}')
                cp = float(c[cb])
                net = pair_net(s['px'], cp, FEE_SELL, FEE_BUY)
                net_live = pair_net(s['px'], cp, FEE_SELL_LIVE, FEE_BUY_LIVE)
                pairs.append({'code': code, 'date': dt, 'gate': gate, 'arm': arm,
                              'fired': fired, 'forced_ma5': forced_ma5, 'deferred': deferred,
                              'sell_bar': s['bar'], 'sell_time': s['time'], 'sell_px': s['px'],
                              'p1': s['p1'], 'p2': s['p2'], 'dif1': s['dif1'], 'dif2': s['dif2'],
                              'decay': s['decay'], 'ma5_above': sig_above,
                              'cover_bar': cb, 'cover_px': cp,
                              'net_pct': round(net, 4), 'net_pct_live': round(net_live, 4),
                              'win': bool(net > 0),
                              'fake_cover': bool(cp > s['px']),
                              'day_chg_pct': day_chg})
                v2.check('cost_nonnegative_pair',
                         net <= 100 * (s['px'] - cp) / s['px'] + 1e-12)
                v2.check('cost_nonnegative_pair_live',
                         net_live <= 100 * (s['px'] - cp) / s['px'] + 1e-12)
    return sigs, pairs, day_rec, stats


# ---------------- 匹配基线 / bootstrap / 聚合（v4 结构，双费率扩展） ----------------

def strat_by_date(pairs, key='net_pct'):
    acc = {}
    for p in pairs:
        acc.setdefault(p['date'], []).append(p[key])
    return {d: float(np.mean(v)) for d, v in acc.items()}


def matched_baseline(cells, arm, gate, mode, n_mc=N_MC, seed=MC_SEED):
    """同格随机入场，同回补规则、同费率（双口径同抽样）。numpy 向量化版。

    mode='random'   : 任意 bar ∈ [WARMUP, idx_1430]
    mode='any_high' : 该日已确认局部高点的确认根
    G1/G2 只在「站上 MA5」的合格 bar 中抽取（隔离 MA5 过滤本身的贡献）；
    G2 回补走预计算的 MA5 顺延表。数学口径与 v4 完全一致（每日期全部 MC 抽样的均值），
    仅随机流从 random.Random 换成同种子 np.random.RandomState（G0 基线与 v4 统计等价、
    非逐位相同）。返回 ({date: mean_net_legacy}, {date: mean_net_live}, n_cells)。
    """
    gated = (gate == 'G2_signal_and_cover_gate')
    rng = np.random.RandomState(seed)
    items = []
    for rec, npairs in cells:
        if mode == 'any_high':
            bars = np.array(rec['high_confirms'], dtype=np.int64)
        else:
            bars = np.arange(WARMUP, rec['idx_1430'] + 1, dtype=np.int64)
        if gate != 'G0_none':
            bars = bars[rec['above'][bars]]
        if len(bars) == 0:
            continue
        c = rec['c']
        sell = c[bars]
        covpx = c[rec['cov'][(arm, gated)][bars]]
        nets = np.empty((len(bars), 2))
        nets[:, 0] = 100 * (sell * (1 - FEE_SELL) - covpx * (1 + FEE_BUY)) / sell
        nets[:, 1] = 100 * (sell * (1 - FEE_SELL_LIVE) - covpx * (1 + FEE_BUY_LIVE)) / sell
        items.append((rec['date'], nets, max(1, int(npairs))))
    if not items:
        return {}, {}, 0
    sum_l, sum_v, cnt = {}, {}, {}
    for date, nets, npairs in items:
        idx = rng.randint(0, len(nets), size=(n_mc, npairs))
        s = nets[idx].reshape(-1, 2).sum(axis=0)
        sum_l[date] = sum_l.get(date, 0.0) + float(s[0])
        sum_v[date] = sum_v.get(date, 0.0) + float(s[1])
        cnt[date] = cnt.get(date, 0) + n_mc * npairs
    return ({d: round(sum_l[d] / cnt[d], 6) for d in sum_l},
            {d: round(sum_v[d] / cnt[d], 6) for d in sum_v}, len(items))


def block_bootstrap(deltas, n_boot=N_BOOT, seed=BOOT_SEED):
    if len(deltas) < 5:
        return None
    arr = np.array(deltas, float)
    n = len(arr)
    # 向量化：一次性抽出全部 bootstrap 索引（同种子 numpy 随机流；与 v4 统计口径一致）
    idx = np.random.RandomState(seed).randint(0, n, size=(n_boot, n))
    means = arr[idx].mean(axis=1)
    return {'mean': round(float(arr.mean()), 4), 'ci_lo': round(float(np.percentile(means, 2.5)), 4),
            'ci_hi': round(float(np.percentile(means, 97.5)), 4),
            'p_gt_0': round(float((means > 0).mean()), 4), 'n_dates': n}


def agg(pairs, key='net_pct'):
    if not pairs:
        return {'n': 0}
    v = np.array([p[key] for p in pairs], float)
    sv = np.sort(v)
    k = int(len(sv) * 0.05)
    core = sv[k:len(sv) - k] if (k > 0 and len(sv) - 2 * k >= 1) else sv
    pos = {}
    for p in pairs:
        pos.setdefault(p['code'], []).append(p[key])
    return {'n': len(v), 'avg_net_pct': round(float(v.mean()), 4),
            'median_net_pct': round(float(np.median(v)), 4),
            'trimmed_mean_pct': round(float(core.mean()), 4),
            'win_rate': round(float((v > 0).mean()), 4),
            'fake_cover_rate': round(float(np.mean([p['fake_cover'] for p in pairs])), 4),
            'forced_ma5_rate': round(float(np.mean([p['forced_ma5'] for p in pairs])), 4),
            'deferred_rate': round(float(np.mean([p['deferred'] for p in pairs])), 4),
            'n_stocks': len(pos), 'n_stocks_pos': sum(1 for x in pos.values() if np.mean(x) > 0)}


# ---------------- 收集 / 评估 / 判定 ----------------

def load_merged(codes, use_cache=True):
    """merge_days 结果缓存（数据层只读；缓存文件在本目录内，可删除重建）。"""
    if use_cache and os.path.exists(DATA_CACHE):
        try:
            with open(DATA_CACHE, 'rb') as f:
                cache = pickle.load(f)
            if all(code in cache for code in codes):
                print(f'[v5] 数据缓存命中 {DATA_CACHE}')
                return cache
        except Exception as e:
            print('[v5] 缓存读取失败，重建:', e)
    cache = {}
    for i, code in enumerate(codes):
        dates, merged, _src = v2.merge_days(code)
        closes = {dt: float(merged[dt][-1]['c']) for dt in dates}
        cache[code] = (dates, merged, closes)
        if (i + 1) % 10 == 0 or i + 1 == len(codes):
            print(f'[v5] 数据加载 {i + 1}/{len(codes)}')
    if use_cache:
        with open(DATA_CACHE, 'wb') as f:
            pickle.dump(cache, f, protocol=4)
    return cache


def collect(codes, start, end, use_cache=True):
    data = load_merged(codes, use_cache=use_cache)
    all_pairs, day_recs, per_code, skipped = [], [], {}, []
    ma5_stats = {'n_sig': 0, 'n_above': 0, 'n_below': 0, 'n_no_ma5': 0}
    for code in codes:
        dates, merged, closes = data[code]
        for i, dt in enumerate(dates):
            if dt < start or dt > end:
                continue
            prior4 = [closes[d] for d in dates[max(0, i - 4):i]]
            ma5_sum4 = sum(prior4) if len(prior4) == 4 else None
            bars = merged[dt]
            _s, pairs, rec, st = run_day(code, dt, bars, ma5_sum4)
            if rec is None:
                skipped.append({'code': code, 'date': dt, 'bars': len(bars)})
                continue
            for k in ma5_stats:
                ma5_stats[k] += st[k]
            all_pairs.extend(pairs)
            day_recs.append(rec)
            per_code[code] = per_code.get(code, 0) + 1
    return all_pairs, day_recs, per_code, skipped, ma5_stats


def evaluate(gate_pairs, day_recs, gate, n_mc=N_MC):
    by_cell = {(r['code'], r['date']): r for r in day_recs}
    res = {}
    for arm in ARMS:
        sub = [p for p in gate_pairs if p['arm'] == arm]
        per_cell = {}
        for p in sub:
            per_cell[(p['code'], p['date'])] = per_cell.get((p['code'], p['date']), 0) + 1
        cells = [(by_cell[k], n) for k, n in per_cell.items() if k in by_cell]
        if cells:
            b_rand, b_rand_live, _nc1 = matched_baseline(cells, arm, gate, 'random', n_mc=n_mc)
            b_high, b_high_live, _nc2 = matched_baseline(cells, arm, gate, 'any_high', n_mc=n_mc)
        else:
            b_rand, b_rand_live, b_high, b_high_live = {}, {}, {}, {}
        s_by = strat_by_date(sub, 'net_pct')
        s_by_live = strat_by_date(sub, 'net_pct_live')
        out = {'strategy': agg(sub, 'net_pct'), 'strategy_live': agg(sub, 'net_pct_live'),
               'n_cells': len(cells),
               'baseline_random': round(float(np.mean(list(b_rand.values()))), 4) if b_rand else None,
               'baseline_random_live': round(float(np.mean(list(b_rand_live.values()))), 4) if b_rand_live else None,
               'baseline_any_high': round(float(np.mean(list(b_high.values()))), 4) if b_high else None,
               'baseline_any_high_live': round(float(np.mean(list(b_high_live.values()))), 4) if b_high_live else None,
               'baseline_note': ('G1/G2 基线仅从站上 MA5 的合格 bar 抽取；G2 基线回补含 MA5 顺延'
                                 if gate != 'G0_none' else '与 v4 数学口径一致（同种子 numpy 随机流）')}
        for tag, base, base_live in (('vs_random', b_rand, b_rand_live),
                                     ('vs_any_high', b_high, b_high_live)):
            common = sorted(set(s_by) & set(base))
            deltas = [s_by[d] - base[d] for d in common]
            deltas_live = [s_by_live[d] - base_live[d] for d in common]
            out[tag] = {'delta_pp': (round(float(np.mean([s_by[d] for d in common]))
                                          - float(np.mean([base[d] for d in common])), 4)
                                     if common else None),
                        'bootstrap': block_bootstrap(deltas),
                        'delta_pp_live': (round(float(np.mean([s_by_live[d] for d in common]))
                                               - float(np.mean([base_live[d] for d in common])), 4)
                                          if common else None),
                        'bootstrap_live': block_bootstrap(deltas_live)}
        res[arm] = out
    return res


def judge(res, strat_key='strategy', delta_key='delta_pp', boot_key='bootstrap'):
    """预注册判定线（与 v4 一致）：以三臂中 Δ vs any_high 最优臂为判定臂。"""
    best, best_d = None, None
    for arm in ARMS:
        d = (res[arm].get('vs_any_high') or {}).get(delta_key)
        if d is not None and (best_d is None or d > best_d):
            best, best_d = arm, d
    r = res[best]
    s, bt = r[strat_key], (r['vs_any_high'][boot_key] or {})
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


def ma5_group_diag(g0_pairs):
    """G0 全部 pairs（E1 臂）按信号根「站上/未站上 MA5」分组诊断，双费率。"""
    sub = [p for p in g0_pairs if p['arm'] == 'E1_dif_turnup']
    out = {}
    for tag, pred in (('above_ma5', lambda p: p['ma5_above'] is True),
                      ('below_ma5', lambda p: p['ma5_above'] is False),
                      ('no_ma5_history', lambda p: p['ma5_above'] is None)):
        grp = [p for p in sub if pred(p)]
        a, a_live = agg(grp, 'net_pct'), agg(grp, 'net_pct_live')
        out[tag] = {'n': a.get('n', 0),
                    'legacy': {k: a.get(k) for k in ('win_rate', 'avg_net_pct', 'median_net_pct')},
                    'live': {k: a_live.get(k) for k in ('win_rate', 'avg_net_pct', 'median_net_pct')}}
    return out


# ---------------- 主流程 ----------------

def fmt(x, nd=4):
    return 'NA' if x is None else (f'{x:.{nd}f}' if isinstance(x, float) else str(x))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--start', default='2025-09-14')
    ap.add_argument('--end', default='2026-08-26')
    ap.add_argument('--mc', type=int, default=N_MC)
    ap.add_argument('--no-cache', action='store_true')
    ap.add_argument('--out', default=os.path.join(OUT, 'results_v5_2026-10-09.json'))
    ap.add_argument('--md', default=os.path.join(OUT, 'summary_v5_2026-10-09.md'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else v2_universe()
    v2.END = args.end
    print(f'[v5] codes={len(codes)} start={args.start} end={args.end} mc={args.mc}')

    all_pairs, day_recs, per_code, skipped, ma5_stats = collect(codes, args.start, args.end,
                                                                use_cache=not args.no_cache)
    g0_pairs = [p for p in all_pairs if p['gate'] == 'G0_none']
    n_sig = len({(p['code'], p['date'], p['sell_time']) for p in g0_pairs})
    n_pairs_gate = {g: len([p for p in all_pairs if p['gate'] == g]) for g in GATES}
    dev = (n_sig - V4_N_SIGNALS) / V4_N_SIGNALS if V4_N_SIGNALS else None
    print(f'[v5] G0 信号(去重)={n_sig} (v4={V4_N_SIGNALS}, 偏差={dev:+.2%}) '
          f'pairs={n_pairs_gate} days={len(day_recs)} skipped={len(skipped)}')
    print(f'[v5] MA5 信号分布: {ma5_stats}')
    if dev is not None and abs(dev) > 0.20:
        print('[v5][警告] G0 信号数与 v4 偏差 >20%，需自查数据快照！')

    results, judges = {}, {}
    for gate in GATES:
        gp = [p for p in all_pairs if p['gate'] == gate]
        results[gate] = evaluate(gp, day_recs, gate, n_mc=args.mc)
        judges[gate] = {'legacy': judge(results[gate]),
                        'live': judge(results[gate], strat_key='strategy_live',
                                      delta_key='delta_pp_live', boot_key='bootstrap_live')}
        print(f'[v5] {gate} 评估完成')

    diag = ma5_group_diag(g0_pairs)

    out = {'meta': {'version': 'v5', 'base': 'v4 克隆 + MA5 门控', 'date': '2026-10-09',
                    'n_codes': len(codes), 'start': args.start, 'end': args.end,
                    'n_mc': args.mc, 'dif_ratio': DIF_RATIO, 'k_swing': K_SWING,
                    'min_gap': MIN_GAP, 'stock_days': sum(per_code.values()),
                    'rule': ('c2<=c1 (低高点) & dif1>0 & dif2<dif1*0.5; 1min; 确认滞后5根; '
                             'MA5_dyn=mean(前4日收盘+当根价); 站上=c[j]>MA5_dyn'),
                    'gates': {'G0_none': '无门控(=v4 重跑)',
                              'G1_signal_gate': '信号根 c<=MA5_dyn 整笔跳过',
                              'G2_signal_and_cover_gate': 'G1 + 回补根未站上则顺延至首个站上 bar，'
                                                          '14:50 仍不满足则强制回补 forced_ma5=True'},
                    'fees': {'legacy': {'sell': FEE_SELL, 'buy': FEE_BUY},
                             'live': {'sell': FEE_SELL_LIVE, 'buy': FEE_BUY_LIVE}},
                    'v4_n_signals_ref': V4_N_SIGNALS,
                    'g0_vs_v4_signal_deviation': round(dev, 4) if dev is not None else None},
           'signal_stats': {'n_signals_dedup_g0': n_sig, 'n_pairs_by_gate': n_pairs_gate,
                            'ma5_signal_split': ma5_stats},
           'diagnostics_g0_e1_by_ma5': diag,
           'per_code_days': per_code, 'skipped': skipped[:20],
           'results': results, 'judge': judges, 'assertions': v2.ASSERTS}
    json.dump(out, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'[v5] -> {args.out}')

    write_md(args.md, out)

    # 控制台核心输出
    for gate in GATES:
        print(f'== {gate} ==')
        for arm in ARMS:
            r = results[gate][arm]
            s, sl = r['strategy'], r['strategy_live']
            vh = r['vs_any_high']
            bt = vh['bootstrap'] or {}
            print(f"  {arm:18} n={s.get('n', 0):>5} avg={fmt(s.get('avg_net_pct'))} "
                  f"med={fmt(s.get('median_net_pct'))} win={fmt(s.get('win_rate'))} "
                  f"| live_avg={fmt(sl.get('avg_net_pct'))} live_win={fmt(sl.get('win_rate'))} "
                  f"| anyHigh={fmt(r['baseline_any_high'])} Δh={fmt(vh['delta_pp'])} "
                  f"CI=[{fmt(bt.get('ci_lo'))},{fmt(bt.get('ci_hi'))}] "
                  f"fm5={fmt(s.get('forced_ma5_rate'))}")
        print(f'  judge legacy: {json.dumps(judges[gate]["legacy"], ensure_ascii=False)}')
        print(f'  judge live  : {json.dumps(judges[gate]["live"], ensure_ascii=False)}')
    print('[v5] MA5 分组诊断 (G0/E1):', json.dumps(diag, ensure_ascii=False))
    bad = [k for k, v in v2.ASSERTS.items() if not v['pass']]
    print('[v5] 断言:', '全部通过' if not bad else f'失败={bad}')


def write_md(path, out):
    m, res, jd = out['meta'], out['results'], out['judge']
    L = []
    L.append('# 分时 MACD 背离做T 实验 v5 小结 — MA5 门控（2026-10-09）\n')
    L.append(f"- 窗口 {m['start']} ~ {m['end']}，{m['n_codes']} 票，{m['stock_days']} 股票·日，MC={m['n_mc']}")
    L.append(f"- G0 信号(去重) = {out['signal_stats']['n_signals_dedup_g0']} "
             f"（v4 = {m['v4_n_signals_ref']}，偏差 {m['g0_vs_v4_signal_deviation']:+.2%}）")
    sp = out['signal_stats']['ma5_signal_split']
    L.append(f"- 信号根 MA5 分布：站上 {sp['n_above']} / 未站上 {sp['n_below']} / 无历史 {sp['n_no_ma5']}"
             f"（站上占比 {sp['n_above'] / max(1, sp['n_sig']):.1%}）")
    L.append(f"- pairs 数：{out['signal_stats']['n_pairs_by_gate']}\n")

    L.append('## 核心指标（E1 臂，双费率）\n')
    L.append('| gate | 口径 | n | avg_net% | median% | win_rate | n_stocks_pos | any_high 基线 | Δh(pp) | CI95 |')
    L.append('|---|---|---|---|---|---|---|---|---|---|')
    for gate in GATES:
        r = res[gate]['E1_dif_turnup']
        for fee, skey, bkey, dkey, btkey in (('legacy', 'strategy', 'baseline_any_high', 'delta_pp', 'bootstrap'),
                                             ('live', 'strategy_live', 'baseline_any_high_live',
                                              'delta_pp_live', 'bootstrap_live')):
            s = r[skey]
            bt = r['vs_any_high'][btkey] or {}
            ci = f"[{fmt(bt.get('ci_lo'))},{fmt(bt.get('ci_hi'))}]"
            L.append(f"| {gate} | {fee} | {s.get('n', 0)} | {fmt(s.get('avg_net_pct'))} | "
                     f"{fmt(s.get('median_net_pct'))} | {fmt(s.get('win_rate'))} | "
                     f"{s.get('n_stocks_pos', 0)}/{s.get('n_stocks', 0)} | {fmt(r[bkey])} | "
                     f"{fmt(r['vs_any_high'][dkey])} | {ci} |")
    L.append('')
    L.append('（G1/G2 基线只从站上 MA5 的 bar 抽取，Δh 度量信号在 MA5 过滤之上的增量；'
             'G2 回补含 MA5 顺延，forced_ma5/deferred 比率见 JSON。）\n')

    L.append('## 三回补臂总览（legacy 口径 avg_net% / win_rate）\n')
    L.append('| gate | E1 | E2 | E3 |')
    L.append('|---|---|---|---|')
    for gate in GATES:
        cells = []
        for arm in ARMS:
            s = res[gate][arm]['strategy']
            cells.append(f"{fmt(s.get('avg_net_pct'))} / {fmt(s.get('win_rate'))}")
        L.append(f"| {gate} | {cells[0]} | {cells[1]} | {cells[2]} |")
    L.append('')

    L.append('## MA5 分组诊断（G0 全部信号 × E1 臂）\n')
    L.append('| 分组 | n | legacy win | legacy avg% | legacy med% | live win | live avg% | live med% |')
    L.append('|---|---|---|---|---|---|---|---|')
    for tag, label in (('above_ma5', '站上 MA5'), ('below_ma5', '未站上 MA5'),
                       ('no_ma5_history', 'MA5 历史不足')):
        g = out['diagnostics_g0_e1_by_ma5'][tag]
        L.append(f"| {label} | {g['n']} | {fmt(g['legacy']['win_rate'])} | "
                 f"{fmt(g['legacy']['avg_net_pct'])} | {fmt(g['legacy']['median_net_pct'])} | "
                 f"{fmt(g['live']['win_rate'])} | {fmt(g['live']['avg_net_pct'])} | "
                 f"{fmt(g['live']['median_net_pct'])} |")
    L.append('')

    L.append('## judge（v4 判定线：win>=55%、avg_net>0、ci_lo>0、stocks>=1/3 等）\n')
    for gate in GATES:
        for fee in ('legacy', 'live'):
            j = jd[gate][fee]
            fails = [k for k, v in j['lines'].items() if not v]
            L.append(f"- {gate} / {fee}: best_arm={j['best_arm']} pass_all=**{j['pass_all']}**"
                     + (f'（未过: {", ".join(fails)}）' if fails else ''))
    L.append('')
    L.append('详细数据见 results_v5_2026-10-09.json。')
    open(path, 'w', encoding='utf-8').write('\n'.join(L) + '\n')
    print(f'[v5] -> {path}')


def v2_universe():
    import glob as _g
    return sorted({os.path.basename(fp).replace('_1year_1min.csv', '').split('.')[0]
                   for fp in _g.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})


if __name__ == '__main__':
    main()
