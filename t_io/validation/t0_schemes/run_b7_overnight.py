# -*- coding: utf-8 -*-
"""B7 隔夜反T 策略化实验 — 2026-09-15（实验员_E1）。

把 Wave 0 标定（calibrate.py → calib_2026-09-15.json）的「尾盘急拉 → 次日低开」线索
做成完整策略形态：信号日 14:55 卖出既有底仓（T+1 合规），次日按三变体接回。

## 与标定的口径差异（诚实声明，写进报告）
标定的 tail30 = close(15:00)/close(14:30)−1、tail5 = close(15:00)/close(14:55)−1，
用到 15:00 收盘价 —— 实盘 14:55 下单时不可知。本实验信号判定全部锁死在 ≤14:55：
  tail30_sig = close(14:55)/close(14:30)−1
  tail5_sig  = close(14:55)/close(14:50)−1
  tail_vol   = Σv(14:30≤t≤14:55)；volr = tail_vol / 前5日同窗口均值
  当日收涨    = close(14:55) > open(09:30)（截至 14:55）
接回价用次日数据是策略内生的合法未来（本身持仓过夜）。

## 策略
  卖出价 = close(14:55)；接回价三变体：①次日开盘 ②次日首30min VWAP(Σamt/Σv, 09:30–10:00)
  ③次日收盘。净收益 = (卖×(1−0.00121) − 接回×(1+0.00015)) / 卖（与生产成本口径一致）。

## 信号变体
  S1 tail30_sig > 1%   S2 tail30_sig > 2%   S3 S2 & volr ≥ 1.5   S4 tail5_sig > 1%

## 对照与基线（预注册）
  ① 随机基线：同资格池随机选同数量卖出日，200 次蒙特卡洛（seed=20260915）
  ② 持有不动基线：全样本次日 gap 分布 + 「每天都卖」同口径净收益（检验是否所有日子都低开）
  ③ OOS：2026-06-01 ~ 2026-08-26 与样本内对比

## 预注册闸门（逐条判定写进 verdict）
  G1 费后净均 > 0 且 n ≥ 200
  G2 优于随机基线 ≥ +0.2pp（实际净均 − MC净均均值 ≥ 0.002）
  G3 胜率 ≥ 55%
  G4 OOS 净均不退化为负（OOS n<20 时记「样本不足，无法判定」，不判过）
  G5 集中度：TOP3 票净收益贡献 < 50%

数据：t_io/backtest_1year_data，39 只 × 2025-09-14~2026-08-26（复用 run_experiment_v2 数据层）。
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

import run_experiment_v2 as v2  # noqa: E402  复用 1min 数据层（与 calibrate.py 同口径）

OUT = HERE
WIN_START, WIN_END = '2025-09-14', '2026-08-26'
OOS_START = '2026-06-01'          # 最后 3 个月作样本外
FEE_S, FEE_B = 0.00121, 0.00015   # 卖 0.121% / 买 0.015%（与生产一致）
VOLR_TH = 1.5
MIN_BAR = 100
N_MC = 200
MC_SEED = 20260915

SIGNALS = {
    'S1_tail30>1%': lambda r: r['tail30'] is not None and r['tail30'] > 0.01,
    'S2_tail30>2%': lambda r: r['tail30'] is not None and r['tail30'] > 0.02,
    'S3_tail30>2%+vol': lambda r: (r['tail30'] is not None and r['tail30'] > 0.02
                                   and r['volr'] is not None and r['volr'] >= VOLR_TH),
    'S4_tail5>1%': lambda r: r['tail5'] is not None and r['tail5'] > 0.01,
}
BUYBACKS = ('open', 'vwap30', 'close')


def discover():
    return sorted({os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
                   for f in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})


def _close_at(day, label):
    for b in day:
        if b['t'] == label:
            return b['c']
    return None


def collect(codes):
    """逐票收集信号日特征 + 次日三口径接回价。信号特征全部 ≤14:55，无未来函数。"""
    rows = []
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        dates = [d for d in dates if WIN_START <= d <= WIN_END]
        if len(dates) < 10:
            continue
        recs = []
        for k, d in enumerate(dates):
            day = merged[d]
            if len(day) < MIN_BAR or day[0]['o'] <= 0:
                continue
            c1430, c1450, c1455 = (_close_at(day, '14:30'), _close_at(day, '14:50'),
                                   _close_at(day, '14:55'))
            if not (c1430 and c1450 and c1455):
                continue
            tail_vol = sum(b['v'] for b in day if '14:30' <= b['t'] <= '14:55')
            # 次日接回价（策略内生合法未来）
            buy = {}
            if k + 1 < len(dates):
                nd = merged[dates[k + 1]]
                first30 = [b for b in nd if b['t'] <= '10:00']
                v_sum = sum(b['v'] for b in first30)
                a_sum = sum(b.get('amt', b['c'] * b['v']) for b in first30)
                buy = {'open': nd[0]['o'],
                       'vwap30': (a_sum / v_sum) if v_sum > 0 else None,
                       'close': nd[-1]['c']}
            recs.append({'code': code, 'date': d,
                         'sell': c1455,
                         'tail30': c1455 / c1430 - 1,
                         'tail5': c1455 / c1450 - 1,
                         'tail_vol': tail_vol,
                         'day_up': c1455 > day[0]['o'],   # 截至 14:55 收涨
                         'next_gap': (buy['open'] / day[-1]['c'] - 1) if buy else None,
                         'buy': buy})
        # 相对量能：当日尾盘量 / 前 5 日同窗口均值（只用 ≤t 数据）
        for k, r in enumerate(recs):
            prior = [x['tail_vol'] for x in recs[max(0, k - 5):k]]
            r['volr'] = (r['tail_vol'] / np.mean(prior)) if prior and np.mean(prior) > 0 else None
        rows.extend(r for r in recs if r['buy'])   # 无次日数据的末日剔除
    return rows


def net_of(r, bb):
    """隔夜反T净收益：14:55 卖出底仓，次日按 bb 口径接回。"""
    b = r['buy'].get(bb)
    if not b or b <= 0:
        return None
    return (r['sell'] * (1 - FEE_S) - b * (1 + FEE_B)) / r['sell']


def _stat(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    if len(a) == 0:
        return None
    return {'n': int(len(a)), 'mean': round(float(a.mean()) * 100, 4),
            'median': round(float(np.median(a)) * 100, 4),
            'win': round(float((a > 0).mean()), 4)}


def max_loss_streak(trades):
    """按时间排序的连续净亏（net<=0）最长段。"""
    s = 0
    best = 0
    for t in sorted(trades, key=lambda x: (x['date'], x['code'])):
        s = s + 1 if t['net'] <= 0 else 0
        best = max(best, s)
    return best


def top3_share(trades):
    """TOP3 票净收益贡献占比（分母=全部交易净收益之和）。"""
    per = {}
    for t in trades:
        per[t['code']] = per.get(t['code'], 0.0) + t['net']
    tot = sum(per.values())
    if tot <= 0:
        return None, per
    top = sorted(per.values(), reverse=True)[:3]
    return round(sum(top) / tot, 4), per


def mc_baseline(pool, n, bb, rng):
    """随机基线：同资格池无放回抽 n 个卖出日，同口径接回，重复 N_MC 次。
    返回 MC 均值分布特征 + 供调用方算实际值分位的原始均值数组。"""
    nets = np.array([x for x in (net_of(r, bb) for r in pool)
                     if x is not None and np.isfinite(x)], float)
    if len(nets) < n or n == 0:
        return None, None
    means = np.array([float(rng.choice(nets, size=n, replace=False).mean())
                      for _ in range(N_MC)])
    return ({'mc_mean': round(float(means.mean()) * 100, 4),
             'mc_std': round(float(means.std()) * 100, 4),
             'mc_p05': round(float(np.percentile(means, 5)) * 100, 4),
             'mc_p95': round(float(np.percentile(means, 95)) * 100, 4)},
            means)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--out', default=os.path.join(OUT, 'results_b7_2026-09-15.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else discover()
    v2.END = WIN_END
    rng = np.random.default_rng(MC_SEED)

    rows = collect(codes)
    days = sorted({r['date'] for r in rows})
    months = len(days) / 21.0                     # 月均交易日≈21
    n_codes = len({r['code'] for r in rows})
    print(f'[b7] codes={n_codes} 股票·日={len(rows)} 交易日={len(days)} 约{months:.1f}个月')

    # ---------- 基线②：持有不动 / 每天都卖 ----------
    gap_all = _stat([r['next_gap'] for r in rows])
    hold_baseline = {
        'next_gap_全样本%': gap_all,
        'gap<0占比': round(float(np.mean([r['next_gap'] < 0 for r in rows
                                          if r['next_gap'] is not None])), 4),
        '每天都卖_净收益%': {bb: _stat([net_of(r, bb) for r in rows]) for bb in BUYBACKS},
    }
    print(f"[b7] 全样本次日gap: n={gap_all['n']} 均={gap_all['mean']}% "
          f"低开率={hold_baseline['gap<0占比']}")

    # ---------- 12 个 信号×接回 cell ----------
    cells = []
    for sname, sfn in SIGNALS.items():
        sig_rows = [r for r in rows if sfn(r)]
        for bb in BUYBACKS:
            trades = [{'code': r['code'], 'date': r['date'], 'net': net_of(r, bb)}
                      for r in sig_rows]
            trades = [t for t in trades if t['net'] is not None]
            nets = [t['net'] for t in trades]
            st = _stat(nets)
            if st is None:
                continue
            # 资格池：该信号可判定（含被否决日）的所有日子 → 随机基线抽样池
            pool = rows
            mc, mc_means = mc_baseline(pool, st['n'], bb, rng)
            # 实际净均在 MC 分布中的分位（>0.95 ≈ 显著优于随机）
            mc_rank = (round(float((mc_means < st['mean'] / 100).mean()), 4)
                       if mc_means is not None else None)
            oos = _stat([t['net'] for t in trades if t['date'] >= OOS_START])
            ins = _stat([t['net'] for t in trades if t['date'] < OOS_START])
            share, per_code = top3_share(trades)
            cell = {
                'signal': sname, 'buyback': bb, 'n': st['n'],
                'net_mean%': st['mean'], 'net_median%': st['median'], 'win': st['win'],
                'freq_次/票/月': round(st['n'] / n_codes / months, 2),
                'mc': mc, 'mc_rank': mc_rank,
                'delta_vs_mc_pp': (round(st['mean'] - mc['mc_mean'], 4) if mc else None),
                'oos': oos, 'in_sample': ins,
                'max_loss_streak': max_loss_streak(trades),
                'top3_share': share,
                'per_stock': {c: {'n': sum(1 for t in trades if t['code'] == c),
                                  'net_mean%': _stat([t['net'] for t in trades
                                                      if t['code'] == c])['mean'],
                                  'win': _stat([t['net'] for t in trades
                                                if t['code'] == c])['win']}
                              for c in sorted(per_code)},
            }
            cells.append(cell)
            print(f"[b7] {sname:22s} × {bb:6s} n={st['n']:4d} 净均={st['mean']:+.4f}% "
                  f"胜={st['win']:.3f} MC均={mc['mc_mean'] if mc else '--'}% "
                  f"Δ={cell['delta_vs_mc_pp']}pp OOS={oos['mean'] if oos else '--'}% "
                  f"TOP3={share} 连亏={cell['max_loss_streak']}")

    # ---------- 分层：按次日 gap 桶 & 按票 ----------
    layers_gap = []
    for sname, sfn in SIGNALS.items():
        sig_rows = [r for r in rows if sfn(r)]
        for lo, hi in ((-99, -0.01), (-0.01, 0.0), (0.0, 0.01), (0.01, 99)):
            sub = [r for r in sig_rows if r['next_gap'] is not None
                   and lo <= r['next_gap'] < hi]
            if not sub:
                continue
            layers_gap.append({'signal': sname,
                               'next_gap桶': f'{lo*100:.0f}~{hi*100:.0f}%',
                               'n': len(sub),
                               'open接回净%': _stat([net_of(r, 'open') for r in sub])})

    # ---------- 交叉：信号 × 当日收涨(截至14:55) ----------
    cross = []
    for sname, sfn in SIGNALS.items():
        sig_rows = [r for r in rows if sfn(r)]
        for up_lab, up_ok in (('当日收涨', True), ('当日未收涨', False)):
            sub = [r for r in sig_rows if r['day_up'] == up_ok]
            if not sub:
                continue
            row = {'signal': sname, 'cond': up_lab, 'n': len(sub),
                   'next_gap%': _stat([r['next_gap'] for r in sub])}
            for bb in BUYBACKS:
                row[f'{bb}接回净%'] = _stat([net_of(r, bb) for r in sub])
            cross.append(row)
            print(f"[b7·交叉] {sname:22s} {up_lab}: n={row['n']} "
                  f"gap均={row['next_gap%']['mean'] if row['next_gap%'] else '--'}% "
                  f"open净={row['open接回净%']['mean'] if row['open接回净%'] else '--'}%")

    # ---------- 闸门逐条判定 ----------
    verdicts = []
    for c in cells:
        g1 = c['net_mean%'] > 0 and c['n'] >= 200
        g2 = (c['delta_vs_mc_pp'] is not None and c['delta_vs_mc_pp'] >= 0.2)
        g3 = c['win'] >= 0.55
        if c['oos'] is None or c['oos']['n'] < 20:
            g4, g4_note = None, 'OOS样本不足(n<20)，无法判定'
        else:
            g4, g4_note = c['oos']['mean'] > 0, ''
        g5 = (c['top3_share'] is not None and c['top3_share'] < 0.5)
        passed = all([g1, g2, g3, g4 is True, g5])
        verdicts.append({'signal': c['signal'], 'buyback': c['buyback'], 'n': c['n'],
                         'G1_净均>0且n>=200': g1, 'G2_优于随机>=+0.2pp': g2,
                         'G3_胜率>=55%': g3,
                         'G4_OOS不为负': g4, 'G4_note': g4_note,
                         'G5_TOP3<50%': g5, 'ALL_PASS': passed})

    result = {
        'meta': {'experiment': 'B7 隔夜反T 策略化',
                 'codes': n_codes, 'rows': len(rows), 'trading_days': len(days),
                 'window': [WIN_START, WIN_END], 'oos_start': OOS_START,
                 'fee': {'sell': FEE_S, 'buy': FEE_B},
                 'signal_time': '≤14:55（tail30=c1455/c1430, tail5=c1455/c1450, '
                                'volr=尾盘量/前5日同窗口均值）',
                 'sell_price': 'close(14:55)',
                 'buyback': {'open': '次日开盘价', 'vwap30': '次日09:30-10:00 Σamt/Σv',
                             'close': '次日收盘价'},
                 'n_mc': N_MC, 'mc_seed': MC_SEED,
                 'note_vs_calib': '标定 tail30/tail5 用 15:00 收盘（14:55 不可知），'
                                  '本实验信号锁死 ≤14:55，与标定数字不可直接比'},
        'hold_baseline': hold_baseline,
        'cells': cells,
        'layers_next_gap': layers_gap,
        'cross_day_up': cross,
        'gates': verdicts,
    }
    json.dump(result, open(args.out, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(f'\n[b7] -> {args.out}')

    print('\n=== 闸门判定 ===')
    for v in verdicts:
        flags = ''.join('✓' if v[k] else ('?' if v[k] is None else '✗')
                        for k in ('G1_净均>0且n>=200', 'G2_优于随机>=+0.2pp',
                                  'G3_胜率>=55%', 'G4_OOS不为负', 'G5_TOP3<50%'))
        print(f"  {v['signal']:22s} × {v['buyback']:6s} n={v['n']:4d} {flags} "
              f"{'PASS' if v['ALL_PASS'] else 'FAIL'} {v['G4_note']}")


if __name__ == '__main__':
    main()
