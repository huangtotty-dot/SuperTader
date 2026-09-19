# -*- coding: utf-8 -*-
"""任务 S2-5：震荡末期 → 波动扩张传导验证（「末期低吸」逻辑判生判死）。

## 预注册口径（写死，2026-09-19，施工前冻结，不参与事后修改）

**事件定义**：进入「震荡末期」= regime 打标器（regime_factors.label_regimes，只读复用）
标签为「震荡末期」（adx14 < 20 且 bbw_pct120 ≤ 0.20，打标优先级排除单边上涨/下跌）
的**首个交易日**（前一交易日标签 ≠ 震荡末期，含从历史不足 NaN 转入）。

**振幅口径**：amp(t) = (high(t) - low(t)) / close(t)，前复权日线。

**H1（自身前后对照）**：事件后 N 日平均振幅 mean(amp[t+1..t+N])（N=3,5,10）
显著高于事件前 20 日基线 mean(amp[t-20..t-1])。
- 窗口完整性：rolling(min_periods=全长)，窗口内任一值缺失则该事件该 N 弃用（NaN 不硬凑）。
- 检验：① 混合样本 Mann-Whitney U（单侧 greater，手写渐近，无 scipy）；
  ② 事件日聚合（每日事件均值，防伪复制）MW；③ 日期内洗牌 MC（n=100，seed=20260919）：
  零假设 = 「同一日期内随机选同样数量的股票日，其后前振幅差与事件无差异」，
  每日候选池上限 500 行等概抽样（与 regime_factors.scenario_value 同款方法）。
- H1 成立闸门（主口径 N=5）：日聚合差 > 0 且 日聚合 MW p < 0.05 且 MC p < 0.05。

**H2（组间对照）**：事件后 N 日平均振幅显著高于「非末期震荡日」对照组
（regime =「震荡」，即 adx14<20 但带宽未收缩到极致的普通震荡日）的**次日 N 日前向振幅**。
- 检验同 H1 三条腿；MC 为事件/对照标签在日期内对换洗牌（保每日计数）。
- H2 成立闸门（主口径 N=5）：日聚合差 > 0 且 MW p < 0.05 且 MC p < 0.05。

**分层**（仅描述统计 + MW，不做 MC）：
- 板块：代码前缀 60/00/30/68（其余归「其他」）；
- 真突破：事件后 10 个交易日内 adx14 上穿 25（adx>25 且前一日 ≤25）→ brk10=1。

**诚实声明（参数敏感性）**：事件定义依赖 ADX/BBW 参数。九宫格复测 H1（N=5）：
ADX 阈值 ∈ {18, 20, 22} × BBW 分位 ∈ {0.15, 0.20, 0.25}。
注：ADX 阈值 ≤ 22 < 25（单边门槛），故敏感性下「状态条件首入日」与打标器口径严格一致
（打标优先级中的单边上涨/下跌要求 adx14>25，与 adx14<22 互斥），无需重跑打标器。

**窗口**：全历史（面板 1996 起，实际覆盖以数据为准）+ 近 3 年（2023-09-01 起，事件日口径；
pre/post 窗口由全历史逐股序列计算后再按事件日过滤，基线可延伸到窗口外——标准事件研究口径）。

**判定规则（预注册）**：
- H1 且 H2 均成立（全历史主口径）→「末期低吸」波动扩张前提**证实**；
- H1 不成立（扩张 ≤0 或不显著）→ **证伪**；
- 其余（H1 成立 H2 不成立 / 窗口间不一致 / 敏感性九宫格不稳定）→ **部分成立**。

## 数据与复用纪律

只读复用：`regime_factor_panel_prevadj.parquet`（前复权因子面板，P1-4 复跑产物，
含 regime 标签与 adx14/bbw_pct120/n_hist）+ regime_factors 的常数与 MW 原语。
不改 regime_factors.py / ic_layer.py / 原始面板。

## 用法（分步，每步 checkpoint，可断点续跑）

```bash
python regime_expansion.py prep                 # 一次性窗口/事件预处理 → prep parquet 缓存
python regime_expansion.py core --window full   # H1+H2 全历史（逐 N 落盘）
python regime_expansion.py core --window recent # 近 3 年
python regime_expansion.py stratify             # 板块 + 真突破分层（两窗口）
python regime_expansion.py sensitivity          # 参数九宫格（全历史，H1 N=5）
python regime_expansion.py report               # 汇总 → regime_expansion_results.json
```
"""
import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from regime_factors import (  # 只读复用：常数 + MW 原语 + regime 名
    _mannwhitney_greater, ADX_RANGE, ADX_TREND, BBW_SQUEEZE_Q, MIN_BARS_LABEL,
    REGIME_SQUEEZE, REGIME_RANGE,
)

FP_PREVADJ = os.path.join(HERE, 'regime_factor_panel_prevadj.parquet')
PREP_CACHE = os.path.join(HERE, 'regime_expansion_prep_prevadj.parquet')

# ── 预注册参数（写死）────────────────────────────────────────────────────────
PRE_N = 20                    # 事件前基线窗口
FWD_NS = (3, 5, 10)           # 事件后振幅窗口
PRIMARY_N = 5                 # 主口径
BRK_WIN = 10                  # 真突破观察窗（事件后 10 日 ADX 上穿 25）
MC_PERM = 100
MC_SEED = 20260919
DAY_CAP = 500                 # 每日 MC 候选池上限（同 scenario_value）
RECENT_START = '2023-09-01'
ADX_GRID = (18.0, 20.0, 22.0)
BBW_GRID = (0.15, 0.20, 0.25)

LOAD_COLS = ['symbol', 'eob', 'high', 'low', 'close', 'adx14', 'bbw_pct120',
             'n_hist', 'regime']
BOARD_MAP = {'60': '沪主板60', '00': '深主板00', '30': '创业板30', '68': '科创板68'}


# ════════════════════════════════════════════════════════════════════════════
# 预处理：窗口 + 事件（全历史一次，缓存 parquet）
# ════════════════════════════════════════════════════════════════════════════
def compute_windows(g: pd.DataFrame) -> pd.DataFrame:
    """单 symbol（按 eob 升序）→ amp / pre20 / postN / brk10。严格因果对齐：
    pre20 = mean(amp[t-20..t-1])，postN = mean(amp[t+1..t+N])（rolling+shift 实现），
    窗口不完整 → NaN。brk10 = 事件后 10 日内 ADX 上穿 25（该列在所有行有定义，
    只在事件行被消费）。
    """
    amp = (g['high'] - g['low']) / g['close'].replace(0, np.nan)
    out = pd.DataFrame({'amp': amp.astype(np.float32)}, index=g.index)
    out['pre20'] = amp.rolling(PRE_N, min_periods=PRE_N).mean().shift(1)
    for n in FWD_NS:
        out[f'post{n}'] = amp.rolling(n, min_periods=n).mean().shift(-n)
    adx = g['adx14']
    cross = ((adx > ADX_TREND) & (adx.shift(1) <= ADX_TREND)).astype(float)
    # rolling(BRK_WIN).max().shift(-BRK_WIN)：t 行 = max(cross[t+1..t+10])
    out['brk10'] = cross.rolling(BRK_WIN, min_periods=1).max().shift(-BRK_WIN)
    return out.astype(np.float32)


def mark_events(df: pd.DataFrame, adx_thr: float = ADX_RANGE,
                bbw_q: float = BBW_SQUEEZE_Q, use_label: bool = True) -> pd.Series:
    """事件 = 进入震荡末期状态的首个交易日（逐 symbol 边沿检测）。

    use_label=True：直接用打标器 regime 标签（主口径，要求 df 含 regime 列）。
    use_label=False：状态条件重算（敏感性网格用；阈值 ≤22<25 时与打标器口径严格一致）。
    """
    if use_label:
        in_state = (df['regime'] == REGIME_SQUEEZE).fillna(False)
    else:
        in_state = ((df['n_hist'] >= MIN_BARS_LABEL)
                    & (df['adx14'] < adx_thr)
                    & (df['bbw_pct120'] <= bbw_q)).fillna(False)
    prev = (in_state.groupby(df['symbol'], sort=False).shift(1)
            .fillna(False).astype(bool))   # shift 引入 NaN 会降级成 object，~object 恒真，必须 astype(bool)
    return in_state.astype(bool) & ~prev


def board_of(symbol: pd.Series) -> pd.Series:
    code = symbol.astype(str).str.split('.').str[-1]
    return code.str[:2].map(BOARD_MAP).fillna('其他')


def cmd_prep(args):
    t0 = time.time()
    print('[prep] 读取前复权因子面板（列裁剪）...', flush=True)
    d = pd.read_parquet(FP_PREVADJ, columns=LOAD_COLS)
    d['symbol'] = d['symbol'].astype('category')
    for c in ('high', 'low', 'close', 'adx14', 'bbw_pct120'):
        d[c] = d[c].astype(np.float32)
    d = d.sort_values(['symbol', 'eob'], kind='mergesort').reset_index(drop=True)
    print(f'[prep] {len(d):,} 行 × {d["symbol"].nunique()} 只，计算窗口...', flush=True)
    win = d.groupby('symbol', sort=False, group_keys=False).apply(compute_windows)
    d = pd.concat([d, win], axis=1)
    d['event'] = mark_events(d, use_label=True)
    d['board'] = board_of(d['symbol']).astype('category')
    keep = (['symbol', 'eob', 'regime', 'adx14', 'bbw_pct120', 'n_hist', 'board',
             'event', 'amp', 'pre20', 'brk10'] + [f'post{n}' for n in FWD_NS])
    d = d[keep]
    d.to_parquet(PREP_CACHE, index=False)
    n_ev = int(d['event'].sum())
    print(f'[prep] 完成 ({time.time() - t0:.0f}s)：事件数={n_ev:,} → {PREP_CACHE}', flush=True)


def _load_prep() -> pd.DataFrame:
    if not os.path.exists(PREP_CACHE):
        raise FileNotFoundError(f'先跑 prep（缺 {PREP_CACHE}）')
    return pd.read_parquet(PREP_CACHE)


# ════════════════════════════════════════════════════════════════════════════
# 统计原语：日期内洗牌 MC（与 regime_factors.scenario_value 同款向量化）
# ════════════════════════════════════════════════════════════════════════════
def _cap_per_day(idx_gid: np.ndarray, n_rows: int, cap: int, seed: int) -> np.ndarray:
    """每日等概抽样至 cap 行（保持原行序的布尔掩码）。"""
    rng = np.random.default_rng(seed)
    keys = rng.random(n_rows)
    order = np.lexsort((keys, idx_gid))
    gid_s = idx_gid[order]
    starts = np.r_[True, gid_s[1:] != gid_s[:-1]]
    grp = np.maximum.accumulate(np.where(starts, np.arange(n_rows), 0))
    keep_sorted = (np.arange(n_rows) - grp) < cap
    keep = np.zeros(n_rows, dtype=bool)
    keep[order] = keep_sorted
    return keep


def _mc_self_contrast(diff: np.ndarray, gid: np.ndarray, k_evt: np.ndarray,
                      obs: float, n_perm: int, seed: int) -> float:
    """H1 MC：每日候选池内随机抽 k_evt 行作「伪事件」，比较日聚合差。

    diff：候选池每行的 post-pre 差；gid：日期组号；k_evt：每日真实事件数。
    零假设：同一日期内随机择股择日，其后前扩张不差于真实事件。
    """
    n_days = int(gid.max()) + 1
    n_day = np.bincount(gid, minlength=n_days)
    valid = (k_evt > 0) & (k_evt <= n_day)
    rng = np.random.default_rng(seed)
    cnt = 0
    for p in range(n_perm):
        keys = rng.random(len(diff))
        order = np.lexsort((keys, gid))
        gid_s = gid[order]
        starts = np.r_[True, gid_s[1:] != gid_s[:-1]]
        grp = np.maximum.accumulate(np.where(starts, np.arange(len(diff)), 0))
        sel_sorted = (np.arange(len(diff)) - grp) < k_evt[gid_s]
        sel = np.empty(len(diff), dtype=bool)
        sel[order] = sel_sorted
        s1 = np.bincount(gid, weights=np.where(sel, diff, 0.0), minlength=n_days)
        with np.errstate(invalid='ignore', divide='ignore'):
            dm = s1 / k_evt
        if np.nanmean(dm[valid]) >= obs:
            cnt += 1
        if (p + 1) % 25 == 0:
            print(f'    [H1-MC] {p + 1}/{n_perm}', flush=True)
    return (cnt + 1) / (n_perm + 1)


def _mc_group_contrast(amp: np.ndarray, gid: np.ndarray, k_evt: np.ndarray,
                       obs: float, n_perm: int, seed: int) -> float:
    """H2 MC：每日「事件/对照」标签对换洗牌（保每日事件数），比较日聚合组差。"""
    n_days = int(gid.max()) + 1
    n_day = np.bincount(gid, minlength=n_days)
    tot = np.bincount(gid, weights=amp, minlength=n_days)
    valid = (k_evt > 0) & (k_evt < n_day)
    rng = np.random.default_rng(seed)
    cnt = 0
    for p in range(n_perm):
        keys = rng.random(len(amp))
        order = np.lexsort((keys, gid))
        gid_s = gid[order]
        starts = np.r_[True, gid_s[1:] != gid_s[:-1]]
        grp = np.maximum.accumulate(np.where(starts, np.arange(len(amp)), 0))
        sel_sorted = (np.arange(len(amp)) - grp) < k_evt[gid_s]
        sel = np.empty(len(amp), dtype=bool)
        sel[order] = sel_sorted
        s1 = np.bincount(gid, weights=np.where(sel, amp, 0.0), minlength=n_days)
        with np.errstate(invalid='ignore', divide='ignore'):
            dm = s1 / k_evt - (tot - s1) / (n_day - k_evt)
        if np.nanmean(dm[valid]) >= obs:
            cnt += 1
        if (p + 1) % 25 == 0:
            print(f'    [H2-MC] {p + 1}/{n_perm}', flush=True)
    return (cnt + 1) / (n_perm + 1)


# ════════════════════════════════════════════════════════════════════════════
# H1 / H2 检验
# ════════════════════════════════════════════════════════════════════════════
def h1_test(d: pd.DataFrame, n: int, n_perm: int = MC_PERM, seed: int = MC_SEED) -> dict:
    """H1：事件后 N 日振幅 vs 事件前 20 日基线（自身前后对照）。"""
    col = f'post{n}'
    e = d[d['event'] & d['pre20'].notna() & d[col].notna()]
    if len(e) == 0:
        return {'n_events': 0, 'pass': False, 'note': '无可用事件（窗口不完整或样本为空）'}
    post, pre = e[col].to_numpy(float), e['pre20'].to_numpy(float)
    diff = post - pre
    # 事件日聚合（防伪复制）
    dd = pd.DataFrame({'eob': e['eob'].to_numpy(), 'post': post, 'pre': pre})
    daily = dd.groupby('eob').mean()
    daily_diff = daily['post'] - daily['pre']
    _, p_pool = _mannwhitney_greater(post, pre)
    _, p_day = _mannwhitney_greater(daily['post'].to_numpy(), daily['pre'].to_numpy())
    obs = float(daily_diff.mean())
    # MC：候选池 = 事件日上有完整窗口的全部股票日，每日 cap 抽样
    ev_dates = e['eob'].unique()
    pool = d[d['eob'].isin(ev_dates) & d['pre20'].notna() & d[col].notna()]
    gid_full = pd.factorize(pool['eob'], sort=True)[0].astype(np.int64)
    if len(pool) > DAY_CAP * (gid_full.max() + 1):
        keep = _cap_per_day(gid_full, len(pool), DAY_CAP, seed + 1)
        pool = pool[keep]
        gid_full = pd.factorize(pool['eob'], sort=True)[0].astype(np.int64)
    k_evt = pd.factorize(e['eob'], sort=True)[0]
    k_per_day = np.bincount(k_evt, minlength=int(gid_full.max()) + 1).astype(np.int64)
    pool_diff = (pool[col] - pool['pre20']).to_numpy(float)
    print(f'  [H1 N={n}] 事件 {len(e):,} 起 / {len(daily):,} 事件日，'
          f'MC 池 {len(pool):,} 行', flush=True)
    mc_p = _mc_self_contrast(pool_diff, gid_full, k_per_day, obs, n_perm, seed)
    return {'n_events': int(len(e)), 'n_event_days': int(len(daily)),
            'pre20_mean': round(float(pre.mean()), 5),
            'post_mean': round(float(post.mean()), 5),
            'diff_mean': round(float(diff.mean()), 5),
            'diff_median': round(float(np.median(diff)), 5),
            'pct_events_expanded': round(float((diff > 0).mean()), 4),
            'daily_diff_mean': round(obs, 5),
            'pct_days_expanded': round(float((daily_diff > 0).mean()), 4),
            'mw_pooled_p': float(f'{p_pool:.3e}'), 'mw_daily_p': float(f'{p_day:.3e}'),
            'mc_p': round(float(mc_p), 4), 'mc_perm': n_perm,
            'pass': bool(obs > 0 and p_day < 0.05 and mc_p < 0.05)}


def h2_test(d: pd.DataFrame, n: int, n_perm: int = MC_PERM, seed: int = MC_SEED) -> dict:
    """H2：事件后 N 日振幅 vs「非末期震荡日」对照组的前向 N 日振幅（组间对照）。"""
    col = f'post{n}'
    e = d[d['event'] & d[col].notna()]
    c = d[(d['regime'] == REGIME_RANGE) & d[col].notna()]
    if len(e) == 0 or len(c) == 0:
        return {'n_events': int(len(e)), 'n_control': int(len(c)),
                'pass': False, 'note': '事件或对照组为空'}
    ea, ca = e[col].to_numpy(float), c[col].to_numpy(float)
    de = e.groupby('eob')[col].mean()
    dc = c.groupby('eob')[col].mean()
    common = de.index.intersection(dc.index)
    if len(common) == 0:
        return {'n_events': int(len(e)), 'n_control': int(len(c)),
                'n_common_days': 0, 'pass': False, 'note': '事件日与对照日无交集'}
    de, dc = de.loc[common], dc.loc[common]
    obs = float((de - dc).mean())
    _, p_pool = _mannwhitney_greater(ea, ca)
    _, p_day = _mannwhitney_greater(de.to_numpy(), dc.to_numpy())
    # MC：事件日上的 事件∪对照 行内标签对换
    u = d[d['eob'].isin(common) & (d['event'] | (d['regime'] == REGIME_RANGE))
          & d[col].notna()]
    gid_full = pd.factorize(u['eob'], sort=True)[0].astype(np.int64)
    if len(u) > DAY_CAP * (gid_full.max() + 1):
        keep = _cap_per_day(gid_full, len(u), DAY_CAP, seed + 1)
        u = u[keep]
        gid_full = pd.factorize(u['eob'], sort=True)[0].astype(np.int64)
    k_per_day = np.bincount(gid_full, weights=u['event'].to_numpy(float),
                            minlength=int(gid_full.max()) + 1).astype(np.int64)
    amp = u[col].to_numpy(float)
    print(f'  [H2 N={n}] 事件 {len(e):,} / 对照 {len(c):,} / 共同日期 {len(common):,}，'
          f'MC 池 {len(u):,} 行', flush=True)
    mc_p = _mc_group_contrast(amp, gid_full, k_per_day, obs, n_perm, seed)
    return {'n_events': int(len(e)), 'n_control': int(len(c)),
            'n_common_days': int(len(common)),
            'event_post_mean': round(float(ea.mean()), 5),
            'control_post_mean': round(float(ca.mean()), 5),
            'diff_mean': round(float(ea.mean() - ca.mean()), 5),
            'daily_diff_mean': round(obs, 5),
            'pct_days_event_higher': round(float(((de - dc) > 0).mean()), 4),
            'mw_pooled_p': float(f'{p_pool:.3e}'), 'mw_daily_p': float(f'{p_day:.3e}'),
            'mc_p': round(float(mc_p), 4), 'mc_perm': n_perm,
            'pass': bool(obs > 0 and p_day < 0.05 and mc_p < 0.05)}


def cmd_core(args):
    d = _load_prep()
    wtag = args.window
    if wtag == 'recent':
        d = d[d['eob'] >= pd.Timestamp(RECENT_START, tz='Asia/Shanghai')]
        print(f'[core] 近3年窗口（{RECENT_START} 起）：{len(d):,} 行', flush=True)
    else:
        print(f'[core] 全历史：{len(d):,} 行', flush=True)
    out = os.path.join(HERE, f'regime_expansion_core_{wtag}_prevadj.json')
    res = {}
    if os.path.exists(out) and not args.force:
        with open(out, encoding='utf-8') as f:
            res = json.load(f)
    res['meta'] = {'window': wtag,
                   'start': RECENT_START if wtag == 'recent' else 'full',
                   'n_rows': int(len(d)),
                   'n_events_total': int(d['event'].sum()),
                   'panel_range': [str(d['eob'].min()), str(d['eob'].max())],
                   'spec': '见 regime_expansion.py 头部预注册注释'}
    steps = args.steps.split(',')
    for n in FWD_NS:
        if 'h1' in steps:
            if str(n) in res.get('H1', {}) and not args.force:
                print(f'  [H1 N={n}] 已有结果，跳过', flush=True)
            else:
                t0 = time.time()
                res.setdefault('H1', {})[str(n)] = h1_test(d, n, n_perm=args.mc_perm)
                r = res['H1'][str(n)]
                print(f'  [H1 N={n}] diff={r["diff_mean"]:+.5f} 日聚合={r["daily_diff_mean"]:+.5f} '
                      f'MW日p={r["mw_daily_p"]:.2e} MCp={r["mc_p"]} pass={r["pass"]} '
                      f'({time.time() - t0:.0f}s)', flush=True)
                _save(out, res)
        if 'h2' in steps:
            if str(n) in res.get('H2', {}) and not args.force:
                print(f'  [H2 N={n}] 已有结果，跳过', flush=True)
            else:
                t0 = time.time()
                res.setdefault('H2', {})[str(n)] = h2_test(d, n, n_perm=args.mc_perm)
                r = res['H2'][str(n)]
                print(f'  [H2 N={n}] diff={r["diff_mean"]:+.5f} 日聚合={r["daily_diff_mean"]:+.5f} '
                      f'MW日p={r["mw_daily_p"]:.2e} MCp={r["mc_p"]} pass={r["pass"]} '
                      f'({time.time() - t0:.0f}s)', flush=True)
                _save(out, res)
    print(f'[core] → {out}', flush=True)


def _save(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


# ════════════════════════════════════════════════════════════════════════════
# 分层：板块 × 真突破（描述统计 + MW，无 MC）
# ════════════════════════════════════════════════════════════════════════════
def _h1_summary(e: pd.DataFrame, n: int) -> dict:
    col = f'post{n}'
    diff = (e[col] - e['pre20']).to_numpy(float)
    _, p = _mannwhitney_greater(e[col].to_numpy(float), e['pre20'].to_numpy(float))
    return {'n_events': int(len(e)),
            'pre20_mean': round(float(e['pre20'].mean()), 5),
            'post_mean': round(float(e[col].mean()), 5),
            'diff_mean': round(float(diff.mean()), 5),
            'pct_events_expanded': round(float((diff > 0).mean()), 4),
            'mw_pooled_p': float(f'{p:.3e}')}


def cmd_stratify(args):
    d0 = _load_prep()
    res = {}
    for wtag, d in [('full', d0),
                    ('recent', d0[d0['eob'] >= pd.Timestamp(RECENT_START, tz='Asia/Shanghai')])]:
        n = PRIMARY_N
        col = f'post{n}'
        e = d[d['event'] & d['pre20'].notna() & d[col].notna()].copy()
        out = {}
        # 板块分层
        out['by_board'] = {b: _h1_summary(g, n) for b, g in e.groupby('board', observed=True)}
        # 真突破分层（事件后 10 日内 ADX 上穿 25）
        eb = e[e['brk10'].notna()]
        by_brk = {str(int(k)): _h1_summary(g, n) for k, g in eb.groupby('brk10')}
        g1 = eb[eb['brk10'] == 1][col].to_numpy(float)
        g0 = eb[eb['brk10'] == 0][col].to_numpy(float)
        if len(g1) and len(g0):
            _, p = _mannwhitney_greater(g1, g0)
            by_brk['mw_breakout_vs_not_p'] = float(f'{p:.3e}')
        out['by_breakout'] = by_brk
        res[wtag] = out
        print(f'[stratify:{wtag}] 板块: ' + ', '.join(
            f"{b} n={s['n_events']:,} diff={s['diff_mean']:+.5f}"
            for b, s in out['by_board'].items()), flush=True)
        print(f'[stratify:{wtag}] 突破: ' + ', '.join(
            f"brk{k} n={s['n_events']:,} diff={s['diff_mean']:+.5f}"
            for k, s in by_brk.items() if isinstance(s, dict)), flush=True)
    path = os.path.join(HERE, 'regime_expansion_stratify_prevadj.json')
    _save(path, res)
    print(f'[stratify] → {path}', flush=True)


# ════════════════════════════════════════════════════════════════════════════
# 参数敏感性：ADX {18,20,22} × BBW分位 {15%,20%,25%} 九宫格（H1 N=5，全历史）
# ════════════════════════════════════════════════════════════════════════════
def cmd_sensitivity(args):
    d = _load_prep()
    n = PRIMARY_N
    col = f'post{n}'
    base = d[d['pre20'].notna() & d[col].notna()]
    rows = []
    for thr in ADX_GRID:
        for q in BBW_GRID:
            t0 = time.time()
            ev = mark_events(base, adx_thr=thr, bbw_q=q, use_label=False)
            e = base[ev]
            diff = (e[col] - e['pre20']).to_numpy(float)
            _, p = _mannwhitney_greater(e[col].to_numpy(float), e['pre20'].to_numpy(float))
            rec = {'adx_thr': thr, 'bbw_q': q, 'n_events': int(len(e)),
                   'diff_mean': round(float(diff.mean()), 5),
                   'pct_events_expanded': round(float((diff > 0).mean()), 4),
                   'mw_pooled_p': float(f'{p:.3e}'),
                   'significant_5pct': bool(diff.mean() > 0 and p < 0.05)}
            rows.append(rec)
            print(f'[sens] ADX<{thr:.0f} × BBW≤{q:.2f}: n={len(e):,} '
                  f'diff={rec["diff_mean"]:+.5f} p={p:.2e} ({time.time() - t0:.0f}s)',
                  flush=True)
    stable = all(r['significant_5pct'] for r in rows)
    path = os.path.join(HERE, 'regime_expansion_sensitivity_prevadj.json')
    _save(path, {'primary_n': n, 'window': 'full', 'grid': rows,
                 'all_cells_significant': stable})
    print(f'[sens] 九宫格全显著={stable} → {path}', flush=True)


# ════════════════════════════════════════════════════════════════════════════
# 汇总
# ════════════════════════════════════════════════════════════════════════════
def cmd_report(args):
    res = {'meta': {'date': '2026-09-19', 'task': 'S2-5 震荡末期→波动扩张传导验证',
                    'spec': '见 regime_expansion.py 头部预注册注释'}}
    for tag, fn in [('core_full', 'regime_expansion_core_full_prevadj.json'),
                    ('core_recent', 'regime_expansion_core_recent_prevadj.json'),
                    ('stratify', 'regime_expansion_stratify_prevadj.json'),
                    ('sensitivity', 'regime_expansion_sensitivity_prevadj.json')]:
        p = os.path.join(HERE, fn)
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                res[tag] = json.load(f)
        else:
            res[tag] = None
            print(f'[report] 缺 {fn}（对应步骤未跑）', flush=True)
    out = os.path.join(HERE, 'regime_expansion_results.json')
    _save(out, res)
    print(f'[report] → {out}', flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('prep')
    p.set_defaults(fn=cmd_prep)
    p = sub.add_parser('core')
    p.set_defaults(fn=cmd_core)
    p.add_argument('--window', choices=['full', 'recent'], required=True)
    p.add_argument('--steps', default='h1,h2')
    p.add_argument('--mc-perm', type=int, default=MC_PERM)
    p.add_argument('--force', action='store_true')
    p = sub.add_parser('stratify')
    p.set_defaults(fn=cmd_stratify)
    p = sub.add_parser('sensitivity')
    p.set_defaults(fn=cmd_sensitivity)
    p = sub.add_parser('report')
    p.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
