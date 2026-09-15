# -*- coding: utf-8 -*-
"""Wave 0 阈值标定（做T第三轮，2026-09-15）— 描述统计，不设达标线。

预注册：C:\\Users\\Lenovo\\.claude\\plans\\binary-exploring-quilt.md

产出三张分布表（喂 A7 / B7 的阈值）：
  ① 缺口分桶 gap=open/prev_close−1 × 当日走势（open→close / open→high / open→low）→ A7 的 gap 阈值
  ② 尾盘急拉（14:30→15:00 与 14:55→15:00）× 相对量能 → 次日 open/prev_close → B7 阈值
  ③ 尾盘强势放量 → 隔夜溢价基线（"尾盘该不该留活动仓过夜"）

预注册降级：1min CSV 无 09:25 竞价 bar（09:30 首根已并入竞价撮合量）→ 竞价 vol_ratio
历史不可得，本脚本只做 gap 口径。

数据：t_io/backtest_1year_data，39 只 × 2025-08-26~2026-08-26（复用 run_experiment_v2 数据层）。
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

# 按路径显式加载同目录的实验台（`import run_experiment` 会撞上 macd_divergence_t 下的同名文件）
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    't0_rx', os.path.join(HERE, 'run_experiment.py'))
rx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rx)

OUT = HERE
WIN_START, WIN_END = '2025-09-14', '2026-08-26'
GAP_EDGES = [-0.03, -0.01, -0.005, 0.0, 0.005, 0.01, 0.03]   # 7 桶
VOLR_TH = 1.5                                                 # 尾盘放量阈值（S1-7 口径）
MIN_BAR = 100


def _tail_vol(day, t_from):
    return sum(b['v'] for b in day if b['t'] >= t_from)


def _close_at(day, label):
    for b in day:
        if b['t'] == label:
            return b['c']
    return None


def discover():
    return sorted({os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
                   for f in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})


def collect(codes):
    """逐票收集 (gap, 当日走势, 尾盘特征, 次日 gap) 记录。全部无未来函数。"""
    rows = []
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        dates = [d for d in dates if WIN_START <= d <= WIN_END]
        if len(dates) < 10:
            continue
        recs = []
        for k, d in enumerate(dates):
            day = merged[d]
            if len(day) < MIN_BAR:
                continue
            o = day[0]['o']
            c = day[-1]['c']
            if o <= 0 or c <= 0:
                continue
            prev = merged[dates[k - 1]][-1]['c'] if k > 0 else None
            nxt_o = merged[dates[k + 1]][0]['o'] if k + 1 < len(dates) else None
            hi = max(b['h'] for b in day)
            lo = min(b['l'] for b in day)
            c1430 = _close_at(day, '14:30')
            c1455 = _close_at(day, '14:55')
            recs.append({
                'code': code, 'date': d,
                'gap': (o / prev - 1) if prev else None,
                'o2c': c / o - 1,
                'o2c1455': (c1455 / o - 1) if c1455 else None,
                'o2h': hi / o - 1, 'o2l': lo / o - 1,
                'tail30': (c / c1430 - 1) if c1430 else None,
                'tail5': (c / c1455 - 1) if c1455 else None,
                'tail_vol': _tail_vol(day, '14:30'),
                'next_gap': (nxt_o / c - 1) if nxt_o else None,
            })
        # 尾盘相对量能：当日尾盘量 / 前 5 日尾盘量均值
        for k, r in enumerate(recs):
            prior = [x['tail_vol'] for x in recs[max(0, k - 5):k]]
            r['volr'] = (r['tail_vol'] / np.mean(prior)) if prior and np.mean(prior) > 0 else None
        rows.extend(recs)
    return rows


def _stat(vals):
    a = np.array([v for v in vals if v is not None and np.isfinite(v)], float)
    if len(a) == 0:
        return None
    return {'n': int(len(a)), 'mean': round(float(a.mean()) * 100, 4),
            'median': round(float(np.median(a)) * 100, 4),
            'win': round(float((a > 0).mean()), 4)}


def table1_gap(rows):
    """① 缺口分桶 × 当日走势"""
    out = []
    edges = [-99] + GAP_EDGES + [99]
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        sub = [r for r in rows if r['gap'] is not None and lo <= r['gap'] < hi]
        if not sub:
            continue
        out.append({'bucket': f'{lo*100:.1f}~{hi*100:.1f}%', 'n': len(sub),
                    'o2c': _stat([r['o2c'] for r in sub]),
                    'o2c1455': _stat([r['o2c1455'] for r in sub]),
                    'o2h': _stat([r['o2h'] for r in sub]),
                    'o2l': _stat([r['o2l'] for r in sub])})
    return out


def table2_tail(rows):
    """② 尾盘急拉 × 相对量能 → 次日 gap"""
    out = []
    for label, key in (('tail30(14:30→15:00)', 'tail30'), ('tail5(14:55→15:00)', 'tail5')):
        for vlab, vok in (('volr<1.5', lambda v: v is not None and v < VOLR_TH),
                          ('volr>=1.5', lambda v: v is not None and v >= VOLR_TH)):
            for slab, sok in (('急拉>1%', lambda x: x is not None and x > 0.01),
                              ('急拉>2%', lambda x: x is not None and x > 0.02),
                              ('急跌<-1%', lambda x: x is not None and x < -0.01)):
                sub = [r for r in rows if sok(r[key]) and vok(r['volr'])
                       and r['next_gap'] is not None]
                if len(sub) < 20:
                    continue
                out.append({'signal': label, 'filter': f'{slab} & {vlab}', 'n': len(sub),
                            'next_gap': _stat([r['next_gap'] for r in sub]),
                            'next_gap_neg_rate': round(
                                float(np.mean([r['next_gap'] < 0 for r in sub])), 4)})
    return out


def table3_overnight(rows):
    """③ 尾盘强势放量 → 隔夜溢价（按当日涨跌 × 尾盘量能分层）"""
    out = []
    for dlab, dok in (('当日涨>0', lambda r: r['o2c'] > 0), ('当日跌<0', lambda r: r['o2c'] < 0)):
        for vlab, vok in (('放量', lambda v: v is not None and v >= VOLR_TH),
                          ('平量', lambda v: v is not None and v < VOLR_TH)):
            sub = [r for r in rows if dok(r) and vok(r['volr']) and r['next_gap'] is not None]
            if not sub:
                continue
            out.append({'bucket': f'{dlab} & 尾盘{vlab}', 'n': len(sub),
                        'next_gap': _stat([r['next_gap'] for r in sub])})
    return out


def table4_gate_attribution(codes):
    """④ A7 正T（gap<=-1%）的闸门归因 + **beta 基准**。

    动机一（归因）：标定给 gap<=-1% 毛 o2c +0.58%，实验却 −0.18% —— 分离波动率门槛与市场过滤。
    动机二（beta 排除）：消融关掉市场过滤后 A7 正T 转正 +0.44%，但那些日子市场本身已动 ≥1%，
    必须对照**同日池内等权 open→close**，否则「信号」可能只是承担了市场 beta。
    """
    mkt = rx.market_r30(codes)
    rows, pool = [], {}
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        dates = [d for d in dates if WIN_START <= d <= WIN_END]
        if len(dates) < 30:
            continue
        st = rx.daily_stats(merged, dates)
        for d in dates:
            day = merged[d]
            if len(day) < MIN_BAR or day[0]['o'] <= 0:
                continue
            o2c = day[-1]['c'] / day[0]['o'] - 1
            pool.setdefault(d, []).append(o2c)
            s = st[d]
            if s['prev_close'] <= 0 or day[0]['o'] / s['prev_close'] - 1 > -0.01:
                continue                                       # 只看 gap<=-1%
            rows.append({'d': d, 'o2c': o2c, 'vol_ok': s['vol_med'] >= rx.VOL_GATE,
                         'mkt': mkt.get(d)})
    poolmean = {d: float(np.mean(v)) for d, v in pool.items() if v}

    def report(cond, pred):
        rs = [r for r in rows if pred(r)]
        ds = [r['d'] for r in rs if r['d'] in poolmean]
        st_ = _stat([r['o2c'] for r in rs])
        if st_ is None:
            return {'cond': cond, 'n': 0}
        st_['cond'] = cond
        st_['beta_同日池内等权%'] = (round(float(np.mean([poolmean[d] for d in ds])) * 100, 4)
                                    if ds else None)
        return st_

    return [
        report('全部 gap<=-1%（标定口径）', lambda r: True),
        report('+ 过波动率门槛', lambda r: r['vol_ok']),
        report('+ 过波动率+市场过滤（原 A7 口径）',
               lambda r: r['vol_ok'] and r['mkt'] is not None
               and abs(r['mkt']) < rx.A7_TREND_THR),
        report('+ 过波动率 & |mkt|>=1%（消融新增的日子）',
               lambda r: r['vol_ok'] and r['mkt'] is not None
               and abs(r['mkt']) >= rx.A7_TREND_THR),
    ]


def _print(title, tbl, cols):
    print(f'\n=== {title} ===')
    for row in tbl:
        parts = []
        for c in cols:
            v = row.get(c)
            if isinstance(v, dict):
                parts.append(f"{c}: n={v['n']} 均={v['mean']}% 中={v['median']}% 胜={v['win']}")
            else:
                parts.append(f'{c}={v}')
        print('  ' + ' | '.join(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--out', default=os.path.join(OUT, 'calib_2026-09-15.json'))
    args = ap.parse_args()
    codes = args.codes.split(',') if args.codes else discover()
    v2.END = WIN_END

    rows = collect(codes)
    t1, t2, t3 = table1_gap(rows), table2_tail(rows), table3_overnight(rows)
    t4 = table4_gate_attribution(codes)
    print(f'[calib] codes={len(codes)} 股票·日={len(rows)} 窗口={WIN_START}~{WIN_END}')
    _print('① 缺口分桶 × 当日走势（%）', t1, ['bucket', 'n', 'o2c', 'o2c1455', 'o2h', 'o2l'])
    _print('② 尾盘急拉 × 量能 → 次日 gap（%）', t2,
           ['signal', 'filter', 'n', 'next_gap', 'next_gap_neg_rate'])
    _print('③ 尾盘强势放量 → 隔夜溢价（%）', t3, ['bucket', 'n', 'next_gap'])
    _print('④ A7 闸门归因 + beta 基准（gap<=-1% 子集, open→close 毛值 %）', t4,
           ['cond', 'n', 'mean', 'median', 'win', 'beta_同日池内等权%'])
    json.dump({'meta': {'codes': len(codes), 'rows': len(rows),
                        'window': [WIN_START, WIN_END], 'volr_th': VOLR_TH},
               'gap_buckets': t1, 'tail_signals': t2, 'overnight': t3},
              open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
    print(f'\n[calib] -> {args.out}')


if __name__ == '__main__':
    main()
