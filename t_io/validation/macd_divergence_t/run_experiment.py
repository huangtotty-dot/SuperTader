# -*- coding: utf-8 -*-
"""分时 MACD 背离做T 实验（2026-09-14）。只读 minute_snapshots，输出机读JSON。"""
import sys, os, json, glob, random
sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
SNAP = os.path.join(ROOT, 't_io', 'minute_snapshots')
OUT  = os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')

MAIN_CODES = ['600176', '600481', '000988', '002639', '588170']
REF_CODES  = ['603667', '300153', '002451']
MAIN_START = '2025-09-14'
FULL_START = '2023-01-01'

K_SWING = 5
MIN_GAP = 15
WARMUP = 30
NO_NEW_AFTER = '14:30'
FLAT_TIME = '14:55'
FEE_SELL = 0.00121
FEE_BUY = 0.00015
HORIZON = 30
FAKE_TH = 0.01
MAX_SIG_PER_DIR = 2

def ema(arr, span):
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out

def load_day(path):
    d = json.load(open(path, encoding='utf-8'))
    bars = d['bars']
    if len(bars) < 60:
        return None
    t = [b['time'][11:16] for b in bars]
    h = np.array([b['high'] for b in bars], float)
    l = np.array([b['low'] for b in bars], float)
    c = np.array([b['close'] for b in bars], float)
    return {'date': d['date'], 'times': t, 'high': h, 'low': l, 'close': c, 'n': len(bars)}

def detect_swings(c, is_high):
    n = len(c)
    res = []
    for i in range(K_SWING, n - K_SWING):
        left = c[i - K_SWING:i]
        right = c[i + 1:i + K_SWING + 1]
        if is_high:
            if c[i] > left.max() and c[i] >= right.max():
                res.append((i, i + K_SWING))
        else:
            if c[i] < left.min() and c[i] <= right.min():
                res.append((i, i + K_SWING))
    return res

def gen_signals(day):
    c, l, h, t, n = day['close'], day['low'], day['high'], day['times'], day['n']
    dif = ema(c, 12) - ema(c, 26)
    sigs = []
    idx_1430 = max([i for i, tt in enumerate(t) if tt <= NO_NEW_AFTER], default=-1)
    # 顶背离（高抛）
    highs = detect_swings(c, True)
    for (i1, _), (i2, cf2) in zip(highs, highs[1:]):
        if i2 - i1 < MIN_GAP:
            continue
        trig = cf2
        if trig < WARMUP or trig > idx_1430:
            continue
        if c[i2] >= c[i1] * 0.998 and dif[i2] < dif[i1]:
            rl = l[i1:i2 + 1].min()
            lvl = c[i2] - (c[i2] - rl) * 0.5
            reason = 'retrace50' if c[trig] < lvl else 'no_new_high_5'
            sigs.append({'type': 'top', 'dir': 'sell', 'trig_idx': trig,
                         'trig_time': t[trig], 'trig_px': float(c[trig]),
                         'p1': float(c[i1]), 'p2': float(c[i2]),
                         'dif1': float(dif[i1]), 'dif2': float(dif[i2]),
                         'confirm_reason': reason})
    # 底背离 A（owner字面）/ B（经典）
    lows = detect_swings(c, False)
    for (i1, _), (i2, cf2) in zip(lows, lows[1:]):
        if i2 - i1 < MIN_GAP:
            continue
        trig = cf2
        if trig < WARMUP or trig > idx_1430:
            continue
        rh = h[i1:i2 + 1].max()
        lvl = c[i2] + (rh - c[i2]) * 0.5
        reason = 'rebound50' if c[trig] > lvl else 'no_new_low_5'
        base = {'dir': 'buy', 'trig_idx': trig, 'trig_time': t[trig],
                'trig_px': float(c[trig]), 'p1': float(c[i1]), 'p2': float(c[i2]),
                'dif1': float(dif[i1]), 'dif2': float(dif[i2]), 'confirm_reason': reason}
        if c[i2] <= c[i1] * 1.002 and dif[i2] < dif[i1]:
            s = dict(base); s['type'] = 'bot_A'; sigs.append(s)
        if c[i2] < c[i1] * 0.998 and dif[i2] > dif[i1]:
            s = dict(base); s['type'] = 'bot_B'; sigs.append(s)
    out = []
    for typ in ('top', 'bot_A', 'bot_B'):
        ss = sorted([s for s in sigs if s['type'] == typ], key=lambda x: x['trig_idx'])
        out.extend(ss[:MAX_SIG_PER_DIR])
    return out

def eval_signal(day, trig_idx, trig_px, direction):
    c, l, h, t, n = day['close'], day['low'], day['high'], day['times'], day['n']
    h_idx = min(trig_idx + HORIZON, n - 1)
    fut = c[h_idx]
    r30 = (trig_px - fut) / trig_px if direction == 'sell' else (fut - trig_px) / trig_px
    win30 = r30 > 0
    fake = r30 < -FAKE_TH
    idx_1455 = max([i for i, tt in enumerate(t) if tt <= FLAT_TIME], default=n - 1)
    if idx_1455 <= trig_idx:
        idx_1455 = n - 1
    if direction == 'sell':
        seg = l[trig_idx + 1: idx_1455 + 1]
        if len(seg) and seg.min() < trig_px:
            exit_px = float(seg.min()); forced = False
        else:
            exit_px = float(c[idx_1455]); forced = True
        net = (trig_px * (1 - FEE_SELL) - exit_px * (1 + FEE_BUY)) / trig_px
    else:
        seg = h[trig_idx + 1: idx_1455 + 1]
        if len(seg) and seg.max() > trig_px:
            exit_px = float(seg.max()); forced = False
        else:
            exit_px = float(c[idx_1455]); forced = True
        net = (exit_px * (1 - FEE_SELL) - trig_px * (1 + FEE_BUY)) / trig_px
    return {'r30': float(r30), 'win30': bool(win30), 'fake': bool(fake),
            'net': float(net), 'closed_win': bool(net > 0), 'forced': bool(forced)}

def day_type(day, prev_close):
    c, h, l = day['close'], day['high'], day['low']
    chg = (c[-1] - prev_close) / prev_close if prev_close else 0.0
    amp = (h.max() - l.min()) / prev_close if prev_close else 0.0
    tags = ['up_day' if chg > 0 else 'down_day']
    tags.append('amp_gt3' if amp > 0.03 else 'amp_le3')
    return tags, float(chg), float(amp)

def agg(records):
    if not records:
        return {'n': 0}
    n = len(records)
    return {
        'n': n,
        'win30_rate': round(sum(r['win30'] for r in records) / n, 4),
        'avg_r30_pct': round(100 * sum(r['r30'] for r in records) / n, 3),
        'closed_win_rate': round(sum(r['closed_win'] for r in records) / n, 4),
        'avg_net_pct': round(100 * sum(r['net'] for r in records) / n, 3),
        'fake_rate': round(sum(r['fake'] for r in records) / n, 4),
        'forced_rate': round(sum(r['forced'] for r in records) / n, 4),
    }

def collect(codes, start):
    all_sig = []
    day_cnt = {}
    prev = {}
    files = []
    for code in codes:
        fs = sorted(glob.glob(os.path.join(SNAP, '20*', '*', code + '_*.json')))
        for f in fs:
            dt = os.path.basename(f).replace('.json', '').split('_')[1]
            if dt >= start:
                files.append((code, dt, f))
    files.sort(key=lambda x: (x[0], x[1]))
    for code, dt, f in files:
        day = load_day(f)
        if day is None:
            continue
        day_cnt[code] = day_cnt.get(code, 0) + 1
        sigs = gen_signals(day)
        tags, chg, amp = day_type(day, prev.get(code))
        prev[code] = float(day['close'][-1])
        for s in sigs:
            ev = eval_signal(day, s['trig_idx'], s['trig_px'], s['dir'])
            rec = {'code': code, 'date': dt, 'day_chg_pct': round(100 * chg, 2),
                   'day_amp_pct': round(100 * amp, 2), 'tags': tags}
            rec.update(s); rec.update(ev)
            all_sig.append(rec)
    return all_sig, day_cnt

def summarize(all_sig, day_cnt, label):
    total_days = sum(day_cnt.values())
    out = {'label': label, 'total_stock_days': total_days, 'per_stock_days': day_cnt, 'types': {}}
    for typ in ('top', 'bot_A', 'bot_B'):
        recs = [r for r in all_sig if r['type'] == typ]
        d = {'overall': agg(recs),
             'density_per_stock_day': round(len(recs) / total_days, 4) if total_days else 0,
             'per_stock': {}, 'by_day_type': {}}
        for code in sorted(day_cnt):
            rs = [r for r in recs if r['code'] == code]
            dd = agg(rs)
            dd['density'] = round(len(rs) / day_cnt[code], 4) if day_cnt[code] else 0
            d['per_stock'][code] = dd
        for tag in ('up_day', 'down_day', 'amp_gt3', 'amp_le3'):
            d['by_day_type'][tag] = agg([r for r in recs if tag in r['tags']])
        out['types'][typ] = d
    return out

def random_baseline(codes, start, n_mc=100, seed=42):
    rng = random.Random(seed)
    loaded = []
    for code in codes:
        fs = sorted(glob.glob(os.path.join(SNAP, '20*', '*', code + '_*.json')))
        for f in fs:
            dt = os.path.basename(f).replace('.json', '').split('_')[1]
            if dt >= start:
                day = load_day(f)
                if day is not None:
                    loaded.append((code, day))
    metrics = []
    for _ in range(n_mc):
        recs = []
        for code, day in loaded:
            t = day['times']
            idx_1430 = max([i for i, tt in enumerate(t) if tt <= NO_NEW_AFTER], default=-1)
            pool = list(range(WARMUP, idx_1430 + 1))
            if len(pool) < 2:
                continue
            for idx in rng.sample(pool, 2):
                direction = rng.choice(['sell', 'buy'])
                ev = eval_signal(day, idx, float(day['close'][idx]), direction)
                recs.append(ev)
        metrics.append(agg(recs))
    keys = ['win30_rate', 'avg_r30_pct', 'closed_win_rate', 'avg_net_pct', 'fake_rate', 'forced_rate']
    summ = {}
    for k in keys:
        vals = [m[k] for m in metrics if k in m]
        summ[k] = {'mean': round(float(np.mean(vals)), 4), 'std': round(float(np.std(vals)), 4)}
    return {'n_mc': n_mc, 'n_days': len(loaded), 'metrics': summ}

def main():
    results = {'meta': {
        'experiment': '分时MACD背离做T', 'date': '2026-09-14',
        'params': {'k_swing': K_SWING, 'min_gap_min': MIN_GAP, 'warmup_bars': WARMUP,
                   'no_new_after': NO_NEW_AFTER, 'flat_time': FLAT_TIME,
                   'fee_sell': FEE_SELL, 'fee_buy': FEE_BUY, 'horizon_min': HORIZON,
                   'fake_threshold': FAKE_TH, 'max_sig_per_dir_per_day': MAX_SIG_PER_DIR,
                   'macd': 'DIF=EMA12-EMA26, 1min close 日内序列, 前30根预热期不出信号'},
        'notes': '闭环高抛=触发价卖出后当日低点接回(理想成交,对齐Renko评审口径=收益上限),'
                 '未跌破触发价则14:55了结;低吸镜像。+30min相对触发价,高抛看回落/低吸看反弹。'}}
    samples = {}
    sig, dc = collect(MAIN_CODES, MAIN_START)
    samples['main_12m_5stocks'] = summarize(sig, dc, 'main: last12m x 5 stocks')
    json.dump(sig, open(os.path.join(OUT, 'signals_main.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    sig2, dc2 = collect(REF_CODES, MAIN_START)
    samples['ref_small_3stocks'] = summarize(sig2, dc2, 'ref: 603667/300153/002451')
    json.dump(sig2, open(os.path.join(OUT, 'signals_ref.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    sig3, dc3 = collect(MAIN_CODES, FULL_START)
    samples['full_history_5stocks'] = summarize(sig3, dc3, 'full history 2023+ x 5 stocks')
    samples['random_baseline_main'] = random_baseline(MAIN_CODES, MAIN_START)
    samples['renko_baseline'] = {'closed_win_rate': 0.545, 'win30_rate': 0.491,
                                 'forced_rate': 0.509, 'note': 'Renko week live (owner)'}
    results['samples'] = samples
    out_path = os.path.join(OUT, 'results_2026-09-14.json')
    json.dump(results, open(out_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('saved:', out_path)
    for k, v in samples.items():
        if 'types' not in v:
            continue
        print('')
        print('==', v['label'], '| stock_days =', v['total_stock_days'])
        for typ, d in v['types'].items():
            o = d['overall']
            print('  %-6s n=%4s density=%.3f w30=%s netW=%s fake=%s forced=%s' % (
                typ, o.get('n', 0), d['density_per_stock_day'],
                o.get('win30_rate', '-'), o.get('closed_win_rate', '-'),
                o.get('fake_rate', '-'), o.get('forced_rate', '-')))
    print('')
    print('random baseline:', json.dumps(samples['random_baseline_main']['metrics'], ensure_ascii=False))

if __name__ == '__main__':
    main()
