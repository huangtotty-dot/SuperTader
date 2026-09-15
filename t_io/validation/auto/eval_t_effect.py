# -*- coding: utf-8 -*-
"""做T效果评估：把「做T能力」从「底仓 beta」里分离 + 成本修正。

背景：GM 回测的 pnl_ratio（如 +31%）**绝大部分是底仓在窗口内的价格涨跌**，不是做T赚的。
必须做分解，否则会把 beta 当能力。

口径（账户期初全为现金，故期初持仓市值=0）：
    账户盈亏 = Σ卖出名义额 − Σ买入名义额 + 期末持仓市值
    底仓盈亏 = Σ 建仓量 × (期末价 − 建仓价)          # 买入持有到期末
    做T盈亏  = 账户盈亏 − 底仓盈亏                    # 即所有往返腿的净贡献

成本修正：GM 回测漏扣印花税（卖出 0.1%），见 memory/gm_backtest_caveats.md。
    补扣 = Σ卖出名义额 × 0.001

用法：python t_io/validation/auto/eval_t_effect.py [--label b7_off] [--cash 400000]
"""
import argparse
import collections
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
_MD = os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')
for _p in (ROOT, _MD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

WIN_END = '2026-07-01'


def _events(d):
    for f in os.listdir(d):
        if f.startswith('events_'):
            return os.path.join(d, f)
    return None


def _end_prices(codes):
    """窗口末日的收盘价（用本地 1min 数据，避免引入新数据源）。"""
    import run_experiment_v2 as v2
    out = {}
    for c in codes:
        try:
            dates, merged, _ = v2.merge_days(c)
            ds = [x for x in dates if x <= WIN_END]
            if ds:
                out[c] = merged[ds[-1]][-1]['c']
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--label', default='b7_off')
    ap.add_argument('--cash', type=float, default=400000.0)
    ap.add_argument('--dir', default=None)
    args = ap.parse_args()
    d = args.dir or os.path.join(ROOT, 't_io', 'validation', 'auto',
                                 f'backtest_holdings_{args.label}')
    ep = _events(d)
    if not ep:
        print(f'[eval] 找不到 events 文件: {d}'); return

    buys = collections.Counter(); sells = collections.Counter()      # code -> 名义额
    seed_px, seed_qty = {}, {}
    pos_after = {}
    nB = nS = 0
    for line in open(ep, encoding='utf-8'):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get('event') != 'fill':
            continue
        c = e.get('code'); q = abs(float(e.get('qty') or 0)); px = float(e.get('price') or 0)
        if c is None or q <= 0 or px <= 0:
            continue
        if e.get('side') == 'BUY':
            buys[c] += q * px; nB += 1
            if c not in seed_px:                       # 首笔买入 = 底仓建仓
                seed_px[c] = px; seed_qty[c] = int(q)
        else:
            sells[c] += q * px; nS += 1
        if e.get('pos_after') is not None:
            pos_after[c] = int(e.get('pos_after') or 0)

    codes = sorted(set(buys) | set(sells))
    endpx = _end_prices(codes)
    sum_buy = sum(buys.values()); sum_sell = sum(sells.values())
    end_val = sum(pos_after.get(c, 0) * endpx.get(c, 0.0) for c in codes)
    acct = sum_sell - sum_buy + end_val
    base = sum(seed_qty.get(c, 0) * (endpx.get(c, seed_px.get(c, 0.0)) - seed_px.get(c, 0.0))
               for c in codes)
    t_leg = acct - base
    stamp = sum_sell * 0.001

    print(f'=== 做T效果分解 · {args.label} ===')
    print(f'成交：买入 {nB} 笔 / 卖出 {nS} 笔')
    print(f'  Σ买入支出      {sum_buy:>14,.0f}')
    print(f'  Σ卖出收入      {sum_sell:>14,.0f}')
    print(f'  期末持仓市值   {end_val:>14,.0f}')
    print(f'  ─────────────────────────────')
    print(f'  账户盈亏       {acct:>14,.0f}   ({acct / args.cash * 100:+.2f}% of 资金)')
    print(f'    ├ 底仓 beta  {base:>14,.0f}   ({base / args.cash * 100:+.2f}%)')
    print(f'    └ 交易净贡献 {t_leg:>14,.0f}   ({t_leg / args.cash * 100:+.2f}%)')
    print('      ⚠️ 该项 = 「买入持有底仓」与实际账户之差，混合了 T 腿 + 止损/止盈退出，')
    print('         不是纯做T能力；且若期末持仓≠建仓量，说明底仓被削薄（见末表）。')
    print()
    print(f'成本修正（GM 漏扣印花税 0.1%/卖出）：补扣 {stamp:,.0f} 元 = {stamp / args.cash * 100:.3f}pp')
    print(f'  修正后 账户 {acct - stamp:>12,.0f} | 底仓 beta {base:>12,.0f} | 做T {t_leg - stamp:>12,.0f}')
    print()
    print(f'{"code":8}{"建仓价":>9}{"期末价":>9}{"建仓量":>9}{"底仓盈亏":>12}{"期末持仓":>10}')
    for c in codes:
        sp = seed_px.get(c, 0.0); et = endpx.get(c, 0.0); q = seed_qty.get(c, 0)
        print(f'{c:8}{sp:>9.3f}{et:>9.3f}{q:>9d}{q * (et - sp):>12,.0f}{pos_after.get(c, 0):>10d}')


if __name__ == '__main__':
    main()
