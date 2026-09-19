# -*- coding: utf-8 -*-
"""种子因子分钟级体检（任务 S2-7）—— 30 个 Alpha191 分钟化种子因子的实战基线。

定位：GP 搜索（S2-3/AlphaGen 臂）开工前，对 S2-4 交付的 30 个种子因子
（alpha191_seeds.py）在 39 只分钟池上建立基线，回答「哪些种子值得进 GP
初始化种群、哪些直接淘汰」。

四层口径（全部预注册，与设计文档 doc/research/2026-09-19_AlphaGen适配设计.md 对齐）：

① 单票时序 IC 初筛（本文件主交付）
   - label = 未来 30 分钟收益：close(t+30)/close(t) − 1（方向口径，非捕获上限
     口径——对齐设计文档 §3.3 候选二「单票时序 IC」的 label 修正口径）。
   - label 不跨日：每日末 30 根 bar 的 label 为 NaN（30 bar 前瞻若跨日则混入
     隔夜跳空，且会在 IS/OOS 边界造成跨段泄漏；日内截断后 IS 的 label 不可能
     触及 OOS 数据，边界天然安全）。
   - 逐只票算因子值与 label 的 Spearman 相关（秩用 ic_layer._avg_rank_contig，
     禁 scipy）；39 只汇总 均值/中位/胜率(IC>0 票占比)/最差/最佳。
   - IS/OOS 硬边界 2026-06-01，分别报告。

② eval_factor.py 交易式终审（IC 前 5 名）
   直接 import eval_factor 的 load_code / aligned_matrix / legs_hold_fast /
   zscores / run_entries / random_baseline / _aggregate，口径逐项对齐：
   z-score（过去 14 日同一 HH:MM）穿越 ±1 入场、次根 bar 开盘成交、
   双边 0.136% 成本（rx._leg_pnl 的 FEE_S+FEE_B=0.00136）、双向都测、
   hold/native 两出场臂、逐腿配对随机基线、OOS 2026-06-01。
   种子因子适配：把 panel 的日 bar 重拼成连续分钟序列喂给 alpha191_seeds
   （跨日窗口在完整序列上计算），再按日切回 {date: array} 喂 eval_factor。
   --leak 前视自检：因子值逐日内整体后移 N 根（与 eval_factor.compute_factor
   的 leak 语义逐位一致），择时能力应塌缩回随机基线。

③ MC 标签日块打乱基线（前 5 名，n=100）
   对 IS 段 label 做日块级时间打乱（保留日内形态、破坏可预测对齐，对齐设计
   文档 §六），重算票均时序 IC 得 null 分布；真实 IC ≤ null 95 分位 → SUSPECT。
   负 IC 因子先按 IS 均值符号定向（做空方向单侧闸门，与 ic_layer 口径一致）。

④ 报告：doc/experiment/2026-09-19_种子因子分钟级体检.md

用法（Bash 单条 ≤290s，IC 计算按票分批 + parquet 缓存到 tmp/seed_eval_cache/）：
  python seed_eval.py ic --codes 000506,000636 ...   # 逐票算因子+IC，落缓存
  python seed_eval.py aggregate                      # 汇总 30 因子总表
  python seed_eval.py mc --factors a001,a002,...     # 前5 MC（读缓存）
  python seed_eval.py final --codes 000506,...       # 终审逐票 parts（落 jsonl）
  python seed_eval.py final-aggregate                # 汇总终审
  python seed_eval.py final --leak 5 --exits hold    # 前视自检

只读复用（不改）：minute_data.py / alpha191_seeds.py / eval_factor.py / ic_layer.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import minute_data as md          # S2-2 分钟数据层（只读）
import alpha191_seeds as seeds    # S2-4 种子库（只读）
import ic_layer                   # P1-2 IC 层（只读，借秩函数）

OOS_START = '2026-06-01'
FWD = 30                          # label 前瞻 30 根 bar（=30 分钟）
MC_N = 100
MC_SEED = 20260919
MIN_PAIRS = 200                   # 单票单段有效样本下限（~1 日的 bar 数）

CACHE_DIR = os.path.join(ROOT, 'tmp', 'seed_eval_cache')
RESULTS_DIR = os.path.join(HERE, 'results')
SEED_NAMES = list(seeds.SEEDS)    # 30 个种子，注册表顺序固定

EPS = 1e-12


# ────────────────────────────────────────────────────────────────────────────
# 基础原语
# ────────────────────────────────────────────────────────────────────────────
def forward_label(close, day_id, fwd=FWD):
    """未来 fwd 根 bar 收益 close(t+fwd)/close(t)−1，**不跨日**（日末 fwd 根为 NaN）。

    方向口径（对齐设计文档 §3.3 label 修正口径）：度量「信号后 30 分钟方向对不对」，
    而非「最高能吃到多少」（捕获上限口径有不可成交的乐观偏差，终审才用真实出场臂）。
    """
    close = np.asarray(close, float)
    day_id = np.asarray(day_id)
    lab = np.full(len(close), np.nan)
    if len(close) <= fwd:
        return lab
    same = day_id[:-fwd] == day_id[fwd:]
    idx = np.flatnonzero(same)
    with np.errstate(divide='ignore', invalid='ignore'):
        v = close[idx + fwd] / np.where(close[idx] > 0, close[idx], np.nan) - 1.0
    lab[idx] = v
    lab[~np.isfinite(lab)] = np.nan
    return lab


def _rank1d(v):
    """单组平均秩（ties 取平均），复用 ic_layer 连续组实现，禁 scipy。"""
    return ic_layer._avg_rank_contig(np.asarray(v, float),
                                     np.zeros(len(v), dtype=np.int64))


def spearman(x, y, min_pairs=MIN_PAIRS):
    """Spearman 相关 = 秩上 Pearson。返回 (rho, n_pairs)；样本不足/零方差 → NaN。"""
    x, y = np.asarray(x, float), np.asarray(y, float)
    m = np.isfinite(x) & np.isfinite(y)
    n = int(m.sum())
    if n < min_pairs:
        return np.nan, n
    rx, ry = _rank1d(x[m]), _rank1d(y[m])
    rx, ry = rx - rx.mean(), ry - ry.mean()
    den = float(np.sqrt((rx ** 2).sum() * (ry ** 2).sum()))
    if den <= 0:
        return np.nan, n
    return float((rx * ry).sum() / den), n


def _bar_dates(ctx):
    """逐 bar 的日期字符串数组。"""
    return np.asarray(ctx.dates, dtype=object)[ctx.day_id]


# ────────────────────────────────────────────────────────────────────────────
# ① IC 初筛：逐票计算（带 parquet 缓存，供 MC 复用）
# ────────────────────────────────────────────────────────────────────────────
def compute_stock_ic(code, cache_dir=CACHE_DIR, force=False):
    """单票：加载分钟线 → 30 种子因子 → label → IS/OOS 时序 Spearman IC。

    返回 {code, n_bars, n_days_is, n_days_oos, per_factor: {name: {...}}}。
    因子值面板缓存为 parquet（MC/复审复用，避免重算）。
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_pq = os.path.join(cache_dir, f'{code}.parquet')
    df = md.load_minutes(code)
    if df.empty:
        return {'code': code, 'error': 'no data'}
    ctx = seeds.MinuteCtx(df)
    if os.path.exists(cache_pq) and not force:
        fa = pd.read_parquet(cache_pq)
        label = fa['label'].to_numpy(float)
        dates = fa['date'].astype(str).to_numpy()
    else:
        t0 = time.perf_counter()
        fa_all = seeds.compute_all(ctx)          # ctx 复用一次预处理
        label = forward_label(ctx.c, ctx.day_id)
        dates = _bar_dates(ctx)
        fa = pd.DataFrame({'date': dates, 'label': label})
        for nm in SEED_NAMES:
            fa[nm] = fa_all[nm].to_numpy().astype(np.float32)
        fa.to_parquet(cache_pq, index=False)
        print(f'    [{code}] 因子面板 {fa.shape} 计算+缓存 {time.perf_counter() - t0:.1f}s',
              flush=True)
    is_m = dates < OOS_START
    oos_m = ~is_m
    per = {}
    for nm in SEED_NAMES:
        fv = fa[nm].to_numpy(float)
        ic_is, n_is = spearman(fv[is_m], label[is_m])
        ic_oos, n_oos = spearman(fv[oos_m], label[oos_m])
        per[nm] = {'ic_is': ic_is, 'n_is': n_is, 'ic_oos': ic_oos, 'n_oos': n_oos}
    return {'code': code, 'n_bars': int(len(fa)),
            'n_days_is': int(len(np.unique(dates[is_m]))),
            'n_days_oos': int(len(np.unique(dates[oos_m]))),
            'per_factor': per}


def cmd_ic(args):
    codes = args.codes.split(',')
    t0 = time.perf_counter()
    for i, c in enumerate(codes):
        r = compute_stock_ic(c, force=args.force)
        out_p = os.path.join(CACHE_DIR, f'{c}_ic.json')
        with open(out_p, 'w', encoding='utf-8') as fh:
            json.dump(r, fh, ensure_ascii=False)
        print(f'  [{i + 1}/{len(codes)}] {c} 完成 累计 {time.perf_counter() - t0:.0f}s',
              flush=True)
    print(f'[ic] 本批 {len(codes)} 只完成，用时 {time.perf_counter() - t0:.0f}s')


def _summarize_ics(ic_map):
    """ic_map: {code: ic} → 均值/中位/胜率/最差/最佳。"""
    items = [(c, v) for c, v in ic_map.items() if v is not None and np.isfinite(v)]
    if not items:
        return {'n': 0}
    vals = np.array([v for _, v in items])
    worst = min(items, key=lambda t: t[1])
    best = max(items, key=lambda t: t[1])
    return {'n': len(items), 'mean': round(float(vals.mean()), 4),
            'median': round(float(np.median(vals)), 4),
            'win': round(float((vals > 0).mean()), 4),
            'std': round(float(vals.std(ddof=1)), 4) if len(vals) > 1 else 0.0,
            'worst': [worst[0], round(worst[1], 4)],
            'best': [best[0], round(best[1], 4)]}


def cmd_aggregate(args):
    rows, bystock = [], []
    for c in md.pool_symbols():
        p = os.path.join(CACHE_DIR, f'{c}_ic.json')
        if not os.path.exists(p):
            print(f'  [warn] 缺 {c} 的 IC 结果，跳过')
            continue
        with open(p, encoding='utf-8') as fh:
            r = json.load(fh)
        if 'per_factor' not in r:
            continue
        bystock.append(r)
    if not bystock:
        print('[aggregate] 无任何逐票结果'); return
    for nm in SEED_NAMES:
        is_map = {r['code']: r['per_factor'][nm]['ic_is'] for r in bystock}
        oos_map = {r['code']: r['per_factor'][nm]['ic_oos'] for r in bystock}
        s_is, s_oos = _summarize_ics(is_map), _summarize_ics(oos_map)
        row = {'factor': nm,
               'is_mean': s_is.get('mean'), 'is_median': s_is.get('median'),
               'is_win': s_is.get('win'), 'is_std': s_is.get('std'),
               'is_worst': s_is.get('worst'), 'is_best': s_is.get('best'),
               'oos_mean': s_oos.get('mean'), 'oos_median': s_oos.get('median'),
               'oos_win': s_oos.get('win'), 'oos_std': s_oos.get('std'),
               'oos_worst': s_oos.get('worst'), 'oos_best': s_oos.get('best'),
               'sign_consistent': (bool(np.sign(s_is.get('mean', 0) or 0)
                                        == np.sign(s_oos.get('mean', 0) or 0))),
               'abs_is_mean': abs(s_is.get('mean') or 0.0)}
        rows.append(row)
    rows.sort(key=lambda r: -r['abs_is_mean'])
    flat = []
    for r in rows:
        f = {k: v for k, v in r.items() if k not in ('is_worst', 'is_best',
                                                     'oos_worst', 'oos_best')}
        f['is_worst'] = f'{r["is_worst"][0]}:{r["is_worst"][1]}' if r.get('is_worst') else None
        f['is_best'] = f'{r["is_best"][0]}:{r["is_best"][1]}' if r.get('is_best') else None
        f['oos_worst'] = f'{r["oos_worst"][0]}:{r["oos_worst"][1]}' if r.get('oos_worst') else None
        f['oos_best'] = f'{r["oos_best"][0]}:{r["oos_best"][1]}' if r.get('oos_best') else None
        flat.append(f)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    df = pd.DataFrame(flat)
    csv_p = os.path.join(RESULTS_DIR, 'seed_eval_ic_summary.csv')
    df.to_csv(csv_p, index=False, encoding='utf-8-sig')
    with open(os.path.join(RESULTS_DIR, 'seed_eval_ic_summary.json'), 'w',
              encoding='utf-8') as fh:
        json.dump({'oos_start': OOS_START, 'fwd_bars': FWD, 'n_stocks': len(bystock),
                   'ranked_by': '|IS mean IC|', 'factors': rows}, fh,
                  ensure_ascii=False, indent=1)
    # 逐票明细长表
    detail = []
    for r in bystock:
        for nm in SEED_NAMES:
            pf = r['per_factor'][nm]
            detail.append({'code': r['code'], 'factor': nm, **pf})
    pd.DataFrame(detail).to_csv(os.path.join(RESULTS_DIR, 'seed_eval_ic_bystock.csv'),
                                index=False, encoding='utf-8-sig')
    top5 = [r['factor'] for r in rows[:5]]
    print(f'[aggregate] {len(bystock)} 只 × {len(rows)} 因子 → {csv_p}')
    print(f'[aggregate] |IS mean IC| TOP5: {top5}')
    hdr = f"{'factor':8}{'IS均':>8}{'IS中位':>8}{'IS胜率':>7}{'OOS均':>8}{'OOS中位':>8}{'OOS胜率':>7}  符号一致"
    print(hdr)
    for r in rows:
        print(f"{r['factor']:8}{r['is_mean']:>8}{r['is_median']:>8}{r['is_win']:>7}"
              f"{r['oos_mean']:>8}{r['oos_median']:>8}{r['oos_win']:>7}  {r['sign_consistent']}")


# ────────────────────────────────────────────────────────────────────────────
# ③ MC 标签日块打乱基线（前 5 因子）
# ────────────────────────────────────────────────────────────────────────────
def _stock_is_blocks(code, factor):
    """读缓存 → IS 段的 (因子秩, label 日块秩列表, 真实 IC)。

    label 秩在 IS 全段一次性算好：日块打乱是值集合的双射，秩不变，
    每次迭代只需按日块重排秩数组（100 次迭代的性能关键）。
    """
    fa = pd.read_parquet(os.path.join(CACHE_DIR, f'{code}.parquet'))
    dates = fa['date'].astype(str).to_numpy()
    is_m = dates < OOS_START
    f = fa[factor].to_numpy(float)[is_m]
    lab = fa['label'].to_numpy(float)[is_m]
    d_is = dates[is_m]
    f_rank = np.full(len(f), np.nan)
    fm = np.isfinite(f)
    if fm.sum() >= MIN_PAIRS:
        f_rank[fm] = _rank1d(f[fm])
    lm = np.isfinite(lab)
    lab_rank = np.full(len(lab), np.nan)
    if lm.sum() >= MIN_PAIRS:
        lab_rank[lm] = _rank1d(lab[lm])
    # 日块切分（IS 段内日期连续）
    edges = np.flatnonzero(np.r_[True, d_is[1:] != d_is[:-1], True])
    blocks = [lab_rank[edges[i]:edges[i + 1]] for i in range(len(edges) - 1)]
    real, _ = spearman(f, lab)
    return f_rank, blocks, real


def mc_factor(codes, factor, n=MC_N, seed=MC_SEED):
    """单因子 MC：日块打乱 label → null 分布（票均 IC）。

    返回 {factor, real_mean, dir, null_mean, null_std, mc_rank, q95, pass,
          stock_pass_rate, per_stock: {code: {real, null_q95, pass}}}
    """
    rng = np.random.default_rng(seed)
    data, reals = {}, {}
    for c in codes:
        try:
            f_rank, blocks, real = _stock_is_blocks(c, factor)
        except FileNotFoundError:
            continue
        if not np.isfinite(real):
            continue
        data[c] = (f_rank, blocks)
        reals[c] = real
    if not data:
        return {'factor': factor, 'error': 'no data'}
    real_arr = np.array(list(reals.values()))
    real_mean = float(real_arr.mean())
    direc = 1.0 if real_mean >= 0 else -1.0      # 负 IC 因子按做空方向定向
    null_stock = {c: np.empty(n) for c in data}
    null_mean = np.empty(n)
    for it in range(n):
        ics = []
        for c, (f_rank, blocks) in data.items():
            perm = rng.permutation(len(blocks))
            lab_p = np.concatenate([blocks[p] for p in perm])
            ic, _ = spearman(f_rank, lab_p)       # f_rank 已是秩；spearman 重排秩幂等
            null_stock[c][it] = ic
            ics.append(ic)
        null_mean[it] = float(np.nanmean(ics))
    q95 = float(np.nanpercentile(direc * null_mean, 95))
    real_oriented = direc * real_mean
    per_stock = {}
    for c in data:
        nul = direc * null_stock[c]
        per_stock[c] = {'real': round(direc * reals[c], 4),
                        'null_q95': round(float(np.nanpercentile(nul, 95)), 4),
                        'pass': bool(direc * reals[c] > np.nanpercentile(nul, 95))}
    return {'factor': factor, 'n_stocks': len(data), 'n_iter': n,
            'real_mean': round(real_mean, 4), 'dir': int(direc),
            'null_mean': round(float(np.nanmean(null_mean)), 4),
            'null_std': round(float(np.nanstd(null_mean, ddof=1)), 4),
            'mc_rank': round(float((direc * null_mean < real_oriented).mean()), 4),
            'q95': round(q95, 4),
            'pass': bool(real_oriented > q95),
            'stock_pass_rate': round(float(np.mean([v['pass']
                                                    for v in per_stock.values()])), 4),
            'per_stock': per_stock}


def _mc_worker(pack):
    factor, codes, n, seed = pack
    return factor, mc_factor(codes, factor, n=n, seed=seed)


def cmd_mc(args):
    factors = args.factors.split(',')
    codes = args.codes.split(',') if args.codes else md.pool_symbols()
    t0 = time.perf_counter()
    if args.workers > 1 and len(factors) > 1:
        import concurrent.futures as cf
        packs = [(nm, codes, args.n, args.seed) for nm in factors]
        with cf.ProcessPoolExecutor(max_workers=min(args.workers, len(factors))) as pool:
            res = dict(pool.map(_mc_worker, packs))
        out = {nm: res[nm] for nm in factors}
        for nm in factors:
            r = out[nm]
            print(f"  [mc] {nm}: real={r.get('real_mean')} dir={r.get('dir')} "
                  f"null={r.get('null_mean')}±{r.get('null_std')} "
                  f"mc_rank={r.get('mc_rank')} pass={r.get('pass')} "
                  f"票通过率={r.get('stock_pass_rate')}", flush=True)
        print(f'  [mc] 并行完成 累计 {time.perf_counter() - t0:.0f}s')
    else:
        out = {}
        for nm in factors:
            out[nm] = mc_factor(codes, nm, n=args.n, seed=args.seed)
            r = out[nm]
            print(f"  [mc] {nm}: real={r.get('real_mean')} dir={r.get('dir')} "
                  f"null={r.get('null_mean')}±{r.get('null_std')} "
                  f"mc_rank={r.get('mc_rank')} pass={r.get('pass')} "
                  f"票通过率={r.get('stock_pass_rate')} 累计 {time.perf_counter() - t0:.0f}s",
                  flush=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    p = os.path.join(RESULTS_DIR, 'seed_eval_mc.json')
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump({'oos_start': OOS_START, 'mc_on': 'IS', 'n_iter': args.n,
                   'seed': args.seed, 'method': 'label 日块打乱（保留日内形态）',
                   'factors': out}, fh, ensure_ascii=False, indent=1)
    print(f'[mc] → {p} 用时 {time.perf_counter() - t0:.0f}s')


# ────────────────────────────────────────────────────────────────────────────
# ② eval_factor 交易式终审（前 5 因子）
# ────────────────────────────────────────────────────────────────────────────
def _panel_continuous_df(panel):
    """把 eval_factor.load_code 的 panel 日 bar 重拼成连续分钟 df（喂种子库）。"""
    frames = []
    for d in panel['dates']:
        ctx = panel['days'][d]
        frames.append(pd.DataFrame({
            'time': pd.to_datetime([d + ' ' + t for t in ctx['t']]),
            'open': ctx['o'], 'high': ctx['h'], 'low': ctx['l'], 'close': ctx['c'],
            'volume': ctx['v'], 'amount': ctx['amt']}))
    df = pd.concat(frames, ignore_index=True)
    assert df['time'].is_monotonic_increasing, f'时间非单调，无法按位切片'
    return df


def panel_seed_fvals(panel, seed_names):
    """连续序列上算种子因子（跨日窗口语义正确），再按日切回 {name: {date: arr}}。"""
    df = _panel_continuous_df(panel)
    fa = seeds.compute_all(df)
    out = {nm: {} for nm in seed_names}
    pos = 0
    for d in panel['dates']:
        n = len(panel['labels'][d])
        for nm in seed_names:
            out[nm][d] = fa[nm].to_numpy()[pos:pos + n]
        pos += n
    assert pos == len(df)
    return out


def apply_leak(fvals, leak):
    """前视自检：逐日整体后移 leak 根（与 eval_factor.compute_factor 逐位同义）。"""
    out = {}
    for d, v in fvals.items():
        w = np.full_like(v, np.nan)
        if 0 < leak < len(v):
            w[leak:] = v[:len(v) - leak]
        out[d] = w
    return out


def final_code(code, seed_names, signs=(1, -1), exits=('hold', 'native'), leak=0):
    """单票终审 parts：完全复用 eval_factor 的 zscore/入场/出场/随机基线函数。"""
    import eval_factor as ef
    ef.v2.END = ef.WIN_END            # 与 eval_factor.main 的设定一致
    panel = ef.load_code(code)
    if not panel:
        return []
    fv_by_name = panel_seed_fvals(panel, seed_names)
    if leak:
        fv_by_name = {nm: apply_leak(fv, leak) for nm, fv in fv_by_name.items()}
    keep, labels, L, _nd = ef.aligned_matrix(panel)
    parts = []
    for nm in seed_names:
        fv = fv_by_name[nm]
        zm = None
        for sign in signs:
            for ex in exits:
                if ex == 'hold' and keep:
                    legs = ef.legs_hold_fast(panel, fv, sign, keep, labels, L)
                    for x in legs:
                        x['code'] = code
                        c0 = panel['days'][x['date']]
                        x['day_type'] = ef.rx.day_type(c0['o'], c0['h'], c0['l'],
                                                       c0['c'], c0['prev_close'])
                else:
                    if zm is None:
                        zm = ef.zscores(fv, panel)
                    legs = ef.run_entries(panel, zm, sign, ex, code)
                rnd = ef.random_baseline(panel, legs, ex, code)
                parts.append({'factor': nm, 'sign': sign, 'exit': ex, 'leak': leak,
                              'code': code, 'legs': legs, 'rand': rnd,
                              'n_days': len(panel['dates'])})
    return parts


def _json_default(o):
    """numpy 标量 → python 标量（legs 里的 bar 索引来自 np.where）。"""
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    raise TypeError(f'{type(o).__name__} not serializable')


def _final_code_worker(pack):
    """子进程入口（Windows spawn 需模块级）。"""
    code, factors, exits, leak = pack
    try:
        return code, final_code(code, factors, exits=tuple(exits), leak=leak)
    except Exception as e:
        print(f'  [warn] {code}: {type(e).__name__} {e}', flush=True)
        return code, []


def cmd_final(args):
    codes = args.codes.split(',')
    factors = args.factors.split(',')
    exits = tuple(args.exits.split(','))
    parts_p = os.path.join(CACHE_DIR, 'final_parts.jsonl')
    os.makedirs(CACHE_DIR, exist_ok=True)
    t0 = time.perf_counter()
    packs = [(c, factors, exits, args.leak) for c in codes]
    done = 0
    with open(parts_p, 'a', encoding='utf-8') as fh:
        if args.workers > 1:
            import concurrent.futures as cf
            with cf.ProcessPoolExecutor(max_workers=args.workers) as pool:
                futs = {pool.submit(_final_code_worker, pk): pk[0] for pk in packs}
                for fut in cf.as_completed(futs):
                    code, parts = fut.result()
                    for p in parts:
                        fh.write(json.dumps(p, ensure_ascii=False,
                                            default=_json_default) + '\n')
                    fh.flush()
                    done += 1
                    print(f'  [{done}/{len(codes)}] {code} parts={len(parts)} '
                          f'累计 {time.perf_counter() - t0:.0f}s', flush=True)
        else:
            for i, c in enumerate(codes):
                try:
                    parts = final_code(c, factors, exits=exits, leak=args.leak)
                except Exception as e:
                    print(f'  [warn] {c}: {type(e).__name__} {e}', flush=True)
                    continue
                for p in parts:
                    fh.write(json.dumps(p, ensure_ascii=False,
                                        default=_json_default) + '\n')
                fh.flush()
                print(f'  [{i + 1}/{len(codes)}] {c} parts={len(parts)} '
                      f'累计 {time.perf_counter() - t0:.0f}s', flush=True)
    print(f'[final] 本批完成 → {parts_p} 用时 {time.perf_counter() - t0:.0f}s')


def cmd_final_aggregate(args):
    import eval_factor as ef
    parts_p = os.path.join(CACHE_DIR, 'final_parts.jsonl')
    cells = {}
    with open(parts_p, encoding='utf-8') as fh:
        for line in fh:
            p = json.loads(line)
            cells.setdefault((p['factor'], p['sign'], p['exit'], p['leak']), []).append(p)
    rows = []
    for (nm, sg, ex, lk), ps in sorted(cells.items()):
        r = ef._aggregate(nm, sg, ex, ps)
        if r:
            r['leak'] = lk
            r['n_codes'] = len(ps)
            rows.append(r)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    p = os.path.join(RESULTS_DIR, 'seed_eval_final.json')
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump({'oos_start': ef.OOS_START, 'win': [ef.WIN_START, ef.WIN_END],
                   'z_thr': ef.Z_THR, 'mag_filter': ef.MAG_FILTER,
                   'cost': '双边0.136%（rx._leg_pnl FEE_S+FEE_B=0.00136）',
                   'cells': rows}, fh, ensure_ascii=False, indent=1)
    print(f"{'factor':8}{'sign':>5}{'exit':>7}{'leak':>5}{'n':>7}{'净均%':>9}{'胜率':>7}"
          f"{'密度':>7}{'随机%':>9}{'Δpp':>8}{'OOSn':>7}{'OOS均':>9}  判定")
    for r in rows:
        print(f"{r['factor']:8}{r['sign']:>5}{r['exit']:>7}{r['leak']:>5}{r['n']:>7}"
              f"{r['net_mean']:>9.3f}{r['win']:>7.3f}{r['density']:>7.3f}"
              f"{r['rand_mean']:>9.3f}{r['delta_vs_random']:>8.3f}{r['oos_n']:>7}"
              f"{(r['oos_mean'] if r['oos_mean'] is not None else float('nan')):>9.3f}"
              f"  {r['verdict']}")
    print(f'[final-aggregate] {len(rows)} 格 → {p}')


# ────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('stage', choices=['ic', 'aggregate', 'mc', 'final',
                                      'final-aggregate'])
    ap.add_argument('--codes', default=None)
    ap.add_argument('--factors', default=None)
    ap.add_argument('--exits', default='hold,native')
    ap.add_argument('--leak', type=int, default=0)
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--n', type=int, default=MC_N)
    ap.add_argument('--seed', type=int, default=MC_SEED)
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()
    if args.stage == 'ic':
        assert args.codes, 'ic 需要 --codes'
        cmd_ic(args)
    elif args.stage == 'aggregate':
        cmd_aggregate(args)
    elif args.stage == 'mc':
        assert args.factors, 'mc 需要 --factors'
        cmd_mc(args)
    elif args.stage == 'final':
        assert args.codes and args.factors, 'final 需要 --codes 与 --factors'
        cmd_final(args)
    else:
        cmd_final_aggregate(args)


if __name__ == '__main__':
    main()
