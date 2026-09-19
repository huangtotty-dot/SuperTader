# -*- coding: utf-8 -*-
"""`run_vol_adaptive_exit.py` 结果的两张分位表（2026-09-18）。

## 为什么要拆出来

主脚本按**当日振幅** (`amp_pct`) 分位 —— 那是用**当天已经走完的高低价**分组，
**是前视**：振幅 20% 的日子按定义就是大动的日子，若它收涨，做多必然赚。
那张表只能当**描述**，不能当证据。

本脚本给出**因果版**：按 **入场时已知** 的信息分组：

  A. σ_ref 分位 —— 前 20 日振幅中位数（入场前就已知）
  B. 标的分位 —— 该票 1 年振幅中位数（选股层面，事前可算）

⇒ 只有这两张表能回答 owner 的「**选**日内波动比较大的标的」。
   若 A/B 表里净收益不随波动单调上升，则「选高波标的」这条**不成立**。

用法：python t_io/validation/t0_schemes/analyze_vol_adaptive_exit.py [--in <json>]
"""
import argparse
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.join(HERE, 'results_vol_adaptive_exit.json')

# 只看这几臂，避免表过宽
SHOW = ['E0_tp05', 'VA_tp30', 'VA_tp50', 'VA_tr40',
        'CEIL_mfe', 'CEIL_hold', 'RAND', 'RAND_E']


def quantile_table(rows, key, bins, label, show):
    print(f'\n{label}')
    print('  ' + ''.ljust(16) + ''.join(f'{k:>11}' for k in show) + f"{'n':>7}")
    base = np.array([x[key] for x in rows['E0_tp05']], float)
    edges = np.percentile(base, bins)
    for b in range(len(edges) - 1):
        lo_, hi_ = edges[b], edges[b + 1]
        sel_idx = (base >= lo_) & (base <= hi_)
        line = f'  Q{b+1} {lo_:>6.2f}~{hi_:>6.2f}'.ljust(16)
        for k in show:
            v = np.array([x['net'] for x in rows[k]], float)[sel_idx]
            line += f'{v.mean():>11.3f}' if len(v) else f'{"-":>11}'
        print(line + f'{int(sel_idx.sum()):>7}')
    # 单调性：净收益 vs 分位序号的 Spearman
    print(f'  ↑ 净收益随分位序号的秩相关（>0 表示"越波动越赚"）:')
    for k in show:
        v = np.array([x['net'] for x in rows[k]], float)
        med = [v[(base >= edges[b]) & (base <= edges[b + 1])].mean()
               for b in range(len(edges) - 1)]
        med = np.array([m for m in med if not np.isnan(m)])
        if len(med) < 3:
            continue
        r = np.corrcoef(np.arange(len(med)), med)[0, 1]
        flag = '  ← 单调↑' if r > 0.8 else ('  ← 单调↓' if r < -0.8 else '')
        print(f'    {k:12} r={r:+.2f}{flag}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='inp', default=DEFAULT)
    args = ap.parse_args()
    if not os.path.exists(args.inp):
        print(f'找不到 {args.inp}（主实验还没跑完？）')
        return
    d = json.load(open(args.inp, encoding='utf-8'))
    rows, meta = d['rows'], d['meta']
    print('=' * 78)
    print(f'vol-adaptive exit 分析 · 池 {meta["codes"]} 只  '
          f'有入场的天 {meta["entries"]}  往返成本 {meta["cost_pct"]:.3f}%')
    print('=' * 78)

    cs = meta.get('code_sigma_pct') or {}
    if cs:
        v = np.array(sorted(cs.values()))
        print(f'  池内标的振幅中位数: {np.median(v):.2f}%  '
              f'区间 {v.min():.2f}~{v.max():.2f}%  （全市场中位 3.32%, P90 4.19%）')

    print('\n【对照臂读法】')
    print('  RAND   = 生产入场 + 随机出场  → 出场有没有技巧（vs E0_tp05）')
    print('  RAND_E = 随机入场 + 生产出场  → 入场有没有 edge')
    print('  CEIL_* = 完美前视，仅作上界，不可实现')

    for k in SHOW:
        if k not in rows:
            continue
        net = np.array([x['net'] for x in rows[k]], float)
        print(f'  {k:10} 净均 {net.mean():+.3f}%  胜率 {(net > 0).mean():.3f}  n={len(net)}')

    quantile_table(rows, 'sigma_pct', [0, 20, 40, 60, 80, 100],
                   '【A】按 σ_ref（前 20 日振幅中位数，入场前已知）分位 → 臂净均%', SHOW)

    # B. 标的层面
    print('\n【B】按标的（该票 1 年振幅中位数，事前可算）分位 → 臂净均%')
    print('  只在 Q 分位内选票，等价于"选波动大的标的"这条选股规则')
    codes = sorted(cs, key=lambda c: cs[c]) if cs else []
    if codes:
        cq = np.array_split(codes, 5)
        print('  ' + ''.ljust(22) + ''.join(f'{k:>11}' for k in SHOW) + f"{'n':>7}")
        for b, grp in enumerate(cq):
            gs = set(grp)
            line = f'  Q{b+1} σ{cq[b][0][:6]}~{cq[b][-1][:6]}'.ljust(22)
            for k in SHOW:
                v = np.array([x['net'] for x in rows[k] if x['code'] in gs], float)
                line += f'{v.mean():>11.3f}' if len(v) else f'{"-":>11}'
            print(line + f'{int(sum(1 for x in rows["E0_tp05"] if x["code"] in gs)):>7}')

    print('\n⚠️ 主脚本的 by_quantile 按**当日振幅**分组 = 前视（当天走完才知道），')
    print('   只能当描述。**结论只认 A/B 两张因果表。**')


if __name__ == '__main__':
    main()
