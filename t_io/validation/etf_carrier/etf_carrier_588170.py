# -*- coding: utf-8 -*-
"""2-B' 588170 载体验证实验 · 主分析（实验员_E4，2026-09-15）。

假说：「低墙 × 高波」——A10 已否决 513130（振幅中位 1.84% 不动、无尾盘急拉生态、
网格毛收益为负、成本墙 0.126pp/往返）。588170（华夏上证科创板半导体材料设备主题ETF，
免印花税、佣金口径同 513130 万0.5、科创板 20% 涨跌幅高波）是生产账上唯一稳定盈利的
做T票。本脚本用与 A10 完全相同的框架对 588170 跑同一套验证。

数据层（与 A10 不同，本票走 minute_snapshots 缓存）：
  t_io/minute_snapshots/20**/(588170_YYYY-MM-DD.json)，241 个交易日覆盖
  2025-09-15 ~ 2026-09-15（12 个月，无需 gm 补数据）。清洗规则：
  1) 份额拆分：2026-07-06 发生 1:3 拆分（gm ADJUST_NONE vs ADJUST_PREV 实测，
     因子恒等于 1/3，证据 data/split_factor_probe.json）→ 2026-07-06 之前价格 ÷3；
  2) volume/amount 口径逐日判定（三段混合源，全部经 gm 日线逐日断言）：
     a) 内容形态：累计序列（volume 全天单调不减、午间走平，dec_share<0.05，
        2026-06-22~09-07 段为主）→ 先做一阶差分还原为逐根量额；
        逐根序列（dec_share≈0.5）→ 不动；
     b) 单位：以 gm 日线全历史（352 行，2025-04-08~2026-09-14，ADJUST_NONE，
        data/gm_daily_full.json）为权威参照，在 (vf,af)∈{1,100}×{1,0.01}
        中取对数误差最小组合（实测全部命中 (股,元) 或 (手,元)，百元假设被否）；
        完整交易日匹配误差 <5%（断言），截断日（末根<14:55）放宽至 20% 并标记；
        gm 参照缺失日（仅 2026-09-15）回退内部 ratio 规则并标记 fallback；
  3) 2026-09-15 的 13:00 bar 量额异常（约为全日 42%，疑似午间累计重复计数），
     该 bar 量额以 11:30/13:01 两根均值插补（仅影响当日成交额统计，价格不动）。
  部分交易日末根早于 15:00（最近源截到 14:5x）：网格强平用当日末根收盘价，
  B7/S4 要求 14:30/14:50/14:55 标签精确存在，缺失则跳过并计数。

三策略（同 A10 框架）：
  网格 ±1%（昨收锚，触格买卖，当日往返 ≤3 对，末根强平）；
  B7 隔夜反T（尾盘30min c14:55/c14:30 > 1% → 14:55 收盘卖 → 次日开盘接回）；
  S4 隔夜反T（最后5min c14:55/c14:50 > 1% → 同 B7 出场，B7 实验中 open 口径最强）。
  每个策略同一信号序列记两套账：ETF 双边 0.01%（万0.5×2）/ 虚拟股票双边 0.136%
  （卖0.121%+买0.015%，同 v2 口径）。对照：随机基线(MC200) + 持有不动。

无前视：B7/S4 信号只用 ≤14:55 数据；截断重跑验证。网格逐根顺序扫描，
出场根严格晚于入场根。

运行：python etf_carrier_588170.py   （managed python）
"""
import sys, os, json, glob, random
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import pandas as pd
from pathlib import Path

ROOT = r'E:\superTrader'
ETF_DIR = os.path.join(ROOT, 't_io', 'validation', 'etf_carrier')
DATA_DIR = os.path.join(ETF_DIR, 'data')
SNAP_DIR = os.path.join(ROOT, 't_io', 'minute_snapshots')
STK_DIR = os.path.join(ROOT, 't_io', 'backtest_1year_data')
CSV_588170 = os.path.join(DATA_DIR, '588170_1year_1min.csv')
QUALITY_JSON = os.path.join(DATA_DIR, 'data_quality_588170.json')
OUT_JSON = os.path.join(ETF_DIR, 'results_588170_2026-09-15.json')

WINDOW_START = '2025-09-15'
WINDOW_END = '2026-09-15'
A10_SUB_WINDOW = ('2026-03-24', '2026-09-15')   # 与 A10 报告对齐的子窗口
STK_WINDOW = ('2025-09-15', '2026-08-26')        # 股票池 CSV 与主窗口的重叠段
MIN_BARS = 200
MAIN = '588170'
STOCKS = ['000988', '002451', '002639', '300054', '603667']

SPLIT_DATE = '2026-07-06'
SPLIT_FACTOR = 3.0
ANOMALY_1300_DATE = '2026-09-15'

FEE_ETF = 0.00005           # 万0.5 单边（双边≈0.01%），588170 佣金口径同 513130
FEE_STK_SELL = 0.00121      # 同 v2：含印花税 0.1%
FEE_STK_BUY = 0.00015
ROUNDTRIP_ETF = 2 * FEE_ETF
ROUNDTRIP_STK = FEE_STK_SELL + FEE_STK_BUY

GRID_PCT = 0.01
GRID_MAX_PAIRS = 3
B7_TAIL_START, B7_TAIL_END = '14:30', '14:55'
S4_START, S4_END = '14:50', '14:55'
B7_THRESHOLD = 0.01
S4_THRESHOLD = 0.01
N_MC, MC_SEED = 200, 42

ASSERTS = {}
def check(name, ok, detail=''):
    ASSERTS[name] = {'pass': bool(ok), 'detail': str(detail)}
    if not ok:
        print('  [断言失败]', name, detail)


# ---------------- 数据层：快照清洗与落地 ----------------

GM_DAILY_JSON = os.path.join(DATA_DIR, 'gm_daily_full.json')


def load_gm_daily_ref():
    """gm 日线权威参照：date -> (close, volume股, amount元)。文件缺失返回 {}。"""
    if not os.path.exists(GM_DAILY_JSON):
        return {}
    rows = json.load(open(GM_DAILY_JSON, encoding='utf-8'))
    return {r[0]: (float(r[1]), float(r[2]), float(r[3])) for r in rows}


def decide_units(vol_field, amt_field, px, gm_ref, dt):
    """逐日判定 (vol_factor, amt_factor)。
    有 gm 参照：在 (1,1)/(100,1)/(1,0.01)/(100,0.01) 中取对数误差最小者，返回
    (vf, af, label, err)。无参照：回退内部 ratio 规则（r≈100 按最新源 (手,元) 处理
    并标记 fallback）。"""
    if dt in gm_ref:
        _, g_vol, g_amt = gm_ref[dt]
        best = None
        for vf, af, label in ((1, 1, 'gu_yuan'), (100, 1, 'shou_yuan'),
                              (1, 0.01, 'gu_baiyuan'), (100, 0.01, 'shou_baiyuan')):
            if g_vol <= 0 or g_amt <= 0:
                continue
            err = (abs(np.log(vol_field * vf / g_vol)) +
                   abs(np.log(amt_field * af / g_amt)))
            if best is None or err < best[3]:
                best = (vf, af, label, err)
        if best is not None:
            return best
    denom = px * vol_field
    r = amt_field / denom if denom > 0 else np.nan
    if 50 < r < 150:
        return 100, 1, 'shou_yuan_fallback', np.nan
    return 1, 1, 'gu_yuan_fallback', np.nan


def build_588170_csv():
    """读 minute_snapshots → 清洗（拆分/累计差分/单位/13:00异常）→ 落 CSV（schema 同 A10）。
    返回 (days, quality)。"""
    files = sorted(Path(SNAP_DIR).rglob('588170_*.json'))
    assert files, 'minute_snapshots 中无 588170 数据'
    gm_ref = load_gm_daily_ref()
    frames, quality = [], {'n_files': len(files), 'days': [], 'unit_counts': {},
                           'anomaly_fixes': [], 'low_bar_days': [],
                           'gm_ref_days': len(gm_ref), 'unit_match_max_err_full': None}
    max_err_full = 0.0
    for f in files:
        d = json.load(open(f, encoding='utf-8'))
        bars = d['bars']
        dt = f.stem.split('_')[1]
        g = pd.DataFrame(bars)[['time', 'open', 'high', 'low', 'close', 'volume', 'amount']]
        g = g.drop_duplicates(subset=['time']).sort_values('time').reset_index(drop=True)
        for c in ('open', 'high', 'low', 'close', 'volume', 'amount'):
            g[c] = g[c].astype(float)
        ts = g['time'].str[11:16].tolist()
        # (a) 内容形态：累计序列 → 一阶差分还原逐根（午间走平天然给出 13:00=0）
        dec_share = float((g['volume'].diff().iloc[1:] < 0).mean()) if len(g) > 2 else 1.0
        is_cum = dec_share < 0.05
        if is_cum:
            dec_amt = float((g['amount'].diff().iloc[1:] < 0).mean())
            check('cumulative_amount_monotonic', dec_amt < 0.05,
                  f'{dt} amount dec_share={dec_amt:.2f}')
            g['volume'] = g['volume'].diff().fillna(g['volume']).clip(lower=0)
            g['amount'] = g['amount'].diff().fillna(g['amount']).clip(lower=0)
        # (b) 13:00 bar 量额异常插补（逐根源 2026-09-15 实测命中，通用规则保留）
        if '13:00' in ts and len(g) >= 100 and not is_cum:
            i = ts.index('13:00')
            med = float(g['volume'].median())
            if g.loc[i, 'volume'] > 10 * med and '11:30' in ts and '13:01' in ts:
                i0, i1 = ts.index('11:30'), ts.index('13:01')
                g.loc[i, 'volume'] = (g.loc[i0, 'volume'] + g.loc[i1, 'volume']) / 2
                g.loc[i, 'amount'] = (g.loc[i0, 'amount'] + g.loc[i1, 'amount']) / 2
                quality['anomaly_fixes'].append({'date': dt, 'fix': '13:00 bar 量额插补为 11:30/13:01 均值'})
        # (c) 单位判定（gm 日线参照优先，作用于还原后的全日合计）
        vol_field = float(g['volume'].sum())
        amt_field = float(g['amount'].sum())
        px = float(g['close'].mean())
        vf, af, unit_label, err = decide_units(vol_field, amt_field, px, gm_ref, dt)
        g['volume'] = g['volume'] * vf
        g['amount'] = g['amount'] * af
        unit_label = ('cum+' if is_cum else '') + unit_label
        quality['unit_counts'][unit_label] = quality['unit_counts'].get(unit_label, 0) + 1
        truncated = (ts[-1] < '15:00') if ts else True
        if err == err and len(g) >= MIN_BARS:  # NaN 或低根数排除日不参与单位断言
            if not truncated:
                max_err_full = max(max_err_full, float(err))
                if err > 0.05:
                    check('unit_match_err_full_day_lt_5pct', False,
                          f'{dt} {unit_label} err={err:.3f}')
            elif err > 0.35:
                # 截断日量额系统性短缺（末根越早缺越多：14:24→0.26, 14:42→0.28, 14:57→0.06），
                # 属预期内偏差而非单位误判；完整日已由 5% 断言锁死单位正确性。
                check('unit_match_err_trunc_day_explained', False,
                      f'{dt} {unit_label} err={err:.3f} 超出截断可解释范围')
        # (d) 拆分调整：2026-07-06 之前价格 ÷3（gm 实测因子恒等 1/3）
        if dt < SPLIT_DATE:
            for c in ('open', 'high', 'low', 'close'):
                g[c] = g[c] / SPLIT_FACTOR
        quality['days'].append({'date': dt, 'n_bars': len(g),
                                'last_bar': ts[-1] if ts else None, 'unit': unit_label,
                                'unit_err': round(float(err), 4) if err == err else None})
        if len(g) < MIN_BARS:
            quality['low_bar_days'].append({'date': dt, 'n_bars': len(g)})
        frames.append(g.assign(date=dt))
    quality['unit_match_max_err_full'] = round(max_err_full, 4)
    alldf = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        'ts_code': '588170.SH', 'time': alldf['time'],
        'close': alldf['close'], 'open': alldf['open'],
        'high': alldf['high'], 'low': alldf['low'],
        'volume': alldf['volume'], 'amount': alldf['amount'],
    })
    out.to_csv(CSV_588170, index=False, encoding='utf-8')
    json.dump(quality, open(QUALITY_JSON, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    # 连续性断言：调整后隔夜跳空不应再有 >21% 的假缺口（20% 涨跌停 + 缓冲）
    days = load_etf_days()
    dts = sorted(days)
    bad = []
    for k in range(1, len(dts)):
        g = days[dts[k]]['o'][0] / days[dts[k - 1]]['c'][-1] - 1
        if abs(g) > 0.21:
            bad.append((dts[k], round(100 * g, 2)))
    check('no_artificial_gap_after_split_adj', not bad, f'bad={bad[:5]}')
    return days, quality


def load_etf_days():
    df = pd.read_csv(CSV_588170)
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


def load_stock_days(code, win):
    fs = glob.glob(os.path.join(STK_DIR, code + '*1min.csv'))
    if not fs:
        return {}
    df = pd.read_csv(fs[0])
    df['date'] = df['time'].str[:10]
    df['t'] = df['time'].str[11:16]
    days = {}
    for dt, g in df.groupby('date'):
        if not (win[0] <= dt <= win[1]):
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
        tot_amt += d['amt'].sum()
        tot_rng += (d['h'].max() - d['l'].min())
    out = {}
    for k, _ in buckets:
        out[k] = {'amt_share': round(agg[k]['amt'] / tot_amt, 4) if tot_amt else None,
                  'range_share': round(agg[k]['range'] / tot_rng, 4) if tot_rng else None}
    return out


# ---------------- 网格策略（ETF 真T+0，当日往返） ----------------

def grid_day(day, prev_close):
    """固定锚 grid：L=昨收*0.99，M=昨收。空手 low<=L → 买于 L；持仓（j>入场根）
    high>=M → 卖于 M。同日最多 GRID_MAX_PAIRS 对；尾盘未平 → 当日末根收盘强平。
    触格即成交于格价（无滑点，偏乐观，报告声明）。"""
    L, M = prev_close * (1 - GRID_PCT), prev_close
    c, h, l, ts, n = day['c'], day['h'], day['l'], day['t'], day['n']
    pairs, pos = [], None
    for i in range(n):
        if pos is None and len(pairs) < GRID_MAX_PAIRS:
            if l[i] <= L:
                pos = {'buy_bar': i, 'buy_t': ts[i], 'buy_px': float(L)}
        elif pos is not None and i > pos['buy_bar']:
            if h[i] >= M:
                pairs.append({**pos, 'sell_bar': i, 'sell_t': ts[i], 'sell_px': float(M),
                              'forced': False})
                pos = None
    if pos is not None:
        pairs.append({**pos, 'sell_bar': n - 1, 'sell_t': ts[n - 1], 'sell_px': float(c[-1]),
                      'forced': True})
    for p in pairs:
        gross = (p['sell_px'] - p['buy_px']) / p['buy_px']
        p.update({'gross_pct': round(100 * gross, 4)})
        check('grid_fill_in_range_buy', p['buy_px'] >= day['l'][p['buy_bar']] - 1e-9,
              f"buy {p['buy_px']} < bar low {day['l'][p['buy_bar']]}")
        check('grid_fill_in_range_sell', p['sell_px'] <= day['h'][p['sell_bar']] + 1e-9,
              f"sell {p['sell_px']} > bar high {day['h'][p['sell_bar']]}")
        check('grid_exit_after_entry', p['sell_bar'] > p['buy_bar'], 'same-bar roundtrip')
    return pairs


# ---------------- B7 / S4 隔夜反T ----------------

def find_bar_exact(day, label):
    """严格口径：标签精确存在才返回索引，否则 None（不允许回退到更早的根，
    防止截断日污染尾盘信号）。"""
    for i, t in enumerate(day['t']):
        if t == label:
            return i
    return None


def tail_signals(days, dates, start_label, end_label, threshold, tag):
    """尾盘急拉：close(end)/close(start)-1 > threshold → end 根收盘卖，次日首根 open 接回。
    信号只用 <=end 根数据。返回 (pairs, skipped_days)。"""
    pairs, skipped = [], []
    for k in range(len(dates) - 1):
        dt, ndt = dates[k], dates[k + 1]
        d, nd = days[dt], days[ndt]
        i_s, i_e = find_bar_exact(d, start_label), find_bar_exact(d, end_label)
        if i_s is None or i_e is None or i_e <= i_s:
            skipped.append(dt)
            continue
        tail_ret = d['c'][i_e] / d['c'][i_s] - 1
        if tail_ret <= threshold:
            continue
        sell_px, buy_px = float(d['c'][i_e]), float(nd['o'][0])
        gross = (sell_px - buy_px) / sell_px
        pairs.append({'date': dt, 'next_date': ndt, 'variant': tag,
                      'tail_ret_pct': round(100 * tail_ret, 3),
                      'sell_t': d['t'][i_e], 'sell_px': sell_px,
                      'buy_px': buy_px, 'gross_pct': round(100 * gross, 4)})
    return pairs, skipped


def tail_ret_dist(days, dates, start_label, end_label):
    rets = []
    for k in range(len(dates) - 1):
        d = days[dates[k]]
        i_s, i_e = find_bar_exact(d, start_label), find_bar_exact(d, end_label)
        if i_s is None or i_e is None or i_e <= i_s:
            continue
        rets.append(100 * (d['c'][i_e] / d['c'][i_s] - 1))
    a = np.array(rets)
    return {'n_days': len(rets),
            'max_pct': round(float(a.max()), 3), 'min_pct': round(float(a.min()), 3),
            'p90_pct': round(float(np.percentile(a, 90)), 3),
            'p99_pct': round(float(np.percentile(a, 99)), 3),
            'n_gt_1pct': int((a > 1).sum()), 'n_gt_0.5pct': int((a > 0.5).sum())}


def apply_book(pairs, fee_buy, fee_sell, direction):
    out = []
    for p in pairs:
        q = dict(p)
        if direction == 'buy_first':
            net = (p['sell_px'] * (1 - fee_sell) - p['buy_px'] * (1 + fee_buy)) / p['buy_px']
        else:
            net = (p['sell_px'] * (1 - fee_sell) - p['buy_px'] * (1 + fee_buy)) / p['sell_px']
        q['net_pct'] = round(100 * net, 4)
        q['win'] = bool(net > 0)
        check('net_le_gross', q['net_pct'] <= p['gross_pct'] + 1e-9, '')
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

def band(ms):
    out = {}
    for k in ('n', 'avg_gross_pct', 'avg_net_pct', 'win_rate'):
        vals = [m[k] for m in ms if k in m and m[k] is not None]
        if vals:
            out[k] = {'mean': round(float(np.mean(vals)), 4),
                      'std': round(float(np.std(vals)), 4)}
    return out


def random_baseline_grid(days, dates, n_mc=N_MC, seed=MC_SEED):
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


def random_baseline_tail(days, dates, n_signals, n_mc=N_MC, seed=MC_SEED):
    """随机抽同样多的日子做「14:55收盘卖→次日开盘接回」，两套费率账。"""
    rng = random.Random(seed + 1)
    res = {'etf': [], 'stk': []}
    pool = [k for k in range(len(dates) - 1)
            if find_bar_exact(days[dates[k]], B7_TAIL_END) is not None]
    for _ in range(n_mc):
        for book, fb, fs in (('etf', FEE_ETF, FEE_ETF), ('stk', FEE_STK_BUY, FEE_STK_SELL)):
            sample = rng.sample(pool, min(n_signals, len(pool)))
            pairs = []
            for k in sample:
                d, nd = days[dates[k]], days[dates[k + 1]]
                i55 = find_bar_exact(d, B7_TAIL_END)
                sp, bp = float(d['c'][i55]), float(nd['o'][0])
                gross = (sp - bp) / sp
                net = (sp * (1 - fs) - bp * (1 + fb)) / sp
                pairs.append({'gross_pct': 100 * gross, 'net_pct': 100 * net, 'win': net > 0})
            res[book].append(agg_pairs(pairs))
    return {b: band(res[b]) for b in res}


# ---------------- 无前视截断验证（B7/S4） ----------------

def lookahead_audit_tail(days, dates, pairs, start_label, end_label, threshold, tag):
    sig_dates = {p['date'] for p in pairs}
    fails = []
    for dt in sig_dates:
        d = days[dt]
        i_e = find_bar_exact(d, end_label)
        i_s = find_bar_exact(d, start_label)
        c_cut = d['c'][:i_e + 1]   # 截断：只保留 <=end 根
        tail_ret = c_cut[i_e] / c_cut[i_s] - 1
        if not (tail_ret > threshold):
            fails.append(dt)
    check(f'{tag}_no_future_truncation', not fails, f'fails={fails[:5]}')
    return len(sig_dates), fails


# ---------------- 品种画像 ----------------

def profile(code, days, label, win):
    dates = sorted(d for d in days if win[0] <= d <= win[1] and days[d]['n'] >= MIN_BARS)
    stats, prev = [], None
    for dt in dates:
        if prev is None:
            prev = days[dt]['c'][-1]
            continue
        s = day_stats(days[dt], prev)
        s['date'] = dt
        stats.append(s)
        prev = days[dt]['c'][-1]
    if not stats:
        return {'label': label, 'code': code, 'n_days': 0}, dates, []
    amps = np.array([s['amp_pct'] for s in stats])
    amts = np.array([s['amt_yi'] for s in stats])
    chgs = np.array([s['chg_pct'] for s in stats])
    gaps = []
    for k in range(1, len(dates)):
        gaps.append(100 * (days[dates[k]]['o'][0] / days[dates[k - 1]]['c'][-1] - 1))
    gaps = np.abs(np.array(gaps))
    prof = {
        'label': label, 'code': code, 'window': list(win), 'n_days': len(stats),
        'amp_median_pct': round(float(np.median(amps)), 3),
        'amp_mean_pct': round(float(amps.mean()), 3),
        'amp_p25_pct': round(float(np.percentile(amps, 25)), 3),
        'amp_p75_pct': round(float(np.percentile(amps, 75)), 3),
        'amp_ge_2pct_day_share': round(float((amps >= 2).mean()), 4),
        'amp_ge_3pct_day_share': round(float((amps >= 3).mean()), 4),
        'amp_ge_5pct_day_share': round(float((amps >= 5).mean()), 4),
        'chg_abs_median_pct': round(float(np.median(np.abs(chgs))), 3),
        'overnight_gap_abs_median_pct': round(float(np.median(gaps)), 3) if len(gaps) else None,
        'overnight_gap_abs_p90_pct': round(float(np.percentile(gaps, 90)), 3) if len(gaps) else None,
        'amt_median_yi': round(float(np.median(amts)), 2),
        'amt_p10_yi': round(float(np.percentile(amts, 10)), 2),
        'amt_p90_yi': round(float(np.percentile(amts, 90)), 2),
        'intraday_structure': intraday_structure(days, dates),
    }
    return prof, dates, stats


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


# ---------------- 主流程 ----------------

def main():
    print('== 数据构建（快照清洗 → CSV）==')
    days, quality = build_588170_csv()
    dates = sorted(d for d in days if WINDOW_START <= d <= WINDOW_END and days[d]['n'] >= MIN_BARS)
    print(f'  588170: {len(days)} 个快照日, 窗口内有效交易日 {len(dates)} '
          f'({dates[0]} ~ {dates[-1]})')
    check('sample_days_ge_200', len(dates) >= 200, f'n={len(dates)}')

    print('== 品种画像 ==')
    prof_main, _, _ = profile(MAIN, days, '科创半导体ETF(12个月)', (WINDOW_START, WINDOW_END))
    prof_sub, _, _ = profile(MAIN, days, '科创半导体ETF(A10对齐子窗口)', A10_SUB_WINDOW)
    profiles = {'588170_12m': prof_main, '588170_a10_window': prof_sub}
    for c in STOCKS:
        sd = load_stock_days(c, STK_WINDOW)
        if sd:
            p, _, _ = profile(c, sd, '股票池(重叠窗口)', STK_WINDOW)
            profiles[c] = p
            print(f"  {c}: 振幅中位={p.get('amp_median_pct')}% n={p.get('n_days')}")

    print('== 网格策略（588170）==')
    grid_raw, prev = [], None
    valid_dates = []
    for dt in dates:
        if prev is None:
            prev = days[dt]['c'][-1]
            continue
        raw = grid_day(days[dt], prev)
        for p in raw:
            p['date'] = dt
        grid_raw.extend(raw)
        valid_dates.append(dt)
        prev = days[dt]['c'][-1]
    grid_etf = apply_book(grid_raw, FEE_ETF, FEE_ETF, 'buy_first')
    grid_stk = apply_book(grid_raw, FEE_STK_BUY, FEE_STK_SELL, 'buy_first')
    grid_days_used = len({p['date'] for p in grid_raw})
    forced_share = (sum(p['forced'] for p in grid_raw) / len(grid_raw)) if grid_raw else None
    print(f'  触发对数={len(grid_raw)} 覆盖天数={grid_days_used}/{len(valid_dates)} '
          f'强平占比={forced_share:.2%}' if forced_share is not None else '  无触发')

    print('== B7 隔夜反T（588170, >1%）==')
    tail_dist_b7 = tail_ret_dist(days, dates, B7_TAIL_START, B7_TAIL_END)
    b7_raw, b7_skipped = tail_signals(days, dates, B7_TAIL_START, B7_TAIL_END,
                                      B7_THRESHOLD, 'B7')
    lookahead_audit_tail(days, dates, b7_raw, B7_TAIL_START, B7_TAIL_END, B7_THRESHOLD, 'b7')
    b7_etf = apply_book(b7_raw, FEE_ETF, FEE_ETF, 'sell_first')
    b7_stk = apply_book(b7_raw, FEE_STK_BUY, FEE_STK_SELL, 'sell_first')
    print(f'  信号数={len(b7_raw)} 跳过缺根日={len(b7_skipped)} 尾盘分布={tail_dist_b7}')

    print('== S4 隔夜反T（588170, 最后5min>1%）==')
    tail_dist_s4 = tail_ret_dist(days, dates, S4_START, S4_END)
    s4_raw, s4_skipped = tail_signals(days, dates, S4_START, S4_END, S4_THRESHOLD, 'S4')
    lookahead_audit_tail(days, dates, s4_raw, S4_START, S4_END, S4_THRESHOLD, 's4')
    s4_etf = apply_book(s4_raw, FEE_ETF, FEE_ETF, 'sell_first')
    s4_stk = apply_book(s4_raw, FEE_STK_BUY, FEE_STK_SELL, 'sell_first')
    print(f'  信号数={len(s4_raw)} 跳过缺根日={len(s4_skipped)} 尾盘5min分布={tail_dist_s4}')

    print('== 基线 ==')
    rb_grid = random_baseline_grid(days, valid_dates)
    rb_b7 = random_baseline_tail(days, dates, len(b7_raw))
    rb_s4 = random_baseline_tail(days, dates, len(s4_raw), seed=MC_SEED + 7)
    first_dt, last_dt = dates[0], dates[-1]
    bh_ret = 100 * (days[last_dt]['c'][-1] / days[first_dt]['o'][0] - 1)
    bh = {'window': [first_dt, last_dt], 'buy_hold_pct': round(float(bh_ret), 3)}

    results = {
        'meta': {
            'experiment': "2-B'_588170载体验证实验", 'date': '2026-09-15', 'owner': '实验员_E4',
            'hypothesis': '低墙×高波：免印花税+万0.5 的高波动 ETF（588170）上，A10 同框架策略是否转正',
            'window': [WINDOW_START, WINDOW_END],
            'a10_alignment_window': list(A10_SUB_WINDOW),
            'main_carrier': MAIN,
            'carrier_name': '华夏上证科创板半导体材料设备主题ETF（gm get_instrumentinfos 实测）',
            'stock_pool_context': {'codes': STOCKS, 'window': list(STK_WINDOW)},
            'fees': {'etf_side': FEE_ETF, 'etf_roundtrip': ROUNDTRIP_ETF,
                     'stk_sell': FEE_STK_SELL, 'stk_buy': FEE_STK_BUY,
                     'stk_roundtrip': ROUNDTRIP_STK},
            'params': {'grid_pct': GRID_PCT, 'grid_max_pairs': GRID_MAX_PAIRS,
                       'grid_fill': '触格即成交于格价(无滑点,偏乐观)',
                       'b7_tail': [B7_TAIL_START, B7_TAIL_END], 'b7_threshold': B7_THRESHOLD,
                       's4_tail': [S4_START, S4_END], 's4_threshold': S4_THRESHOLD,
                       'tail_sell': '信号根(14:55)收盘', 'tail_buyback': '次日首根open',
                       'n_mc': N_MC, 'mc_seed': MC_SEED},
            'data_source': {
                'etf_1min': 't_io/minute_snapshots 缓存（241 日，2025-04-08~2026-09-15），'
                            '本实验窗口 2025-09-15~2026-09-15 完整覆盖，无需 gm 补数据',
                'cleaning': ['2026-07-06 1:3 拆分：之前价格÷3（gm ADJUST_NONE/PREV 实测因子 1/3，'
                             '证据 data/split_factor_probe.json）',
                             '量额口径三段混合源：累计序列（2026-06-22 起部分日）先一阶差分还原逐根，'
                             '再以 gm 日线全历史（352 行，data/gm_daily_full.json）逐日判定单位'
                             '（实测为 股/手 两种，amount 恒为元；完整日匹配误差<5% 断言）',
                             '2026-09-15 13:00 bar 量额异常插补（仅影响当日成交额）'],
                'stock_1min': 't_io/backtest_1year_data 既有CSV重叠窗口裁剪（仅画像对照）'},
        },
        'assertions': ASSERTS,
        'data_quality_summary': {
            'n_snapshot_files': quality['n_files'],
            'unit_counts': quality['unit_counts'],
            'anomaly_fixes': quality['anomaly_fixes'],
            'low_bar_days_excluded': quality['low_bar_days'],
            'valid_days_in_window': len(dates),
            'last_bar_before_1500_days': sum(
                1 for d in quality['days']
                if WINDOW_START <= d['date'] <= WINDOW_END and d['last_bar'] < '15:00'),
            'detail_json': 'data/data_quality_588170.json',
        },
        'profiles': profiles,
        'grid_588170': {
            'n_pairs': len(grid_raw),
            'days_with_trade': grid_days_used, 'days_total': len(valid_dates),
            'day_coverage': round(grid_days_used / len(valid_dates), 4) if valid_dates else None,
            'forced_close_share': round(forced_share, 4) if forced_share is not None else None,
            'book_etf': agg_pairs(grid_etf), 'book_stk': agg_pairs(grid_stk),
            'cost_wall': wall(grid_raw, grid_etf, grid_stk),
            'random_baseline': rb_grid,
            'pairs': grid_etf,
        },
        'b7_588170': {
            'n_signals': len(b7_raw),
            'skipped_days_missing_tail_bars': len(b7_skipped),
            'signal_rate_per_day': round(len(b7_raw) / max(1, len(dates) - 1), 4),
            'tail_ret_distribution': tail_dist_b7,
            'book_etf': agg_pairs(b7_etf), 'book_stk': agg_pairs(b7_stk),
            'cost_wall': wall(b7_raw, b7_etf, b7_stk),
            'random_baseline': rb_b7,
            'pairs': b7_etf,
        },
        's4_588170': {
            'n_signals': len(s4_raw),
            'skipped_days_missing_tail_bars': len(s4_skipped),
            'signal_rate_per_day': round(len(s4_raw) / max(1, len(dates) - 1), 4),
            'tail_ret_distribution': tail_dist_s4,
            'book_etf': agg_pairs(s4_etf), 'book_stk': agg_pairs(s4_stk),
            'cost_wall': wall(s4_raw, s4_etf, s4_stk),
            'random_baseline': rb_s4,
            'pairs': s4_etf,
        },
        'buy_hold_588170': bh,
        'iopv_probe': {
            'finding': 'gm current() 实时快照有 iopv 字段（2026-09-15 15:29 实测 '
                       'iopv=0.9218 vs price=0.922，溢价 +0.02%）；history_n(1d) 请求 '
                       'iopv 字段被静默丢弃（无历史序列）；get_instrumentinfos 无净值字段。',
            'evidence': ['data/iopv_probe_588170.json'],
            'conclusion': '588170 盘中溢价率可经 current() 每 30s 自行采集落盘（见 '
                          'scripts/iopv_snapshot.py 草案）；历史溢价率不可得，上线前需自建序列。',
        },
    }
    json.dump(results, open(OUT_JSON, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('saved:', OUT_JSON)

    print('\n== 核心数字 ==')
    for key in ['588170_12m', '588170_a10_window'] + STOCKS:
        p = profiles.get(key)
        if p and p.get('n_days'):
            print(f"  {key}: 振幅中位={p['amp_median_pct']}% ≥2%日={p['amp_ge_2pct_day_share']:.0%} "
                  f"≥5%日={p['amp_ge_5pct_day_share']:.0%} 额中位={p['amt_median_yi']}亿 n={p['n_days']}")
    print('网格:', json.dumps(results['grid_588170']['cost_wall'], ensure_ascii=False),
          json.dumps(results['grid_588170']['book_etf'], ensure_ascii=False))
    print('网格随机基线:', json.dumps(rb_grid, ensure_ascii=False))
    print('B7:', json.dumps(results['b7_588170']['cost_wall'], ensure_ascii=False),
          json.dumps(results['b7_588170']['book_etf'], ensure_ascii=False))
    print('B7随机基线:', json.dumps(rb_b7, ensure_ascii=False))
    print('S4:', json.dumps(results['s4_588170']['cost_wall'], ensure_ascii=False),
          json.dumps(results['s4_588170']['book_etf'], ensure_ascii=False))
    print('S4随机基线:', json.dumps(rb_s4, ensure_ascii=False))
    print('持有不动:', json.dumps(bh, ensure_ascii=False))
    bad = [k for k, v in ASSERTS.items() if not v['pass']]
    print('断言:', '全部通过' if not bad else f'失败={sorted(set(bad))}')


if __name__ == '__main__':
    main()
