# -*- coding: utf-8 -*-
"""因子裁决器：因子 → 交易 → 预注册判定。

## 固定规则（**不参与搜索**，见 plan）

- 入场：因子值做**同日历史 z-score**（过去 14 日同一 HH:MM 的均值/σ），
  `sign * z > +1` → 正T（买现金）；`sign * z < −1` → 倒T（卖底仓）。**次根 bar 开盘成交。**
- 出场：两臂固定 —— `hold`（持到 14:55）/ `native`（回升至 VWAP 或 ±1.0%）
- 波动率门槛、成本、随机基线：复用 `t0_schemes/run_experiment.py`（口径与本会话全部实验一致）

## 预注册硬约束（编码在本文件）

- **量级预滤**：`|费后净均 − 随机基线| < 0.2%` → 直接标 `DEAD`，不进候选池
- **多重比较**：每个因子**双向都测**（`sign=+1/−1`），**两个方向都计入检验次数**
  → 避免"事后挑符号"这种最隐蔽的过拟合
- OOS：2026-06-01 ~ 2026-08-26

用法：
  python eval_factor.py --baseline            # 全部基线因子（双向）× 两出场臂
  python eval_factor.py --factor vwap_dev     # 单个因子
  python eval_factor.py --baseline --leak 5   # 前视自检：因子整体后移 5 根
"""
import argparse
import importlib.util
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
_T0 = os.path.join(ROOT, 't_io', 'validation', 't0_schemes')
_MD = os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')
for _p in (ROOT, _T0, _MD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_experiment_v2 as v2  # noqa: E402
import factor_ops as fo  # noqa: E402
import factor_lib as fl  # noqa: E402

# 复用实验台（按路径加载：`import run_experiment` 会撞上 macd_divergence_t 下的同名文件）
_spec = importlib.util.spec_from_file_location('t0_rx', os.path.join(_T0, 'run_experiment.py'))
rx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rx)

WIN_START, WIN_END = '2025-09-14', '2026-08-26'
OOS_START = '2026-06-01'
Z_THR = 1.0
MAG_FILTER = 0.002            # 0.2%：|净均 − 随机| 低于此 → DEAD
N_MC = 50
MIN_HIST_DAYS = 5
MAX_ENTRIES = 3               # 每票每日最多 3 次（同生产 config.max_buy_times_per_stock）


# ── 面板与因子值 ─────────────────────────────────────────────────────────
def load_code(code):
    dates, merged, _src = v2.merge_days(code)
    dates = [d for d in dates if WIN_START <= d <= WIN_END]
    if len(dates) < 30:
        return None
    st = rx.daily_stats(merged, dates)
    days, labels = {}, {}
    for d in dates:
        day = merged[d]
        if len(day) < rx.MIN_1M:
            continue
        s = st[d]
        days[d] = fo.day_ctx(day, s['prev_close'], s['atr'])
        labels[d] = days[d]['t']
    return {'dates': [d for d in dates if d in days], 'days': days, 'labels': labels}


def compute_factor(panel, fname, leak=0):
    """→ {date: np.ndarray}；leak>0 时整体后移（前视自检用）。"""
    fn = fl.FACTORS.get(fname) or fl.CONTROL[fname]     # CONTROL = 阳性对照（zz_oracle）
    out = {}
    for d in panel['dates']:
        ctx = panel['days'][d]
        # tod_bias 需要跨日同刻历史
        if fname == 'tod_bias':
            ctx = dict(ctx, hist_tod=_hist_same_minute(panel, d))
        try:
            v = fn(ctx)
        except Exception:
            v = np.full(ctx['n'], np.nan)
        v = np.asarray(v, float)
        if leak:
            w = np.full_like(v, np.nan)
            if leak < len(v):
                w[leak:] = v[:len(v) - leak]
            v = w
        out[d] = v
    return out


def _hist_same_minute(panel, day, look=14):
    ds = panel['dates']
    k = ds.index(day)
    acc = {}
    for pd_ in ds[max(0, k - look):k]:
        for t, c in zip(panel['labels'][pd_], panel['days'][pd_]['c']):
            acc.setdefault(t, []).append(c)
    return {t: float(np.mean(v)) for t, v in acc.items() if len(v) >= 3}


def zscores(fvals, panel, look=14):
    """同日历史 z-score（过去 look 日的同一 HH:MM）。严格因果：只用严格更早的日。"""
    ds, labels = panel['dates'], panel['labels']
    out = {}
    for k, d in enumerate(ds):
        prior = ds[max(0, k - look):k]
        arr = fvals.get(d)
        if arr is None or len(prior) < MIN_HIST_DAYS:
            out[d] = None
            continue
        acc = {}
        for pd_ in prior:
            a = fvals.get(pd_)
            if a is None:
                continue
            for t, v in zip(labels[pd_], a):
                if np.isfinite(v):
                    acc.setdefault(t, []).append(v)
        stat = {t: (float(np.mean(v)), float(np.std(v, ddof=1)))
                for t, v in acc.items() if len(v) >= 3}
        z = np.full(len(arr), np.nan)
        for i, t in enumerate(labels[d]):
            if t in stat and stat[t][1] > 1e-12 and np.isfinite(arr[i]):
                z[i] = (arr[i] - stat[t][0]) / stat[t][1]
        out[d] = z
    return out


# ── 交易模拟（出场固定）──────────────────────────────────────────────────
def _exit_hold(day, ei, fill, direction, j_f):
    return float(day['c'][j_f]), 'hold1455'


def _exit_native(day, ei, fill, direction, j_f):
    """回升至当日 VWAP 或 ±1.0%（与 run_experiment.B9_native 同口径）。"""
    tgt = fill * (1 + 0.010) if direction == 'long' else fill * (1 - 0.010)
    h, l, c, w = day['h'], day['l'], day['c'], day['vwap']
    for j in range(ei + 1, j_f + 1):
        if direction == 'long':
            if h[j] >= tgt:
                return tgt, 'tp'
            if np.isfinite(w[j]) and c[j] >= w[j]:
                return float(c[j]), 'vwap'
        else:
            if l[j] <= tgt:
                return tgt, 'tp'
            if np.isfinite(w[j]) and c[j] <= w[j]:
                return float(c[j]), 'vwap'
    return float(c[j_f]), 'force1455'


def run_entries(panel, zmap, sign, exit_name, code):
    """按固定规则生成腿并算费后净收益（%）+ 分层标签。"""
    ds = panel['dates']
    out = []
    for d in ds:
        z = zmap.get(d)
        if z is None:
            continue
        ctx = panel['days'][d]
        o = ctx['o']
        j_f = rx.force_idx(ctx['t'])
        dt = rx.day_type(ctx['o'], ctx['h'], ctx['l'], ctx['c'], ctx['prev_close'])
        sig = []
        for i in range(len(z) - 1):
            if not np.isfinite(z[i]):
                continue
            s = sign * z[i]
            s_prev = sign * z[i - 1] if (i > 0 and np.isfinite(z[i - 1])) else np.nan
            # **穿越触发**（不是"超阈即发"）：否则因子持续超阈会每根 bar 发一次，
            # 实测会产生 81 次/票/日的荒谬密度（生产引擎约 3 次/日）。
            if not np.isfinite(s_prev):
                continue                  # 前值未知 ⇒ 无法判定"穿越"，不发信号（与快路径一致）
            if s_prev <= Z_THR < s:
                sig.append((i + 1, 'long'))
            elif s_prev >= -Z_THR > s:
                sig.append((i + 1, 'short'))
        sig = [(b, dr) for b, dr in sig if 1 <= b <= j_f and o[b] > 0]
        # 每日上限：与项目生产口径一致（config: max_buy_times_per_stock=3）。
        # 高频因子穿越频繁（rev1 实测 59 次/票/日），不设上限就不是"做T"而是高频刷单，
        # 且与 T+1 / 仓位约束不符。取当日**最早**的 MAX_ENTRIES 次穿越。
        sig = sig[:MAX_ENTRIES]
        for k, (ei, direction) in enumerate(sig):
            nxt = sig[k + 1][0] if k + 1 < len(sig) else None
            hi = j_f if (nxt is None or nxt > j_f) else nxt - 1
            if hi < ei:
                continue
            fill = float(o[ei])
            if exit_name == 'hold':
                ex_px, reason = _exit_hold(ctx, ei, fill, direction, j_f)
            else:
                ex_px, reason = _exit_native(ctx, ei, fill, direction, j_f)
            out.append({'code': code, 'date': d, 'bar': ei, 'dir': direction,
                        'net': rx._leg_pnl(direction, fill, ex_px), 'day_type': dt,
                        'reason': reason})
    return out


def random_baseline(panel, legs, exit_name, code, k=20, seed=20260917):
    """**逐腿配对的**随机基线：对每一条真实腿，在同一 (code, 日期, **方向**) 上随机换个入场 bar。

    ⚠️ 2026-09-17 修正：旧实现按 (票,日) 从当天方向集合里随机抽方向，导致空值的**多空构成**
    与因子只是"比例相同"而非**逐笔相同**；实测镜像格之间随机基线摆动 1.6pp，
    使 Δ 在度量"多空/漂移暴露"而不是择时能力（`smart_dev` 双向 Δ=±0.701 精确反对称即其签名）。
    逐腿配对后，空值的多空构成与因子**逐笔一致**，残余 Δ 只可能来自**择时**。
    """
    r = np.random.RandomState(seed)
    out = {'long': [], 'short': []}
    by_date = {}
    for x in legs:
        by_date.setdefault(x['date'], []).append(x)
    for d, xs in by_date.items():
        ctx = panel['days'][d]
        o = ctx['o']
        j_f = rx.force_idx(ctx['t'])
        for x in xs:
            direction = x['dir']
            for _ in range(k):
                b = int(r.randint(1, max(j_f, 1)))
                if o[b] <= 0:
                    continue
                fill = float(o[b])
                if exit_name == 'hold':
                    ex_px, _w = _exit_hold(ctx, b, fill, direction, j_f)
                else:
                    ex_px, _w = _exit_native(ctx, b, fill, direction, j_f)
                out[direction].append(rx._leg_pnl(direction, fill, ex_px))
    return out


# ── 主流程 ───────────────────────────────────────────────────────────────
def eval_one(panel_by_code, fname, sign, exit_name, leak=0):
    legs, rand = [], []
    for code, panel in panel_by_code.items():
        fv = compute_factor(panel, fname, leak=leak)
        zm = zscores(fv, panel)
        ls = run_entries(panel, zm, sign, exit_name, code)
        legs.extend(ls)
        dirs_by_day = {}
        for x in ls:
            dirs_by_day.setdefault(x['date'], []).append(x['dir'])
        rand.extend(random_baseline(panel, dirs_by_day, exit_name, code))
    if not legs:
        return None
    net = np.array([x['net'] for x in legs], float)
    rn = np.array(rand, float) if rand else np.array([])
    rmean = float(rn.mean()) if len(rn) else 0.0
    n_gated = sum(len(p['dates']) for p in panel_by_code.values())
    oos = np.array([x['net'] for x in legs if x['date'] >= OOS_START], float)
    return {
        'factor': fname, 'sign': sign, 'exit': exit_name,
        'n': int(len(net)), 'net_mean': round(float(net.mean()), 4),
        'net_median': round(float(np.median(net)), 4),
        'win': round(float((net > 0).mean()), 4),
        'density': round(len(net) / max(n_gated, 1), 3),
        'rand_mean': round(rmean, 4),
        'delta_vs_random': round(float(net.mean() - rmean), 4),
        'by_day_type': {t: round(float(np.mean([x['net'] for x in legs if x['day_type'] == t])), 4)
                        for t in ('up', 'range', 'down')
                        if any(x['day_type'] == t for x in legs)},
        'oos_n': int(len(oos)), 'oos_mean': round(float(oos.mean()), 4) if len(oos) else None,
        'verdict': ('DEAD(magnitude)' if abs(net.mean() - rmean) < MAG_FILTER * 100 else 'KEEP'),
    }


def _cell_worker(pack):
    """子进程入口（Windows spawn 需模块级）。worker 自己加载面板，避免父进程 pickle 大对象。"""
    code, fname, sign, exit_name, leak = pack
    v2.END = WIN_END          # ⚠️ worker 是独立进程，父进程设的 v2.END 传不过来，必须在此重设
    try:
        panel = load_code(code)
        if not panel:
            return None
        fv = compute_factor(panel, fname, leak=leak)
        if exit_name == 'hold':                       # 快路径（已与慢路径交叉验证 |Δ|=0）
            keep, labels, L, _nd = aligned_matrix(panel)
            legs = legs_hold_fast(panel, fv, sign, keep, labels, L) if keep else []
            for x in legs:
                x['code'] = code
                ctx = panel['days'][x['date']]
                x['day_type'] = rx.day_type(ctx['o'], ctx['h'], ctx['l'], ctx['c'],
                                            ctx['prev_close'])
        else:
            zm = zscores(fv, panel)
            legs = run_entries(panel, zm, sign, exit_name, code)
        rnd = random_baseline(panel, legs, exit_name, code)

        return {'legs': legs, 'rand': rnd, 'n_days': len(panel['dates'])}
    except Exception as e:
        print(f'  [warn] {code} {fname} sign={sign} {exit_name}: {type(e).__name__} {e}')
        return None


def aligned_matrix(panel):
    """把一天一个数组摊成 [n_days, n_bars] 矩阵。

    各交易日 bar 数基本一致（剔除异常日）；标签一致性强校验，不一致的日直接剔除。
    这是**向量化提速的前提**：14 日同刻 z-score 于是变成沿轴 0 的滑动统计。
    """
    import collections as _c
    lens = [len(panel['labels'][d]) for d in panel['dates']]
    L = _c.Counter(lens).most_common(1)[0][0]
    keep = [d for d, n in zip(panel['dates'], lens) if n == L]
    if not keep:
        return [], None, None, 0
    labels = panel['labels'][keep[0]]
    bad = {d for d in keep if panel['labels'][d] != labels}
    keep = [d for d in keep if d not in bad]
    if not keep:
        return [], None, None, 0
    return keep, labels, L, len(keep)


def zscore_matrix(M, look=14, min_hist=MIN_HIST_DAYS):
    """[n_days, n_bars] 上做**过去 look 日同刻**的 z-score（严格因果：不含当日）。"""
    n = M.shape[0]
    Z = np.full_like(M, np.nan)
    for k in range(n):
        a, b = max(0, k - look), k
        if b - a < min_hist:
            continue
        w = M[a:b]
        with np.errstate(invalid='ignore'):
            m = np.nanmean(w, axis=0)
            s = np.nanstd(w, axis=0, ddof=1)
        ok = np.isfinite(m) & np.isfinite(s) & (s > 1e-12) & np.isfinite(M[k])
        Z[k][ok] = (M[k][ok] - m[ok]) / s[ok]
    return Z


def legs_hold_matrix(panel, M, sign, keep, labels, L):
    """核心：给定 [n_days, n_bars] 因子矩阵，向量化产出 `hold` 臂腿清单。"""
    Z = zscore_matrix(M)
    S = sign * Z
    O = np.array([panel['days'][d]['o'] for d in keep], float)
    C = np.array([panel['days'][d]['c'] for d in keep], float)
    JF = np.array([rx.force_idx(panel['days'][d]['t']) for d in keep], int)
    nets = []
    long_m = (S[:, 1:] > Z_THR) & (S[:, :-1] <= Z_THR)
    short_m = (S[:, 1:] < -Z_THR) & (S[:, :-1] >= -Z_THR)
    legs = []
    for r in range(len(keep)):
        bars = []
        # ⚠️ off-by-one：掩码下标 j 已是「前一根」（比较 S[j] 与 S[j+1]），
        # 穿越发生在 bar j+1，故成交在 j+2（与 run_entries 的 i→i+1 同义）。
        for off, is_long in ((np.where(long_m[r])[0], True), (np.where(short_m[r])[0], False)):
            for j in off:
                bars.append((j + 2, 'long' if is_long else 'short'))
        bars.sort()
        bars = [(b, dr) for b, dr in bars if 1 <= b <= JF[r] and O[r][b] > 0][:MAX_ENTRIES]
        for ei, dr in bars:
            net = rx._leg_pnl(dr, float(O[r][ei]), float(C[r][JF[r]]))
            legs.append({'code': None, 'date': keep[r], 'bar': ei, 'dir': dr, 'net': net,
                         'day_type': None, 'reason': 'hold1455'})
            nets.append(net)
    if not nets:
        return []
    return legs


def legs_hold_fast(panel, fvals, sign, keep, labels, L):
    """`legs_hold_matrix` 的 dict 入参包装（fvals = {date: array}）。"""
    M = np.full((len(keep), L), np.nan)
    for r, d in enumerate(keep):
        a = fvals.get(d)
        if a is not None and len(a) == L:
            M[r] = a
    return legs_hold_matrix(panel, M, sign, keep, labels, L)


def score_hold_fast(panel, fvals, sign, keep, labels, L):
    """→ (n, net_mean)，供 GP 适应度用。"""
    legs = legs_hold_fast(panel, fvals, sign, keep, labels, L)
    if not legs:
        return 0, 0.0
    return len(legs), float(np.mean([x['net'] for x in legs]))


def _aggregate(fname, sign, exit_name, parts):
    legs, rnd = [], {'long': [], 'short': []}
    n_gated = 0
    for p in parts:
        if not p:
            continue
        legs.extend(p['legs'])
        rnd['long'].extend(p['rand']['long'])
        rnd['short'].extend(p['rand']['short'])
        n_gated += p['n_days']
    if not legs:
        return None
    net = np.array([x['net'] for x in legs], float)
    allr = np.array(rnd['long'] + rnd['short'], float) if (rnd['long'] or rnd['short']) else np.array([])
    rmean = float(allr.mean()) if len(allr) else 0.0
    oos = np.array([x['net'] for x in legs if x['date'] >= OOS_START], float)
    d_dir = {}
    for dd in ('long', 'short'):
        fl = np.array([x['net'] for x in legs if x['dir'] == dd], float)
        nl = np.array(rnd[dd], float) if rnd[dd] else np.array([])
        if len(fl) and len(nl):
            d_dir[dd] = {'n': int(len(fl)), 'factor': round(float(fl.mean()), 4),
                         'rand': round(float(nl.mean()), 4),
                         'delta': round(float(fl.mean() - nl.mean()), 4)}
    return {
        'factor': fname, 'sign': sign, 'exit': exit_name,
        'n': int(len(net)), 'net_mean': round(float(net.mean()), 4),
        'net_median': round(float(np.median(net)), 4),
        'win': round(float((net > 0).mean()), 4),
        'density': round(len(net) / max(n_gated, 1), 3),
        'rand_mean': round(rmean, 4),
        'delta_vs_random': round(float(net.mean() - rmean), 4),
        'delta_by_dir': d_dir,
        'by_day_type': {t: round(float(np.mean([x['net'] for x in legs if x['day_type'] == t])), 4)
                        for t in ('up', 'range', 'down')
                        if any(x['day_type'] == t for x in legs)},
        'oos_n': int(len(oos)), 'oos_mean': round(float(oos.mean()), 4) if len(oos) else None,
        'verdict': ('DEAD(magnitude)' if abs(float(net.mean()) - rmean) < MAG_FILTER * 100 else 'KEEP'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--factors', default=None, help='逗号分隔；缺省=全部基线')
    ap.add_argument('--codes', default=None)
    ap.add_argument('--exits', default='hold,native')
    ap.add_argument('--leak', type=int, default=0, help='前视自检：因子整体后移 N 根')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--out', default=os.path.join(HERE, 'ledger_baseline.jsonl'))
    args = ap.parse_args()

    import glob as _glob
    import concurrent.futures as cf
    names = args.factors.split(',') if args.factors else list(fl.FACTORS)
    codes = args.codes.split(',') if args.codes else sorted(
        {os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
         for f in _glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})
    v2.END = WIN_END
    t0 = time.time()
    print(f'[eval] codes={len(codes)}  因子={len(names)}  出场={args.exits}  leak={args.leak} '
          f'workers={args.workers}')
    rows = []
    cells = [(fn, sg, ex) for fn in names for sg in (1, -1) for ex in args.exits.split(',')]
    with open(args.out, 'a', encoding='utf-8') as fh, \
            cf.ProcessPoolExecutor(max_workers=args.workers) as pool:
        for ci, (fn, sg, ex) in enumerate(cells):
            packs = [(c, fn, sg, ex, args.leak) for c in codes]
            parts = list(pool.map(_cell_worker, packs))
            r = _aggregate(fn, sg, ex, parts)
            if r:
                rows.append(r)
                fh.write(json.dumps(r, ensure_ascii=False) + '\n')
            if ci % 10 == 0:
                print(f'  [{ci + 1}/{len(cells)}] {fn} sign={sg} {ex} 用时 {time.time() - t0:.0f}s',
                      flush=True)
    rows.sort(key=lambda r: -r['delta_vs_random'])
    print(f"\n{'factor':14}{'sign':>5}{'exit':>7}{'n':>7}{'净均%':>9}{'中位%':>9}{'胜率':>7}"
          f"{'密度':>7}{'随机%':>9}{'Δpp':>8}{'Δlong':>8}{'Δshort':>8}  判定")
    for r in rows:
        dd = r.get('delta_by_dir') or {}
        dl = dd.get('long', {}).get('delta')
        ds = dd.get('short', {}).get('delta')
        print(f"{r['factor']:14}{r['sign']:>5}{r['exit']:>7}{r['n']:>7}{r['net_mean']:>9.3f}"
              f"{r['net_median']:>9.3f}{r['win']:>7.3f}{r['density']:>7.3f}"
              f"{r['rand_mean']:>9.3f}{r['delta_vs_random']:>8.3f}"
              f"{(dl if dl is not None else 0):>8.3f}{(ds if ds is not None else 0):>8.3f}"
              f"  {r['verdict']}")
    keep = [r for r in rows if r['verdict'].startswith('KEEP')]
    print(f"\n[eval] 候选 {len(rows)} 条（含双向），过 0.2% 量级预滤的 {len(keep)} 条；"
          f"用时 {time.time() - t0:.0f}s → {args.out}")
    if rows:
        print(f"[eval] ⚠️ 多重比较：本轮共检验 {len(rows)} 次，"
              f"Bonferroni 阈值 ≈ {0.05 / max(len(rows), 1):.2e}（正式用 BH-FDR）")


if __name__ == '__main__':
    main()
