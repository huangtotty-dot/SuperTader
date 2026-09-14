# -*- coding: utf-8 -*-
"""日内漂移（intraday drift）验证与选股因子探索 — 2026-09-14 owner 要求。

## 缘起
T0 方案矩阵实验（`t_io/validation/t0_schemes/`）里，**同格随机基线**无意中测出一阶效应：
「随机时点入场 → 持到 14:55」平均 **+0.233%/笔**，且 A3|B5 的同格随机基线达 +0.771%。
这与调研 Q1 的结构性发现一致：「A股低开/日内收益为正」是制度性现象
（沪深300 日内年化 **+29.2%** vs 隔夜 **−19.3%**）；而 6 类做T信号在同样本上**全负**。

**所以本实验把问题从"挑做T信号"换成"验证并捕捉这个漂移"**——若成立，它是标的筛选层的事。

## 测什么
最简单可交易的形态：**当日买入 → 当日卖出**（回到 base，属正T口径）。
  臂 = (入场时点, 出场时点) 组合，全部 1min 收盘价成交，双边费 0.136%。
并检验调研点名的**低开因子**：`gap = 开盘/昨收 − 1` 是否预测日内收益。

用法：python t_io/validation/intraday_drift/run_experiment.py [--codes ...]
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

import run_experiment_v2 as v2  # noqa: E402

OUT = HERE
FEE_S, FEE_B = 0.00121, 0.00015
ARMS = [('09:31', '14:55'), ('10:00', '14:55'), ('10:30', '14:55'),
        ('13:01', '14:55'), ('09:31', '15:00'), ('10:00', '15:00')]
GAP_BUCKETS = [(-1.0, -0.03, '<-3%'), (-0.03, -0.01, '-3~-1%'), (-0.01, 0.0, '-1~0%'),
               (0.0, 0.01, '0~1%'), (0.01, 0.03, '1~3%'), (0.03, 1.0, '>3%')]
MIN_1M = 100


def discover():
    return sorted({os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
                   for f in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})


def net_long(buy, sell):
    return 100 * (sell * (1 - FEE_S) - buy * (1 + FEE_B)) / buy


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--out', default=os.path.join(OUT, 'results_2026-09-14.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else discover()
    v2.END = '2026-08-26'

    rows = []          # 每票每日：各臂净收益 + gap + 日型
    per_code = {}
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        prev_close = 0.0
        for dt in dates:
            if dt < '2025-09-14' or dt > '2026-08-26':
                prev_close = merged[dt][-1]['c'] if merged[dt] else prev_close
                continue
            bars = merged[dt]
            if len(bars) < MIN_1M:
                prev_close = bars[-1]['c'] if bars else prev_close
                continue
            lm = {b['t']: i for i, b in enumerate(bars)}
            op = bars[0]['o']
            rec = {'code': code, 'date': dt,
                   'gap': (op / prev_close - 1) if prev_close > 0 else None,
                   'day_ret': (bars[-1]['c'] / prev_close - 1) if prev_close > 0 else None,
                   'amp': (max(b['h'] for b in bars) - min(b['l'] for b in bars)) / op if op > 0 else None}
            for (tin, tout) in ARMS:
                i, j = lm.get(tin), lm.get(tout)
                rec[f'{tin}->{tout}'] = (net_long(bars[i]['c'], bars[j]['c'])
                                         if (i is not None and j is not None and i < j) else None)
            rows.append(rec)
            prev_close = bars[-1]['c']

    print(f'[drift] codes={len(codes)} 股票·日={len(rows)}')
    print(f"\n{'臂(买→卖)':16}{'n':>7}{'净均%':>9}{'中位%':>9}{'胜率':>8}{'t统计':>9}")
    summary = {}
    for (tin, tout) in ARMS:
        k = f'{tin}->{tout}'
        v = np.array([r[k] for r in rows if r[k] is not None])
        if not len(v):
            continue
        t = float(v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))) if len(v) > 2 and v.std() else 0.0
        summary[k] = {'n': len(v), 'avg': round(float(v.mean()), 4),
                      'median': round(float(np.median(v)), 4),
                      'win_rate': round(float((v > 0).mean()), 4), 't_stat': round(t, 2)}
        print(f"{k:16}{len(v):>7}{v.mean():>9.3f}{np.median(v):>9.3f}{(v>0).mean():>8.3f}{t:>9.2f}")

    # 低开因子：gap 分桶 → 日内收益（用 09:31→14:55 臂）
    print(f"\n{'gap 分桶':12}{'n':>7}{'09:31→14:55 净均%':>20}{'胜率':>8}")
    gap_tab = {}
    for lo, hi, lab in GAP_BUCKETS:
        v = np.array([r['09:31->14:55'] for r in rows
                      if r.get('gap') is not None and r['09:31->14:55'] is not None
                      and lo <= r['gap'] < hi])
        if not len(v):
            continue
        gap_tab[lab] = {'n': len(v), 'avg': round(float(v.mean()), 4),
                        'win_rate': round(float((v > 0).mean()), 4)}
        print(f"{lab:12}{len(v):>7}{v.mean():>20.3f}{(v>0).mean():>8.3f}")

    # 逐票：09:31→14:55 的稳定性
    print(f"\n逐票 09:31→14:55 净均%（前 12 / 后 5）")
    byc = {}
    for r in rows:
        if r['09:31->14:55'] is not None:
            byc.setdefault(r['code'], []).append(r['09:31->14:55'])
    vals = sorted(((c, float(np.mean(v)), len(v)) for c, v in byc.items()), key=lambda x: -x[1])
    for c, m, n in vals[:12]:
        print(f"   {c:8}{m:>8.3f}%  (n={n})")
    print('   ...')
    for c, m, n in vals[-5:]:
        print(f"   {c:8}{m:>8.3f}%  (n={n})")
    pos = sum(1 for _, m, _ in vals if m > 0)
    print(f"\n   正净均票数: {pos}/{len(vals)}")
    summary['per_code'] = {c: round(m, 4) for c, m, _ in vals}
    summary['n_codes_pos'] = pos
    summary['gap_table'] = gap_tab

    json.dump({'meta': {'codes': len(codes), 'stock_days': len(rows)},
               'summary': summary}, open(args.out, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(f'\n[drift] -> {args.out}')


if __name__ == '__main__':
    main()
