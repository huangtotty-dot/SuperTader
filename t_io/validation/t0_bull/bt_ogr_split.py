# -*- coding: utf-8 -*-
"""把 GM 回测的收益拆成「趋势收益（底仓买入持有）」与「做T收益（OGR 叠加）」两笔（2026-09-24）。

## 为什么
owner（2026-09-24）：「年化阈值先跑完 统计出**趋势收益**和**做T收益**后再定夺」。
账户级 `pnl_ratio` 里绝大部分是 20 只高波底仓的买入持有，**不能归功于规则**；
所以必须把两笔分开，否则看合计会严重高估做T的贡献。

## 口径
- **做T收益**：Σ over OGR 平腿 [ `qty×(sell_fill − buy_fill)` − 费 ]（元）。
  费按生产口径 `core/cost_model.fees('stock')`（买 FB / 卖 FS）。价用审计里的**真实成交价**。
- **趋势收益**：底仓买入持有的市值变化 = Σ over 篮子 [ `base_qty × (P_end − P_seed)` ]（元）。
  · P_seed = 事件桥里**每票第一笔买入成交价**（= init 播种底仓那 20 笔）。
  · P_end  = 离线的 d540 面板在**窗口末日**的收盘（`cl_1500`）。
- **合计**另与回测指标 `pnl_ratio × 初始资金` 对照，差额大就提示（现金拖累/取整/复权差异）。

用法：python bt_ogr_split.py <回测输出目录> [--end 2026-07-01] [--initial-cash 5800000]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parents[3]
T0 = Path(__file__).resolve().parent
for _p in (str(ROOT), str(T0)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from core.cost_model import fees      # noqa: E402

FS, FB = fees('stock')


def ogr_legs(d: Path):
    """OGR 平腿：[(code, date, qty, buy_fill, sell_fill)]。"""
    buys, legs = {}, []
    for line in (d / 'backtrace.jsonl').open(encoding='utf-8'):
        if '"ogr_buy"' not in line and '"ogr_sell"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        e = o.get('event')
        if e == 'ogr_buy':
            buys[(o.get('code'), str(o.get('time'))[:10])] = o
        elif e == 'ogr_sell':
            k = (o.get('code'), str(o.get('time'))[:10])
            b = buys.get(k) or {}
            legs.append((o.get('code'), k[1], int(o.get('qty') or 0),
                         b.get('fill_px'), o.get('fill_px')))
    return [x for x in legs if x[3] and x[4] and x[2] > 0]


def base_seed(d: Path):
    """底仓播种价：事件桥里**每票第一笔买入成交** {code: (qty, price)}。"""
    out = {}
    for f in sorted(d.glob('events_*.jsonl')):
        for line in f.open(encoding='utf-8'):
            if '"fill"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get('event') == 'fill' and o.get('side') == 'BUY':
                c = o.get('code')
                if c and c not in out:
                    out[c] = (int(o.get('qty') or 0), float(o.get('price') or 0))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir')
    ap.add_argument('--end', default='2026-07-01')
    ap.add_argument('--initial-cash', type=float, default=5800000.0)
    ap.add_argument('--basket-csv', default=str(T0 / 'vol_basket_2026-04-08.csv'))
    a = ap.parse_args()
    d = Path(a.outdir)

    # ── 做T收益 ──
    legs = ogr_legs(d)
    pnl_t, notional = 0.0, 0.0
    for c, day, q, bf, sf in legs:
        buy_not, sell_not = q * bf, q * sf
        pnl_t += (sell_not - buy_not) - (buy_not * FB + sell_not * FS)
        notional += buy_not
    n = len(legs)
    print(f'=== {d.name} ===')
    print(f'【做T收益】OGR 平腿 {n} 条')
    if n:
        print(f'  净收益 = {pnl_t:+,.0f} 元   名义额合计 = {notional:,.0f} 元'
              f'   平均每腿 = {pnl_t / n:+,.0f} 元   占名义 {(pnl_t / notional) * 100:+.4f}%')

    # ── 趋势收益 ──
    seed = base_seed(d)
    try:
        from e0_stage16_vol_conditioning import load
        D = load()
        D['c6'] = D['code'].astype(str).str.slice(0, 6)
        D['dd'] = D['date'].astype(str).str.slice(0, 10)
        end = (D[D['dd'] <= a.end].sort_values('dd').groupby('c6')['cl_1500'].last())
    except Exception as e:
        end, D = {}, None
        print(f'  （离线面板加载失败，趋势收益算不了: {e}）')
    pnl_b, cost_b = 0.0, 0.0
    rows = []
    for c, (q, px) in sorted(seed.items()):
        pe = float(end.get(c) or 0) if len(end) else 0.0
        if q > 0 and px > 0 and pe > 0:
            pnl_b += q * (pe - px)
            cost_b += q * px
            rows.append((c, q, px, pe, q * (pe - px)))
    print(f'\n【趋势收益】底仓买入持有（{len(rows)} 只，播种→{a.end}）')
    if rows:
        print(f'  市值变化 = {pnl_b:+,.0f} 元   底仓成本 = {cost_b:,.0f} 元'
              f'   涨幅 = {(pnl_b / cost_b) * 100:+.2f}%')
    tot = pnl_t + pnl_b
    print(f'\n【合计】趋势 {pnl_b:+,.0f} + 做T {pnl_t:+,.0f} = {tot:+,.0f} 元'
          f'   占初始资金 {a.initial_cash:,.0f} 的 {(tot / a.initial_cash) * 100:+.2f}%')
    print(f'  ⇒ 做T 在合计里的占比 = {pnl_t / tot * 100:+.1f}%' if tot else '')
    print(f'\n⚠️ 请与回测指标 pnl_ratio 对照（见驱动 stdout 的「回测已完成」段）：'
          f'pnl_ratio×{a.initial_cash:,.0f} 应≈合计；差得多说明底仓播种价/复权口径有偏差。')
    if rows:
        print(f'   明细前 5: ' + '; '.join(
            f'{c} {q}股 {px:.3f}→{pe:.3f}' for c, q, px, pe, _ in rows[:5]))


if __name__ == '__main__':
    main()
