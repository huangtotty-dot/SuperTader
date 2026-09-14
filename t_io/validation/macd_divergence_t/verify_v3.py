# -*- coding: utf-8 -*-
"""v3 验证与稳健性（2026-09-14）— 独立于 run_experiment_v3.py 的复核口径。

产出（全部只读，不改生产）：
  A. 单元对账：臂 S 与臂 R 的 (票,日) 单元数必须一致
  B. **v1 假象复现**（对照实验，v3 存在的理由）：同一批策略 pair，分别对
     · 基线 A（v1 式：全样本无条件随机）
     · 基线 B（v3 式：同格 + 同过滤条件随机）
     若 A 显著"优于"B，即证明 v1 的 +15pp 是 conditioning 假象而非信号价值
  C. F2 阈值敏感性（-0.3% / -0.5% / -1.0%）
  D. 剔除覆盖不足的 4 票（600547/601318/300017/300475）后主终点是否翻转

运行：python verify_v3.py [--mc 200]
"""
import argparse
import json
import os
import random
import sys

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import run_experiment_v2 as v2  # noqa: E402
import run_experiment_v3 as v3  # noqa: E402

THIN = {'600547', '601318', '300017', '300475'}


def v1_style_baseline(all_pairs, day_recs, n_mc):
    """v1 式：不区分日型，在**全部** (票,日) 单元的随机 bar 卖出（同回补路径）。"""
    rng = random.Random(7)
    cells = list({(r['code'], r['date']): r for r in day_recs}.values())
    acc = {}
    for _ in range(n_mc):
        for rec in cells:
            hi = rec['idx_1430']
            if hi is None or hi < v2.WARMUP5:
                continue
            sb = rng.randint(v2.WARMUP5, hi)
            cover_px, _m = v3._cover_for(rec, sb)
            acc.setdefault(rec['date'], []).append(v3._net(float(rec['closes'][sb]), cover_px))
    return {d: float(np.mean(v)) for d, v in acc.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mc', type=int, default=200)
    ap.add_argument('--out', default=os.path.join(HERE, 'verify_v3_2026-09-14.json'))
    args = ap.parse_args()

    codes = v3.discover_codes()
    all_sigs, all_pairs, day_recs, day_bars, per_code, _sk = v3.collect(codes, '2025-09-14', '2026-08-26')
    print(f'[verify] codes={len(codes)} pairs={len(all_pairs)} stock_days={sum(per_code.values())}')
    out = {}

    # ---- A. 单元对账 ----
    recon = {}
    for name in v3.SUBSETS:
        sub = [p for p in all_pairs if v3._pair_in(name, p)]
        strat_cells = {(p['code'], p['date']) for p in sub}
        by_cell = {(r['code'], r['date']): r for r in day_recs}
        cells = [(by_cell[k], 1) for k in strat_cells if k in by_cell]
        _b, n_cells = v3.matched_baseline(cells, v3.subset_pred(name), n_mc=1) if cells else ({}, 0)
        recon[name] = {'strategy_cells': len(strat_cells), 'baseline_cells': n_cells,
                       'ok': len(strat_cells) == n_cells}
    out['A_cell_reconciliation'] = recon
    print('[A] 单元对账:', all(v['ok'] for v in recon.values()), recon)

    # ---- B. v1 假象复现 ----
    down_idx = {}
    for r in day_recs:
        down_idx[(r['code'], r['date'])] = (r['posthoc'] == 'down')
    down_pairs = [p for p in all_pairs if down_idx.get((p['code'], p['date']))]
    s_down = v3.strat_by_date(down_pairs)
    baseA = v1_style_baseline(all_pairs, day_recs, args.mc)              # v1 式：全样本随机
    cells_down = [(r, 1) for r in day_recs if r['posthoc'] == 'down']
    baseB, _ = v3.matched_baseline(cells_down, v3.subset_pred('ALL'), n_mc=args.mc)  # 同格随机
    def mean_of(d):
        return round(float(np.mean(list(d.values()))), 4) if d else None
    out['B_v1_illusion'] = {
        'strategy_down_day_avg': mean_of(s_down), 'n_pairs': len(down_pairs),
        'baselineA_v1_unconditioned_avg': mean_of(baseA),
        'baselineB_matched_down_avg': mean_of(baseB),
        'gap_vs_A_pp': (round(mean_of(s_down) - mean_of(baseA), 4)
                        if (s_down and baseA) else None),
        'gap_vs_B_pp': (round(mean_of(s_down) - mean_of(baseB), 4)
                        if (s_down and baseB) else None),
        'verdict': 'A 制造假象、B 才是真对照',
    }
    print('[B] v1 假象:', json.dumps(out['B_v1_illusion'], ensure_ascii=False))

    # ---- C. F2 阈值敏感性（复用同一次 collect，阈值只改判定） ----
    sens = {}
    for th in (-0.003, -0.005, -0.010):
        res = v3.evaluate_set(all_pairs, day_recs, 'sens', n_mc=args.mc,
                              f2_thresh=th, only=['F1F2'])
        r = res['F1F2']
        sens[f'{th:.3f}'] = {'n': r['strategy'].get('n'), 'avg': r['strategy'].get('avg_net_pct'),
                             'median': r['strategy'].get('median_net_pct'),
                             'win': r['strategy'].get('win_rate'), 'delta': r['delta_pp'],
                             'boot': r.get('bootstrap')}
    out['C_f2_sensitivity'] = sens
    print('[C] F2 敏感性:', json.dumps(sens, ensure_ascii=False))

    # ---- D. 剔除覆盖不足票（复用已收集数据，仅过滤票） ----
    pairs_thin = [p for p in all_pairs if p['code'] not in THIN]
    recs_thin = [r for r in day_recs if r['code'] not in THIN]
    res_thin = v3.evaluate_set(pairs_thin, recs_thin, 'thin', n_mc=args.mc, only=['F1F2'])
    r = res_thin['F1F2']
    out['D_excl_thin_coverage'] = {'codes_kept': len([c for c in codes if c not in THIN]),
                                   'strategy': r['strategy'], 'delta_pp': r['delta_pp'],
                                   'bootstrap': r.get('bootstrap')}
    print('[D] 剔除 4 薄覆盖票:', json.dumps(out['D_excl_thin_coverage'], ensure_ascii=False))

    json.dump(out, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('[verify] ->', args.out)


if __name__ == '__main__':
    main()
