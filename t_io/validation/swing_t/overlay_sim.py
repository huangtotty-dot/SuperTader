# -*- coding: utf-8 -*-
"""波段做T 叠加回测 —— 底仓不动 + 活动仓按「状态」加减。

## 与日内做T 实验台的根本区别（这决定了度量必须换）

  日内台（`t0_schemes/`）：适应度 = **单笔往返净收益**。
      优化它会去追「低吸高抛赚 0.5%」—— 而那正是 owner 说的**卖飞**。
  本模块：适应度 = **叠加 vs 纯买入持有**。
      总收益 / 最大回撤 / Calmar / **卖飞度量**。

## owner 确认的口径（2026-09-18）

  · **底仓全程不动**（这才是"拿住"）
  · **活动仓 = 底仓 × 30%**（可配）
  · **震荡段**：活动仓低吸高抛
  · **单边上涨段**：活动仓**只加不减**（防卖飞）
  · 底仓**不因做T被减**；日线 MACD 下行风险 → **清仓换股**（属选股层，本模块不实现）

## 目标（owner 原话）

  1. 对看好的标的，**减少波动带来的负面情绪**，在趋势不明显时能拿住
  2. **行情好的时候扩大收益**

  ⇒ 度量必须同时看「回撤/波动」与「收益」两侧，不能只看收益。

## 诚实边界（写在代码里，免得事后自我安慰）

  · 活动仓仅 30% ⇒ 对总仓位的回撤/收益影响**上限 30%**
  · 平滑有代价：叠加费后为负时，"情绪好受"是**用收益换波动**，
    必须把价格标出来（每降 1 单位回撤付多少收益）

用法：python overlay_sim.py [--codes ...] [--act 0.3] [--grid 0.02]
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
for _p in (ROOT, os.path.join(ROOT, 't_io', 'validation', 't0_schemes'),
           os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_experiment_v2 as v2  # noqa: E402

FEE_S, FEE_B = 0.00121, 0.00015      # 与生产一致：双边 0.136%
INIT = 100.0                          # 底仓规模归一化


def daily_bars(code):
    """从 1min 面板聚合出日线（O/H/L/C/V），并按日期升序。"""
    dates, merged, _ = v2.merge_days(code)
    out = []
    for d in sorted(merged):
        b = merged[d]
        if not b:
            continue
        out.append({'d': d, 'o': float(b[0]['o']), 'h': max(x['h'] for x in b),
                    'l': min(x['l'] for x in b), 'c': float(b[-1]['c'])})
    return out


def regime(closes, i, win=20):
    """**状态识别（待挖因子的占位基线）**：震荡 / 单边上涨 / 单边下跌。

    基线口径（简单、只用已发生数据）：收盘 vs MA20，且 MA20 的斜率方向。
      · c > MA20 且 MA20 上行 → 'up'
      · c < MA20 且 MA20 下行 → 'down'
      · 否则                  → 'range'
    这是**要被挖出来的因子替换掉**的那一环。
    """
    if i < win:
        return 'range'
    ma_now = np.mean(closes[i - win + 1:i + 1])
    ma_prev = np.mean(closes[i - win:i])
    if closes[i] > ma_now and ma_now > ma_prev:
        return 'up'
    if closes[i] < ma_now and ma_now < ma_prev:
        return 'down'
    return 'range'


def simulate_control(bars, act_frac=0.3):
    """⛔ **阳性对照（故意前视）**：活动仓在**次日上涨**时持有、次日下跌时空仓。

    用途：验证本度量台**有灵敏度**。若它都测不出「收益更高 + 回撤更浅」，
    说明度量台是坏的 —— 那么用它挖出来的一切都不可解读。
    **绝不可当策略。**
    """
    closes = np.array([b['c'] for b in bars], float)
    base = INIT
    act_cap = base * act_frac
    act_qty, cash = 0.0, 0.0
    nav = []
    for i, b in enumerate(bars):
        c = closes[i]
        if i + 1 < len(bars):
            nxt_up = closes[i + 1] > c          # ← 前视：知道明天的方向
            want = act_cap / c if nxt_up else 0.0
            if want > act_qty:                  # 买入差额
                q = want - act_qty
                cash -= q * c * (1 + FEE_B); act_qty = want
            elif want < act_qty:                # 卖出差额
                q = act_qty - want
                cash += q * c * (1 - FEE_S); act_qty = want
        nav.append(base * c + act_qty * c + cash)
    return np.array(nav, float), np.array([base * x for x in closes], float)


def simulate(bars, act_frac=0.3, grid=0.02, max_act=2.0, regime_fn=None):
    """regime_fn(closes, i) -> 'up'|'down'|'range'；缺省用占位基线 regime()。"""
    """底仓不动 + 活动仓按状态加减。返回逐日净值（叠加 vs 纯持有）。

    act_frac: 活动仓额度 = 底仓 × act_frac
    grid:     高抛低吸的触发幅度（相对当日基准 MA20）
    max_act:  单边上涨段"只加不减"时，活动仓最多加到额度的 max_act 倍
    """
    closes = np.array([b['c'] for b in bars], float)
    base = INIT
    act_cap = base * act_frac
    cash = 0.0
    act_qty = 0.0
    act_cost = 0.0
    nav_overlay, nav_bh = [], []
    trades = 0
    for i, b in enumerate(bars):
        c = closes[i]
        if i >= 20:
            ma = float(np.mean(closes[i - 20 + 1:i + 1]))
            reg = (regime_fn or regime)(closes, i)
            # ── 活动仓动作（只用 ≤i 的信息，成交按当日收盘近似）──
            if reg == 'range':
                if act_qty == 0 and c <= ma * (1 - grid):
                    q = act_cap / c
                    cash -= q * c * (1 + FEE_B); act_qty += q; act_cost = c; trades += 1
                elif act_qty > 0 and c >= ma * (1 + grid):
                    cash += act_qty * c * (1 - FEE_S); act_qty = 0.0; trades += 1
            elif reg == 'up':
                # 只加不减（防卖飞）
                if c <= ma * (1 - grid) and act_qty < act_cap / c * max_act:
                    q = (act_cap / c) * 0.5
                    cash -= q * c * (1 + FEE_B); act_qty += q; trades += 1
            # down: 不加不减
        nav_overlay.append(base * c + act_qty * c + cash)
        nav_bh.append(base * closes[0] if False else base * c)
    nav_overlay = np.array(nav_overlay, float)
    nav_bh = np.array([base * closes[0]] + [base * x for x in closes[1:]], float)
    return nav_overlay, nav_bh, trades


def stats(nav):
    r = np.diff(nav) / nav[:-1]
    dd = nav / np.maximum.accumulate(nav) - 1
    tot = nav[-1] / nav[0] - 1
    mdd = float(dd.min())
    return {'total': float(tot), 'maxdd': mdd,
            'vol': float(r.std() * np.sqrt(252)),
            'calmar': float(tot / abs(mdd)) if mdd < 0 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--act', type=float, default=0.3)
    ap.add_argument('--grid', type=float, default=0.02)
    ap.add_argument('--control', action='store_true',
                    help='阳性对照：活动仓用未来信息（验证度量台灵敏度，绝不可当策略）')
    ap.add_argument('--out', default=os.path.join(HERE, 'overlay_2026-09-18.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else sorted(
        {os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
         for f in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})
    rows = []
    for c in codes:
        bars = daily_bars(c)
        if len(bars) < 60:
            continue
        if args.control:
            no, nb = simulate_control(bars, act_frac=args.act); tr = -1
        else:
            no, nb, tr = simulate(bars, act_frac=args.act, grid=args.grid)
        so, sb = stats(no), stats(nb)
        so['code'] = c; so['trades'] = tr
        so['bh_total'] = sb['total']; so['bh_maxdd'] = sb['maxdd']
        so['d_total'] = so['total'] - sb['total']
        so['d_maxdd'] = so['maxdd'] - sb['maxdd']       # >0 表示回撤更浅
        rows.append(so)
    if not rows:
        print('无数据'); return
    agg = lambda k: float(np.mean([r[k] for r in rows]))
    print(f'波段做T 叠加 · {len(rows)} 只票 · 活动仓={args.act:.0%} 格距={args.grid:.1%}')
    print(f"{'':16}{'总收益':>10}{'最大回撤':>10}{'年化波动':>10}{'Calmar':>9}")
    print(f"{'叠加(overlay)':16}{agg('total'):>10.2%}{agg('maxdd'):>10.2%}{agg('vol'):>10.2%}{agg('calmar') or 0:>9.2f}")
    print(f"{'纯持有(B&H)':16}{agg('bh_total'):>10.2%}{agg('bh_maxdd'):>10.2%}{'':>10}{'':>9}")
    print(f"{'差(叠加−持有)':16}{agg('d_total'):>+10.2%}{agg('d_maxdd'):>+10.2%}")
    print(f"\n  平均交易次数 {agg('trades'):.1f}")
    print(f"  回撤更浅的票: {sum(1 for r in rows if r['d_maxdd'] > 0)}/{len(rows)}")
    print(f"  收益更高的票: {sum(1 for r in rows if r['d_total'] > 0)}/{len(rows)}")
    json.dump({'meta': vars(args), 'per_code': rows}, open(args.out, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(f'  -> {args.out}')


if __name__ == '__main__':
    main()
