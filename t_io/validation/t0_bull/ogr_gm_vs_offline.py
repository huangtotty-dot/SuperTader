# -*- coding: utf-8 -*-
"""把 GM 回测的每腿价与**离线 d540 面板**逐 (code,date) 对齐，定位差异出在买腿还是卖腿（2026-09-24）。

Stage17 §2 明确要求：回测结果与期望值显著不符 ⇒ 实现/前视有问题。
本轮 GM 成交口径 +2.92%/腿 vs 靶子 +0.9689%/腿（高 1.95pp）⇒ 必须定位。

对比量（按 (code,date) 配对）：
  GM 买腿 ref_px        vs  离线 op_auc     （集合竞价价）
  GM 卖腿 ref_px        vs  离线 cl_1000    （10:00 价）
  GM 每腿比值 sell/buy  vs  离线 cl_1000/op_auc
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T0 = ROOT / 't_io' / 'validation' / 't0_bull'
sys.path.insert(0, str(T0))
sys.path.insert(0, str(ROOT))

from e0_stage16_vol_conditioning import load      # noqa: E402

OUT = ROOT / 't_io' / 'validation' / 'auto' / 'backtest_holdings_ogr_full_limit'


def gm_legs():
    buys, sells = {}, []
    for line in (OUT / 'backtrace.jsonl').open(encoding='utf-8'):
        if '"ogr_' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        e, day, c = o.get('event'), str(o.get('time'))[:10], o.get('code')
        if e == 'ogr_buy':
            buys[(c, day)] = o
        elif e == 'ogr_sell':
            sells.append((c, day, o.get('buy_px'), o.get('ref_px'), o.get('fill_px')))
    out = []
    for c, day, buy_px, sell_ref, sell_fill in sells:
        b = buys.get((c, day)) or {}
        out.append((c, day, b.get('ref_px'), buy_px, sell_ref, sell_fill))
    return out


def main() -> None:
    D = load()
    D['code'] = D['code'].astype(str).str.slice(0, 6)
    D['d'] = D['date'].astype(str).str.slice(0, 10)
    idx = {(r.code, r.d): (float(r.op_auc), float(r.cl_1000)) for r in D.itertuples()}
    legs = gm_legs()
    print(f'GM 平腿 {len(legs)} 条；离线面板 {len(idx)} 个 (code,date)')

    rb, rs, rr = [], [], []
    for c, day, gm_buy, buy_px, sell_ref, sell_fill in legs:
        k = (c, day)
        if k not in idx or not gm_buy or not sell_ref:
            continue
        auc, cl10 = idx[k]
        rb.append(gm_buy / auc - 1)                 # GM 买价 / 竞价
        rs.append(sell_ref / cl10 - 1)              # GM 卖价 / 10:00 价
        rr.append((sell_ref / gm_buy) / (cl10 / auc) - 1)   # 每腿比值的比
    n = len(rb)
    print(f'可比腿 {n} 条\n')
    if n:
        print(f'  GM买价/离线竞价 − 1 ：中位 {statistics.median(rb):+.4%}  '
              f'均值 {statistics.fmean(rb):+.4%}')
        print(f'  GM卖价/离线10:00 − 1：中位 {statistics.median(rs):+.4%}  '
              f'均值 {statistics.fmean(rs):+.4%}')
        print(f'  每腿比值/cl−1      ：中位 {statistics.median(rr):+.4%}  '
              f'均值 {statistics.fmean(rr):+.4%}')
        print('\n判读：买价那一行若显著为负 ⇒ GM 的买价低于真实开盘价（前视/取价错）；'
              '两行都接近 0 而每腿仍差 ⇒ 成本或口径差异。')

        # ── 交集检验：同 (code,date) 上逐腿比 GM vs 离线 ──
        from core.cost_model import fees
        fs, fb = fees('stock')
        gx, ox, pairs = [], [], []
        for c, day, gm_buy, buy_px, sell_ref, sell_fill in legs:
            k = (c, day)
            if k not in idx or not buy_px or not sell_fill:
                continue
            auc, cl10 = idx[k]
            gm_net = ((sell_fill / buy_px) * (1 - fs) - (1 + fb)) * 100
            off_net = ((cl10 / auc) * (1 - fs) - (1 + fb)) * 100
            gx.append(gm_net); ox.append(off_net); pairs.append((c, day, gm_net, off_net))
        if gx:
            print(f'\n=== 交集 {len(gx)} 腿（逐腿配对）===')
            print(f'  GM   净均 = {statistics.fmean(gx):+.4f}%')
            print(f'  离线 净均 = {statistics.fmean(ox):+.4f}%')
            print(f'  逐腿差中位 = {statistics.median([a - b for _, _, a, b in pairs]):+.4f}pp'
                  f'   均值 = {statistics.fmean([a - b for _, _, a, b in pairs]):+.4f}pp')

        # ── 腿集构成：GM 触发的 (code,date) 在离线里有多少、离线独有的有多少 ──
        gm_keys = {(c, day) for c, day, *_ in legs}
        R = D[(D['gap'] < 0)] if False else None
        D['gap'] = D['op_auc'] / D.groupby('code')['cl_1500'].shift(1) - 1
        D['mkt_gap'] = D.groupby('d')['gap'].transform('median')
        D['rel'] = D['gap'] - D['mkt_gap']
        Win = D[(D['d'] >= '2026-04-08') & (D['d'] <= '2026-07-01')]
        BAS = Win[Win['code'].isin({c for c, _ in gm_keys})]
        R = BAS[(BAS['mkt_gap'] < 0) & (BAS['rel'] <= -0.01)]
        off_keys = {(r.code, r.d) for r in R.itertuples()}
        print(f'\n=== 腿集构成（2026-04-08~07-01）===')
        print(f'  离线(全市场mkt_gap) {len(off_keys)} 腿 / GM {len(gm_keys)} 腿 / 交集 '
              f'{len(off_keys & gm_keys)}')
        only_off = off_keys - gm_keys
        if only_off:
            sub = R[[ (r.code, r.d) in only_off for r in R.itertuples() ]]
            net = ((sub['cl_1000'] / sub['op_auc']) * (1 - fs) - (1 + fb)) * 100
            print(f'  离线独有 {len(only_off)} 腿的净均 = {net.mean():+.4f}%'
                  f'   <-- 若显著低于离线整体 ⇒ 腿集选择造成差异')
        print(f'  离线篮子靶子(全腿) = +1.1558%（n=223）   GM 成交口径 = +2.92%（n=127）')



if __name__ == '__main__':
    main()
