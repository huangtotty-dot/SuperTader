# -*- coding: utf-8 -*-
"""B7 择时层因子过滤实验 — 2026-09-21（实验员_B7F，预注册）。

## 目的
GP 因子挖掘两轮（gp_ledger_seed0/1，约束版，日内 30min horizon 费后不可交易）
产出的 6 个 in_pool 候选因子，不做直接交易，改作 B7 隔夜反T（S1 口径，E1 已验证
n=421、费后净均 +0.6375%）的**择时过滤层**：「因子状态好的日子才执行 B7」。

## 预注册设计（冻结，不许事后改规则）
1. 复现 E1 的 S1 信号腿清单：直接 import run_b7_overnight 的 collect()/net_of()，
   同一数据层同一口径。先验证 n 与费后净均与 E1 报告一致（n=421、+0.6375%），
   偏差 >5% 立即中止排查（--no-abort 可覆盖，仅调试用）。
2. 每候选因子在每条信号腿的**信号日 14:30 那根 bar** 取 z 值：
   因子矩阵由 gp_miner.ExprEvaluator.eval_matrix 逐日因果滚动求值（右对齐窗口
   含当前 bar，不跨日），z = gp_miner._zscore_matrix(F, 14, 5)（过去 14 日同刻
   z，严格因果、不含当日）→ 只用 ≤14:30 信息，无未来函数。
3. 三个固定过滤变体：F1 z>0 才做；F2 z>1 才做；F3 z<−1 才做（反向）。
   对照组 = 全部 S1 信号腿（不过滤）。
4. 指标：n、费后净均、胜率；IS（<2026-06-01）/OOS（≥2026-06-01）分开报；
   过滤组与「同因子 z 有效但未过闸的剩余腿」对比（防选择偏差）。
5. 判定（预注册）：「有择时价值」= 过滤组 n≥80 且净均较对照提升 ≥+0.15pp
   且 OOS 提升同向（OOS 过滤组−OOS对照 > 0）。6 因子 × 3 变体全报。

## 费用口径（与 E1 完全一致，保证与 +0.6375% 可比）
净收益 = (卖×(1−0.00121) − 次日开盘×(1+0.00015)) / 卖；卖价 = close(14:55)。

## 运行
  python run_b7_factor_filter.py            # 全管线（带缓存，可重复运行）
  python run_b7_factor_filter.py --stage legs|factors|analyze
"""
import argparse
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
FM = os.path.join(ROOT, 't_io', 'validation', 'factor_mining')
for _p in (ROOT, FM, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_b7_overnight as b7                      # noqa: E402  E1 实验脚本（只读复用）
import run_experiment_v2 as v2                     # noqa: E402  经 b7 间接载入，同一数据层
from t_io.validation.factor_mining import gp_miner, minute_data  # noqa: E402

OUT_JSON = os.path.join(HERE, 'results_b7_factor_filter_2026-09-21.json')
CACHE_LEGS = os.path.join(HERE, '_cache_b7ff_legs.json')
CACHE_Z = os.path.join(HERE, '_cache_b7ff_z.json')

LEDGERS = [os.path.join(FM, 'results', 'gp_mine', f'gp_ledger_seed{s}.json')
           for s in (0, 1)]
TOP_K = 3                       # 每轮取 in_pool=true 前 3 个
OOS_START = '2026-06-01'
E1_N, E1_MEAN = 421, 0.6375     # E1 报告基准（S1×open）
E1_TOL = 0.05                   # 复现偏差容忍 5%

VARIANTS = {                    # 预注册冻结
    'F1_z>0': lambda z: z > 0.0,
    'F2_z>1': lambda z: z > 1.0,
    'F3_z<-1': lambda z: z < -1.0,
}
GATE_N, GATE_LIFT_PP = 80, 0.15   # 预注册判定阈值


def load_factors():
    """台账取候选：每 seed 的 candidates 按 fitness 降序，in_pool=true 前 3 个。"""
    facs = []
    for path in LEDGERS:
        d = json.load(open(path, encoding='utf-8'))
        seed = d.get('meta', {}).get('seed', os.path.basename(path))
        cands = sorted(d['candidates'], key=lambda x: -x.get('fitness', -9e9))
        for rank, c in enumerate([x for x in cands if x.get('in_pool')][:TOP_K]):
            facs.append({'seed': seed, 'rank': rank + 1,
                         'expr': c['expr'], 'sign_hint': c.get('sign_hint'),
                         'fitness': c.get('fitness'),
                         'fid': f"s{seed}#{rank + 1}"})
    return facs


# ══════════════════════ 阶段1：复现 E1 信号腿 ══════════════════════
def stage_legs(no_abort=False):
    if os.path.exists(CACHE_LEGS):
        legs = json.load(open(CACHE_LEGS, encoding='utf-8'))
        print(f'[legs] 命中缓存 {CACHE_LEGS}（{len(legs)} 条）')
        return legs
    codes = b7.discover()
    v2.END = b7.WIN_END
    rows = b7.collect(codes)
    legs = []
    for r in rows:
        if not (r['tail30'] is not None and r['tail30'] > 0.01):   # S1
            continue
        net = b7.net_of(r, 'open')                                  # 次日开盘接回
        if net is None:
            continue
        legs.append({'code': r['code'], 'date': r['date'], 'sell': r['sell'],
                     'tail30': r['tail30'], 'net': net,
                     'oos': r['date'] >= OOS_START})
    json.dump(legs, open(CACHE_LEGS, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'[legs] -> {CACHE_LEGS}（{len(legs)} 条）')
    return legs


def check_reproduction(legs, no_abort=False):
    n = len(legs)
    mean = float(np.mean([l['net'] for l in legs])) * 100
    win = float(np.mean([l['net'] > 0 for l in legs]))
    dn, dm = abs(n - E1_N) / E1_N, abs(mean - E1_MEAN) / abs(E1_MEAN)
    ok = dn <= E1_TOL and dm <= E1_TOL
    print(f'[复现校验] n={n} vs E1 {E1_N}（偏差 {dn:.2%}）；'
          f'净均={mean:+.4f}% vs E1 {E1_MEAN:+.4f}%（偏差 {dm:.2%}）；胜率={win:.4f}'
          f' -> {"一致" if ok else "不一致"}')
    if not ok and not no_abort:
        raise SystemExit('[复现校验] 偏差 >5%，按预注册纪律中止，先排查再继续。')
    return {'n': n, 'net_mean%': round(mean, 4), 'win': round(win, 4),
            'e1_n': E1_N, 'e1_mean%': E1_MEAN,
            'dev_n': round(dn, 6), 'dev_mean': round(dm, 6), 'pass': bool(ok)}


# ══════════════════════ 阶段2：因子 z 值（信号日 14:30 bar） ══════════════════════
def _kept_days_and_ref(df):
    """镜像 build_stock_panel 的模长对齐/标签一致性过滤，返回 (kept_dates, ref_hhmm)。
    用于定位 14:30 bar 的列下标；断言与 panel.dates 一致。"""
    t = df['time']
    day_codes = (t.dt.year * 10000 + t.dt.month * 100 + t.dt.day).to_numpy()
    hhmm = (t.dt.hour * 100 + t.dt.minute).to_numpy()
    uniq, starts = np.unique(day_codes, return_index=True)
    ends = np.r_[starts[1:], len(day_codes)]
    L = int(np.bincount(ends - starts).argmax())
    ref, kept = None, []
    for u, s, e in zip(uniq, starts, ends):
        if e - s != L:
            continue
        lab = hhmm[s:e]
        if ref is None:
            ref = lab
        elif not np.array_equal(lab, ref):
            continue
        kept.append(f'{u // 10000:04d}-{u // 100 % 100:02d}-{u % 100:02d}')
    return kept, ref


def stage_factors(facs):
    if os.path.exists(CACHE_Z):
        zmap = json.load(open(CACHE_Z, encoding='utf-8'))
        print(f'[factors] 命中缓存 {CACHE_Z}')
        return zmap
    codes = minute_data.pool_symbols()
    print(f'[factors] 加载池：{len(codes)} 只 …')
    raw = minute_data.load_pool(codes, b7.WIN_START, b7.WIN_END, verbose=True)
    _gp_fns, ns = gp_miner.build_gp_function_set()
    ev = gp_miner.ExprEvaluator(ns)
    zmap = {}          # fid -> {sym: {date: z}}
    for sym in codes:
        df = raw[sym]
        if df.empty:
            continue
        panel = gp_miner.build_stock_panel(sym, df, 'close30')
        if panel is None:
            print(f'[factors] {sym}: panel 构建失败（日数不足），跳过')
            continue
        kept, ref = _kept_days_and_ref(df)
        assert kept == panel.dates, f'{sym}: 日清单镜像校验失败'
        hits = np.flatnonzero(ref == 1430)
        if len(hits) != 1:
            print(f'[factors] {sym}: 14:30 标签缺失（{len(hits)} 个），跳过')
            continue
        col = int(hits[0])
        for f in facs:
            F = ev.eval_matrix(f['expr'], panel)
            Z = gp_miner._zscore_matrix(F, 14, 5)     # 严格因果：过去14日同刻
            zcol = Z[:, col]
            rec = zmap.setdefault(f['fid'], {}).setdefault(sym, {})
            for d, z in zip(panel.dates, zcol):
                if np.isfinite(z):
                    rec[d] = round(float(z), 6)
        print(f'[factors] {sym}: {panel.n_days} 日 × L={panel.L}，col(14:30)={col} 完成')
    json.dump(zmap, open(CACHE_Z, 'w', encoding='utf-8'))
    print(f'[factors] -> {CACHE_Z}')
    return zmap


# ══════════════════════ 阶段3：过滤分析 + 预注册判定 ══════════════════════
def _stat(nets):
    a = np.array(nets, float)
    if len(a) == 0:
        return None
    return {'n': int(len(a)), 'net_mean%': round(float(a.mean()) * 100, 4),
            'win': round(float((a > 0).mean()), 4)}


def _split_is_oos(pairs):
    """pairs: [(net, oos)] -> (全样本nets, IS nets, OOS nets)"""
    alln = [p[0] for p in pairs]
    isn = [p[0] for p in pairs if not p[1]]
    oon = [p[0] for p in pairs if p[1]]
    return alln, isn, oon


def stage_analyze(facs, legs, zmap):
    ctrl_all, ctrl_is, ctrl_oos = _split_is_oos([(l['net'], l['oos']) for l in legs])
    control = {'all': _stat(ctrl_all), 'IS': _stat(ctrl_is), 'OOS': _stat(ctrl_oos)}
    print(f"[对照组·全部信号] n={control['all']['n']} "
          f"净均={control['all']['net_mean%']:+.4f}% 胜={control['all']['win']:.4f} | "
          f"IS={control['IS']['net_mean%']:+.4f}%(n={control['IS']['n']}) "
          f"OOS={control['OOS']['net_mean%']:+.4f}%(n={control['OOS']['n']})")

    cells = []
    for f in facs:
        fid = f['fid']
        # 每条腿的 z（无 z = 面板缺日/历史不足/因子NaN → 不进任何变体）
        tagged = []
        for l in legs:
            z = zmap.get(fid, {}).get(l['code'], {}).get(l['date'])
            tagged.append((l, z))
        n_noz = sum(1 for _l, z in tagged if z is None)
        for vname, vfn in VARIANTS.items():
            in_g = [(l['net'], l['oos']) for l, z in tagged
                    if z is not None and vfn(z)]
            out_g = [(l['net'], l['oos']) for l, z in tagged
                     if z is not None and not vfn(z)]
            s_all, s_is, s_oos = _split_is_oos(in_g)
            r_all, r_is, r_oos = _split_is_oos(out_g)
            st = {'all': _stat(s_all), 'IS': _stat(s_is), 'OOS': _stat(s_oos)}
            rem = {'all': _stat(r_all), 'IS': _stat(r_is), 'OOS': _stat(r_oos)}
            # 预注册判定
            lift = (st['all']['net_mean%'] - control['all']['net_mean%']
                    if st['all'] else None)
            lift_oos = (st['OOS']['net_mean%'] - control['OOS']['net_mean%']
                        if st['OOS'] and control['OOS'] else None)
            g_n = bool(st['all'] and st['all']['n'] >= GATE_N)
            g_lift = bool(lift is not None and lift >= GATE_LIFT_PP)
            g_oos = bool(lift_oos is not None and lift_oos > 0)
            verdict = g_n and g_lift and g_oos
            cells.append({'fid': fid, 'expr': f['expr'],
                          'sign_hint': f['sign_hint'], 'variant': vname,
                          'n_no_z': n_noz, 'filtered': st, 'remainder': rem,
                          'lift_vs_ctrl_pp': (round(lift, 4)
                                              if lift is not None else None),
                          'lift_oos_pp': (round(lift_oos, 4)
                                          if lift_oos is not None else None),
                          'gate': {'n>=80': g_n, 'lift>=+0.15pp': g_lift,
                                   'OOS同向': g_oos, 'PASS': verdict}})
            a = st['all'] or {'n': 0, 'net_mean%': float('nan'), 'win': float('nan')}
            o = st['OOS'] or {'n': 0, 'net_mean%': float('nan')}
            ra = rem['all'] or {'n': 0, 'net_mean%': float('nan')}
            print(f"[{fid:6s}] {vname:7s} n={a['n']:3d} 净均={a['net_mean%']:+.4f}% "
                  f"胜={a['win']:.3f} | 剩余 n={ra['n']:3d} 净均={ra['net_mean%']:+.4f}% | "
                  f"IS={st['IS']['net_mean%'] if st['IS'] else '--'} "
                  f"OOS={o['net_mean%']:+.4f}%(n={o['n']}) | "
                  f"提升={lift if lift is not None else '--'}pp "
                  f"OOS提升={lift_oos if lift_oos is not None else '--'}pp "
                  f"{'PASS' if verdict else 'fail'}")
    return control, cells


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage', default='all',
                    choices=('all', 'legs', 'factors', 'analyze'))
    ap.add_argument('--no-abort', action='store_true',
                    help='复现校验偏差>5%时不中止（仅调试）')
    args = ap.parse_args()

    facs = load_factors()
    print(f'[候选因子] {len(facs)} 个：')
    for f in facs:
        print(f"  {f['fid']:6s} fitness={f['fitness']} sign={f['sign_hint']} {f['expr']}")

    legs = stage_legs(args.no_abort)
    repro = check_reproduction(legs, args.no_abort)
    if args.stage == 'legs':
        return
    zmap = stage_factors(facs)
    if args.stage == 'factors':
        return
    control, cells = stage_analyze(facs, legs, zmap)

    result = {
        'meta': {'experiment': 'B7 择时层因子过滤（预注册）', 'experimenter': 'B7F',
                 'date': '2026-09-21',
                 'base_strategy': 'B7 隔夜反T S1×open（E1: n=421, 净均+0.6375%）',
                 'fee': {'sell': b7.FEE_S, 'buy': b7.FEE_B},
                 'z_def': 'gp_miner._zscore_matrix(F,14,5) @ 信号日14:30 bar（严格因果）',
                 'oos_start': OOS_START,
                 'variants': list(VARIANTS),
                 'gate': f'过滤组 n>={GATE_N} 且净均提升>=+{GATE_LIFT_PP}pp 且 OOS提升>0',
                 'candidates': facs},
        'reproduction_check': repro,
        'control': control,
        'cells': cells,
        'summary': {'n_pass': sum(1 for c in cells if c['gate']['PASS']),
                    'n_cells': len(cells)},
    }
    json.dump(result, open(OUT_JSON, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(f'\n[完成] 过闸 {result["summary"]["n_pass"]}/{len(cells)} -> {OUT_JSON}')


if __name__ == '__main__':
    main()
