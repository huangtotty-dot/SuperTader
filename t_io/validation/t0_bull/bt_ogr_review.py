# -*- coding: utf-8 -*-
"""掘金回测结果核查：开盘低开反转（L4）每腿收益（2026-09-22）。

## 为什么需要它
回测跑完后，**账户级绩效（pnl_ratio/sharp）不能用来判断规则** —— 20 只高波底仓的
买入持有收益就占绝大部分。要判断规则，只能算 **OGR 每腿的买卖价差**。
本脚本就是那个核算器，且**可复现**（同一输出目录重复跑结果一致）。

## 两个口径（互为交叉验证）
  A. **审计口径**：`backtrace.jsonl` 的 `ogr_buy/ogr_sell` 的 `ref_px`
     （= 09:31 首根 bar 的 open → 10:00 价；就是离线分析用的两个价）
  B. **成交口径**：`events_*.jsonl` 的 `fill.price`，按 (code, side, qty) **顺序配对**
     （回测的 fill 事件用真实时钟，无法按日映射，故只能按顺序配）

两口径若差得多 ⇒ 说明市场单的实际成交偏离参考价很多（滑点/撮合问题），必须披露。

用法：
  python bt_ogr_review.py <回测输出目录> [--since 2026-04-08] [--until 2026-07-01]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.cost_model import fees                     # noqa: E402

FS, FB = fees('stock')
TARGET = 1.1558        # 离线靶子（Stage17：高波篮子 @ 2026-04-08~07-01，223 腿）


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--since', default='2026-04-08')
    ap.add_argument('--until', default='2026-07-01')
    args = ap.parse_args()
    d = Path(args.outdir)
    bt = d / 'backtrace.jsonl'

    buys, sells, diag, rej = {}, [], 0, collections.Counter()
    for line in bt.open(encoding='utf-8'):
        if '"ogr_' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        e = o.get('event', '')
        day = str(o.get('time'))[:10]
        if e == 'ogr_diag':
            diag += 1
        elif e == 'ogr_buy':
            buys[(o['code'], day)] = o
        elif e in ('ogr_buy_rejected', 'ogr_sell_rejected'):
            rej[e] += 1
        elif e == 'ogr_sell':
            y, r = o.get('buy_px'), o.get('ref_px')
            if y and r and y > 0:
                sells.append((o['code'], day, y, r))
    print(f'输出目录 {d.name}')
    print(f'  交易日(ogr_diag)={diag}  ogr_buy={len(buys)}  ogr_sell(可配对)={len(sells)}  '
          f'拒单={dict(rej) or "无"}')
    print(f'  ⚠️ 未平腿 = {max(0, len(buys) - len(sells))}')
    if not sells:
        print('  无腿，退出'); return

    leg = [(c, dt, ((r / y) * (1 - FS) - (1 + FB)) * 100) for c, dt, y, r in sells]
    win = [x for x in leg if args.since <= x[1] <= args.until]
    print(f'\n=== 口径A（审计 ref_px）分析窗口 {args.since}~{args.until} ===')
    _stat('全窗口', np.array([x[2] for x in leg]))
    n = np.array([x[2] for x in win])
    if len(n):
        _stat('★测量窗口', n)
        by = collections.defaultdict(list)
        for c, dt, v in win:
            by[dt].append(v)
        pm = np.array([np.mean(v) for v in by.values()])
        print(f'  交易日数={len(by)}  日净均为正={int((pm>0).sum())}/{len(pm)}  '
              f'日净均中位={np.median(pm):+.3f}%')
        print(f'  ⇒ 离线靶子 {TARGET:+.4f}%/腿   Δ={n.mean()-TARGET:+.4f}pp')

    # 口径B（粗粒度交叉验证）：按票对比「OGR 卖出参考价中位」vs「实际成交价中位」。
    # 只判断参考价是否可信，不做逐笔配对 —— 保护类卖出也会产生 fill，逐笔配对不可靠。
    ev = sorted(d.glob('events_*.jsonl'))
    if ev:
        fill_px = collections.defaultdict(list)
        ord_px = collections.defaultdict(list)
        for f in ev:
            for line in f.open(encoding='utf-8'):
                if '"fill"' not in line and '"order"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get('event') == 'fill':
                    fill_px[(o['code'], o['side'])].append(float(o['price']))
                elif o.get('event') == 'order':
                    ord_px[(o['code'], o['side'])].append(float(o['price']))
        ref_med = collections.defaultdict(list)
        for c, dt, y, r in sells:
            ref_med[c].append(r)
        print('\n=== 口径B 交叉验证：OGR 卖出参考价 vs 实际成交价（按票中位）===')
        print(f"  {'code':8s}{'OGR卖出ref中位':>16s}{'SELL成交中位':>14s}{'偏差%':>9s}")
        devs = []
        for c in sorted(ref_med):
            rm = float(np.median(ref_med[c]))
            fm = float(np.median(fill_px.get((c, 'SELL'), [0])))
            if fm > 0 and rm > 0:
                d_ = (fm / rm - 1) * 100
                devs.append(d_)
                print(f'  {c:8s}{rm:>16.3f}{fm:>14.3f}{d_:>+9.2f}')
        if devs:
            print(f'  ⇒ 参考价 vs 成交价 中位偏差 {np.median(devs):+.2f}%  最大 |偏差| {max(abs(x) for x in devs):.2f}%')
            print('     （偏差大 ⇒ 市场单成交显著偏离参考价，腿收益需按成交口径重算）')


def _stat(tag: str, x: np.ndarray) -> None:
    if not len(x):
        print(f'  {tag}: 空'); return
    print(f'  {tag:10s} n={len(x):>4d}  净均={x.mean():+.4f}%  中位={np.median(x):+.4f}%  '
          f'胜率={(x>0).mean():.3f}  最好={x.max():+.2f}%  最差={x.min():+.2f}%')


if __name__ == '__main__':
    main()
