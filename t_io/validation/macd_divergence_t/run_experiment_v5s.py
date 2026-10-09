# -*- coding: utf-8 -*-
"""分时 MACD 背离做T 实验 v5s（2026-10-09）— MA 约束口径敏感性扫描。

任务：主实验（动态 MA5 门控）的结论是否对 MA 口径选择稳健。
信号根处判定门控，不满足则跳过该笔；只跑 E1 回补臂（DIF 负区上拐，14:50 强制兜底）。

变体：
  MA5_dyn        动态 MA5 = mean(前4个已完成交易日收盘 + 当前价)，当前价 > 它（主实验参照臂）
  MA5_static     静态 MA5 = mean(前5个已完成交易日收盘)，当前价 > 它
  MA10_dyn       动态 MA10 = mean(前9日收盘 + 当前价)
  MA20_dyn       动态 MA20 = mean(前19日收盘 + 当前价)
  MA5_dyn_buf05  动态 MA5 + 站上缓冲：当前价 > MA5_dyn * 1.005

数据层复用 run_experiment_v2（load_csv_days/merge_days），信号层复用 run_experiment_v4
（高点递降 + DIF 腰斩，1min，确认滞后5根），39 票 v2_universe()，窗口/种子与 v4 一致。
费率只用 owner 实盘口径：买 0.0001054、卖 0.0006054（在 import 后改写 v4 模块常量，
v4.pair_net 全部调用点随之生效，信号/回补/随机基线同费率）。

vs_random 基线：语义严格复刻 v4.matched_baseline(mode='random')（同格随机入场、同回补
规则、同费率、按日池化均值），仅做向量化加速（每格的 (随机卖出根->净收益) 与随机抽样
用 numpy 预计算/批量生成，数学口径不变）；bootstrap 直接复用 v4.block_bootstrap。
注意：基线按各变体过滤后的 pairs 重建 cells（每格对数=过滤后该格对数），符合任务要求。

两阶段执行（防单次超时）：--stage collect（可按票断点续跑）→ --stage eval。
产物：results_v5s_2026-10-09.json + results_v5s_2026-10-09.md。
"""
import argparse
import json
import os
import pickle
import sys
import traceback

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_experiment_v2 as v2  # noqa: E402  数据层
import run_experiment_v4 as v4  # noqa: E402  信号层 + block_bootstrap

# ---- owner 实盘费率口径（覆盖 v4 模块常量，pair_net 全局生效）----
v4.FEE_SELL = 0.0006054
v4.FEE_BUY = 0.0001054
FEE_SELL, FEE_BUY = v4.FEE_SELL, v4.FEE_BUY

START = '2025-09-14'
END = '2026-08-26'          # 与 v4 主实验窗口一致
N_MC = v4.N_MC              # 500，种子一致
MC_SEED = v4.MC_SEED        # 20260914
ARM = 'E1_dif_turnup'       # 只跑 E1 回补臂（内含 14:50 强制兜底）

OUT_JSON = os.path.join(HERE, 'results_v5s_2026-10-09.json')
OUT_MD = os.path.join(HERE, 'results_v5s_2026-10-09.md')
CKPT = os.path.join(HERE, '_v5s_collect_ckpt.pkl')

MIN_HISTORY = {'MA5_dyn': 4, 'MA5_static': 5, 'MA10_dyn': 9, 'MA20_dyn': 19, 'MA5_dyn_buf05': 4}
VARIANTS = list(MIN_HISTORY)


def ma_gate(variant, px, prev_closes):
    """信号根处判定。prev_closes = 当前日之前所有已完成交易日收盘（升序）。
    返回 True/False；历史不足返回 None（不计入、单独计数）。"""
    k = MIN_HISTORY[variant]
    if len(prev_closes) < k:
        return None
    if variant == 'MA5_static':
        ma = float(np.mean(prev_closes[-5:]))
        return px > ma
    if variant == 'MA5_dyn':
        ma = float(np.mean(prev_closes[-4:] + [px]))
        return px > ma
    if variant == 'MA10_dyn':
        ma = float(np.mean(prev_closes[-9:] + [px]))
        return px > ma
    if variant == 'MA20_dyn':
        ma = float(np.mean(prev_closes[-19:] + [px]))
        return px > ma
    if variant == 'MA5_dyn_buf05':
        ma = float(np.mean(prev_closes[-4:] + [px]))
        return px > ma * 1.005
    raise ValueError(variant)


# ---------------- stage 1: collect（按票 checkpoint，可续跑） ----------------

def slim_rec(rec):
    """只保留 matched_baseline('random') 所需字段，减小 checkpoint 体积。"""
    return {'code': rec['code'], 'date': rec['date'], 'n': rec['n'],
            'c': rec['c'], 'dif': rec['dif'], 'hist': rec['hist'],
            'idx_1430': rec['idx_1430'], 'idx_1450': rec['idx_1450']}


def collect(codes):
    ck = {}
    if os.path.exists(CKPT):
        ck = pickle.load(open(CKPT, 'rb'))
        print(f'[v5s] 续跑：已有 {len(ck)} 票 checkpoint')
    for ci, code in enumerate(codes):
        if code in ck:
            continue
        dates, merged, _src = v2.merge_days(code)
        prev_closes, pairs, recs = [], [], []
        for dt in dates:
            bars = merged[dt]
            if START <= dt <= END and len(bars) >= v4.MIN_1M:
                _sigs, day_pairs, rec = v4.run_day(code, dt, bars)
                if rec is not None:
                    for p in day_pairs:
                        if p['arm'] != ARM:
                            continue
                        p['gates'] = {v: ma_gate(v, p['sell_px'], prev_closes) for v in VARIANTS}
                        pairs.append(p)
                    recs.append(slim_rec(rec))
            if bars:
                prev_closes.append(float(bars[-1]['c']))
        ck[code] = {'pairs': pairs, 'recs': recs}
        pickle.dump(ck, open(CKPT, 'wb'))
        print(f'[v5s] collect {ci + 1}/{len(codes)} {code}: pairs={len(pairs)} days={len(recs)}', flush=True)
    return ck


# ---------------- stage 2: evaluate ----------------

def cover_cache_for(rec):
    """每个候选卖出根 sb 的 E1 回补价。语义严格 = v4._cover_for(rec, sb, 'E1_dif_turnup')：
    首个 j>sb 且 dif[j]<0 且 dif[j]>dif[j-1]；从未触发则 14:50 兜底（j1450<=sb 时取末日）。
    实现：E1 触发根列表一次性 O(n) 算出，再 searchsorted 定位（避免每格 O(n^2) 扫描超时）。"""
    n, dif, c = rec['n'], rec['dif'], rec['c']
    trig = np.nonzero((dif[1:] < 0) & (dif[1:] > dif[:-1]))[0] + 1  # 全部 E1 触发根
    j1450 = rec['idx_1450']
    out = np.empty(n)
    pos = np.searchsorted(trig, np.arange(n) + 1)  # 首个 > sb 的触发根下标
    hit = pos < len(trig)
    out[hit] = c[trig[pos[hit]]]
    for sb in np.nonzero(~hit)[0]:                 # 兜底分支（与原实现逐根等价）
        j = j1450 if j1450 > sb else n - 1
        out[sb] = c[j]
    return out


def baseline_random_vectorized(cells, n_mc=N_MC, seed=MC_SEED):
    """语义 = v4.matched_baseline(cells, ARM, 'random')：
    每格随机卖出根 ∈ [WARMUP, idx_1430]，同回补规则同费率，按日池化 n_mc*npairs 次取均值。
    向量化：净收益表 nets[sb] 预计算；随机索引 numpy 批量生成。"""
    rng = np.random.RandomState(seed)
    elig = []
    for rec, npairs in cells:
        lo, hi = v4.WARMUP, rec['idx_1430']
        if hi < lo:
            continue
        c = rec['c']
        covers = cover_cache_for(rec)
        bars = np.arange(lo, hi + 1)
        nets = 100 * (c[bars] * (1 - FEE_SELL) - covers[bars] * (1 + FEE_BUY)) / c[bars]
        elig.append((rec['date'], nets, max(1, int(npairs))))
    if not elig:
        return {}, 0
    sum_by, cnt_by = {}, {}
    for date, nets, npairs in elig:
        # 一次性生成 n_mc*npairs 个 iid 均匀抽样（与逐轮 MC 池化口径完全等价，仅去 python 循环）
        draw = nets[rng.randint(0, len(nets), n_mc * npairs)]
        sum_by[date] = sum_by.get(date, 0.0) + float(draw.sum())
        cnt_by[date] = cnt_by.get(date, 0) + n_mc * npairs
    return {d: sum_by[d] / cnt_by[d] for d in sum_by}, len(elig)


def agg_variant(sub, n_universe):
    if not sub:
        return {'n_pairs': 0}
    v = np.array([p['net_pct'] for p in sub], float)
    pos = {}
    for p in sub:
        pos.setdefault(p['code'], []).append(p['net_pct'])
    return {'n_pairs': len(v),
            'win_rate': round(float((v > 0).mean()), 4),
            'avg_net_pct': round(float(v.mean()), 4),
            'median_net_pct': round(float(np.median(v)), 4),
            'n_stocks': len(pos),
            'n_stocks_pos': sum(1 for x in pos.values() if np.mean(x) > 0),
            'n_universe': n_universe}


def evaluate(ck, codes):
    all_pairs = [p for code in codes for p in ck[code]['pairs']]
    day_recs = [r for code in codes for r in ck[code]['recs']]
    by_cell = {(r['code'], r['date']): r for r in day_recs}
    print(f'[v5s] E1 pairs(门控前)={len(all_pairs)} days={len(day_recs)}')

    results, errors = {}, {}
    for var in VARIANTS:
        try:
            sub = [p for p in all_pairs if p['gates'][var] is True]
            n_insuf = sum(1 for p in all_pairs if p['gates'][var] is None)
            per_cell = {}
            for p in sub:
                k = (p['code'], p['date'])
                per_cell[k] = per_cell.get(k, 0) + 1
            cells = [(by_cell[k], n) for k, n in per_cell.items() if k in by_cell]
            b_rand, nc = baseline_random_vectorized(cells) if cells else ({}, 0)
            s_by = v4.strat_by_date(sub)
            common = sorted(set(s_by) & set(b_rand))
            deltas = [s_by[d] - b_rand[d] for d in common]
            boot = v4.block_bootstrap(deltas)
            out = agg_variant(sub, len(codes))
            out.update({
                'n_signals_pre_gate': len(all_pairs),
                'gate_pass_rate': round(len(sub) / len(all_pairs), 4) if all_pairs else None,
                'n_skipped_insufficient_history': n_insuf,
                'n_cells': len(cells),
                'baseline_random': (round(float(np.mean(list(b_rand.values()))), 4)
                                    if b_rand else None),
                'vs_random': {'delta_pp': (round(float(np.mean([s_by[d] for d in common]))
                                                 - float(np.mean([b_rand[d] for d in common])), 4)
                                           if common else None),
                              'bootstrap': boot},
            })
            results[var] = out
            print(f"[v5s] {var:14} n={out.get('n_pairs', 0):>5} win={out.get('win_rate')} "
                  f"avg={out.get('avg_net_pct')} Δr={out['vs_random']['delta_pp']} "
                  f"ci_lo={(boot or {}).get('ci_lo')}", flush=True)
        except Exception:
            errors[var] = traceback.format_exc()
            print(f'[v5s] 变体 {var} 跑不通:\n{errors[var]}', flush=True)
    return results, errors, len(all_pairs), len(day_recs)


def render_md(results, errors, n_pairs_pre, n_days, codes):
    L = []
    L.append('# v5s MA 约束口径敏感性扫描（2026-10-09）')
    L.append('')
    L.append('- 信号：v4 口径「高点递降 + DIF 腰斩」1min，只跑 E1 回补臂（DIF 负区上拐，14:50 强制兜底）')
    L.append(f'- 股票池：{len(codes)} 票 v2_universe()；窗口 {START}~{END}；股票·日 {n_days}；门控前 E1 pairs={n_pairs_pre}')
    L.append(f'- 费率（owner 实盘口径）：买 {FEE_BUY}、卖 {FEE_SELL}')
    L.append('- 门控在信号根处判定（当前价=信号触发根收盘价），不满足则跳过该笔；历史不足不计入并单列')
    L.append(f'- vs_random：复刻 v4 matched_baseline(mode=random)，按变体过滤后 pairs 重建 cells；'
             f'MC={N_MC} seed={MC_SEED}；bootstrap 复用 v4.block_bootstrap（n={v4.N_BOOT}, seed={v4.BOOT_SEED}）')
    L.append('')
    L.append('| 变体 | n_pairs | 门控通过率 | win_rate | avg_net_pct | median_net_pct | n_stocks_pos/39 | 随机基线 | Δ vs random(pp) | ci_lo | ci_hi |')
    L.append('|---|---|---|---|---|---|---|---|---|---|---|')
    for var in VARIANTS:
        if var in errors:
            L.append(f'| {var} | **跑不通** | - | - | - | - | - | - | - | - | - |')
            continue
        r = results[var]
        bt = r['vs_random']['bootstrap'] or {}
        L.append(f"| {var} | {r.get('n_pairs', 0)} | {r.get('gate_pass_rate')} | {r.get('win_rate')} "
                 f"| {r.get('avg_net_pct')} | {r.get('median_net_pct')} "
                 f"| {r.get('n_stocks_pos', 0)}/{r.get('n_universe', len(codes))} "
                 f"| {r.get('baseline_random')} | {r['vs_random']['delta_pp']} "
                 f"| {bt.get('ci_lo')} | {bt.get('ci_hi')} |")
    L.append('')
    if errors:
        L.append('## 跑不通的变体（不静默跳过）')
        for var, tb in errors.items():
            L.append(f'### {var}')
            L.append('```')
            L.append(tb.strip().splitlines()[-1] if tb.strip() else 'unknown')
            L.append('```')
        L.append('')
    insuf = {v: results[v]['n_skipped_insufficient_history'] for v in results
             if results[v].get('n_skipped_insufficient_history')}
    if insuf:
        L.append(f"- 历史不足（窗口最早期，前 N 日收盘不够）而未计入的 pairs：{insuf}")
        L.append('')
    return '\n'.join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage', default='all', choices=['collect', 'eval', 'all'])
    args = ap.parse_args()
    codes = v4.v2_universe()
    v2.END = END  # 与 v4 一致：快照只补到 CSV 覆盖末日
    assert len(codes) == 39, f'universe={len(codes)}'
    print(f'[v5s] codes={len(codes)} window={START}~{END} fee=买{FEE_BUY}/卖{FEE_SELL} stage={args.stage}')

    if args.stage in ('collect', 'all'):
        collect(codes)
    if args.stage in ('eval', 'all'):
        ck = pickle.load(open(CKPT, 'rb'))
        assert all(c in ck for c in codes), 'checkpoint 不完整，请先跑 collect'
        results, errors, n_pre, n_days = evaluate(ck, codes)
        out = {'meta': {'version': 'v5s', 'date': '2026-10-09', 'n_codes': len(codes),
                        'start': START, 'end': END, 'arm': ARM,
                        'fee_buy': FEE_BUY, 'fee_sell': FEE_SELL,
                        'n_mc': N_MC, 'mc_seed': MC_SEED,
                        'n_boot': v4.N_BOOT, 'boot_seed': v4.BOOT_SEED,
                        'signal_rule': 'v4: c2<=c1 & dif1>0 & dif2<dif1*0.5; 1min; 确认滞后5根',
                        'gate_def': {
                            'MA5_dyn': 'c > mean(前4日收盘,c)',
                            'MA5_static': 'c > mean(前5个已完成交易日收盘)',
                            'MA10_dyn': 'c > mean(前9日收盘,c)',
                            'MA20_dyn': 'c > mean(前19日收盘,c)',
                            'MA5_dyn_buf05': 'c > mean(前4日收盘,c)*1.005'},
                        'n_pairs_pre_gate': n_pre, 'stock_days': n_days},
               'results': results, 'errors': errors,
               'assertions': v2.ASSERTS}
        json.dump(out, open(OUT_JSON, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        open(OUT_MD, 'w', encoding='utf-8').write(render_md(results, errors, n_pre, n_days, codes))
        print(f'[v5s] -> {OUT_JSON}')
        print(f'[v5s] -> {OUT_MD}')


if __name__ == '__main__':
    main()
