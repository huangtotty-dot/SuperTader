# -*- coding: utf-8 -*-
"""日内做T 基线因子库 —— **LLM/GP 臂的对照基线**。

没有基线臂就无法判断生成器贡献了什么（本项目栽过"无法归因的实验"）。
本文件覆盖侦察确认的「全仓不存在、需新写」7 类，并复用 `analysis/indicators.py` 已有口径。

约定：每个因子是 `f(ctx) -> np.ndarray`，长度 = 当日 bar 数，**严格因果**（见 factor_ops）。
符号（做多方向）**不在此处预设**：`eval_factor.py` 对每个因子**双向都测**，
且**双向都计入多重比较分母**——避免"事后挑符号"这种最隐蔽的过拟合。
"""
from __future__ import annotations

import numpy as np

from factor_ops import (decay_linear, dist_high, dist_low, downvol_share, ema, range_pos,
                        realized_skew, smart_money_dev, tail_vol_share, ts_corr, ts_mean,
                        ts_rank, ts_std, volume_concentration, volume_quantile, vwap_dev)


# ── 1. 价格位置 / 相对成本锚 ─────────────────────────────────────────────
def f_vwap_dev(ctx):
    """价对当日累计 VWAP 的偏离。均值回复先验：低于均价 → 买。"""
    return vwap_dev(ctx)


def f_range_pos(ctx):
    """当日迄今区间内的价格位置（0=迄今最低, 1=迄今最高）。"""
    return range_pos(ctx)


def f_dist_high(ctx):
    """距当日迄今最高点的距离（≤0）。"""
    return dist_high(ctx)


def f_dist_low(ctx):
    """距当日迄今最低点的距离（≥0）。"""
    return dist_low(ctx)


def f_ema_dev(ctx, n=20):
    """价对因果 EMA(n) 的偏离。"""
    e = ema(ctx['c'], n)
    return ctx['c'] / np.where(e > 0, e, np.nan) - 1


# ── 2. 量价相关性 CPV（东吴口径，日内改造）──────────────────────────────
def f_cpv_pv(ctx, n=30):
    """30min 滚动 corr(价格, 成交量)。原始因子负向：低相关 → 未来收益高。"""
    return ts_corr(ctx['c'], ctx['v'], n)


def f_cpv_rv(ctx, n=30):
    """30min 滚动 corr(收益, 成交量) —— 东吴另有收益率-成交量版本。"""
    return ts_corr(ctx['ret'], ctx['v'], n)


# ── 3. 已实现矩（海通口径）──────────────────────────────────────────────
def f_rv(ctx, n=60):
    """已实现波动（60min 分钟收益标准差）。"""
    return ts_std(ctx['ret'], n)


def f_rskew(ctx, n=60):
    """已实现偏度（右偏=急拉尾巴，海通口径：偏度高 → 未来收益低）。"""
    return realized_skew(ctx, n)


def f_downvol(ctx, n=60):
    """下行波动占比 = Σr²·I(r<0)/Σr²（海通：占比高 → 恐慌后反弹，未来收益高）。"""
    return downvol_share(ctx, n)


# ── 4. 成交量结构 ────────────────────────────────────────────────────────
def f_vol_q(ctx):
    """当前分钟量在当日**迄今**的分位（放量程度，因果）。"""
    return volume_quantile(ctx)


def f_vol_hhi(ctx):
    """成交量集中度（HHI，迄今累积）——越高说明量越集中在少数分钟。"""
    return volume_concentration(ctx)


def f_tail_vol(ctx):
    """尾盘(>=14:30)成交量占当日迄今比；尾盘前为 NaN。"""
    return tail_vol_share(ctx)


def f_vol_ratio(ctx, n=30):
    """当前分钟量 / 过去 n 分钟因果均量 − 1（放量倍数）。"""
    m = ts_mean(ctx['v'], n)
    prev = np.full(len(m), np.nan)
    prev[1:] = m[:-1]                       # 用**上一根**的均值，避免自比
    return ctx['v'] / np.where(prev > 0, prev, np.nan) - 1


# ── 5. 聪明钱（开源/方正口径，日内滚动改造）──────────────────────────────
def f_smart_dev(ctx, n=120):
    """价对"聪明钱 VWAP"的偏离；<0 = 价在聪明钱成本之下（吸筹嫌疑）。"""
    return smart_money_dev(ctx, top_frac=0.2, beta=0.5, n=n)


def f_smart_smax(ctx, n=60):
    """近 n 分钟的最大聪明度 S=max|R|/√V —— 是否有大资金异动。"""
    from factor_ops import delay
    s = np.abs(ctx['ret']) / np.sqrt(np.maximum(ctx['v'], 1.0))
    return ts_mean(s, n) if False else delay(ts_mean(s, 5), 1)


# ── 6. 时段效应 ──────────────────────────────────────────────────────────
def f_tod_bias(ctx):
    """相对"过去 14 日同一 HH:MM 收盘均值"的偏离。ctx['hist_tod'] 由 eval_factor 预计算。"""
    h = ctx.get('hist_tod') or {}
    c = ctx['c']
    base = np.asarray([h.get(t, np.nan) for t in ctx['t']], float)
    return c / np.where(base > 1e-9, base, np.nan) - 1


# ── 7. 动量 / 反转 ───────────────────────────────────────────────────────
def f_mom30(ctx):
    """30min 动量（因果）。"""
    from factor_ops import delay
    p = delay(ctx['c'], 30)
    return ctx['c'] / np.where(p > 0, p, np.nan) - 1


def f_mom5(ctx):
    """5min 动量。"""
    from factor_ops import delay
    p = delay(ctx['c'], 5)
    return ctx['c'] / np.where(p > 0, p, np.nan) - 1


def f_rev1(ctx):
    """1min 反转（取上一根收益的负）。"""
    from factor_ops import delay
    return -delay(ctx['ret'], 1)


def f_amp(ctx, n=30):
    """滚动振幅（(high−low)/close）之比，因果。"""
    from factor_ops import _roll
    amp = (ctx['h'] - ctx['l']) / np.where(ctx['c'] > 0, ctx['c'], np.nan)
    return _roll(amp, n, np.mean)


def f_decay_ret(ctx, n=10):
    """线性衰减加权的分钟收益（近期权重高）。"""
    return decay_linear(ctx['ret'], n)


# ── ⛔ 阳性对照：故意含前视，**禁止进入挖掘候选池** ──────────────────────
def f_zz_oracle(ctx):
    """**故意的前视因子**（阳性对照，不是候选）。

    返回「从当前 bar 到收盘」的未来收益 —— 即因子"知道"当日剩余涨幅。
    用途：验证裁决器**有灵敏度**。若实验台连它都点不亮（Δ 不大、净均不为正），
    说明实验台迟钝，那么「44/44 基线全负」的结论也不可信。
    ⚠️ 必须在任何挖掘/报告中排除（名字前缀 zz_ 标识）。
    """
    from factor_ops import delay
    c = ctx['c']
    j_f = len(c) - 1
    for i, x in enumerate(ctx['t']):
        if x > '14:55':
            j_f = i - 1
            break
    fut = np.full(len(c), np.nan)
    if j_f > 0 and c[j_f] > 0:
        fut[:j_f] = c[j_f] / np.where(c[:j_f] > 0, c[:j_f], np.nan) - 1
    return fut


# ── 注册表 ───────────────────────────────────────────────────────────────
FACTORS = {
    'vwap_dev': f_vwap_dev,
    'range_pos': f_range_pos,
    'dist_high': f_dist_high,
    'dist_low': f_dist_low,
    'ema_dev20': f_ema_dev,
    'cpv_pv30': f_cpv_pv,
    'cpv_rv30': f_cpv_rv,
    'rv60': f_rv,
    'rskew60': f_rskew,
    'downvol60': f_downvol,
    'vol_q': f_vol_q,
    'vol_hhi': f_vol_hhi,
    'tail_vol': f_tail_vol,
    'vol_ratio30': f_vol_ratio,
    'smart_dev': f_smart_dev,
    'smart_s5': f_smart_smax,
    'tod_bias': f_tod_bias,
    'mom30': f_mom30,
    'mom5': f_mom5,
    'rev1': f_rev1,
    'amp30': f_amp,
    'decay_ret10': f_decay_ret,
}
# 阳性对照单独放：默认**不**进 FACTORS（避免污染基线/挖掘）
CONTROL = {'zz_oracle': f_zz_oracle}
