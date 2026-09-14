# -*- coding: utf-8 -*-
"""分时 MACD 背离做T 实验 v3（2026-09-14）— 扩池 + 同格匹配基线。

预注册：doc/solutions/2026-09-14_分时MACD背离做T实验_plan.md（v3 段）
报告：doc/experiment/2026-09-14_分时MACD背离做T实验.md §十

v3 相对 v2 的改动（**只此三条**，信号与交易机器完全复用 v2）：
1. **扩池**：5 票 → 全部 39 票（~9,394 股票·日，样本 ×7.9）。
2. **日环境从「掐信号的门控」改为「划子集的因果过滤器」**：
   v2 `eval_gate()` 一旦触发就掐掉后续所有信号，与「门控顶背离高抛」互斥——所以那个假设
   在 v2 里根本没被真正测到（349 个门控日只产出 3 对）。v3 改为在确认根 k 打标签、不掐信号：
     F1   close[k] < VWAP(<=k)
     F2   close[k]/prev_close - 1 <= -0.5%
     F3   市场级日线 regime == trend_dn（复用 core.build_decision.regime_from_index_daily）
3. **同格匹配基线**：v1 的 62.1% 是「事后收跌日子集 vs 无条件下随机基线」，收跌日上任何卖出侧
   都更容易赢 → 那 +15.8pp 极可能全是日型选择。v3 的基线**只在同一批 (票,日) 单元、且在同样的
   过滤条件为真的 bar 上**随机取点，并用与策略完全相同的回补路径（含"首个低吸信号价回补"——
   v2 基线缺这条路，R3 已指出其偏袒基线）与费率。

判定：预注册四条线 + 日期分块 bootstrap。结果为负则如实记录并关闭方向（owner 已裁决）。
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

import run_experiment_v2 as v2  # noqa: E402  复用信号/交易机器（不重写）

OUT = HERE
INDEX_SYMS = ['sh000001', 'sh000688', 'sz399001', 'sz399006']

F2_THRESH = -0.005      # 预注册主口径
N_MC = 500
N_BOOT = 2000
MC_SEED = 20260914
BOOT_SEED = 20260914

SUBSETS = ['ALL', 'F1', 'F2', 'F3', 'F1F2', 'F1F2F3', 'V2GATE']


# ---------------- 数据层（复用 v2） ----------------

def discover_codes():
    codes = set()
    for fp in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv')):
        codes.add(os.path.basename(fp).replace('_1year_1min.csv', '').split('.')[0])
    return sorted(codes)


def _load_json_retry(fp, tries=8):
    """实盘进程会并发刷新 index 缓存（读到过截断窗口）：空/坏 JSON 退避重试。"""
    import time as _t
    last = None
    for _i in range(tries):
        try:
            with open(fp, encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            last = e
            _t.sleep(0.25 * (2 ** min(_i, 3)))
    raise RuntimeError(f'index 缓存读取失败（并发写？）: {fp} :: {last}')


def load_index_regime():
    """{index_symbol: {date: regime}}，逐日调用生产口径 regime_from_index_daily（<=date 截断，无未来）。"""
    from core.build_decision import regime_from_index_daily
    out = {}
    for sym in INDEX_SYMS:
        fp = os.path.join(ROOT, 't_io', 'cache', 'daily_kline', f'index_{sym}.json')
        if not os.path.exists(fp):
            continue
        df = pd.DataFrame(_load_json_retry(fp)['rows'])
        df['date'] = df['date'].astype(str)
        reg = {}
        for d in df['date']:
            reg[d] = regime_from_index_daily(df, d)['regime']
        out[sym] = reg
    return out


def resolve_regime(code, date, idx_reg, cache={}):
    if code not in cache:
        from core.board_index import resolve_index
        cache[code] = resolve_index(code)[0]
    return (idx_reg.get(cache[code]) or {}).get(date, 'unknown')


# ---------------- 因果过滤器（全部只用 <=k 数据） ----------------

def build_filter_arrays(day5, prev_close):
    """返回 (f1, chg)：f1 = 当根收盘跌破截至当根 VWAP；chg = 当根相对昨收涨跌幅（F2 由阈值派生）。"""
    c, v, amt = day5['c'], day5['v'], day5['amt']
    cv, ca = np.cumsum(v), np.cumsum(amt)
    with np.errstate(divide='ignore', invalid='ignore'):
        vwap = np.where(cv > 0, ca / np.where(cv > 0, cv, 1), c)
    f1 = c < vwap
    chg = ((c / prev_close - 1) if prev_close else np.zeros(len(c)))
    return f1, chg


def subset_pred(name, f2_thresh=None):
    """返回 pred(day_rec, bar) -> bool。F2 阈值可变，故派生自 rec['chg'] 而非预存布尔。"""
    th = F2_THRESH if f2_thresh is None else f2_thresh
    if name == 'ALL':
        return lambda rec, bar: True
    if name == 'F1':
        return lambda rec, bar: bool(rec['f1'][bar])
    if name == 'F2':
        return lambda rec, bar: bool(rec['chg'][bar] <= th)
    if name == 'F3':
        return lambda rec, bar: rec['f3'] == 'trend_dn'
    if name == 'F1F2':
        return lambda rec, bar: bool(rec['f1'][bar] and rec['chg'][bar] <= th)
    if name == 'F1F2F3':
        return lambda rec, bar: bool(rec['f1'][bar] and rec['chg'][bar] <= th
                                     and rec['f3'] == 'trend_dn')
    if name == 'V2GATE':
        return lambda rec, bar: rec['gate_bar'] is not None and bar >= rec['gate_bar']
    raise ValueError(name)


# ---------------- 单日：信号 + 过滤器 + 配对 ----------------

def run_day_v3(code, dt, day5, prev_close, f3):
    """与 v2 run_day 同回补规则，但**不因门控掐信号**；改为对每对打过滤标签。"""
    dif, dea = v2.macd_5m(day5['c'])
    sigs = v2.detect_signals(day5, dif, dea)
    gate_bar = v2.eval_gate(day5, prev_close)
    posthoc = v2.day_type_posthoc(day5, prev_close)
    idx_1450 = v2.find_label_idx(day5, v2.FORCED_LABEL)
    f1, chg = build_filter_arrays(day5, prev_close)
    c = day5['c']

    pairs, open_pair = [], None
    for s in sigs:
        s['gated_before'] = gate_bar is not None and s['bar'] >= gate_bar
        if s['vetoed']:
            s['used'] = 'vetoed'
            continue
        if s['dir'] == 'sell':
            if open_pair is None and len(pairs) < v2.MAX_PAIRS:
                open_pair = {'sell_bar': s['bar'], 'sell_time': s['time'], 'sell_px': s['px'],
                             'sell_sig': s['type']}
                s['used'] = 'open_pair'
            else:
                s['used'] = 'ignored_cap_or_open'
        else:
            if open_pair is not None and s['bar'] > open_pair['sell_bar']:
                open_pair.update({'cover_bar': s['bar'], 'cover_time': s['time'],
                                  'cover_px': s['px'], 'cover_method': s['type']})
                pairs.append(open_pair)
                s['used'] = 'cover_pair'
                open_pair = None
            else:
                s['used'] = 'unused_no_open'
    if open_pair is not None:
        sb = open_pair['sell_bar']
        if gate_bar is not None and gate_bar > sb:
            open_pair.update({'cover_bar': gate_bar, 'cover_time': day5['labels'][gate_bar],
                              'cover_px': float(c[gate_bar]), 'cover_method': 'unilateral_gate'})
        else:
            cb = idx_1450 if (idx_1450 is not None and idx_1450 > sb) else day5['n'] - 1
            open_pair.update({'cover_bar': cb, 'cover_time': day5['labels'][cb],
                              'cover_px': float(c[cb]), 'cover_method': 'forced_1450'})
        pairs.append(open_pair)

    for p in pairs:
        gross = (p['sell_px'] - p['cover_px']) / p['sell_px']
        net = (p['sell_px'] * (1 - v2.FEE_SELL) - p['cover_px'] * (1 + v2.FEE_BUY)) / p['sell_px']
        p.update({'gross_pct': round(100 * gross, 4), 'net_pct': round(100 * net, 4),
                  'win': bool(net > 0), 'fake_cover': bool(p['cover_px'] > p['sell_px']),
                  'code': code, 'date': dt, 'day_type_posthoc': posthoc,
                  'day_chg_pct': round(100 * (c[-1] / prev_close - 1), 3) if prev_close else None,
                  'f1': bool(f1[p['sell_bar']]),
                  'chg': float(chg[p['sell_bar']]),
                  'f2': bool(chg[p['sell_bar']] <= F2_THRESH),
                  'f3': bool(f3 == 'trend_dn'), 'f3_regime': f3})
        v2.check('cost_nonnegative_pair', net <= gross + 1e-12, f'{code} {dt} net>gross')

    buy_bars = [s['bar'] for s in sigs if s['dir'] == 'buy' and not s['vetoed']]
    day_rec = {'code': code, 'date': dt, 'gate_bar': gate_bar, 'posthoc': posthoc,
               'prev_close': prev_close, 'closes': c, 'labels': day5['labels'], 'n5': day5['n'],
               'idx_1430': v2.find_label_idx(day5, v2.NO_NEW_AFTER),
               'idx_1450': idx_1450, 'buy_bars': buy_bars, 'f3': f3,
               'f1': f1, 'chg': chg}
    return sigs, pairs, day_rec


# ---------------- 同格匹配基线 ----------------

def _net(sell_px, cover_px):
    return 100 * (sell_px * (1 - v2.FEE_SELL) - cover_px * (1 + v2.FEE_BUY)) / sell_px


def _cover_for(rec, sb):
    """与策略完全相同的回补路径：首个低吸信号价 → 门控价 → 14:50/末根。"""
    c = rec['closes']
    nxt = [b for b in rec['buy_bars'] if b > sb]
    if nxt:
        return float(c[nxt[0]]), 'buy_signal'
    gb = rec['gate_bar']
    if gb is not None and gb > sb:
        return float(c[gb]), 'unilateral_gate'
    cb = rec['idx_1450'] if (rec['idx_1450'] is not None and rec['idx_1450'] > sb) else rec['n5'] - 1
    return float(c[cb]), 'forced_1450'


def matched_baseline(cells, pred, n_mc=N_MC, seed=MC_SEED):
    """cells = [(day_rec, n_pairs_in_cell)]，在**同格**且 pred 为真的 bar 上随机取卖出时点。

    每个单元抽 **n_pairs_in_cell** 根（与策略在该单元的 pair 数一一对应），避免
    "策略 2 对 vs 基线 1 对"的不对称稀释。返回 (per_date_mean_net, n_cells)。
    """
    rng = random.Random(seed)
    elig = {}
    for rec, npairs in cells:
        bars = [k for k in range(v2.WARMUP5, (rec['idx_1430'] or -1) + 1) if pred(rec, k)]
        if bars:
            elig[(rec['code'], rec['date'])] = (rec, bars, max(1, int(npairs)))
    if not elig:
        return {}, 0
    acc = {}
    for _ in range(n_mc):
        for (code, date), (rec, bars, npairs) in elig.items():
            for _j in range(npairs):
                sb = rng.choice(bars)
                cover_px, _m = _cover_for(rec, sb)
                acc.setdefault(date, []).append(_net(float(rec['closes'][sb]), cover_px))
    return {d: float(np.mean(v)) for d, v in acc.items()}, len(elig)


# ---------------- 统计 ----------------

def strat_by_date(pairs):
    acc = {}
    for p in pairs:
        acc.setdefault(p['date'], []).append(p['net_pct'])
    return {d: float(np.mean(v)) for d, v in acc.items()}


def block_bootstrap(deltas, n_boot=N_BOOT, seed=BOOT_SEED):
    """对逐日 Δ 做日期分块 bootstrap → 95% CI（股票·日横截面相关，朴素 t 会虚高）。"""
    if len(deltas) < 5:
        return None
    rng = random.Random(seed)
    arr = np.array(deltas, dtype=float)
    n = len(arr)
    means = np.array([float(np.mean(arr[np.random.RandomState(rng.randint(0, 2**31 - 1)).randint(0, n, n)]))
                      for _ in range(n_boot)])
    return {'mean': round(float(arr.mean()), 4),
            'ci_lo': round(float(np.percentile(means, 2.5)), 4),
            'ci_hi': round(float(np.percentile(means, 97.5)), 4),
            'p_gt_0': round(float((means > 0).mean()), 4), 'n_dates': n}


def agg_v3(pairs):
    a = v2.agg_pairs(pairs) if pairs else {'n': 0}
    if pairs:
        v = np.array([p['net_pct'] for p in pairs], dtype=float)
        a['median_net_pct'] = round(float(np.median(v)), 4)
        sv = np.sort(v)
        k = int(len(sv) * 0.05)
        core = sv[k:len(sv) - k] if (k > 0 and len(sv) - 2 * k >= 1) else sv
        a['trimmed_mean_pct'] = round(float(np.mean(core)), 4)
        pos = {}
        for p in pairs:
            pos.setdefault(p['code'], []).append(p['net_pct'])
        a['n_stocks'] = len(pos)
        a['n_stocks_pos'] = sum(1 for v2_ in pos.values() if np.mean(v2_) > 0)
    return a


# ---------------- 主流程 ----------------

def evaluate_set(all_pairs, day_recs, label, n_mc=N_MC, f2_thresh=None, only=None):
    """对每个预注册子集：策略汇总 + 同格匹配基线 + Δ + 日期分块 bootstrap CI。"""
    by_cell = {(r['code'], r['date']): r for r in day_recs}
    res = {}
    for name in (only or SUBSETS):
        pred = subset_pred(name, f2_thresh)
        sub = [p for p in all_pairs if _pair_in(name, p, f2_thresh)]
        per_cell = {}
        for p in sub:
            per_cell.setdefault((p['code'], p['date']), 0)
            per_cell[(p['code'], p['date'])] += 1
        cells = [(by_cell[k], n) for k, n in per_cell.items() if k in by_cell]
        base_by_date, n_cells = matched_baseline(cells, pred, n_mc=n_mc) if cells else ({}, 0)
        s_by_date = strat_by_date(sub)
        common = sorted(set(s_by_date) & set(base_by_date))
        deltas = [s_by_date[d] - base_by_date[d] for d in common]
        s_mean = float(np.mean(list(s_by_date.values()))) if s_by_date else None
        b_mean = float(np.mean(list(base_by_date.values()))) if base_by_date else None
        res[name] = {
            'strategy': agg_v3(sub),
            'baseline_matched_mean_pct': round(b_mean, 4) if b_mean is not None else None,
            'baseline_by_date': {k: round(v, 4) for k, v in base_by_date.items()},
            'n_matched_cells': n_cells,
            'delta_pp': round(s_mean - b_mean, 4) if (s_mean is not None and b_mean is not None) else None,
            'bootstrap': block_bootstrap(deltas),
        }
    return res


def _pair_in(name, p, f2_thresh=None):
    th = F2_THRESH if f2_thresh is None else f2_thresh
    f2 = p.get('chg', 0.0) <= th
    if name == 'ALL':
        return True
    if name == 'F1':
        return bool(p.get('f1'))
    if name == 'F2':
        return bool(f2)
    if name == 'F3':
        return bool(p.get('f3'))
    if name == 'F1F2':
        return bool(p.get('f1') and f2)
    if name == 'F1F2F3':
        return bool(p.get('f1') and f2 and p.get('f3'))
    if name == 'V2GATE':
        return bool(p.get('v2gate'))
    return False


def collect(codes, start, end):
    all_sigs, all_pairs, day_recs, day_bars = [], [], [], {}
    idx_reg = load_index_regime()
    per_code_days, skipped = {}, []
    for code in codes:
        dates, merged, src = v2.merge_days(code)
        prev_close = None
        for dt in dates:
            if dt < start or dt > end:
                bars = merged[dt]
                prev_close = float(bars[-1]['c'])
                continue
            bars = merged[dt]
            if len(bars) < v2.MIN_1M_BARS:
                skipped.append({'code': code, 'date': dt, 'reason': f'1m={len(bars)}'})
                prev_close = float(bars[-1]['c'])
                continue
            day5 = v2.agg5(bars)
            if day5['n'] < v2.MIN_5M_BARS:
                skipped.append({'code': code, 'date': dt, 'reason': f"5m={day5['n']}"})
                prev_close = float(bars[-1]['c'])
                continue
            f3 = resolve_regime(code, dt, idx_reg)
            sigs, pairs, rec = run_day_v3(code, dt, day5, prev_close, f3)
            for s in sigs:
                s.update({'code': code, 'date': dt, 'day_type_posthoc': rec['posthoc']})
            gb = rec['gate_bar']
            for p in pairs:
                p['v2gate'] = gb is not None and p['sell_bar'] >= gb
            all_sigs.extend(sigs)
            all_pairs.extend(pairs)
            day_recs.append(rec)
            day_bars[(code, dt)] = bars
            per_code_days[code] = per_code_days.get(code, 0) + 1
            prev_close = float(bars[-1]['c'])
    return all_sigs, all_pairs, day_recs, day_bars, per_code_days, skipped


def judge(res):
    """按预注册四条线判定主终点 H1（F1F2 子集）。"""
    h1 = res.get('F1F2', {})
    s, b, boot = h1.get('strategy', {}), h1.get('bootstrap') or {}, h1.get('delta_pp')
    n = s.get('n', 0)
    lines = {
        'n>=100': n >= 100,
        'delta>=0.10pp': (h1.get('delta_pp') is not None and h1['delta_pp'] >= 0.10),
        'ci_lo>0': bool(b and b.get('ci_lo', -1) > 0),
        'win>=55%_and_R+5pp': bool(s.get('win_rate', 0) >= 0.55),
        'median>0': bool(s.get('median_net_pct', -1) > 0),
        'stocks>=1/3': bool(s.get('n_stocks') and s.get('n_stocks_pos', 0) >= s['n_stocks'] / 3),
    }
    return {'lines': lines, 'pass_all': all(lines.values()),
            'note': 'n<100 仅探索性' if n < 100 else ''}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None, help='逗号分隔；缺省=全部 39 票')
    ap.add_argument('--start', default='2025-09-14')
    ap.add_argument('--end', default='2026-08-26')
    ap.add_argument('--mc', type=int, default=N_MC)
    ap.add_argument('--out', default=os.path.join(OUT, 'results_v3_2026-09-14.json'))
    args = ap.parse_args()

    codes = args.codes.split(',') if args.codes else discover_codes()
    # 收窄 v2.END：避免 merge_days 读取仍在被实盘写入的当日快照（并发截断风险），并省去无用 IO
    v2.END = args.end
    print(f'[v3] codes={len(codes)} start={args.start} end={args.end} mc={args.mc}')

    all_sigs, all_pairs, day_recs, day_bars, per_code_days, skipped = collect(codes, args.start, args.end)
    print(f'[v3] sigs={len(all_sigs)} pairs={len(all_pairs)} days={len(day_recs)} skipped={len(skipped)}')

    n_samp, fails = v2.lookahead_audit(day_bars, [s for s in all_sigs if s['dir'] == 'sell'])

    res = evaluate_set(all_pairs, day_recs, 'v3', n_mc=args.mc)

    out = {'meta': {'version': 'v3', 'codes': codes, 'n_codes': len(codes),
                    'start': args.start, 'end': args.end, 'n_mc': args.mc,
                    'f2_thresh': F2_THRESH, 'n_stock_days': sum(per_code_days.values())},
           'assertions': v2.ASSERTS,
           'data': {'per_code_days': per_code_days, 'skipped': skipped[:20]},
           'signal_stats': {'n_sigs': len(all_sigs),
                            'n_sell': sum(1 for s in all_sigs if s['dir'] == 'sell'),
                            'n_vetoed': sum(1 for s in all_sigs if s['vetoed'])},
           'lookahead': {'n_sampled': n_samp, 'fails': fails[:5], 'ok': not fails},
           'pairs': all_pairs, 'results': res, 'judge': judge(res)}
    json.dump(out, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'[v3] -> {args.out}')

    for name in SUBSETS:
        r = res.get(name, {})
        s = r.get('strategy', {})
        print(f"  {name:8} n={s.get('n', 0):>4} avg={s.get('avg_net_pct')} "
              f"win={s.get('win_rate')} med={s.get('median_net_pct')} "
              f"delta={r.get('delta_pp')} cells={r.get('n_matched_cells')}")
    print('[v3] judge:', json.dumps(out['judge'], ensure_ascii=False))


if __name__ == '__main__':
    main()
