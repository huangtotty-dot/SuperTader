# -*- coding: utf-8 -*-
"""A10 ETF 载体验证实验 · 主分析（实验员_E2，2026-09-15）。

假说：「成本是那堵墙 → 降墙」。载体：恒生科技ETF 513130/513180（真T+0、免印花税、
佣金万0.5 双边≈0.01%）。对照虚拟股票成本（卖0.121%+买0.015%=双边0.136%，同 v2 口径）。

三部分：
 1. 品种画像：513130/513180 vs 5 只股票池（000988/002451/002639/300054/603667），
    同一窗口 2026-03-23~2026-09-15（gm 基金权限只能取 180 自然日内的 1min）。
 2. 网格策略（昨收 ±1% 固定锚，触格买卖，真T+0 当日往返，强制当日平仓）。
 3. B7 隔夜反T（尾盘30min急拉>1% → 14:55 信号根收盘卖 → 次日开盘接回）。
每个策略同一信号序列记两套账（ETF 成本 / 股票成本）+ 随机基线(MC) + 持有不动。

无前视：B7 信号只用 ≤14:55 数据；截断重跑验证。网格逐根顺序扫描，
出场根严格在入场根之后（同根不出场）。gm 拉数据见 fetch_data.py（用户 Python）。

运行：python etf_carrier_analysis.py   （managed python）
"""
import sys, os, json, glob, random
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import pandas as pd

ROOT = r'E:\superTrader'
ETF_DIR = os.path.join(ROOT, 't_io', 'validation', 'etf_carrier')
DATA_DIR = os.path.join(ETF_DIR, 'data')
STK_DIR = os.path.join(ROOT, 't_io', 'backtest_1year_data')
OUT_JSON = os.path.join(ETF_DIR, 'results_etf_carrier_2026-09-15.json')

WINDOW_START = '2026-03-23'
WINDOW_END = '2026-09-15'
MIN_BARS = 200
MAIN = '513130'
ETFS = ['513130', '513180']
STOCKS = ['000988', '002451', '002639', '300054', '603667']

FEE_ETF = 0.00005           # 万0.5 单边（双边≈0.01%）
FEE_STK_SELL = 0.00121      # 同 v2：含印花税 0.1%
FEE_STK_BUY = 0.00015
ROUNDTRIP_ETF = 2 * FEE_ETF            # 0.0001 = 0.01%
ROUNDTRIP_STK = FEE_STK_SELL + FEE_STK_BUY  # 0.00136 = 0.136%

GRID_PCT = 0.01
GRID_MAX_PAIRS = 3
B7_TAIL_START, B7_TAIL_END = '14:30', '14:55'
B7_THRESHOLD = 0.01
N_MC, MC_SEED = 200, 42

ASSERTS = {}
def check(name, ok, detail=''):
    ASSERTS[name] = {'pass': bool(ok), 'detail': str(detail)}
    if not ok:
        print('  [断言失败]', name, detail)


# ---------------- 数据层 ----------------

def load_etf_days(code):
    df = pd.read_csv(os.path.join(DATA_DIR, code + '_1year_1min.csv'))
    df['date'] = df['time'].str[:10]
    df['t'] = df['time'].str[11:16]
    days = {}
    for dt, g in df.groupby('date'):
        g = g.sort_values('time')
        days[dt] = {'t': g['t'].tolist(), 'o': g['open'].to_numpy(float),
                    'h': g['high'].to_numpy(float), 'l': g['low'].to_numpy(float),
                    'c': g['close'].to_numpy(float), 'v': g['volume'].to_numpy(float),
                    'amt': g['amount'].to_numpy(float), 'n': len(g)}
    return days


def load_stock_days(code):
    fs = glob.glob(os.path.join(STK_DIR, code + '*1min.csv'))
    if not fs:
        return {}
    df = pd.read_csv(fs[0])
    df['date'] = df['time'].str[:10]
    df['t'] = df['time'].str[11:16]
    days = {}
    for dt, g in df.groupby('date'):
        if not (WINDOW_START <= dt <= WINDOW_END):
            continue
        g = g.sort_values('time')
        days[dt] = {'t': g['t'].tolist(), 'o': g['open'].to_numpy(float),
                    'h': g['high'].to_numpy(float), 'l': g['low'].to_numpy(float),
                    'c': g['close'].to_numpy(float), 'v': g['volume'].to_numpy(float),
                    'amt': g['amount'].to_numpy(float), 'n': len(g)}
    return days


def day_stats(day, prev_close):
    h, l, c, amt = day['h'].max(), day['l'].min(), day['c'][-1], day['amt'].sum()
    amp = (h - l) / prev_close if prev_close else np.nan
    chg = c / prev_close - 1 if prev_close else np.nan
    return {'amp_pct': 100 * amp, 'chg_pct': 100 * chg, 'amt_yi': amt / 1e8,
            'high': h, 'low': l, 'close': c}


def intraday_structure(days, dates):
    """波动/成交额在 早盘(<=10:00) 午前(10:01-11:30) 午后早段(13:01-14:00) 尾盘(14:01-15:00) 的分布。"""
    buckets = [('open30', lambda t: t <= '10:00'),
               ('mid_am', lambda t: '10:00' < t <= '11:30'),
               ('early_pm', lambda t: '13:00' <= t <= '14:00'),
               ('tail', lambda t: t > '14:00')]
    agg = {k: {'amt': 0.0, 'range': 0.0} for k, _ in buckets}
    tot_amt = tot_rng = 0.0
    for dt in dates:
        d = days[dt]
        for k, f in buckets:
            idx = [i for i, t in enumerate(d['t']) if f(t)]
            if not idx:
                continue
            agg[k]['amt'] += d['amt'][idx].sum()
            agg[k]['range'] += (d['h'][idx].max() - d['l'][idx].min())
            tot_amt += 0  # placeholder
        tot_amt += d['amt'].sum()
        tot_rng += (d['h'].max() - d['l'].min())
    out = {}
    for k, _ in buckets:
        out[k] = {'amt_share': round(agg[k]['amt'] / tot_amt, 4) if tot_amt else None,
                  'range_share': round(agg[k]['range'] / tot_rng, 4) if tot_rng else None}
    return out


# ---------------- 网格策略（真T+0，当日往返） ----------------

def grid_day(day, prev_close, fee_buy, fee_sell):
    """固定锚 grid：L=昨收*0.99，M=昨收，U=昨收*1.01。
    空手时 low<=L → 买入价 L；持仓时（j>入场根）high>=M → 卖出价 M。
    同日可重复，最多 GRID_MAX_PAIRS 对；尾盘未平 → 15:00 根收盘价强制平仓。
    成交价取格价（触碰即成交，无滑点，偏乐观——在报告中声明）。
    返回 pairs，同时按传入费率计净收益；毛收益另存供双账簿换算。"""
    L, M = prev_close * (1 - GRID_PCT), prev_close
    c, h, l, ts, n = day['c'], day['h'], day['l'], day['t'], day['n']
    pairs, pos = [], None
    for i in range(n):
        if pos is None and len(pairs) < GRID_MAX_PAIRS:
            if l[i] <= L:
                pos = {'buy_bar': i, 'buy_t': ts[i], 'buy_px': L}
        elif pos is not None and i > pos['buy_bar']:
            if h[i] >= M:
                pairs.append({**pos, 'sell_bar': i, 'sell_t': ts[i], 'sell_px': M,
                              'forced': False})
                pos = None
    if pos is not None:
        pairs.append({**pos, 'sell_bar': n - 1, 'sell_t': ts[n - 1], 'sell_px': float(c[-1]),
                      'forced': True})
    for p in pairs:
        gross = (p['sell_px'] - p['buy_px']) / p['buy_px']
        net = (p['sell_px'] * (1 - fee_sell) - p['buy_px'] * (1 + fee_buy)) / p['buy_px']
        p.update({'gross_pct': round(100 * gross, 4), 'net_pct': round(100 * net, 4),
                  'win': bool(net > 0)})
        check('grid_fill_in_range_buy', p['buy_px'] >= day['l'][p['buy_bar']] - 1e-9,
              f"buy {p['buy_px']} < bar low {day['l'][p['buy_bar']]}")
        check('grid_fill_in_range_sell', p['sell_px'] <= day['h'][p['sell_bar']] + 1e-9,
              f"sell {p['sell_px']} > bar high {day['h'][p['sell_bar']]}")
        check('grid_exit_after_entry', p['sell_bar'] > p['buy_bar'], 'same-bar roundtrip')
        check('grid_net_le_gross', p['net_pct'] <= p['gross_pct'] + 1e-9, '')
    return pairs


# ---------------- B7 隔夜反T ----------------

def find_bar(day, label):
    """收盘标签口径：返回标签 <= label 的最后一根索引。"""
    idx = [i for i, t in enumerate(day['t']) if t <= label]
    return max(idx) if idx else None


def b7_signals(days, dates, threshold=B7_THRESHOLD):
    """尾盘30min急拉：close(14:55)/close(14:30)-1 > threshold → 14:55根收盘卖，次日首根 open 接回。
    信号只用 <=14:55 数据。返回 pairs（含毛收益），费率在外层套两本账。"""
    pairs = []
    for k in range(len(dates) - 1):
        dt, ndt = dates[k], dates[k + 1]
        d, nd = days[dt], days[ndt]
        i30, i55 = find_bar(d, B7_TAIL_START), find_bar(d, B7_TAIL_END)
        if i30 is None or i55 is None or i55 <= i30:
            continue
        tail_ret = d['c'][i55] / d['c'][i30] - 1
        if tail_ret <= threshold:
            continue
        sell_px, buy_px = float(d['c'][i55]), float(nd['o'][0])
        gross = (sell_px - buy_px) / sell_px
        pairs.append({'date': dt, 'next_date': ndt, 'tail_ret_pct': round(100 * tail_ret, 3),
                      'sell_t': d['t'][i55], 'sell_px': sell_px,
                      'buy_px': buy_px, 'gross_pct': round(100 * gross, 4)})
    return pairs


def tail_ret_dist(days, dates):
    """全部交易日的尾盘30min收益分布（阈值合理性/敏感性证据）。"""
    rets = []
    for k in range(len(dates) - 1):
        d = days[dates[k]]
        i30, i55 = find_bar(d, B7_TAIL_START), find_bar(d, B7_TAIL_END)
        if i30 is None or i55 is None or i55 <= i30:
            continue
        rets.append(100 * (d['c'][i55] / d['c'][i30] - 1))
    a = np.array(rets)
    return {'n_days': len(rets),
            'max_pct': round(float(a.max()), 3), 'min_pct': round(float(a.min()), 3),
            'p90_pct': round(float(np.percentile(a, 90)), 3),
            'p99_pct': round(float(np.percentile(a, 99)), 3),
            'n_gt_1pct': int((a > 1).sum()), 'n_gt_0.5pct': int((a > 0.5).sum())}


def net_of(gross_pct, fee_buy, fee_sell):
    """由毛收益精确重算净收益（与 fill 价路径无关的近似：净 = 毛 - 双边费率，一阶）。
    为精确，用 (1+g)(1-fs)/(1+fb)-1 形式对 sell-then-buy（反T）与 buy-then-sell（网格）分别算，
    这里统一在策略内已算好 net，本函数仅用于基线。"""
    pass


def apply_book(pairs, fee_buy, fee_sell, direction):
    """同一信号序列套一本费率账。direction='buy_first'(网格) 或 'sell_first'(B7)。
    需要原价：网格对存 buy_px/sell_px；B7 对存 sell_px/buy_px。"""
    out = []
    for p in pairs:
        q = dict(p)
        if direction == 'buy_first':
            net = (p['sell_px'] * (1 - fee_sell) - p['buy_px'] * (1 + fee_buy)) / p['buy_px']
        else:
            net = (p['sell_px'] * (1 - fee_sell) - p['buy_px'] * (1 + fee_buy)) / p['sell_px']
        q['net_pct'] = round(100 * net, 4)
        q['win'] = bool(net > 0)
        out.append(q)
    return out


def agg_pairs(pairs):
    if not pairs:
        return {'n': 0}
    n = len(pairs)
    return {'n': n,
            'avg_gross_pct': round(sum(p['gross_pct'] for p in pairs) / n, 4),
            'avg_net_pct': round(sum(p['net_pct'] for p in pairs) / n, 4),
            'total_net_pct': round(sum(p['net_pct'] for p in pairs), 3),
            'win_rate': round(sum(p['win'] for p in pairs) / n, 4)}


# ---------------- 随机基线 ----------------

def random_baseline_grid(days, dates, n_mc=N_MC, seed=MC_SEED):
    """每日随机 1 对当日往返：入场根 ∈ [10:00, 14:00] 收盘买，出场根为之后随机根收盘卖。
    两套费率账。返回 {'etf': band, 'stk': band}。"""
    rng = random.Random(seed)
    res = {'etf': [], 'stk': []}
    for _ in range(n_mc):
        for book, fb, fs in (('etf', FEE_ETF, FEE_ETF), ('stk', FEE_STK_BUY, FEE_STK_SELL)):
            pairs = []
            for dt in dates:
                d = days[dt]
                idxs = [i for i, t in enumerate(d['t']) if '10:00' <= t <= '14:00']
                if len(idxs) < 5:
                    continue
                eb = rng.choice(idxs)
                xb = rng.randint(eb + 1, d['n'] - 1)
                bp, sp = d['c'][eb], d['c'][xb]
                gross = (sp - bp) / bp
                net = (sp * (1 - fs) - bp * (1 + fb)) / bp
                pairs.append({'gross_pct': 100 * gross, 'net_pct': 100 * net, 'win': net > 0})
            res[book].append(agg_pairs(pairs))
    return {b: band(res[b]) for b in res}


def random_baseline_b7(days, dates, n_signals, n_mc=N_MC, seed=MC_SEED):
    """随机抽同样多的日子做「14:55收盘卖→次日开盘接回」，两套费率账。"""
    rng = random.Random(seed + 1)
    res = {'etf': [], 'stk': []}
    for _ in range(n_mc):
        for book, fb, fs in (('etf', FEE_ETF, FEE_ETF), ('stk', FEE_STK_BUY, FEE_STK_SELL)):
            sample = rng.sample(range(len(dates) - 1), min(n_signals, len(dates) - 1))
            pairs = []
            for k in sample:
                d, nd = days[dates[k]], days[dates[k + 1]]
                i55 = find_bar(d, B7_TAIL_END)
                if i55 is None:
                    continue
                sp, bp = float(d['c'][i55]), float(nd['o'][0])
                gross = (sp - bp) / sp
                net = (sp * (1 - fs) - bp * (1 + fb)) / sp
                pairs.append({'gross_pct': 100 * gross, 'net_pct': 100 * net, 'win': net > 0})
            res[book].append(agg_pairs(pairs))
    return {b: band(res[b]) for b in res}


def band(ms):
    out = {}
    for k in ('n', 'avg_gross_pct', 'avg_net_pct', 'win_rate'):
        vals = [m[k] for m in ms if k in m and m[k] is not None]
        if vals:
            out[k] = {'mean': round(float(np.mean(vals)), 4),
                      'std': round(float(np.std(vals)), 4)}
    return out


# ---------------- 无前视截断验证（B7） ----------------

def lookahead_audit_b7(days, dates, b7_pairs):
    """把每个信号日截断到 14:55 根，重算 tail_ret，必须仍触发（且信号根价一致）。"""
    sig_dates = {p['date'] for p in b7_pairs}
    fails = []
    for dt in sig_dates:
        d = days[dt]
        i55 = find_bar(d, B7_TAIL_END)
        i30 = find_bar(d, B7_TAIL_START)
        # 截断：只保留 <=i55 的根，重算
        c_cut = d['c'][:i55 + 1]
        tail_ret = c_cut[i55] / c_cut[i30] - 1
        if not (tail_ret > B7_THRESHOLD):
            fails.append(dt)
    check('b7_no_future_truncation', not fails, f'fails={fails[:5]}')
    return len(sig_dates), fails


# ---------------- 主流程 ----------------

def profile(code, days, label):
    dates = sorted(d for d in days if WINDOW_START <= d <= WINDOW_END and days[d]['n'] >= MIN_BARS)
    stats, prev = [], None
    for dt in dates:
        if prev is None:
            prev = days[dt]['c'][-1]
            continue
        s = day_stats(days[dt], prev)
        s['date'] = dt
        stats.append(s)
        prev = days[dt]['c'][-1]
    amps = np.array([s['amp_pct'] for s in stats])
    amts = np.array([s['amt_yi'] for s in stats])
    chgs = np.array([s['chg_pct'] for s in stats])
    prof = {
        'label': label, 'code': code, 'n_days': len(stats),
        'amp_median_pct': round(float(np.median(amps)), 3),
        'amp_mean_pct': round(float(amps.mean()), 3),
        'amp_p25_pct': round(float(np.percentile(amps, 25)), 3),
        'amp_p75_pct': round(float(np.percentile(amps, 75)), 3),
        'amp_ge_2pct_day_share': round(float((amps >= 2).mean()), 4),
        'chg_abs_median_pct': round(float(np.median(np.abs(chgs))), 3),
        'amt_median_yi': round(float(np.median(amts)), 2),
        'amt_p10_yi': round(float(np.percentile(amts, 10)), 2),
        'amt_p90_yi': round(float(np.percentile(amts, 90)), 2),
        'intraday_structure': intraday_structure(days, dates),
        'dates': dates,
    }
    return prof, dates, stats


def main():
    print('== 数据加载 ==')
    etf_days = {c: load_etf_days(c) for c in ETFS}
    stk_days = {c: load_stock_days(c) for c in STOCKS}
    for c in ETFS + STOCKS:
        d = etf_days.get(c) or stk_days.get(c)
        dates = [x for x in sorted(d) if WINDOW_START <= x <= WINDOW_END and d[x]['n'] >= MIN_BARS]
        print(f'  {c}: {len(dates)} 有效交易日')

    print('== 品种画像 ==')
    profiles = {}
    for c in ETFS:
        profiles[c], _, _ = profile(c, etf_days[c], '恒生科技ETF')
    for c in STOCKS:
        if stk_days[c]:
            profiles[c], _, _ = profile(c, stk_days[c], '股票池')

    # ---- 实验主载体 513130 ----
    days = etf_days[MAIN]
    dates = sorted(d for d in days if WINDOW_START <= d <= WINDOW_END and days[d]['n'] >= MIN_BARS)
    check('sample_days_ge_60', len(dates) >= 60, f'n={len(dates)}')

    print('== 网格策略（513130）==')
    grid_gross_pairs, prev = [], None
    valid_dates = []
    for dt in dates:
        if prev is None:
            prev = days[dt]['c'][-1]
            continue
        raw = grid_day(days[dt], prev, 0, 0)  # 先跑零费率拿 fill 价
        for p in raw:
            p['date'] = dt
        grid_gross_pairs.extend(raw)
        valid_dates.append(dt)
        prev = days[dt]['c'][-1]
    grid_etf = apply_book(grid_gross_pairs, FEE_ETF, FEE_ETF, 'buy_first')
    grid_stk = apply_book(grid_gross_pairs, FEE_STK_BUY, FEE_STK_SELL, 'buy_first')
    grid_days_used = len({p['date'] for p in grid_gross_pairs})
    forced_share = (sum(p['forced'] for p in grid_gross_pairs) / len(grid_gross_pairs)
                    if grid_gross_pairs else None)
    print(f'  触发对数={len(grid_gross_pairs)} 覆盖天数={grid_days_used}/{len(valid_dates)} '
          f'强平占比={forced_share:.2%}' if forced_share is not None else '  无触发')

    print('== B7 隔夜反T（513130）==')
    tail_dist = tail_ret_dist(days, dates)
    b7_raw = b7_signals(days, dates)
    n_sig, _ = lookahead_audit_b7(days, dates, b7_raw)
    print(f'  信号数={len(b7_raw)} (截断验证抽样 {n_sig})  尾盘收益分布={tail_dist}')
    b7_etf = apply_book(b7_raw, FEE_ETF, FEE_ETF, 'sell_first')
    b7_stk = apply_book(b7_raw, FEE_STK_BUY, FEE_STK_SELL, 'sell_first')
    # 敏感性：阈值降到 0.5%
    b7_raw_s = b7_signals(days, dates, threshold=0.005)
    b7_s_etf = apply_book(b7_raw_s, FEE_ETF, FEE_ETF, 'sell_first')
    b7_s_stk = apply_book(b7_raw_s, FEE_STK_BUY, FEE_STK_SELL, 'sell_first')

    print('== 基线 ==')
    rb_grid = random_baseline_grid(days, valid_dates)
    rb_b7 = random_baseline_b7(days, dates, len(b7_raw))
    first_dt, last_dt = dates[0], dates[-1]
    bh_ret = 100 * (days[last_dt]['c'][-1] / days[first_dt]['o'][0] - 1)
    bh = {'window': [first_dt, last_dt], 'buy_hold_pct': round(float(bh_ret), 3)}

    # 513180 稳健性（同口径简跑）
    print('== 513180 稳健性 ==')
    days2 = etf_days['513180']
    dates2 = sorted(d for d in days2 if WINDOW_START <= d <= WINDOW_END and days2[d]['n'] >= MIN_BARS)
    g2, prev = [], None
    vd2 = []
    for dt in dates2:
        if prev is None:
            prev = days2[dt]['c'][-1]
            continue
        raw = grid_day(days2[dt], prev, 0, 0)
        for p in raw:
            p['date'] = dt
        g2.extend(raw)
        vd2.append(dt)
        prev = days2[dt]['c'][-1]
    g2_etf = apply_book(g2, FEE_ETF, FEE_ETF, 'buy_first')
    g2_stk = apply_book(g2, FEE_STK_BUY, FEE_STK_SELL, 'buy_first')
    b7_2 = b7_signals(days2, dates2)
    b7_2_etf = apply_book(b7_2, FEE_ETF, FEE_ETF, 'sell_first')
    b7_2_stk = apply_book(b7_2, FEE_STK_BUY, FEE_STK_SELL, 'sell_first')

    # ---- 成本墙量化 ----
    def wall(raw, etf_book, stk_book):
        if not raw:
            return {}
        gm = np.mean([p['gross_pct'] for p in raw])
        ne = np.mean([p['net_pct'] for p in etf_book])
        ns = np.mean([p['net_pct'] for p in stk_book])
        return {'avg_gross_pct': round(float(gm), 4),
                'avg_net_etf_pct': round(float(ne), 4),
                'avg_net_stk_pct': round(float(ns), 4),
                'cost_drag_etf_pp': round(float(gm - ne), 4),
                'cost_drag_stk_pp': round(float(gm - ns), 4),
                'wall_height_pp': round(float(ne - ns), 4),
                'sign_flip_etf_vs_stk': bool((ne > 0) != (ns > 0))}

    results = {
        'meta': {
            'experiment': 'A10_ETF载体验证实验', 'date': '2026-09-15', 'owner': '实验员_E2',
            'hypothesis': '成本是那堵墙 → 降墙（免印花税+万0.5 ETF 载体是否让做T策略转正）',
            'window': [WINDOW_START, WINDOW_END],
            'window_note': 'gm 基金品种权限仅 180 自然日 1min（>=2026-03-19），实际 2026-03-23~2026-09-15',
            'main_carrier': MAIN, 'robustness_carrier': '513180',
            'stock_pool': STOCKS,
            'fees': {'etf_side': FEE_ETF, 'etf_roundtrip': ROUNDTRIP_ETF,
                     'stk_sell': FEE_STK_SELL, 'stk_buy': FEE_STK_BUY,
                     'stk_roundtrip': ROUNDTRIP_STK},
            'params': {'grid_pct': GRID_PCT, 'grid_max_pairs': GRID_MAX_PAIRS,
                       'grid_fill': '触格即成交于格价(无滑点,偏乐观)',
                       'b7_tail': [B7_TAIL_START, B7_TAIL_END], 'b7_threshold': B7_THRESHOLD,
                       'b7_sell': '14:55信号根收盘', 'b7_buyback': '次日首根open',
                       'n_mc': N_MC, 'mc_seed': MC_SEED},
            'data_source': {'etf_1min': 'gm SDK 前复权 60s (fetch_data.py, 用户Python)',
                            'stock_1min': 't_io/backtest_1year_data 既有CSV同窗口裁剪'},
        },
        'assertions': ASSERTS,
        'profiles': profiles,
        'grid_513130': {
            'n_pairs': len(grid_gross_pairs),
            'days_with_trade': grid_days_used, 'days_total': len(valid_dates),
            'day_coverage': round(grid_days_used / len(valid_dates), 4) if valid_dates else None,
            'forced_close_share': round(forced_share, 4) if forced_share is not None else None,
            'book_etf': agg_pairs(grid_etf), 'book_stk': agg_pairs(grid_stk),
            'cost_wall': wall(grid_gross_pairs, grid_etf, grid_stk),
            'random_baseline': rb_grid,
            'pairs': grid_etf,
        },
        'b7_513130': {
            'n_signals': len(b7_raw),
            'signal_rate_per_day': round(len(b7_raw) / max(1, len(dates) - 1), 4),
            'tail_ret_distribution': tail_dist,
            'book_etf': agg_pairs(b7_etf), 'book_stk': agg_pairs(b7_stk),
            'cost_wall': wall(b7_raw, b7_etf, b7_stk),
            'sensitivity_threshold_0.5pct': {
                'n_signals': len(b7_raw_s),
                'book_etf': agg_pairs(b7_s_etf), 'book_stk': agg_pairs(b7_s_stk),
                'cost_wall': wall(b7_raw_s, b7_s_etf, b7_s_stk)},
            'random_baseline': rb_b7,
            'pairs': b7_etf,
        },
        'buy_hold_513130': bh,
        'robustness_513180': {
            'grid': {'n_pairs': len(g2), 'book_etf': agg_pairs(g2_etf),
                     'book_stk': agg_pairs(g2_stk), 'cost_wall': wall(g2, g2_etf, g2_stk)},
            'b7': {'n_signals': len(b7_2), 'book_etf': agg_pairs(b7_2_etf),
                   'book_stk': agg_pairs(b7_2_stk), 'cost_wall': wall(b7_2, b7_2_etf, b7_2_stk)},
        },
        'iopv_gap': {
            'finding': 'gm SDK：IOPV 仅实时可得(current/subscribe 字段 iopv)，无历史 IOPV 序列；'
                       'history_1d 请求 iopv 字段被静默丢弃；get_fundamentals(fund) 对该基金返回空。',
            'evidence': ['data/iopv_probe.json', 'data/nav_probe.json'],
            'conclusion': '上线前必须解决的数据缺口：盘中溢价率监控需自行采集 IOPV 快照落盘，'
                          '或引入外部源（基金公司官网/交易所/东财）。',
        },
    }
    json.dump(results, open(OUT_JSON, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('saved:', OUT_JSON)

    print('\n== 核心数字 ==')
    for c in [MAIN, '513180'] + STOCKS:
        p = profiles.get(c)
        if p and p.get('n_days'):
            print(f"  {c}: 振幅中位={p['amp_median_pct']}% ≥2%日占比={p['amp_ge_2pct_day_share']:.0%} "
                  f"额中位={p['amt_median_yi']}亿 n={p['n_days']}")
    print('网格513130:', json.dumps(results['grid_513130']['cost_wall'], ensure_ascii=False),
          json.dumps(results['grid_513130']['book_etf'], ensure_ascii=False))
    print('网格随机基线:', json.dumps(rb_grid, ensure_ascii=False))
    print('B7 513130:', json.dumps(results['b7_513130']['cost_wall'], ensure_ascii=False),
          json.dumps(results['b7_513130']['book_etf'], ensure_ascii=False))
    print('B7敏感性(0.5%):', json.dumps(results['b7_513130']['sensitivity_threshold_0.5pct'],
                                       ensure_ascii=False))
    print('B7随机基线:', json.dumps(rb_b7, ensure_ascii=False))
    print('513180稳健性:', json.dumps(results['robustness_513180'], ensure_ascii=False)[:600])
    print('持有不动:', json.dumps(bh, ensure_ascii=False))
    bad = [k for k, v in ASSERTS.items() if not v['pass']]
    print('断言:', '全部通过' if not bad else f'失败={sorted(set(bad))}')


if __name__ == '__main__':
    main()
