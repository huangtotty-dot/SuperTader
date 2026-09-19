# -*- coding: utf-8 -*-
"""T 腿出场规则对比实验（2026-09-14 owner 要求）。

## 问题
现状出场只有三条（core/t_decision.py:219-232）：**+0.5% 固定止盈 / 时间止损(已禁用) / 14:55 尾盘强平**。
先验（2026-08-26，39 票全样本）已证：**卖出信号 99% 不触发**（292 笔仅 2 笔正常高抛），
系统实为"追跌买入后持有"。owner 判断"固定 +0.5% 问题很大"，要求改为
**按价格与量能（MACD 趋势）动态止盈**。

## 设计：固定入场、只变出场
入场一律用**生产口径**（Renko 向下砖 + 15min MACD hist > 0，先验 60.6% 有效），
每日取**首个入场**，各出场臂从**同一次入场**独立推演 → 差异 100% 归因于出场规则。

## 出场候选
  E0  +0.5% 固定止盈（现状 baseline）
  E1  移动止盈：自入场后峰值回落 K%（K=0.3/0.5/0.8/1.2%），须曾浮盈>0.2% 才激活
  E2  MACD 动量出场：15min MACD hist 由正转负
  E3  量能出场：成交量 >1.5×前20根均量 且 收盘回落（量增价跌）
  E4  移动止盈(0.5%) 与 量能 先到先算
  E5  移动止盈(0.5%) 与 MACD 先到先算
全部以 **14:55 强制平仓**兜底。费：买 0.00015 / 卖 0.00121（与生产一致）。

数据：`t_io/backtest_1year_data`（39 票 × 1 年 1min）。
用法：python t_io/validation/t_exit_rules/run_experiment.py [--codes ...]
"""
import argparse
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
BRICK = 0.003
TP = 0.005
TRAIL_ACTIVATE = 0.002          # 移动止盈激活门槛（曾浮盈 > 0.2% 才跟踪）
FORCE = '14:55'
NO_NEW_AFTER = '14:30'
WARMUP = 10
VOL_MULT, VOL_WIN = 1.5, 20
MIN_1M, MIN_5M = 100, 30

EXITS = ['E0_tp05', 'E0hi_tp05', 'E1_trail03', 'E1_trail05', 'E1_trail08', 'E1_trail12',
         'E2_macd_flip', 'E3_vol_spike', 'E4_trail05_vol', 'E5_trail05_macd', 'E6_tp_OR_macd', 'E7_tp_OR_trail05']
TRAIL_OF = {'E1_trail03': 0.003, 'E1_trail05': 0.005, 'E1_trail08': 0.008, 'E1_trail12': 0.012}


def ema(a, span):
    o = np.empty_like(a); o[0] = a[0]; al = 2.0 / (span + 1)
    for i in range(1, len(a)):
        o[i] = al * a[i] + (1 - al) * o[i - 1]
    return o


def macd_hist(c):
    dif = ema(c, 12) - ema(c, 26)
    return (dif - ema(dif, 9)) * 2.0


def h15_hist(c, t):
    """15min MACD hist，按桶内**只用上一根已完成桶**（因果）。"""
    buckets, labels = {}, []
    for i, tt in enumerate(t):
        m = int(tt[:2]) * 60 + int(tt[3:])
        k = ((m + 14) // 15) * 15 if tt != '09:30' else 570
        if k not in buckets:
            buckets[k] = []; labels.append(k)
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


def renko_dirs(c):
    from core.t_decision import RenkoBuilder
    b = RenkoBuilder(brick_size_pct=BRICK); out = []
    for i in range(len(c)):
        b.update(i, float(c[i]), float(c[i]), float(c[i])); out.append(b.brick_direction)
    return out


def entry_bar(dirs, h15, labels):
    """生产口径入场：Renko 向下砖 + 15min MACD hist>0，取当日首个、≤14:30。"""
    for i in range(max(1, WARMUP), len(dirs)):
        if labels[i] > NO_NEW_AFTER:
            break
        if dirs[i] == 'down' and h15[i] > 0:
            return i
    return None


def simulate(kind, e, c, v, h15, labels, hi=None):
    """从入场 e 推演到出场，返回 (exit_idx, reason)。14:55 兜底。"""
    n = len(c)
    j1455 = max([i for i, t in enumerate(labels) if t <= FORCE], default=n - 1)
    if j1455 <= e:
        return n - 1, 'eod'
    k = TRAIL_OF.get(kind)
    peak = c[e]
    for i in range(e + 1, j1455 + 1):
        peak = max(peak, c[i])
        hit = None
        if kind == 'E0_tp05':
            if c[i] >= c[e] * (1 + TP):
                hit = 'tp'
        elif kind == 'E0hi_tp05':
            # 生产口径：内核拿的是**实时报价**（可触及盘中高点），非收盘价
            if hi is not None and hi[i] >= c[e] * (1 + TP):
                hit = 'tp'
        elif kind in TRAIL_OF:
            if peak >= c[e] * (1 + TRAIL_ACTIVATE) and c[i] <= peak * (1 - k):
                hit = 'trail'
        elif kind == 'E2_macd_flip':
            if h15[i] < 0 <= h15[i - 1]:
                hit = 'macd'
        elif kind == 'E3_vol_spike':
            w0 = max(0, i - VOL_WIN)
            if i - w0 >= 5 and v[i] > VOL_MULT * v[w0:i].mean() and c[i] < c[i - 1]:
                hit = 'vol'
        elif kind == 'E4_trail05_vol':
            w0 = max(0, i - VOL_WIN)
            if peak >= c[e] * (1 + TRAIL_ACTIVATE) and c[i] <= peak * 0.995:
                hit = 'trail'
            elif i - w0 >= 5 and v[i] > VOL_MULT * v[w0:i].mean() and c[i] < c[i - 1]:
                hit = 'vol'
        elif kind == 'E6_tp_OR_macd':
            # 保留 +0.5% 止盈（守 72% 胜率），只把"14:55 兜底"换成 MACD 提前离场（砍 -1.57% 的尾巴）
            if c[i] >= c[e] * (1 + TP):
                hit = 'tp'
            elif h15[i] < 0 <= h15[i - 1]:
                hit = 'macd'
        elif kind == 'E7_tp_OR_trail05':
            if c[i] >= c[e] * (1 + TP):
                hit = 'tp'
            elif peak >= c[e] * (1 + TRAIL_ACTIVATE) and c[i] <= peak * 0.995:
                hit = 'trail'
        elif kind == 'E5_trail05_macd':
            if peak >= c[e] * (1 + TRAIL_ACTIVATE) and c[i] <= peak * 0.995:
                hit = 'trail'
            elif h15[i] < 0 <= h15[i - 1]:
                hit = 'macd'
        if hit:
            return i, hit
    return j1455, 'force1455'


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
    rows = {k: [] for k in EXITS}
    n_days = 0
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        for dt in dates:
            if dt < '2025-09-14' or dt > '2026-08-26':
                continue
            bars = merged[dt]
            if len(bars) < MIN_1M:
                continue
            t = [b['t'] for b in bars]
            c = np.array([b['c'] for b in bars], float)
            v = np.array([b['v'] for b in bars], float)
            hi = np.array([b['h'] for b in bars], float)
            dirs = renko_dirs(c)
            h15 = h15_hist(c, t)
            e = entry_bar(dirs, h15, t)
            if e is None:
                continue
            n_days += 1
            for kind in EXITS:
                xi, why = simulate(kind, e, c, v, h15, t, hi=hi)
                buy_px, sell_px = float(c[e]), float(c[xi])
                net = 100 * (sell_px * (1 - FEE_S) - buy_px * (1 + FEE_B)) / buy_px
                rows[kind].append({'code': code, 'date': dt, 'net': net,
                                   'win': net > 0, 'hold_min': xi - e, 'reason': why,
                                   'gross': 100 * (sell_px - buy_px) / buy_px})
    print(f'[exit] codes={len(codes)} 有入场的天数={n_days}')
    print(f"\n{'臂':18}{'n':>6}{'净均%':>9}{'中位%':>9}{'胜率':>8}{'均持仓min':>10}  出场分布")
    summary = {}
    for k in EXITS:
        r = rows[k]
        if not r:
            continue
        net = np.array([x['net'] for x in r]); hm = np.array([x['hold_min'] for x in r])
        import collections
        dist = dict(collections.Counter(x['reason'] for x in r))
        summary[k] = {'n': len(r), 'avg_net': round(float(net.mean()), 4),
                      'median_net': round(float(np.median(net)), 4),
                      'win_rate': round(float((net > 0).mean()), 4),
                      'avg_hold_min': round(float(hm.mean()), 1),
                      'exit_dist': dist}
        print(f"{k:18}{len(r):>6}{net.mean():>9.3f}{np.median(net):>9.3f}"
              f"{(net>0).mean():>8.3f}{hm.mean():>10.1f}  {dist}")
    json.dump({'meta': {'codes': len(codes), 'days': n_days}, 'summary': summary, 'rows': rows},
              open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
    print(f'\n[exit] -> {args.out}')


if __name__ == '__main__':
    main()
