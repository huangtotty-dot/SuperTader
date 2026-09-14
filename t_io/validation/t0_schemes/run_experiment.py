# -*- coding: utf-8 -*-
"""日内 T+0 完整方案矩阵实验（A1–A6 入场 × B1/B5 出场）— 2026-09-14 owner 要求。

预注册：`C:\\Users\\Lenovo\\.claude\\plans\\mighty-sniffing-feigenbaum.md`
来源：`doc/research/2026-09-14_日内T0完整方案_综合清单.md`（四源调研 Q1–Q4）

## 为什么做
本会话已证**出场不是瓶颈**（11 种出场净均值全在 [−0.12,+0.02]）→ 按调研指引主攻**入场侧**。

## 归因模型（Q4「总发现3」点名必须重写）
A股 T+1 ⇒ 每个信号拆成**一条当日闭合的往返腿**：
  正T(long) 买现金→卖：净 = (卖×(1−费卖) − 买×(1+费买)) / 买
  反T(short) 卖底仓→买回：净 = (卖×(1−费卖) − 买回×(1+费买)) / 卖
两腿当日闭合（14:55 强平兜底）。费 卖0.00121/买0.00015（双边 0.136%，与生产一致）。

## 预注册取值（文档未写死处，已声明）
  A5: r1=close(10:00)/close(09:30)−1, r7=close(14:30)/close(14:00)−1（close-to-close）
  A6: 收阳 = close(11:30) > open(09:30)
  A2: 变体甲固定锚±1%、变体乙移动锚 g∈{0.8,1.0,1.2}%；连买≤3
  波动率门槛: 前 20 日日内振幅中位数 ≥ 2.0%
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
_MD = os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')
for _p in (ROOT, _MD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_experiment_v2 as v2  # noqa: E402  复用 1min 数据层

OUT = HERE
FEE_S, FEE_B = 0.00121, 0.00015
TP = 0.005                 # B1 +0.5%
FORCE_LABEL = '14:55'
A1_TIMES = ('10:29', '11:29', '13:59')
GRID_G = (0.008, 0.010, 0.012)
DT_N, DT_K = 4, 0.5        # Dual Thrust: 前 N 日（不含今日）, K1=K2=K
VOL_GATE = 0.020           # 前 20 日日内振幅中位数门槛
MIN_1M = 100
N_MC = 200

ENTRIES = ['A1_noise', 'A2_grid_fixed', 'A2_grid_move08', 'A3_rbreaker',
           'A4_dualthrust', 'A5_momentum', 'A6_halfday']
EXITS = ['B1_tp05', 'B5_rbreaker_rev']


# ---------------- 工具 ----------------
def lbl_map(t):
    return {x: i for i, x in enumerate(t)}


def bars_of(day):
    return (np.array([b['o'] for b in day], float), np.array([b['h'] for b in day], float),
            np.array([b['l'] for b in day], float), np.array([b['c'] for b in day], float),
            np.array([b['v'] for b in day], float))


def force_idx(t):
    idx = [i for i, x in enumerate(t) if x <= FORCE_LABEL]
    return idx[-1] if idx else len(t) - 1


# ---------------- 出场模块 ----------------
def exit_tp05(direction, ei, fill, o, h, l, c, hi_bar):
    """B1：正T 到 +0.5% 卖 / 反T 到 −0.5% 买回。
    未达标返回 (None,'end') —— 由调用方决定"被下一条信号反手"还是"14:55 强平"。"""
    tgt = fill * (1 + TP) if direction == 'long' else fill * (1 - TP)
    for j in range(ei + 1, hi_bar + 1):
        if direction == 'long' and h[j] >= tgt:
            return tgt, 'tp'
        if direction == 'short' and l[j] <= tgt:
            return tgt, 'tp'
    return None, 'end'


def exit_rb_rev(direction, ei, fill, o, h, l, c, hi_bar, y_h, y_l, y_c):
    """B5 = R-Breaker 反转腿（两段确认）：持多须**当日最高价曾破 sSetup**、随后跌破 sEnter → 平；
    持空须**当日最低价曾破 bSetup**、随后升破 bEnter → 平。未达标在调用方兜底。"""
    if not (y_h and y_l and y_c):
        return None, 'end'
    piv = (y_h + y_l + y_c) / 3.0
    ssetup = piv + (y_h - y_l)
    senter = 2 * piv - y_l
    bsetup = piv - (y_h - y_l)
    benter = 2 * piv - y_h
    hit = False
    for j in range(ei + 1, hi_bar + 1):
        if direction == 'long':
            if h[j] >= ssetup:
                hit = True
            if hit and c[j] < senter:
                return c[j], 'rb_rev'
        else:
            if l[j] <= bsetup:
                hit = True
            if hit and c[j] > benter:
                return c[j], 'rb_rev'
    return None, 'end'


def _leg_pnl(direction, fill, out):
    if direction == 'long':
        return 100 * (out * (1 - FEE_S) - fill * (1 + FEE_B)) / fill
    return 100 * (fill * (1 - FEE_S) - out * (1 + FEE_B)) / fill


# ---------------- 入场模块 ----------------
def a1_noise(t, o, h, l, c, hist_disp, prev_close, lm):
    """西部噪声带：过去14日同一分钟位移均值构成带；仅三时点判突破。"""
    if hist_disp is None or prev_close <= 0:
        return []
    disp = hist_disp  # {label: mean_disp}
    op = o[0]
    out = []
    for tt in A1_TIMES:
        i = lm.get(tt)
        if i is None or i + 1 >= len(c):
            continue
        m = disp.get(tt)
        if not m:
            continue
        up, lo = max(op * (1 + m), prev_close), min(op * (1 - m), prev_close)
        if c[i] > up:
            out.append((i + 1, 'long'))
        elif c[i] < lo:
            out.append((i + 1, 'short'))
    return out


def a2_grid_fixed(t, o, h, l, c, prev_close, lm):
    """变体甲：昨收为中心 ±1% 一格；下穿买、上穿卖。"""
    if prev_close <= 0:
        return []
    lo_p, hi_p = prev_close * 0.99, prev_close * 1.01
    out = []
    for i in range(1, len(c) - 1):
        if c[i - 1] > lo_p >= c[i]:
            out.append((i + 1, 'long'))
        elif c[i - 1] < hi_p <= c[i]:
            out.append((i + 1, 'short'))
    return out


def a2_grid_move(t, o, h, l, c, prev_close, lm, g):
    """变体乙：移动锚——价格≤锚×(1−g)买、≥锚×(1+g)卖，触发后锚点重置为现价。"""
    if prev_close <= 0:
        return []
    anchor, out = prev_close, []
    for i in range(len(c) - 1):
        if c[i] <= anchor * (1 - g):
            out.append((i + 1, 'long')); anchor = c[i]
        elif c[i] >= anchor * (1 + g):
            out.append((i + 1, 'short')); anchor = c[i]
    return out


def a3_rbreaker(t, o, h, l, c, y_h, y_l, y_c, lm):
    """R-Breaker（Saidenberg pivot 版）：六轨 + 反转腿出场另配 B5。"""
    if not (y_h and y_l and y_c):
        return []
    piv = (y_h + y_l + y_c) / 3.0
    bbreak = y_h + 2 * (piv - y_l)
    sbreak = y_l - 2 * (y_h - piv)
    out = []
    for i in range(len(c) - 1):
        if c[i] > bbreak:
            out.append((i + 1, 'long')); break
        if c[i] < sbreak:
            out.append((i + 1, 'short')); break
    return out


def a4_dualthrust(t, o, h, l, c, dt_range, lm):
    """Dual Thrust：Range=Max(HH−LC,HC−LL)（前N日不含今日）；买=开+K×R、卖=开−K×R。"""
    if not dt_range:
        return []
    op = o[0]
    buy_line, sell_line = op + DT_K * dt_range, op - DT_K * dt_range
    for i in range(len(c) - 1):
        if c[i] > buy_line:
            return [(i + 1, 'long')]
        if c[i] < sell_line:
            return [(i + 1, 'short')]
    return []


def a5_momentum(t, o, h, l, c, lm):
    """首半小时 r1 与第七半小时 r7 同向 → 14:30 顺向开仓。"""
    i1a, i1b = lm.get('09:30'), lm.get('10:00')
    i7a, i7b = lm.get('14:00'), lm.get('14:30')
    if None in (i1a, i1b, i7a, i7b) or c[i1a] <= 0 or c[i7a] <= 0:
        return []
    r1 = c[i1b] / c[i1a] - 1
    r7 = c[i7b] / c[i7a] - 1
    if r1 == 0 or r7 == 0 or (r1 > 0) != (r7 > 0):
        return []
    i = i7b
    return [(i + 1, 'long' if r1 > 0 else 'short')] if i + 1 < len(c) else []


def a6_halfday(t, o, h, l, c, lm):
    """上午收阳 → 午后开盘买；收阴 → 午后开盘卖底仓。

    注：本数据为**终点标签**且无 '13:00'（午休后第一根是 '13:01'），故用 13:01 代表午后开盘。
    """
    i_am = lm.get('11:30')
    i_pm = lm.get('13:01')
    if i_am is None or i_pm is None or i_pm + 1 >= len(c):
        return []
    return [(i_pm + 1, 'long' if c[i_am] > o[0] else 'short')]


# ---------------- 单日回放 ----------------
def run_day(day_ctx, entry, exit_kind):
    t, o, h, l, c, v = day_ctx['t'], day_ctx['o'], day_ctx['h'], day_ctx['l'], day_ctx['c'], day_ctx['v']
    lm = day_ctx['lm']
    j_f = force_idx(t)
    sigs = []
    if entry == 'A1_noise':
        sigs = a1_noise(t, o, h, l, c, day_ctx['hist_disp'], day_ctx['prev_close'], lm)
    elif entry == 'A2_grid_fixed':
        sigs = a2_grid_fixed(t, o, h, l, c, day_ctx['prev_close'], lm)
    elif entry.startswith('A2_grid_move'):
        g = {'A2_grid_move08': 0.008, 'A2_grid_move10': 0.010, 'A2_grid_move12': 0.012}[entry]
        sigs = a2_grid_move(t, o, h, l, c, day_ctx['prev_close'], lm, g)
    elif entry == 'A3_rbreaker':
        sigs = a3_rbreaker(t, o, h, l, c, day_ctx['y_h'], day_ctx['y_l'], day_ctx['y_c'], lm)
    elif entry == 'A4_dualthrust':
        sigs = a4_dualthrust(t, o, h, l, c, day_ctx['dt_range'], lm)
    elif entry == 'A5_momentum':
        sigs = a5_momentum(t, o, h, l, c, lm)
    elif entry == 'A6_halfday':
        sigs = a6_halfday(t, o, h, l, c, lm)

    pairs = []
    sigs = [(b, d) for b, d in sigs if 1 <= b <= j_f and o[b] > 0]
    # 仓位状态机：同一时刻至多一条腿；下一条信号 = 反手平掉前一条腿（网格/DualThrust 语义）
    for k, (ei, direction) in enumerate(sigs):
        fill = o[ei]
        nxt = sigs[k + 1][0] if k + 1 < len(sigs) else None
        hi_bar = j_f if (nxt is None or nxt > j_f) else nxt - 1
        if hi_bar < ei:
            continue
        if exit_kind == 'B5_rbreaker_rev':
            ex_px, why = exit_rb_rev(direction, ei, fill, o, h, l, c, hi_bar,
                                     day_ctx['y_h'], day_ctx['y_l'], day_ctx['y_c'])
        else:
            ex_px, why = exit_tp05(direction, ei, fill, o, h, l, c, hi_bar)
        if ex_px is None:                       # 未达出场条件
            if nxt is not None and nxt <= j_f:  # 被下一条信号反手
                ex_px, why = o[nxt], 'reverse'
            else:                               # 14:55 强平兜底
                ex_px, why = c[j_f], 'force1455'
        net = _leg_pnl(direction, fill, ex_px)
        pairs.append({'bar': ei, 'dir': direction, 'fill': fill, 'exit': ex_px,
                      'reason': why, 'net': net, 'win': net > 0})
    return pairs


def day_type(o, h, l, c, prev_close):
    if prev_close <= 0:
        return 'range'
    rng = h.max() - l.min()
    if rng <= 0:
        return 'range'
    chg = c[-1] / prev_close - 1
    if c[-1] > o[0] and (h.max() - c[-1]) / rng < 0.3 and chg > 0.01:
        return 'up'
    if c[-1] < o[0] and (c[-1] - l.min()) / rng < 0.3 and chg < -0.01:
        return 'down'
    return 'range'


def daily_stats(merged, dates):
    """每日期 line 高/低/收/开 + 前20日振幅中位数 + Dual Thrust Range + 昨日 H/L/C。"""
    st = {}
    closes, highs, lows, opens = {}, {}, {}, {}
    for d in dates:
        b = merged[d]
        highs[d] = max(x['h'] for x in b)
        lows[d] = min(x['l'] for x in b)
        opens[d] = b[0]['o']
        closes[d] = b[-1]['c']
    for k, d in enumerate(dates):
        prev = dates[k - 1] if k > 0 else None
        amps = [(highs[x] - lows[x]) / opens[x] for x in dates[max(0, k - 20):k] if opens[x] > 0]
        rng = None
        if k >= DT_N:
            win = dates[k - DT_N:k]                      # 前 N 日，不含今日
            HH = max(highs[x] for x in win); LL = min(lows[x] for x in win)
            HC = max(closes[x] for x in win); LC = min(closes[x] for x in win)
            rng = max(HH - LC, HC - LL)
        st[d] = {'prev_close': closes[prev] if prev else 0.0,
                 'y_h': highs[prev] if prev else 0.0,
                 'y_l': lows[prev] if prev else 0.0,
                 'y_c': closes[prev] if prev else 0.0,
                 'dt_range': rng,
                 'vol_med': float(np.median(amps)) if amps else 0.0}
    return st


def a1_disp_table(merged, dates, k, max_look=14):
    """过去 max_look 日（不含今日）同一时刻的 |收/开−1| 均值。"""
    prior = dates[max(0, k - max_look):k]
    if len(prior) < 5:
        return None
    acc = {}
    for d in prior:
        b = merged[d]
        op = b[0]['o']
        if op <= 0:
            continue
        for x in b:
            acc.setdefault(x['t'], []).append(abs(x['c'] / op - 1))
    return {tt: float(np.mean(v)) for tt, v in acc.items() if v}


def discover():
    return sorted({os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
                   for f in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--out', default=os.path.join(OUT, 'results_2026-09-14.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else discover()
    v2.END = '2026-08-26'

    cells = {}
    rand = {}
    n_days = n_gated = 0
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        dates = [d for d in dates if '2025-09-14' <= d <= '2026-08-26']
        if len(dates) < 30:
            continue
        st = daily_stats(merged, dates)
        for k, d in enumerate(dates):
            day = merged[d]
            if len(day) < MIN_1M:
                continue
            n_days += 1
            s = st[d]
            if s['vol_med'] < VOL_GATE:      # 日度波动率门槛：低波日不开T
                continue
            n_gated += 1
            o, h, l, c, v = bars_of(day)
            j_f_local = force_idx([x['t'] for x in day])
            dctx = {'t': [x['t'] for x in day], 'o': o, 'h': h, 'l': l, 'c': c, 'v': v,
                    'lm': lbl_map([x['t'] for x in day]), 'prev_close': s['prev_close'],
                    'y_h': s['y_h'], 'y_l': s['y_l'], 'y_c': s['y_c'],
                    'dt_range': s['dt_range'],
                    'hist_disp': a1_disp_table(merged, dates, k)}
            dt = day_type(o, h, l, c, s['prev_close'])
            for entry in ENTRIES:
                for ex in EXITS:
                    if ex == 'B5_rbreaker_rev' and entry != 'A3_rbreaker':
                        continue                     # B5 是 A3 自带的反转腿
                    key = f'{entry}|{ex}'
                    day_pairs = run_day(dctx, entry, ex)
                    for p in day_pairs:
                        cells.setdefault(key, []).append({**p, 'code': code, 'date': d, 'day_type': dt})
                    # 同格随机基线（达标线②）：同一 (票,日)、同方向分布、随机入场 bar、同出场模块
                    if day_pairs:
                        import random as _rnd
                        _r = _rnd.Random(hash((code, d, key)) & 0xFFFF)
                        _bars = [p['bar'] for p in day_pairs]
                        _dirs = [p['dir'] for p in day_pairs]
                        for _ in range(N_MC):
                            _d2 = _r.choice(_dirs)
                            _b2 = _r.randint(1, j_f_local)
                            _f2 = o[_b2]
                            if _f2 <= 0:
                                continue
                            if ex == 'B5_rbreaker_rev':
                                _x, _w = exit_rb_rev(_d2, _b2, _f2, o, h, l, c, j_f_local,
                                                     s['y_h'], s['y_l'], s['y_c'])
                            else:
                                _x, _w = exit_tp05(_d2, _b2, _f2, o, h, l, c, j_f_local)
                            if _x is None:
                                _x, _w = c[j_f_local], 'force1455'
                            rand.setdefault(key, []).append(
                                {'net': _leg_pnl(_d2, _f2, _x), 'code': code, 'date': d})
    print(f'[t0] codes={len(codes)} 交易日={n_days} 过波动率门槛={n_gated}')
    print(f"\n{'cell':28}{'n':>6}{'净均%':>9}{'中位%':>9}{'胜率':>8}{'密度/票/日':>11}  出场分布")
    summary = {}
    import collections
    for key in sorted(cells):
        r = cells[key]
        if not r:
            continue
        net = np.array([x['net'] for x in r])
        dist = dict(collections.Counter(x['reason'] for x in r))
        density = len(r) / max(n_gated, 1)
        summary[key] = {'n': len(r), 'avg_net': round(float(net.mean()), 4),
                        'median_net': round(float(np.median(net)), 4),
                        'win_rate': round(float((net > 0).mean()), 4),
                        'density_per_stock_day': round(density, 3),
                        'exit_dist': dist,
                        'by_day_type': {t: {'n': len([x for x in r if x['day_type'] == t]),
                                            'avg': round(float(np.mean([x['net'] for x in r if x['day_type'] == t])), 4)
                                            if any(x['day_type'] == t for x in r) else None}
                                        for t in ('up', 'range', 'down')}}
        rn = np.array([x['net'] for x in rand.get(key, [])]) if rand.get(key) else np.array([])
        delta = round(float(net.mean() - rn.mean()), 4) if len(rn) else None
        summary[key]['random_avg'] = round(float(rn.mean()), 4) if len(rn) else None
        summary[key]['delta_vs_random'] = delta
        print(f"{key:28}{len(r):>6}{net.mean():>9.3f}{np.median(net):>9.3f}"
              f"{(net>0).mean():>8.3f}{density:>11.3f}  rand={summary[key]['random_avg']} Δ={delta}")
    json.dump({'meta': {'codes': len(codes), 'days': n_days, 'gated_days': n_gated,
                        'vol_gate': VOL_GATE, 'dt_N': DT_N, 'dt_K': DT_K},
               'summary': summary}, open(args.out, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(f'\n[t0] -> {args.out}')


if __name__ == '__main__':
    main()
