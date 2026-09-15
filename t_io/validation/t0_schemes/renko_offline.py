# -*- coding: utf-8 -*-
"""生产 Renko 做T内核 · 离线批量回放 —— 用**真实代码路径**量出入场 skill。

动机：本轮 A1–A8 全是**代理入场**，生产真正跑的 `core/t_decision.TDecisionEngine`
从未进过离线回测。要谈"改入场"，先得知道现在这个入场的 skill 是多少。

**驱动的是生产同源决策核（非复刻）**：
  · core/t_decision.TDecisionEngine.evaluate —— Renko 向下砖 + 15分MACD金叉 → BUY_LOW；
    目标止盈/时间止损/尾盘强平 → SELL_HIGH
  · 无未来函数：每根 bar 只喂截至该 bar 的 1min 窗口（对齐生产 history_n(60s, count=240)）
  · 每票一个内核实例、跨日持久（与生产一致，Renko 砖与 t_entry_price 是状态）
  · **不含执行侧门链**（floor_protection 等）——本脚本量的是「信号本身的 skill」，
    执行侧另论（见 t_io/validation/auto/replay_600176_20260914.py）

输出与 `run_experiment.py` 同口径：费后净均/中位/胜率/密度 + 同格随机基线。

用法：python t_io/validation/t0_schemes/renko_offline.py [--codes 600176,000988] [--max-days N]
"""
import argparse
import glob
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
_MD = os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')
for _p in (ROOT, _MD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_experiment_v2 as v2  # noqa: E402
from core.t_decision import TDecisionEngine  # noqa: E402

OUT = HERE
WIN_START, WIN_END = '2025-09-14', '2026-08-26'
FEE_S, FEE_B = 0.00121, 0.00015      # 与生产/前几轮一致（双边 0.136%）
WINDOW = 240                          # 生产 history_n(60s, count=240)
N_MC = 200
MIN_1M = 100


def discover():
    return sorted({os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
                   for f in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})


def _leg_pnl(direction, fill, out):
    if direction == 'long':
        return 100 * (out * (1 - FEE_S) - fill * (1 + FEE_B)) / fill
    return 100 * (fill * (1 - FEE_S) - out * (1 + FEE_B)) / fill


def _random_baseline(day, j_f, dirs, tp=0.005, n=N_MC, seed=0):
    """同口径随机基线：随机入场 bar + 同方向分布 + 同出场规则。

    ⚠️ 止盈判定必须**逐 1min 收盘**（`c[j] >= tgt`），与内核 `price >= entry*(1+tp)` 一致。
    用盘中最高价 `h[j]` 判定会更容易命中、把基线抬高，从而低估入场 skill。
    """
    o = np.array([b['o'] for b in day], float)
    c = np.array([b['c'] for b in day], float)
    r = np.random.RandomState(seed)
    out = []
    for _ in range(n):
        b = r.randint(1, max(j_f, 1))
        if o[b] <= 0:
            continue
        d = dirs[r.randint(0, len(dirs))] if dirs else 'long'
        fill = o[b]
        tgt = fill * (1 + tp) if d == 'long' else fill * (1 - tp)
        ex = None
        for j in range(b + 1, j_f + 1):
            if (d == 'long' and c[j] >= tgt) or (d == 'short' and c[j] <= tgt):
                ex = tgt
                break
        if ex is None:
            ex = c[j_f]
        out.append(_leg_pnl(d, fill, ex))
    return out


def run_code(code, max_days=None, tp=0.005):
    dates, merged, _src = v2.merge_days(code)
    dates = [d for d in dates if WIN_START <= d <= WIN_END]
    if max_days:
        dates = dates[:max_days]
    if len(dates) < 5:
        return None

    # 1) 预建全量 1min DataFrame（时间戳先解析好，循环里只做 .iloc 切片 —— 这是性能关键）
    recs, spans = [], []
    for d in dates:
        day = merged[d]
        if len(day) < MIN_1M:
            continue
        st = len(recs)
        for b in day:
            recs.append((pd.Timestamp(f'{d} {b["t"]}:00'), b['o'], b['h'], b['l'],
                         b['c'], b['v'], b.get('amt') or 0.0))
        spans.append((d, st, len(recs), day))
    if not spans:
        return None
    full = pd.DataFrame(recs, columns=['time', 'open', 'high', 'low', 'close',
                                       'volume', 'amount'])

    eng = TDecisionEngine()          # 每票一个实例、跨日持久（与生产一致）
    legs, rnd = [], []
    for d, st, en, day in spans:
        eng.reset_day(d)             # 与生产日切一致（清 Renko 段与 t_entry_price）
        open_px = day[0]['o']
        cum_amt = cum_vol = 0.0
        t_leg = None
        dirs_today = []
        for i in range(1, en - st):
            idx = st + i
            sub = full.iloc[max(0, idx - WINDOW + 1):idx + 1]
            b = day[i]
            cp = float(b['c'])
            cum_amt += float(b.get('amt') or 0.0)
            cum_vol += float(b['v'])
            vwap = (cum_amt / cum_vol) if cum_vol > 0 else cp
            today_ret = cp / open_px - 1 if open_px > 0 else 0.0
            t_val = int(b['t'][:2]) * 100 + int(b['t'][3:5])
            try:
                sig, _bs, _ss, reason, _meta = eng.evaluate(
                    code, code, sub, cp, t_val, vwap, today_ret, "range", d,
                    params={"swing_take_profit_pct": tp})
            except Exception as e:
                print(f'  [warn] {code} {d} {b["t"]} evaluate 异常: {e}')
                sig = None
            if sig is None:
                continue
            if sig.action in ('BUY_LOW', 'ADD_POS') and t_leg is None:
                t_leg = {'bar': i, 'fill': cp}
                dirs_today.append('long')
            elif sig.action in ('SELL_HIGH', 'TARGET_SELL', 'TREND_EXIT') and t_leg:
                legs.append({'code': code, 'date': d, 'bar': t_leg['bar'], 'dir': 'long',
                             'fill': t_leg['fill'], 'exit': cp, 'reason': reason,
                             'net': _leg_pnl('long', t_leg['fill'], cp)})
                t_leg = None
        if t_leg is not None:        # 当日未平 —— 生产由 sell_channels 尾盘回补兜底，此处按 14:55 计
            j_f = en - st - 1
            legs.append({'code': code, 'date': d, 'bar': t_leg['bar'], 'dir': 'long',
                         'fill': t_leg['fill'], 'exit': day[j_f]['c'], 'reason': 'force1455',
                         'net': _leg_pnl('long', t_leg['fill'], day[j_f]['c'])})
        if dirs_today:
            rnd.extend(_random_baseline(day, en - st - 1, dirs_today,
                                        seed=abs(hash((code, d))) & 0xFFFF))
    return {'code': code, 'legs': legs, 'rnd': rnd, 'days': len(spans), 'gated': len(spans)}


def _baseline_code(code, max_days=None, tp=0.005):
    """只算随机基线（不跑内核）——基线不依赖引擎，纯数据+出场规则，故可便宜重算。"""
    dates, merged, _src = v2.merge_days(code)
    dates = [d for d in dates if WIN_START <= d <= WIN_END]
    if max_days:
        dates = dates[:max_days]
    rnd = []
    for d in dates:
        day = merged[d]
        if len(day) < MIN_1M:
            continue
        rnd.extend(_random_baseline(day, len(day) - 1, ['long'], tp=tp,
                                    seed=abs(hash((code, d))) & 0xFFFF))
    return {'code': code, 'rnd': rnd, 'legs': [], 'days': len(dates), 'gated': len(dates)}


def _worker_bl(pack):
    code, max_days, tp = pack
    try:
        return _baseline_code(code, max_days, tp)
    except Exception as e:
        print(f'  [warn] {code} baseline 失败: {e}')
        return None


def _worker(pack):
    """ProcessPool 入口（模块级，Windows spawn 要求可 pickle）。"""
    code, max_days, tp = pack
    try:
        return run_code(code, max_days, tp)
    except Exception as e:
        print(f'  [warn] {code} 失败: {e}')
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--max-days', type=int, default=None)
    ap.add_argument('--tp', type=float, default=0.005,
                    help='内核 swing_take_profit_pct；调大（如 1.0）等价于"只靠时间/尾盘出场"')
    ap.add_argument('--baseline-only', action='store_true',
                    help='只算随机基线（不跑内核）；基线不依赖引擎故可便宜重算')
    ap.add_argument('--workers', type=int, default=6,
                    help='并行进程数（内核是 pandas 重的纯函数，天然可并行）')
    ap.add_argument('--out', default=os.path.join(OUT, 'renko_offline_2026-09-15.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else discover()
    v2.END = WIN_END

    all_legs, all_rnd = [], []
    n_days = n_gated = 0
    t0 = time.time()

    # 并行：不同标的彼此独立（每票一个内核实例）；单进程太慢（~5s/票/日，全量 ≈12h）
    import concurrent.futures as cf
    _fn = _worker_bl if args.baseline_only else _worker
    with cf.ProcessPoolExecutor(max_workers=args.workers) as ex:
        for k, r in enumerate(ex.map(_fn, [(c, args.max_days, args.tp) for c in codes])):
            if not r:
                continue
            all_legs.extend(r['legs']); all_rnd.extend(r['rnd'])
            n_days += r['days']; n_gated += r['gated']
            if k % 5 == 0 or k == len(codes) - 1:
                el = time.time() - t0
                print(f'[{k+1}/{len(codes)}] {r["code"]} 腿={len(r["legs"])} 累计={len(all_legs)} '
                      f'用时={el:.0f}s', flush=True)

    if not all_legs and not all_rnd:
        print('[renko] 无成交腿且无基线'); return
    net = np.array([x['net'] for x in all_legs], float)
    rn = np.array(all_rnd, float) if all_rnd else np.array([])
    dist = {}
    for x in all_legs:
        dist[x['reason'][:22]] = dist.get(x['reason'][:22], 0) + 1
    print(f'\n[renko] codes={len(codes)} 交易日={n_days} tp={args.tp}')
    if args.baseline_only:
        print(f"  随机基线(逐1min收盘判止盈, 全日子) 净均={rn.mean():.4f}%  n={len(rn)}")
        print(f"  ⇒ 对比已测内核入场净均 −0.0770% → Δ={-0.0770 - rn.mean():+.4f}pp")
        return
    print(f"{'cell':22}{'n':>7}{'净均%':>9}{'中位%':>9}{'胜率':>8}{'密度/票/日':>11}")
    print(f"{'Renko内核(正T)':22}{len(net):>7}{net.mean():>9.3f}{np.median(net):>9.3f}"
          f"{(net > 0).mean():>8.3f}{(len(net) / max(n_gated, 1)):>11.3f}")
    print(f"  随机基线 净均={rn.mean():.4f}%  Δ={net.mean() - rn.mean():+.4f}pp")
    print(f'  出场分布: {dist}')
    json.dump({'meta': {'codes': len(codes), 'days': n_days, 'gated': n_gated,
                        'window': [WIN_START, WIN_END], 'tp': args.tp},
               'legs': all_legs,
               'summary': {'n': len(net), 'avg_net': round(float(net.mean()), 4),
                           'median_net': round(float(np.median(net)), 4),
                           'win_rate': round(float((net > 0).mean()), 4),
                           'density': round(len(net) / max(n_gated, 1), 3),
                           'random_avg': round(float(rn.mean()), 4) if len(rn) else None,
                           'delta_vs_random': round(float(net.mean() - rn.mean()), 4) if len(rn) else None,
                           'exit_dist': dist}},
              open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
    print(f'\n[renko] -> {args.out}')


if __name__ == '__main__':
    main()
