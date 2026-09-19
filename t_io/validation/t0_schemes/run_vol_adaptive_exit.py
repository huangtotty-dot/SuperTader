# -*- coding: utf-8 -*-
"""高波标的 × 波动自适应出场（2026-09-18 owner 要求）。

## owner 的问题
「标的尽量选择日内波动比较大的 —— 我总觉得肯定有因子可以捕捉日内的相对高点和低点。」

## 先把问题拆成两个可分别证伪的命题

  P1「选波动大的标的」 —— 成本固定 0.136%/往返，可捕获价差随振幅放大
      ⇒ 净 ≈ k×振幅 − 0.136%，振幅越大越可能为正。
  P2「有因子能捕捉日内高低点」 —— 即存在规则使捕获率 k 足够大。

**P1 已用全市场面板证伪**（2026-09-18）：5,494 只中位振幅 3.32%，
保本只需捕获 **4.1%**（0.136/3.32）—— 81% 的标的都够。**约束从来是 k，不是振幅。**

## 本脚本要证伪的最后一格：**高波标的 × 波动自适应出场**

先前所有出场实验（`t_exit_rules/`，12 臂）用的都是**固定阈值**（+0.5% 止盈、
0.3~1.2% 移动止盈）。而固定阈值在**高波标的**上截断更狠：振幅 5% 的票，
+0.5% 只吃到 10%。⇒ **「按波动缩放出场」这一格从未测过。**

本脚本即测这一格：**同一入场，出场阈值按该标的自身波动缩放。**

## 设计：固定入场、只变出场（与 t_exit_rules 同构，差异 100% 归因于出场）

入场 = 生产口径（Renko 向下砖 + 15min MACD hist>0），每日首个、≤14:30。
全部以 14:55 强平兜底。费 卖 0.00121 / 买 0.00015（往返 0.136%，与生产一致）。

出场臂：
  E0_tp05        固定 +0.5% 止盈（现状 baseline，收盘价触发）
  E0hi_tp05      同上但**盘中触及**触发（生产内核拿实时报价）
  VA_tp{frac}    **止盈 = frac × σ_ref**（σ_ref = 前 20 日日内振幅中位数，因果）
  VA_tr{frac}    移动止盈 = frac × σ_ref（峰值回落触发，须曾浮盈 0.3σ）
  CEIL_mfe       完美出场：卖在入场后的最高价（**上界，不可实现**）
  CEIL_hold      持到 14:55（不做T）
  RAND          随机出场根（MC 200 次，**「无技巧」基线**）

## 度量
  净% = (卖×(1−费卖) − 买×(1+费买)) / 买 × 100
  捕获率 k = 毛价差 / 当日振幅 —— **回答"吃到了振幅的百分之几"**
  ⇒ 保本线 k* = 0.136 / 振幅%

用法：python t_io/validation/t0_schemes/run_vol_adaptive_exit.py [--codes ...]
"""
import argparse
import collections
import glob
import json
import os
import sys

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
COST = (FEE_S + FEE_B) * 100          # 往返成本（%），≈0.136
BRICK = 0.003
FORCE = '14:55'
NO_NEW_AFTER = '14:30'
WARMUP = 10
VOL_WIN_DAYS = 20                     # σ_ref 回看天数
MIN_1M = 100
SIGMA_FLOOR = 0.005                   # σ_ref 下限（防极端低波票把阈值压到 0）
N_MC = 200
MC_SEED = 42

TP_FRACS = (0.20, 0.30, 0.40, 0.50)
TR_FRACS = (0.25, 0.40)
TRAIL_ACTIVATE_FRAC = 0.30            # 须曾浮盈 0.3σ 才启动移动止盈

EXITS = (['E0_tp05', 'E0hi_tp05']
         + [f'VA_tp{int(f * 100)}' for f in TP_FRACS]
         + [f'VA_tr{int(f * 100)}' for f in TR_FRACS]
         + ['CEIL_mfe', 'CEIL_hold', 'RAND', 'RAND_E'])


def renko_dirs(c):
    from core.t_decision import RenkoBuilder
    b = RenkoBuilder(brick_size_pct=BRICK)
    out = []
    for i in range(len(c)):
        b.update(i, float(c[i]), float(c[i]), float(c[i]))
        out.append(b.brick_direction)
    return out


def h15_hist(c, t):
    """15min MACD hist，只用已完成的桶（因果）。"""
    buckets, labels = {}, []
    for i, tt in enumerate(t):
        m = int(tt[:2]) * 60 + int(tt[3:])
        k = ((m + 14) // 15) * 15 if tt != '09:30' else 570
        if k not in buckets:
            buckets[k] = []
            labels.append(k)
        buckets[k].append(i)
    out = np.zeros(len(c))
    if len(labels) < 3:
        return out
    h = macd_hist(np.array([c[buckets[k][-1]] for k in labels], float))
    for j, k in enumerate(labels):
        v = h[j - 1] if j > 0 else 0.0
        for i in buckets[k]:
            out[i] = v
    return out


def ema(a, span):
    o = np.empty_like(a)
    o[0] = a[0]
    al = 2.0 / (span + 1)
    for i in range(1, len(a)):
        o[i] = al * a[i] + (1 - al) * o[i - 1]
    return o


def macd_hist(c):
    dif = ema(c, 12) - ema(c, 26)
    return (dif - ema(dif, 9)) * 2.0


def entry_bar(dirs, h15, labels):
    for i in range(max(1, WARMUP), len(dirs)):
        if labels[i] > NO_NEW_AFTER:
            break
        if dirs[i] == 'down' and h15[i] > 0:
            return i
    return None


def day_sigma(day_rows, i):
    """前 VOL_WIN_DAYS 日日内振幅中位数（不含当日，因果）。"""
    hist = [r['amp'] for r in day_rows[max(0, i - VOL_WIN_DAYS):i]]
    if not hist:
        return None
    return max(float(np.median(hist)), SIGMA_FLOOR)


def simulate(kind, e, c, hi, lo, labels, sigma, rng):
    """从入场 e 推演到 14:55。返回 (exit_idx, reason)。"""
    n = len(c)
    j1455 = max([i for i, t in enumerate(labels) if t <= FORCE], default=n - 1)
    if j1455 <= e:
        return n - 1, 'eod'
    tp = None
    trail = None
    if kind.startswith('VA_tp'):
        tp = int(kind[5:]) / 100.0 * sigma
    elif kind.startswith('VA_tr'):
        trail = int(kind[6:]) / 100.0 * sigma
    activate = TRAIL_ACTIVATE_FRAC * sigma
    peak = c[e]
    if kind == 'CEIL_hold':
        return j1455, 'hold1455'
    if kind == 'CEIL_mfe':
        seg = hi[e + 1:j1455 + 1]
        if len(seg) == 0:
            return j1455, 'hold1455'
        return e + 1 + int(np.argmax(seg)), 'mfe'
    if kind == 'RAND':
        return int(rng.integers(e + 1, j1455 + 1)), 'rand'
    for i in range(e + 1, j1455 + 1):
        peak = max(peak, c[i])
        hit = None
        if kind in ('E0_tp05', 'E0hi_tp05'):
            ref = hi[i] if kind == 'E0hi_tp05' else c[i]
            if ref >= c[e] * (1 + 0.005):
                hit = 'tp'
        elif tp is not None:
            if hi[i] >= c[e] * (1 + tp):      # 盘中触及
                hit = 'tp_vol'
        elif trail is not None:
            if peak >= c[e] * (1 + activate) and c[i] <= peak * (1 - trail):
                hit = 'trail_vol'
        if hit:
            return i, hit
    return j1455, 'force1455'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--out', default=os.path.join(OUT, 'results_vol_adaptive_exit.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else sorted(
        {os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
         for f in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})
    v2.END = '2026-08-26'

    rng = np.random.default_rng(MC_SEED)
    rows = collections.defaultdict(list)
    code_sigma = {}
    n_days = n_entry = 0

    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        dates = [d for d in dates if '2025-09-14' <= d <= '2026-08-26']
        # 先算每日振幅（供 σ_ref 回看）
        day_amp = {}
        for dt in dates:
            bars = merged[dt]
            if len(bars) < MIN_1M:
                continue
            h = np.array([b['h'] for b in bars], float)
            l = np.array([b['l'] for b in bars], float)
            cl = float(bars[-1]['c'])
            if cl > 0 and h.max() > 0:
                day_amp[dt] = float((h.max() - l.min()) / cl)
        usable = [d for d in dates if d in day_amp]
        day_rows = [{'d': d, 'amp': day_amp[d]} for d in usable]
        if not day_rows:
            continue
        sig_list = [day_amp[d] for d in usable]
        code_sigma[code] = float(np.median(sig_list)) * 100

        for idx, dt in enumerate(usable):
            n_days += 1
            bars = merged[dt]
            t = [b['t'] for b in bars]
            c = np.array([b['c'] for b in bars], float)
            hi = np.array([b['h'] for b in bars], float)
            lo = np.array([b['l'] for b in bars], float)
            dirs = renko_dirs(c)
            h15 = h15_hist(c, t)
            e = entry_bar(dirs, h15, t)
            if e is None:
                continue
            sigma = day_sigma(day_rows, idx)
            if sigma is None:
                continue
            n_entry += 1
            amp_pct = day_amp[dt] * 100
            # RAND_E 对照：随机入场 + 生产出场（隔离「入场是否有 edge」）
            e_noafter = max([i for i, x in enumerate(t) if x <= NO_NEW_AFTER],
                            default=len(t) - 1)
            e_rand = int(rng.integers(WARMUP, e_noafter + 1)) if e_noafter > WARMUP else e
            for kind in EXITS:
                if kind == 'RAND_E':
                    xi, why = simulate('E0_tp05', e_rand, c, hi, lo, t, sigma, rng)
                    buy, sell = float(c[e_rand]), float(c[xi])
                else:
                    xi, why = simulate(kind, e, c, hi, lo, t, sigma, rng)
                    buy, sell = float(c[e]), float(c[xi])
                gross = 100 * (sell - buy) / buy
                net = 100 * (sell * (1 - FEE_S) - buy * (1 + FEE_B)) / buy
                rows[kind].append({
                    'code': code, 'date': dt, 'net': net, 'gross': gross,
                    'win': net > 0, 'hold_min': xi - e, 'reason': why,
                    'amp_pct': amp_pct, 'sigma_pct': sigma * 100,
                    'k_capture': gross / amp_pct if amp_pct > 0 else 0.0,
                })

    print(f'[vol-exit] 池 {len(codes)} 只  有数据的天={n_days}  有入场的天={n_entry}')
    print(f'  往返成本 = {COST:.3f}%   σ_ref 回看 = 前 {VOL_WIN_DAYS} 日振幅中位数')
    print(f'  池内振幅中位数（各票 1 年）: '
          f'{np.median(list(code_sigma.values())):.2f}%   '
          f'区间 {min(code_sigma.values()):.2f}~{max(code_sigma.values()):.2f}%')

    summary = {}
    print(f"\n{'臂':12}{'n':>6}{'净均%':>9}{'中位%':>9}{'胜率':>8}"
          f"{'毛均%':>9}{'捕获k':>8}{'保本k*':>8}{'持仓min':>9}  出场分布")
    for kind in EXITS:
        r = rows[kind]
        if not r:
            continue
        net = np.array([x['net'] for x in r])
        gr = np.array([x['gross'] for x in r])
        hm = np.array([x['hold_min'] for x in r])
        kc = np.array([x['k_capture'] for x in r])
        kstar = COST / np.array([x['amp_pct'] for x in r])
        dist = dict(collections.Counter(x['reason'] for x in r))
        summary[kind] = {
            'n': len(r), 'avg_net': round(float(net.mean()), 4),
            'median_net': round(float(np.median(net)), 4),
            'win_rate': round(float((net > 0).mean()), 4),
            'avg_gross': round(float(gr.mean()), 4),
            'capture_k': round(float(kc.mean()), 4),
            'breakeven_k': round(float(kstar.mean()), 4),
            'avg_hold_min': round(float(hm.mean()), 1),
            'exit_dist': dist,
        }
        print(f"{kind:12}{len(r):>6}{net.mean():>9.3f}{np.median(net):>9.3f}"
              f"{(net > 0).mean():>8.3f}{gr.mean():>9.3f}{kc.mean():>8.3f}"
              f"{kstar.mean():>8.3f}{hm.mean():>9.1f}  {dist}")

    # 按标的波动分位看「选波动大的标的」是否成立
    allamp = np.array([x['amp_pct'] for x in rows['E0_tp05']])
    qs = np.percentile(allamp, [0, 20, 40, 60, 80, 100])
    print(f'\n按当日振幅分位 × 臂（净均%）  —— P1「振幅越大越好」的检验')
    hdr = '  分位'.ljust(10) + ''.join(f'{k:>12}' for k in EXITS)
    print(hdr)
    buckets = {}
    for b in range(5):
        lo_, hi_ = qs[b], qs[b + 1]
        line = f'  Q{b+1} {lo_:>4.1f}~{hi_:>4.1f}%'
        for kind in EXITS:
            sel = [x['net'] for x in rows[kind] if lo_ <= x['amp_pct'] <= hi_]
            line += f'{(np.mean(sel) if sel else float("nan")):>12.3f}'
            buckets.setdefault(kind, []).append(
                {'q': b + 1, 'lo': round(float(lo_), 2), 'hi': round(float(hi_), 2),
                 'n': len(sel), 'net': round(float(np.mean(sel)), 4) if sel else None})
        print(line)

    json.dump({'meta': {'codes': len(codes), 'days': n_days, 'entries': n_entry,
                        'cost_pct': COST, 'sigma_win_days': VOL_WIN_DAYS,
                        'code_sigma_pct': {k: round(v, 3) for k, v in code_sigma.items()}},
               'summary': summary, 'by_quantile': buckets, 'rows': dict(rows)},
              open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
    print(f'\n[vol-exit] -> {args.out}')


if __name__ == '__main__':
    main()
