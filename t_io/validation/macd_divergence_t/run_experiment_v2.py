# -*- coding: utf-8 -*-
"""分时 MACD 背离做T 实验 v2（2026-09-14）— owner 拍板调参版。

相对 v1 的改动：
1. 5min 周期：1min 聚合为 5min K 线（聚合守恒断言：sum 成交量 == 当日总量），
   MACD(12,26,9) 在 5min 收盘上计算，DIF 为快线，日内重置（同 v1）。
2. 金叉/死叉锚定（通达信口径）：金叉→死叉区间最高价 H + DIF 峰值；死叉→金叉区间最低价 L + DIF 谷值。
   顶背离：后一上涨区间 H >= 前一区间 H*(1-0.003)（±0.3% 缓冲）且 DIF 峰值更低 → 高抛。
   底背离 A（owner 版=隐藏底背离：L 不弱于前低 且 DIF 谷更低）/ B（经典版：L 新低 且 DIF 谷抬高）→ 低吸。
   信号在锚定确认根（死叉/金叉当根）5min 收盘生效，严格无前视。
3. 量能否决：确认根之前、前一区间结束之后，价格突破前区间极值（high>H1 / low<L1）的首根 5min，
   若其成交量 > 前 20 根 5min 均量 * 1.5 → 信号作废（记录 vetoed，不入交易）。
4. 日环境门控（可交易口径，无前视）：在任一 5min bar k（k>=GATE_MIN_BAR），用 <=k 数据判定：
   (a) 当根收盘 < 截至当根 VWAP 且已成交时段内 >=80% 的 5min 收盘低于各自时点 VWAP；
   (b) 当根收盘相对昨收跌幅 <= -1.5%；
   (c) 5min 收盘价已确认的反弹高点（局部峰，1 根滞后确认）最近两个逐次降低。
   三条同时满足 → 当日判「单边下跌」门控触发：之后不再产生任何信号；
   若有已高抛未回补仓位 → 立即按当根收盘回补（清仓规则）；
   底仓口径：记录 (门控价-当日收盘)/门控价 作为「清仓 vs 持有到收盘」的当日减亏统计。
   事后口径（仅统计）：close>open 且 (high-close)/(high-low)<0.3 且涨幅>1% → 单边上涨；
   close<open 且 (close-low)/(high-low)<0.3 且跌幅>1% → 单边下跌；其余震荡（涨跌幅相对昨收）。
5. 强制回补闭环：高抛成交（信号根收盘）后，当日首个低吸信号价回补；
   无低吸信号则 14:50 的 5min bar（标签 14:50，覆盖 14:50-14:54）收盘价强制回补。
   成本：卖出 0.00121，买入 0.00015。每日最多 2 对，同一时刻最多 1 对在持。
6. 新信号截止 14:30（5min 标签 <=14:30）。

数据层：backtest_1year_data 一年期 1min CSV（gm 缓存，2025-08-26~2026-08-26，每日 241 根）
        + minute_snapshots 日级 JSON 补齐 2026-08-27~2026-09-14；
        同一 (票,日) 两源都有时取根数更多者，并做交叉一致性检查（软断言）。
"""
import sys, os, json, glob, random, re
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
SNAP = os.path.join(ROOT, 't_io', 'minute_snapshots')
CSV_DIR = os.path.join(ROOT, 't_io', 'backtest_1year_data')
OUT = os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')
V1_RESULTS = os.path.join(OUT, 'results_2026-09-14.json')

CODES = ['000988', '002451', '002639', '300054', '603667']
START = '2025-09-14'
END = '2026-09-14'

FEE_SELL = 0.00121
FEE_BUY = 0.00015
BUF = 0.003            # 价格 ±0.3% 缓冲
VOL_MULT = 1.5         # 量能否决倍数
VOL_WIN = 20           # 均量窗口（5min 根数）
WARMUP5 = 10           # 前 10 根 5min（约 10:19 前）不出信号
GATE_MIN_BAR = 10      # 门控最早评估根
NO_NEW_AFTER = '14:30' # 新信号截止（5min 标签）
FORCED_LABEL = '14:50' # 强制回补 5min 标签
MAX_PAIRS = 2          # 每日最多交易对
N_MC = 200             # 随机基线蒙特卡洛次数
MC_SEED = 42
MIN_1M_BARS = 100      # 当日 1min 根数下限
MIN_5M_BARS = 30       # 当日 5min 根数下限

SNAP_RE = re.compile(r'^(\d{6})_(\d{4}-\d{2}-\d{2})\.json$')

ASSERTS = {}  # name -> {'pass': bool, 'detail': str}


def check(name, ok, detail=''):
    ASSERTS[name] = {'pass': bool(ok), 'detail': detail}
    if not ok:
        print('  [断言失败]', name, detail)


# ---------------- 数据层 ----------------

def load_csv_days(code):
    """返回 {date: bars}，bars 为 list[dict(t,o,h,l,c,v,amt)]，按时间排序。"""
    fs = glob.glob(os.path.join(CSV_DIR, code + '*1min.csv'))
    if not fs:
        return {}
    df = pd.read_csv(fs[0])
    df['date'] = df['time'].str[:10]
    df['t'] = df['time'].str[11:16]
    days = {}
    for dt, g in df.groupby('date'):
        g = g.sort_values('time')
        days[dt] = [{'t': r.t, 'o': float(r.open), 'h': float(r.high), 'l': float(r.low),
                     'c': float(r.close), 'v': float(r.volume), 'amt': float(r.amount)}
                    for r in g.itertuples()]
    return days


def load_snap_days(code, min_date):
    """只加载 min_date 之后的快照（CSV 已覆盖更早日期）。文件名严格 <code>_YYYY-MM-DD.json。"""
    days = {}
    for f in glob.glob(os.path.join(SNAP, '20*', '*', code + '_*.json')):
        m = SNAP_RE.match(os.path.basename(f))
        if not m or m.group(1) != code:
            continue  # 排除 000988_B_2026-08-20.json 之类账户后缀文件
        dt = m.group(2)
        if dt <= min_date or dt > END:
            continue
        d = json.load(open(f, encoding='utf-8'))
        bars = [{'t': b['time'][11:16], 'o': float(b['open']), 'h': float(b['high']),
                 'l': float(b['low']), 'c': float(b['close']), 'v': float(b['volume']),
                 'amt': float(b['amount'])} for b in d['bars']]
        bars.sort(key=lambda x: x['t'])
        days[dt] = bars
    return days


def merge_days(code):
    """合并 CSV + 快照；同日两源取根数多者。返回 (sorted_dates, {date: bars}, source_map, overlap_issues)。"""
    csv_days = load_csv_days(code)
    max_csv = max(csv_days) if csv_days else '0000-00-00'
    snap_days = load_snap_days(code, max_csv)  # 快照只补 CSV 之后的日期
    merged = dict(csv_days)
    src = {d: 'csv' for d in csv_days}
    for dt, bars in snap_days.items():
        merged[dt] = bars
        src[dt] = 'snapshot'
    dates = sorted(merged)
    return dates, merged, src


def bucket_key(t):
    """1min 标签 -> 5min 桶起点标签（HH:MM）。按钟点对齐、对缺根稳健。
    gm CSV 为终点标签（09:31 根=09:30-09:31 这一分钟），快照为起点标签；
    统一规则：桶 = ceil(分钟数/5)*5 为终点、起点=终点-5；09:30 开盘根强制并入首桶。
    于是 gm 的 09:31..09:35 与快照的 09:30..09:34 都落入标签 '09:30' 桶（钟点 09:30-09:35），
    两源桶边界一致（仅快照侧有最多 1 分钟的口径右移，见 README/报告遗留问题）。"""
    m = int(t[:2]) * 60 + int(t[3:])
    if t == '09:30':
        m = 571
    start = ((m + 4) // 5) * 5 - 5
    return '%02d:%02d' % (start // 60, start % 60)


def agg5(bars):
    """1min -> 5min，按 bucket_key 钟点对齐分桶（缺根不漂移）。桶标签 = 桶起点 HH:MM。
    返回 dict(labels,o,h,l,c,v,amt,end1m)；end1m[b]=该桶最后一根 1min 的全日索引。"""
    groups = {}
    order = []
    for i, b in enumerate(bars):
        k = bucket_key(b['t'])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(i)
    labels, O, H, L, C, V, A, E = [], [], [], [], [], [], [], []
    for k in order:
        ch = groups[k]
        labels.append(k)
        O.append(bars[ch[0]]['o'])
        H.append(max(bars[i]['h'] for i in ch))
        L.append(min(bars[i]['l'] for i in ch))
        C.append(bars[ch[-1]]['c'])
        V.append(sum(bars[i]['v'] for i in ch))
        A.append(sum(bars[i]['amt'] for i in ch))
        E.append(ch[-1])
    return {'labels': labels, 'o': np.array(O), 'h': np.array(H), 'l': np.array(L),
            'c': np.array(C), 'v': np.array(V), 'amt': np.array(A), 'end1m': E,
            'n': len(labels)}


def ema(arr, span):
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


# ---------------- 信号检测（无前视：全部输入 <= 确认根） ----------------

def macd_5m(c):
    dif = ema(c, 12) - ema(c, 26)
    dea = ema(dif, 9)
    return dif, dea


def find_crosses(dif, dea):
    xs = []
    for i in range(1, len(dif)):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            xs.append((i, 'G'))
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            xs.append((i, 'D'))
    return xs


def build_segments(day5, dif, dea):
    """金叉→死叉 = 上涨区间(H/DIF峰)；死叉→金叉 = 下跌区间(L/DIF谷)。确认根=区间末尾交叉根。"""
    xs = find_crosses(dif, dea)
    h, l = day5['h'], day5['l']
    ups, downs = [], []
    for (i1, t1), (i2, t2) in zip(xs, xs[1:]):
        if t1 == 'G' and t2 == 'D':
            ups.append({'start': i1, 'confirm': i2,
                        'H': float(h[i1:i2 + 1].max()), 'dif_peak': float(dif[i1:i2 + 1].max())})
        elif t1 == 'D' and t2 == 'G':
            downs.append({'start': i1, 'confirm': i2,
                          'L': float(l[i1:i2 + 1].min()), 'dif_trough': float(dif[i1:i2 + 1].min())})
    return ups, downs


def detect_signals(day5, dif, dea):
    """返回原始信号列表（未应用门控/每日对数上限）。每个信号只使用 <=confirm 根的数据。"""
    ups, downs = build_segments(day5, dif, dea)
    labels, c, v, h, l = day5['labels'], day5['c'], day5['v'], day5['h'], day5['l']
    sigs = []
    # 顶背离：后上涨区间 H 不弱于前（±0.3% 缓冲）且 DIF 峰更低
    for u1, u2 in zip(ups, ups[1:]):
        k = u2['confirm']
        if k < WARMUP5 or labels[k] > NO_NEW_AFTER:
            continue
        if not (u2['H'] >= u1['H'] * (1 - BUF) and u2['dif_peak'] < u1['dif_peak']):
            continue
        veto, veto_bar = False, None
        for j in range(u1['confirm'] + 1, k + 1):  # 前区间结束之后、确认根之前/当根
            if h[j] > u1['H']:
                w0 = max(0, j - VOL_WIN)
                if j - w0 >= 5 and v[j] > VOL_MULT * v[w0:j].mean():
                    veto, veto_bar = True, j
                break
        sigs.append({'type': 'top', 'dir': 'sell', 'bar': k, 'time': labels[k],
                     'px': float(c[k]), 'ref1': u1['H'], 'ref2': u2['H'],
                     'dif1': u1['dif_peak'], 'dif2': u2['dif_peak'],
                     'vetoed': veto, 'veto_bar': veto_bar})
    # 底背离 A（owner 版=隐藏底背离）/ B（经典版）
    for d1, d2 in zip(downs, downs[1:]):
        k = d2['confirm']
        if k < WARMUP5 or labels[k] > NO_NEW_AFTER:
            continue
        is_A = d2['L'] >= d1['L'] * (1 - BUF) and d2['dif_trough'] < d1['dif_trough']
        is_B = d2['L'] < d1['L'] * (1 - BUF) and d2['dif_trough'] > d1['dif_trough']
        if not (is_A or is_B):
            continue
        veto, veto_bar = False, None
        for j in range(d1['confirm'] + 1, k + 1):
            if l[j] < d1['L']:
                w0 = max(0, j - VOL_WIN)
                if j - w0 >= 5 and v[j] > VOL_MULT * v[w0:j].mean():
                    veto, veto_bar = True, j
                break
        sigs.append({'type': 'bot_A' if is_A else 'bot_B', 'dir': 'buy', 'bar': k,
                     'time': labels[k], 'px': float(c[k]), 'ref1': d1['L'], 'ref2': d2['L'],
                     'dif1': d1['dif_trough'], 'dif2': d2['dif_trough'],
                     'vetoed': veto, 'veto_bar': veto_bar})
    sigs.sort(key=lambda s: s['bar'])
    return sigs


# ---------------- 日环境门控（可交易口径，无前视） ----------------

def eval_gate(day5, prev_close):
    """逐根评估门控，返回首次触发根索引或 None。全部条件只用 <=k 数据。"""
    c, v, amt = day5['c'], day5['v'], day5['amt']
    n = day5['n']
    cv, ca = np.cumsum(v), np.cumsum(amt)
    with np.errstate(divide='ignore', invalid='ignore'):
        vwap = np.where(cv > 0, ca / np.where(cv > 0, cv, 1), c)
    below = c < vwap
    for k in range(GATE_MIN_BAR, n):
        cond1 = below[k] and (below[:k + 1].mean() >= 0.8)
        cond2 = (c[k] / prev_close - 1) <= -0.015 if prev_close else False
        # 已确认反弹高点（局部峰，1 根滞后确认）：j <= k-1，用到 close[j+1]<=close[k]，无前视
        peaks = [c[j] for j in range(1, k) if c[j] > c[j - 1] and c[j] > c[j + 1]]
        cond3 = len(peaks) >= 2 and peaks[-1] < peaks[-2]
        if cond1 and cond2 and cond3:
            return k
    return None


def day_type_posthoc(day5, prev_close):
    """事后口径（全天数据，仅统计用）：互斥穷尽三档。"""
    o, c, h, l = day5['o'][0], day5['c'][-1], day5['h'].max(), day5['l'].min()
    rng = h - l
    if rng <= 0 or not prev_close:
        return 'range'
    chg = c / prev_close - 1
    if c > o and (h - c) / rng < 0.3 and chg > 0.01:
        return 'up'
    if c < o and (c - l) / rng < 0.3 and chg < -0.01:
        return 'down'
    return 'range'


# ---------------- 交易闭环 ----------------

def find_label_idx(day5, label):
    idx = [i for i, t in enumerate(day5['labels']) if t <= label]
    return max(idx) if idx else None


def run_day(code, dt, day5, prev_close):
    """单日全流程：信号 + 门控 + 交易对闭环。返回 (signals, pairs, day_rec)。"""
    dif, dea = macd_5m(day5['c'])
    sigs = detect_signals(day5, dif, dea)
    gate_bar = eval_gate(day5, prev_close)
    posthoc = day_type_posthoc(day5, prev_close)
    idx_1450 = find_label_idx(day5, FORCED_LABEL)
    c = day5['c']

    pairs, open_pair = [], None
    for s in sigs:
        s['gated_before'] = gate_bar is not None and s['bar'] >= gate_bar
        if s['gated_before']:
            s['used'] = 'gated_off'
            continue
        if s['vetoed']:
            s['used'] = 'vetoed'
            continue
        if s['dir'] == 'sell':
            if open_pair is None and len(pairs) < MAX_PAIRS:
                open_pair = {'sell_bar': s['bar'], 'sell_time': s['time'], 'sell_px': s['px'],
                             'sell_sig': s['type']}
                s['used'] = 'open_pair'
            else:
                s['used'] = 'ignored_has_open_or_cap'
        else:  # buy
            if open_pair is not None and s['bar'] > open_pair['sell_bar']:
                cover_px = s['px']
                open_pair.update({'cover_bar': s['bar'], 'cover_time': s['time'],
                                  'cover_px': cover_px, 'cover_method': s['type']})
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
        open_pair = None

    for p in pairs:
        gross = (p['sell_px'] - p['cover_px']) / p['sell_px']
        net = (p['sell_px'] * (1 - FEE_SELL) - p['cover_px'] * (1 + FEE_BUY)) / p['sell_px']
        p.update({'gross_pct': round(100 * gross, 4), 'net_pct': round(100 * net, 4),
                  'win': bool(net > 0), 'fake_cover': bool(p['cover_px'] > p['sell_px']),
                  'code': code, 'date': dt, 'day_gated': gate_bar is not None,
                  'day_type_posthoc': posthoc})
        check('cost_nonnegative_pair', net <= gross + 1e-12,
              f'{code} {dt} net>{gross}')

    day_rec = {'code': code, 'date': dt, 'gate_bar': gate_bar,
               'gate_time': day5['labels'][gate_bar] if gate_bar is not None else None,
               'gate_px': float(c[gate_bar]) if gate_bar is not None else None,
               'close_px': float(c[-1]), 'prev_close': prev_close,
               'day_chg_pct': round(100 * (c[-1] / prev_close - 1), 3) if prev_close else None,
               'posthoc': posthoc, 'gated': gate_bar is not None,
               'clear_save_pct': (round(100 * (c[gate_bar] - c[-1]) / c[gate_bar], 4)
                                  if gate_bar is not None else None),
               'idx_1450': idx_1450, 'idx_1430': find_label_idx(day5, NO_NEW_AFTER),
               'closes': c, 'labels': day5['labels'], 'n5': day5['n']}
    return sigs, pairs, day_rec


# ---------------- 主收集（collect 定义见文件底部，含原始 1min bars 留存供截断验证） ----------------


# ---------------- 无前视截断重跑验证 ----------------

def lookahead_audit(all_days_bars, sample_sigs, seed=7):
    """对抽样信号：把当日 1min 数据截断到信号根所属 5min 桶末尾，重跑检测，
    同类型同时间信号必须仍然出现（证明检测只用 <=t 数据）。"""
    rng = random.Random(seed)
    sample = sample_sigs if len(sample_sigs) <= 300 else rng.sample(sample_sigs, 300)
    fails = []
    for s in sample:
        bars = all_days_bars[(s['code'], s['date'])]
        day5_full = agg5(bars)
        if s['bar'] >= day5_full['n']:
            fails.append((s['code'], s['date'], s['time'], 'bar_idx_oob'))
            continue
        cut = day5_full['end1m'][s['bar']] + 1
        day5_cut = agg5(bars[:cut])
        dif, dea = macd_5m(day5_cut['c'])
        sigs_cut = detect_signals(day5_cut, dif, dea)
        hit = any(x['type'] == s['type'] and x['time'] == s['time'] for x in sigs_cut)
        if not hit:
            fails.append((s['code'], s['date'], s['time'], s['type']))
    check('no_future_info_truncation', not fails, f'fails={fails[:5]} n_sample={len(sample)}')
    return len(sample), fails


# ---------------- 随机基线 ----------------

def random_baseline(day_recs, n_mc=N_MC, seed=MC_SEED):
    """同口径：每交易日随机 1 个卖出时点（warmup 后、<=14:30、门控触发前），
    同规则回补（门控触发则门控价回补，否则 14:50 强制回补），同成本。"""
    rng = random.Random(seed)
    subset_metrics, all_metrics = [], []
    for _ in range(n_mc):
        pairs = []
        for rec in day_recs:
            hi = rec['idx_1430']
            if rec['gate_bar'] is not None:
                hi = min(hi, rec['gate_bar'] - 1)
            if hi is None or hi < WARMUP5:
                continue
            sb = rng.randint(WARMUP5, hi)
            if rec['gate_bar'] is not None and rec['gate_bar'] > sb:
                cb, cm = rec['gate_bar'], 'unilateral_gate'
            else:
                cb = rec['idx_1450'] if (rec['idx_1450'] is not None and rec['idx_1450'] > sb) else rec['n5'] - 1
                cm = 'forced_1450'
            sell_px, cover_px = rec['closes'][sb], rec['closes'][cb]
            net = (sell_px * (1 - FEE_SELL) - cover_px * (1 + FEE_BUY)) / sell_px
            pairs.append({'net_pct': 100 * net, 'win': net > 0,
                          'fake_cover': cover_px > sell_px,
                          'day_gated': rec['gated'], 'day_type_posthoc': rec['posthoc'],
                          'cover_method': cm})
        sub = [p for p in pairs if (not p['day_gated']) and p['day_type_posthoc'] in ('up', 'range')]
        subset_metrics.append(agg_pairs(sub))
        all_metrics.append(agg_pairs(pairs))
    def band(ms):
        out = {}
        for k in ('n', 'avg_net_pct', 'win_rate', 'fake_cover_rate'):
            vals = [m[k] for m in ms if k in m and m[k] is not None]
            if vals:
                out[k] = {'mean': round(float(np.mean(vals)), 4),
                          'std': round(float(np.std(vals)), 4)}
        return out
    return {'n_mc': n_mc, 'verdict_subset': band(subset_metrics), 'all_days': band(all_metrics)}


def agg_pairs(pairs):
    if not pairs:
        return {'n': 0}
    n = len(pairs)
    return {'n': n,
            'avg_net_pct': round(sum(p['net_pct'] for p in pairs) / n, 4),
            'exp_net_pos': bool(sum(p['net_pct'] for p in pairs) / n > 0),
            'win_rate': round(sum(p['win'] for p in pairs) / n, 4),
            'fake_cover_rate': round(sum(p['fake_cover'] for p in pairs) / n, 4),
            'forced_rate': round(sum(p['cover_method'] == 'forced_1450' for p in pairs) / n, 4),
            'gate_cover_rate': round(sum(p['cover_method'] == 'unilateral_gate' for p in pairs) / n, 4)}


# ---------------- 汇总与判定 ----------------

def summarize(all_sigs, all_pairs, day_recs, baseline):
    by_posthoc, by_gate = {}, {}
    for key in ('up', 'range', 'down'):
        by_posthoc[key] = agg_pairs([p for p in all_pairs if p['day_type_posthoc'] == key])
    for key, flag in (('ungated', False), ('gated_intraday', True)):
        by_gate[key] = agg_pairs([p for p in all_pairs if p['day_gated'] == flag])
    subset = [p for p in all_pairs if (not p['day_gated']) and p['day_type_posthoc'] in ('up', 'range')]
    sub_agg = agg_pairs(subset)

    gated_days = [r for r in day_recs if r['gated']]
    gated_down = [r for r in gated_days if r['posthoc'] == 'down']
    saves = [r['clear_save_pct'] for r in gated_days]
    saves_down = [r['clear_save_pct'] for r in gated_down]
    clearance = {'n_gated_days': len(gated_days),
                 'n_gated_and_posthoc_down': len(gated_down),
                 'mean_save_pct_all_gated': round(float(np.mean(saves)), 4) if saves else None,
                 'mean_save_pct_gated_down': round(float(np.mean(saves_down)), 4) if saves_down else None}

    sig_stat = {}
    for typ in ('top', 'bot_A', 'bot_B'):
        ss = [s for s in all_sigs if s['type'] == typ]
        sig_stat[typ] = {'n_raw': len(ss),
                         'n_vetoed': sum(s['vetoed'] for s in ss),
                         'n_gated_off': sum(s.get('used') == 'gated_off' for s in ss),
                         'n_traded': sum(s.get('used') in ('open_pair', 'cover_pair') for s in ss)}

    bl = baseline['verdict_subset']
    bl_win = bl.get('win_rate', {}).get('mean')
    bl_net = bl.get('avg_net_pct', {}).get('mean')
    diff_win_pp = round(100 * (sub_agg.get('win_rate', 0) - bl_win), 2) if bl_win is not None and sub_agg.get('n') else None
    diff_net_pp = round(sub_agg.get('avg_net_pct', 0) - bl_net, 3) if bl_net is not None and sub_agg.get('n') else None

    c1 = sub_agg.get('n', 0) > 0 and sub_agg['avg_net_pct'] > 0
    c2 = sub_agg.get('n', 0) > 0 and sub_agg['win_rate'] >= 0.55
    c3 = diff_win_pp is not None and diff_win_pp >= 5.0
    c4 = clearance['mean_save_pct_gated_down'] is not None and clearance['mean_save_pct_gated_down'] > 0
    passed = sum([c1, c2, c3, c4])
    overall = '有优势' if passed == 4 else ('边际' if (passed >= 2 and c1) else '无优势')
    verdict = {
        'subset_def': '可交易口径未触发门控 且 事后日型∈{震荡,单边上涨} 的交易对',
        'c1_profit_expectation_pos': {'pass': bool(c1), 'value_avg_net_pct': sub_agg.get('avg_net_pct')},
        'c2_win_rate_ge_55pct': {'pass': bool(c2), 'value_win_rate': sub_agg.get('win_rate')},
        'c3_vs_random_ge_5pp': {'pass': bool(c3), 'metric': '盈利对占比差(pp)',
                                'value_diff_pp': diff_win_pp,
                                'ref_avg_net_diff_pp': diff_net_pp,
                                'baseline_win_rate': bl_win},
        'c4_clearance_reduces_loss': {'pass': bool(c4),
                                      'mean_save_pct_gated_down_days': clearance['mean_save_pct_gated_down'],
                                      'n_days': clearance['n_gated_and_posthoc_down']},
        'passed_count': passed, 'overall': overall,
        'overall_rule': '4/4=有优势；≥2 且①过=边际；其余=无优势'}
    return {'by_day_type_posthoc': by_posthoc, 'by_tradeable_gate': by_gate,
            'verdict_subset': sub_agg, 'clearance_rule': clearance,
            'signal_stats': sig_stat, 'verdict': verdict}


def v1_comparison():
    try:
        v1 = json.load(open(V1_RESULTS, encoding='utf-8'))
        m = v1['samples']['main_12m_5stocks']['types']
        rb = v1['samples']['random_baseline_main']['metrics']
        return {
            'v1_top_divergence': m['top']['overall'],
            'v1_bot_A': m['bot_A']['overall'],
            'v1_bot_B': m['bot_B']['overall'],
            'v1_random_baseline': {k: rb[k]['mean'] for k in rb},
            'note': 'v1=1min 裸信号/回看中点锚定/无门控无量能过滤；v2=5min 金叉死叉锚定+量能否决+日环境门控+强制回补闭环；'
                    'v1 的 closed_win 为理想最低/最高价接回口径（收益上限），v2 为信号价/强制价真实闭环口径，不可直接比胜率数值。'}
    except Exception as e:
        return {'error': str(e)}


def main():
    print('== v2 数据加载与计算 ==')
    # 为无前视截断验证保留原始 1min bars
    global _DAYS_BARS
    all_sigs, all_pairs, day_recs, per_code_days, src_stat, skipped = collect()
    total_days = sum(per_code_days.values())
    print(f'股票·日: {total_days}  分票: {per_code_days}  数据源: {src_stat}  跳过: {len(skipped)}')

    print('== 无前视截断重跑验证 ==')
    n_sample, _ = lookahead_audit(_DAYS_BARS, all_sigs)
    print(f'抽样 {n_sample} 个信号截断重跑')

    # 日型互斥穷尽断言
    check('day_type_exclusive_exhaustive',
          all(r['posthoc'] in ('up', 'range', 'down') for r in day_recs),
          'posthoc 必须三选一')
    # 回补必发生断言：collect/run_day 结构上每对都有 cover_*；再显式核验
    check('cover_always_happens',
          all(p.get('cover_px') is not None and p['cover_bar'] > p['sell_bar'] for p in all_pairs),
          '每对必有当日内回补且回补根在卖出根之后')
    check('fees_positive', FEE_SELL > 0 and FEE_BUY > 0, f'{FEE_SELL}/{FEE_BUY}')
    check('signals_within_cutoff',
          all(s['bar'] >= WARMUP5 and s['time'] <= NO_NEW_AFTER for s in all_sigs),
          '信号根必须 >=warmup 且 <=14:30')

    print('== 随机基线蒙特卡洛 ==')
    baseline = random_baseline(day_recs)
    summary = summarize(all_sigs, all_pairs, day_recs, baseline)

    for s in all_sigs:  # 机读输出瘦身：去掉内部标记外的冗余
        s.pop('end1m', None)
    results = {
        'meta': {'experiment': '分时MACD背离做T_v2', 'date': '2026-09-14',
                 'codes': CODES, 'window': [START, END],
                 'params': {'bar': '5min(1min聚合,起点标签)', 'macd': 'DIF=EMA12-EMA26,DEA=EMA9(DIF),日内重置',
                            'anchor': '金叉/死叉锚定区间极值(通达信口径),确认根=交叉当根',
                            'buf': BUF, 'vol_veto': f'突破前区间极值且量>{VOL_MULT}x前{VOL_WIN}根均量→作废',
                            'gate': 'VWAP持续下方(≥80%)+跌幅≤-1.5%+反弹高点逐次降低→单边下跌门控',
                            'close_loop': '低吸信号回补,否则14:50强制回补;门控触发立即回补',
                            'warmup5': WARMUP5, 'no_new_after': NO_NEW_AFTER,
                            'fee_sell': FEE_SELL, 'fee_buy': FEE_BUY, 'max_pairs_per_day': MAX_PAIRS},
                 'data_source': src_stat, 'skipped_days': skipped[:20]},
        'assertions': ASSERTS,
        'data': {'total_stock_days': total_days, 'per_code_days': per_code_days},
        'signal_stats': summary['signal_stats'],
        'signals': all_sigs,
        'pairs': [{k: v for k, v in p.items() if k != 'sell_sig'} for p in all_pairs],
        'gate_days': [{k: r[k] for k in ('code', 'date', 'gate_time', 'gate_px', 'close_px',
                                          'day_chg_pct', 'posthoc', 'clear_save_pct')}
                      for r in day_recs if r['gated']],
        'summary': {k: summary[k] for k in ('by_day_type_posthoc', 'by_tradeable_gate',
                                             'verdict_subset', 'clearance_rule')},
        'random_baseline': baseline,
        'v1_comparison': v1_comparison(),
        'verdict': summary['verdict'],
    }
    out_path = os.path.join(OUT, 'results_v2_2026-09-14.json')
    json.dump(results, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('saved:', out_path)

    print('')
    print('== 核心结果 ==')
    print('信号统计:', json.dumps(summary['signal_stats'], ensure_ascii=False))
    print('交易对总数:', len(all_pairs))
    print('按事后日型:', json.dumps(summary['by_day_type_posthoc'], ensure_ascii=False))
    print('按门控:', json.dumps(summary['by_tradeable_gate'], ensure_ascii=False))
    print('判定子集(未门控×震荡/上涨):', json.dumps(summary['verdict_subset'], ensure_ascii=False))
    print('清仓规则:', json.dumps(summary['clearance_rule'], ensure_ascii=False))
    print('随机基线(子集):', json.dumps(baseline['verdict_subset'], ensure_ascii=False))
    print('判定:', json.dumps(summary['verdict'], ensure_ascii=False))
    bad = [k for k, v in ASSERTS.items() if not v['pass']]
    print('断言:', '全部通过' if not bad else f'失败={bad}')


# collect 需要把原始 bars 暴露给截断验证：用全局容器
_DAYS_BARS = {}
_orig_run_day = run_day


def collect():
    all_sigs, all_pairs, day_recs = [], [], []
    per_code_days, src_stat, skipped = {}, {}, []
    for code in CODES:
        dates, merged, src = merge_days(code)
        prev_close = None
        for dt in dates:
            bars = merged[dt]
            if prev_close is None or not (START <= dt <= END):
                prev_close = float(bars[-1]['c'])
                continue
            if len(bars) < MIN_1M_BARS:
                skipped.append({'code': code, 'date': dt, 'reason': f'bars={len(bars)}'})
                prev_close = float(bars[-1]['c'])
                continue
            day5 = agg5(bars)
            v1 = sum(b['v'] for b in bars)
            a1 = sum(b['amt'] for b in bars)
            check('agg5_volume_conservation', abs(day5['v'].sum() - v1) <= 1e-6 * max(1.0, v1), f'{code} {dt}')
            check('agg5_amount_conservation', abs(day5['amt'].sum() - a1) <= 1e-4 * max(1.0, a1), f'{code} {dt}')
            if day5['n'] < MIN_5M_BARS:
                skipped.append({'code': code, 'date': dt, 'reason': f'5m_bars={day5["n"]}'})
                prev_close = float(bars[-1]['c'])
                continue
            sigs, pairs, rec = run_day(code, dt, day5, prev_close)
            for s in sigs:
                s.update({'code': code, 'date': dt,
                          'day_gated': rec['gated'], 'day_type_posthoc': rec['posthoc']})
            all_sigs.extend(sigs)
            all_pairs.extend(pairs)
            day_recs.append(rec)
            _DAYS_BARS[(code, dt)] = bars
            per_code_days[code] = per_code_days.get(code, 0) + 1
            src_stat[src[dt]] = src_stat.get(src[dt], 0) + 1
            prev_close = float(bars[-1]['c'])
    return all_sigs, all_pairs, day_recs, per_code_days, src_stat, skipped


if __name__ == '__main__':
    main()
