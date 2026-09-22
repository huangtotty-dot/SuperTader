# -*- coding: utf-8 -*-
"""日内 T+0 完整方案矩阵实验（A1–A6 入场 × B1/B5 出场）— 2026-09-14 owner 要求。

预注册：`C:\\Users\\Lenovo\\.claude\\plans\\mighty-sniffing-feigenbaum.md`
来源：`doc/research/2026-09-14_日内T0完整方案_综合清单.md`（四源调研 Q1–Q4）

## 为什么做
本会话已证**出场不是瓶颈**（11 种出场净均值全在 [−0.12,+0.02]）→ 按调研指引主攻**入场侧**。

## 归因模型（Q4「总发现3」点名必须重写）
A股 T+1 ⇒ 每个信号拆成**一条当日闭合的往返腿**：
  正T(long) 买现金→卖：净 = (卖×(1−费卖) − 买×(1+费买)) / 买
  反T(short) 卖底仓→买回：净 = (卖×(1−费卖) − 买回×(1+费买)) / 卖
两腿当日闭合（14:55 强平兜底）。费由 `core/cost_model.py` 单一真源给出
（owner 实际费率：股票往返 0.06908% / ETF 0.01908%；`ST_COST_VENUE=legacy`
可切回历史口径 0.136% 做回归对照）。

## 预注册取值（文档未写死处，已声明）
  A5: r1=close(10:00)/close(09:30)−1, r7=close(14:30)/close(14:00)−1（close-to-close）
  A6: 收阳 = close(11:30) > open(09:30)
  A2: 变体甲固定锚±1%、变体乙移动锚 g∈{0.8,1.0,1.2}%；连买≤3
  波动率门槛: 前 20 日日内振幅中位数 ≥ 2.0%
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
_MD = os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t')
for _p in (ROOT, _MD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_experiment_v2 as v2  # noqa: E402  复用 1min 数据层

OUT = HERE
# 成本单一真源 core/cost_model.py（旧口径 0.136% 含 2023 年前已废止的 0.1% 印花税）
# ST_COST_VENUE ∈ {stock, etf, legacy} 覆盖，用于回归对照与成本敏感性
from core.cost_model import (fees as _cost_fees, round_trip as _cost_rt,  # noqa: E402
                            leg_pnl as _cost_leg_pnl)
FEE_S, FEE_B = _cost_fees()
COST_ROUND_TRIP = _cost_rt()
TP = 0.005                 # B1 +0.5%
FORCE_LABEL = '14:55'
A1_TIMES = ('10:29', '11:29', '13:59')
GRID_G = (0.008, 0.010, 0.012)
DT_N, DT_K = 4, 0.5        # Dual Thrust: 前 N 日（不含今日）, K1=K2=K
VOL_GATE = 0.020           # 前 20 日日内振幅中位数门槛
MIN_1M = 100
N_MC = 200

# --- 第三轮（2026-09-15）：A7 隔夜-日内反转 + 出场截断对照 ---
# 阈值由 Wave 0 标定锁定（t0_schemes/calibrate.py → calib_2026-09-15.json）：
#   gap<=-1% 桶 n=2046 当日 o2c 均 +0.55%（其中 <=-3% 桶 +1.11%, n=438）
#   gap>=+3% 桶 n=427  o2c 均 -0.02% —— 倒T 那条腿无数据支持（高开日内仍上行）
A7_GAP_THR = 0.010         # 唯一自由参数：|gap| 阈值
A7_TP = 0.010              # B9_native 固定价差目标（S2 给 0.8~1.5%，取中值 1.0%）
A7_TREND_THR = 0.010       # 市场代理 10:30 收益 |·| < 1% 才开仓
MKT_FILTER = True
# 预注册降级：无沪深300分钟数据 → 用池内等权「10:30 收益」作市场代理（无未来函数）

ENTRIES = ['A1_noise', 'A2_grid_fixed', 'A2_grid_move08', 'A3_rbreaker',
           'A4_dualthrust', 'A5_momentum', 'A6_halfday',
           'A7_dual', 'A7_long_only', 'A7_short_only']
EXITS = ['B1_tp05', 'B0_hold', 'B9_native', 'B5_rbreaker_rev']

# 本轮 cells（≤13，控制配额）：A7 全族 + A1–A6 的出场截断对照
# 出场对照实验（2026-09-15 追加）：同一批入场，只换出场规则 ——
# B1_tp05 = 现行生产 +0.5% 固定止盈；B0_hold = 持到 14:55（上界参考）；B11_td9exit = 反向九转出场
# 出场时点右移（2026-09-15 追加）：B0_hold=当日 14:55 平，B12_next_open=持到次日开盘。
# 两者逐腿差值 = 隔夜成分（入场价与成本相同）。A7_long_only/A7_short_only 用于验符号。
EXIT_TEST_ENTRIES = ('A1_noise', 'A2_grid_fixed', 'A2_grid_move08', 'A3_rbreaker',
                     'A4_dualthrust', 'A5_momentum', 'A6_halfday',
                     'A7_long_only', 'A7_short_only')
CELLS = ([(e, 'B0_hold') for e in EXIT_TEST_ENTRIES]
         + [(e, 'B12_next_open') for e in EXIT_TEST_ENTRIES])


# ---------------- 工具 ----------------
def lbl_map(t):
    return {x: i for i, x in enumerate(t)}


def bars_of(day):
    return (np.array([b['o'] for b in day], float), np.array([b['h'] for b in day], float),
            np.array([b['l'] for b in day], float), np.array([b['c'] for b in day], float),
            np.array([b['v'] for b in day], float))


def force_idx(t):
    idx = [i for i, x in enumerate(t) if x <= FORCE_LABEL]
    return idx[-1] if idx else len(t) - 1


# ---------------- 出场模块 ----------------
def exit_tp05(direction, ei, fill, o, h, l, c, hi_bar):
    """B1：正T 到 +0.5% 卖 / 反T 到 −0.5% 买回。
    未达标返回 (None,'end') —— 由调用方决定"被下一条信号反手"还是"14:55 强平"。"""
    tgt = fill * (1 + TP) if direction == 'long' else fill * (1 - TP)
    for j in range(ei + 1, hi_bar + 1):
        if direction == 'long' and h[j] >= tgt:
            return tgt, 'tp'
        if direction == 'short' and l[j] <= tgt:
            return tgt, 'tp'
    return None, 'end'


def exit_rb_rev(direction, ei, fill, o, h, l, c, hi_bar, y_h, y_l, y_c):
    """B5 = R-Breaker 反转腿（两段确认）：持多须**当日最高价曾破 sSetup**、随后跌破 sEnter → 平；
    持空须**当日最低价曾破 bSetup**、随后升破 bEnter → 平。未达标在调用方兜底。"""
    if not (y_h and y_l and y_c):
        return None, 'end'
    piv = (y_h + y_l + y_c) / 3.0
    ssetup = piv + (y_h - y_l)
    senter = 2 * piv - y_l
    bsetup = piv - (y_h - y_l)
    benter = 2 * piv - y_h
    hit = False
    for j in range(ei + 1, hi_bar + 1):
        if direction == 'long':
            if h[j] >= ssetup:
                hit = True
            if hit and c[j] < senter:
                return c[j], 'rb_rev'
        else:
            if l[j] <= bsetup:
                hit = True
            if hit and c[j] > benter:
                return c[j], 'rb_rev'
    return None, 'end'


def exit_native(direction, ei, fill, o, h, l, c, hi_bar, vwap):
    """B9 = A7 自带出场（S2 方案卡1）：正T 回升至当日 VWAP 或 +1.0% 卖出；
    倒T 跌破 VWAP 或 −1.0% 回补。vwap 为当日累计 VWAP 序列（无未来）。"""
    tgt = fill * (1 + A7_TP) if direction == 'long' else fill * (1 - A7_TP)
    for j in range(ei + 1, hi_bar + 1):
        vw = vwap[j] if j < len(vwap) else np.nan
        if direction == 'long':
            if h[j] >= tgt:
                return tgt, 'tp'
            if np.isfinite(vw) and c[j] >= vw:
                return c[j], 'vwap'
        else:
            if l[j] <= tgt:
                return tgt, 'tp'
            if np.isfinite(vw) and c[j] <= vw:
                return c[j], 'vwap'
    return None, 'end'


def exit_td9(direction, ei, fill, o, h, l, c, hi_bar, lm, td_day):
    """B11 = 反向九转出场：持多遇「卖九转」、持空遇「买九转」即平；否则 14:55 兜底。

    在 5m 边界读信号（只用截至该边界的数据）、次根 1min 开盘成交 —— 与 A8 入场同口径。
    无其它止盈止损：本格要回答的是「九转本身是不是一个好的出场触发器」。
    """
    want = -1 if direction == 'long' else 1
    for lb in sorted(lm):
        if lb <= '09:30' or lb > '14:50' or int(lb[3:5]) % 5 != 0:
            continue
        tb = lm[lb] + 1
        if tb <= ei:
            continue
        if tb > hi_bar:
            break
        if td_day.get(lb, 0) == want:
            return o[tb], 'td9exit'
    return None, 'end'


def _leg_pnl(direction, fill, out):
    return _cost_leg_pnl(direction, fill, out, fee_s=FEE_S, fee_b=FEE_B)


# ---------------- 入场模块 ----------------
def a1_noise(t, o, h, l, c, hist_disp, prev_close, lm):
    """西部噪声带：过去14日同一分钟位移均值构成带；仅三时点判突破。"""
    if hist_disp is None or prev_close <= 0:
        return []
    disp = hist_disp  # {label: mean_disp}
    op = o[0]
    out = []
    for tt in A1_TIMES:
        i = lm.get(tt)
        if i is None or i + 1 >= len(c):
            continue
        m = disp.get(tt)
        if not m:
            continue
        up, lo = max(op * (1 + m), prev_close), min(op * (1 - m), prev_close)
        if c[i] > up:
            out.append((i + 1, 'long'))
        elif c[i] < lo:
            out.append((i + 1, 'short'))
    return out


def a2_grid_fixed(t, o, h, l, c, prev_close, lm):
    """变体甲：昨收为中心 ±1% 一格；下穿买、上穿卖。"""
    if prev_close <= 0:
        return []
    lo_p, hi_p = prev_close * 0.99, prev_close * 1.01
    out = []
    for i in range(1, len(c) - 1):
        if c[i - 1] > lo_p >= c[i]:
            out.append((i + 1, 'long'))
        elif c[i - 1] < hi_p <= c[i]:
            out.append((i + 1, 'short'))
    return out


def a2_grid_move(t, o, h, l, c, prev_close, lm, g):
    """变体乙：移动锚——价格≤锚×(1−g)买、≥锚×(1+g)卖，触发后锚点重置为现价。"""
    if prev_close <= 0:
        return []
    anchor, out = prev_close, []
    for i in range(len(c) - 1):
        if c[i] <= anchor * (1 - g):
            out.append((i + 1, 'long')); anchor = c[i]
        elif c[i] >= anchor * (1 + g):
            out.append((i + 1, 'short')); anchor = c[i]
    return out


def a3_rbreaker(t, o, h, l, c, y_h, y_l, y_c, lm):
    """R-Breaker（Saidenberg pivot 版）：六轨 + 反转腿出场另配 B5。"""
    if not (y_h and y_l and y_c):
        return []
    piv = (y_h + y_l + y_c) / 3.0
    bbreak = y_h + 2 * (piv - y_l)
    sbreak = y_l - 2 * (y_h - piv)
    out = []
    for i in range(len(c) - 1):
        if c[i] > bbreak:
            out.append((i + 1, 'long')); break
        if c[i] < sbreak:
            out.append((i + 1, 'short')); break
    return out


def a4_dualthrust(t, o, h, l, c, dt_range, lm):
    """Dual Thrust：Range=Max(HH−LC,HC−LL)（前N日不含今日）；买=开+K×R、卖=开−K×R。"""
    if not dt_range:
        return []
    op = o[0]
    buy_line, sell_line = op + DT_K * dt_range, op - DT_K * dt_range
    for i in range(len(c) - 1):
        if c[i] > buy_line:
            return [(i + 1, 'long')]
        if c[i] < sell_line:
            return [(i + 1, 'short')]
    return []


def a5_momentum(t, o, h, l, c, lm):
    """首半小时 r1 与第七半小时 r7 同向 → 14:30 顺向开仓。"""
    i1a, i1b = lm.get('09:30'), lm.get('10:00')
    i7a, i7b = lm.get('14:00'), lm.get('14:30')
    if None in (i1a, i1b, i7a, i7b) or c[i1a] <= 0 or c[i7a] <= 0:
        return []
    r1 = c[i1b] / c[i1a] - 1
    r7 = c[i7b] / c[i7a] - 1
    if r1 == 0 or r7 == 0 or (r1 > 0) != (r7 > 0):
        return []
    i = i7b
    return [(i + 1, 'long' if r1 > 0 else 'short')] if i + 1 < len(c) else []


def a6_halfday(t, o, h, l, c, lm):
    """上午收阳 → 午后开盘买；收阴 → 午后开盘卖底仓。

    注：本数据为**终点标签**且无 '13:00'（午休后第一根是 '13:01'），故用 13:01 代表午后开盘。
    """
    i_am = lm.get('11:30')
    i_pm = lm.get('13:01')
    if i_am is None or i_pm is None or i_pm + 1 >= len(c):
        return []
    return [(i_pm + 1, 'long' if c[i_am] > o[0] else 'short')]


def a7_overnight(t, o, h, l, c, prev_close, lm, mode):
    """A7 隔夜-日内反转（S2 方案卡1）：低开 → 正T（现金买）；高开 → 倒T（卖底仓）。

    入场 = 开盘 bar（fill = o[09:30] = 集合竞价开盘价），与 Wave 0 标定所用的
    open→close 口径逐字一致。阈值 A7_GAP_THR 由标定锁定，不再调参。
    mode ∈ {dual, long_only, short_only}，后两者为消融。
    """
    if prev_close <= 0:
        return []
    i = lm.get('09:30')
    if i is None or i + 1 >= len(c) or o[i] <= 0:
        return []
    gap = o[i] / prev_close - 1
    if gap <= -A7_GAP_THR and mode in ('dual', 'long_only'):
        return [(i, 'long')]
    if gap >= A7_GAP_THR and mode in ('dual', 'short_only'):
        return [(i, 'short')]
    return []


# ---------------- A8 神奇九转（TD Sequential 9）----------------
A8_VARIANTS = {'A8_td9': 'base', 'A8_td9_noatr': 'noatr',
               'A8_td9_noma20': 'noma20', 'A8_td9_bare': 'bare'}


def _td9(closes):
    """最近 13 个 5m 收盘（末位=最新）。买九转=连续 9 根低于各自 4 根前；卖九转反之。"""
    if len(closes) < 13:
        return 0
    buy = sell = True
    for i in range(1, 10):
        if closes[-i] >= closes[-i - 4]:
            buy = False
        if closes[-i] <= closes[-i - 4]:
            sell = False
    if buy:
        return 1
    if sell:
        return -1
    return 0


def td_5m_map(merged, dates):
    """跨日连续的 5m 收盘序列上算九转 → {date: {label: sig}}。

    无未来函数：5m 边界 L 只用截至 L 的已收盘 5m bar（= 1min bar 终点标签且 MM%5==0，
    跳过 09:30 开盘竞价 bar），决策在 L+1 的 1min bar 开盘成交。
    """
    seq = []
    for d in dates:
        for b in merged[d]:
            t = b['t']
            if t <= '09:30' or t > '14:50':     # 14:50 供出场侧使用；入场侧另限 14:25
                continue
            if int(t[3:5]) % 5 == 0:
                seq.append((d, t, b['c']))
    out, closes = {}, []
    for d, t, cl in seq:
        closes.append(cl)
        sig = _td9(closes)
        if sig:
            out.setdefault(d, {})[t] = sig
    return out


def a8_plan_day(day, lm, td_day, prev_close, j_f, atr, ma20, code, variant):
    """忠实复刻聚宽「神奇九转做T升级版」（双创板自适应 + ATR 过滤 + MA20 趋势过滤）。

    决策在 5m 边界读 c[i]，成交在 o[i+1]（无未来）。状态机与原文一致：
    风控先于信号；反向信号只平不反手；14:55 强平。
    variant ∈ {base, noatr, noma20, bare}（后三者为消融）。
    """
    o, h, c = day['o'], day['h'], day['c']
    dual = code[:2] in ('30', '68')
    tp = 0.035 if dual else 0.015
    sl = 0.025 if dual else 0.015
    use_atr = variant in ('base', 'noma20')
    use_ma = variant in ('base', 'noatr')
    limit = prev_close * (1.20 if dual else 1.10) if prev_close > 0 else 0.0

    legs, cur = [], None
    day_start, hi_so_far = None, 0.0
    for t in sorted(lm):
        if t <= '09:30' or t > '14:25' or int(t[3:5]) % 5 != 0:
            continue
        i = lm[t]
        if i + 1 >= len(c) or i + 1 > j_f:
            continue
        ref, tb, px = c[i], i + 1, o[i + 1]
        if px <= 0:
            continue
        if day_start is None:
            day_start = ref
        hi_so_far = max(hi_so_far, h[i])

        if cur is not None:                                   # 1) 风控先于信号
            e = cur['entry']
            pnl = (px - e) / e if cur['dir'] == 'long' else (e - px) / e
            if pnl >= tp or pnl <= -sl:
                cur.update(cb=tb, px=px, why='tp' if pnl >= tp else 'sl')
                legs.append(cur); cur = None
                continue

        sig = td_day.get(t, 0)
        if cur is None:
            if sig == 0:
                continue
            if use_atr and atr > 0 and day_start is not None \
                    and abs(ref - day_start) < 0.5 * atr:
                continue                                      # ATR 噪声过滤
            if sig == 1:
                if use_ma and ma20 > 0 and ref < ma20:
                    continue                                  # 防暴跌拒接飞刀
                cur = {'ob': tb, 'dir': 'long', 'entry': px}
            else:
                if limit > 0 and hi_so_far >= limit - 0.01:
                    continue                                  # 涨停拦截防卖飞
                if use_ma and ma20 > 0 and ref >= ma20:
                    continue                                  # 防逼空拒摸高顶
                cur = {'ob': tb, 'dir': 'short', 'entry': px}
        else:
            if (cur['dir'] == 'long' and sig == -1) or (cur['dir'] == 'short' and sig == 1):
                cur.update(cb=tb, px=px, why='sig')           # 反向信号只平不反手
                legs.append(cur); cur = None
    if cur is not None:
        cur.update(cb=j_f, px=c[j_f], why='force1455')
        legs.append(cur)
    return legs


def a8_td9(t, o, h, l, c, lm, plans, variant):
    return [(lg['ob'], lg['dir']) for lg in plans[variant]]


def exit_a8(plan, ei, direction):
    """从计划里取该腿的预定平仓价（计划本身已含 tp/sl/反向信号/14:55）。"""
    for lg in plan:
        if lg['ob'] == ei and lg['dir'] == direction:
            return lg['px'], lg['why']
    return None, 'end'


def exit_tpsl(direction, ei, fill, o, h, l, c, hi_bar, tp, sl):
    """分板块止盈止损扫描 —— 仅用于 A8 格子的随机基线（同出场规则）。"""
    up, dn = fill * (1 + tp), fill * (1 - sl)
    for j in range(ei + 1, hi_bar + 1):
        if direction == 'long':
            if h[j] >= up:
                return up, 'tp'
            if l[j] <= dn:
                return dn, 'sl'
        else:
            if l[j] <= fill * (1 - tp):
                return fill * (1 - tp), 'tp'
            if h[j] >= fill * (1 + sl):
                return fill * (1 + sl), 'sl'
    return None, 'end'


# ---------------- 单日回放 ----------------
def run_day(day_ctx, entry, exit_kind):
    t, o, h, l, c, v = day_ctx['t'], day_ctx['o'], day_ctx['h'], day_ctx['l'], day_ctx['c'], day_ctx['v']
    lm = day_ctx['lm']
    j_f = force_idx(t)
    sigs = []
    if entry == 'A1_noise':
        sigs = a1_noise(t, o, h, l, c, day_ctx['hist_disp'], day_ctx['prev_close'], lm)
    elif entry == 'A2_grid_fixed':
        sigs = a2_grid_fixed(t, o, h, l, c, day_ctx['prev_close'], lm)
    elif entry.startswith('A2_grid_move'):
        g = {'A2_grid_move08': 0.008, 'A2_grid_move10': 0.010, 'A2_grid_move12': 0.012}[entry]
        sigs = a2_grid_move(t, o, h, l, c, day_ctx['prev_close'], lm, g)
    elif entry == 'A3_rbreaker':
        sigs = a3_rbreaker(t, o, h, l, c, day_ctx['y_h'], day_ctx['y_l'], day_ctx['y_c'], lm)
    elif entry == 'A4_dualthrust':
        sigs = a4_dualthrust(t, o, h, l, c, day_ctx['dt_range'], lm)
    elif entry == 'A5_momentum':
        sigs = a5_momentum(t, o, h, l, c, lm)
    elif entry == 'A6_halfday':
        sigs = a6_halfday(t, o, h, l, c, lm)
    elif entry.startswith('A7_'):
        mode = {'A7_dual': 'dual', 'A7_long_only': 'long_only',
                'A7_short_only': 'short_only'}[entry]
        sigs = a7_overnight(t, o, h, l, c, day_ctx['prev_close'], lm, mode)
    elif entry.startswith('A8_'):
        sigs = a8_td9(t, o, h, l, c, lm, day_ctx['a8_plans'], A8_VARIANTS[entry])

    pairs = []
    sigs = [(b, d) for b, d in sigs if 0 <= b <= j_f and o[b] > 0]
    # 仓位状态机：同一时刻至多一条腿；下一条信号 = 反手平掉前一条腿（网格/DualThrust 语义）
    for k, (ei, direction) in enumerate(sigs):
        fill = o[ei]
        nxt = sigs[k + 1][0] if k + 1 < len(sigs) else None
        if exit_kind in ('B0_hold', 'B12_next_open'):
            hi_bar = j_f                 # 永不被下一条信号反手：持到 14:55 / 次日开盘
        else:
            hi_bar = j_f if (nxt is None or nxt > j_f) else nxt - 1
        if hi_bar < ei:
            continue
        if exit_kind == 'B0_hold':
            ex_px, why = c[j_f], 'hold1455'
        elif exit_kind == 'B12_next_open':
            # 出场时点右移：持到次日开盘（隔夜敞口）。与 B0_hold 的差 = 逐腿隔夜成分。
            nx = day_ctx['next_open']
            ex_px, why = (nx, 'next_open') if nx > 0 else (c[j_f], 'hold1455')
        elif exit_kind == 'B10_td9':
            ex_px, why = exit_a8(day_ctx['a8_plans'][A8_VARIANTS[entry]], ei, direction)
        elif exit_kind == 'B11_td9exit':
            ex_px, why = exit_td9(direction, ei, fill, o, h, l, c, hi_bar,
                                  lm, day_ctx['td_day'])
        elif exit_kind == 'B9_native':
            ex_px, why = exit_native(direction, ei, fill, o, h, l, c, hi_bar, day_ctx['vwap'])
        elif exit_kind == 'B5_rbreaker_rev':
            ex_px, why = exit_rb_rev(direction, ei, fill, o, h, l, c, hi_bar,
                                     day_ctx['y_h'], day_ctx['y_l'], day_ctx['y_c'])
        else:
            ex_px, why = exit_tp05(direction, ei, fill, o, h, l, c, hi_bar)
        if ex_px is None:                       # 未达出场条件
            if nxt is not None and nxt <= j_f:  # 被下一条信号反手
                ex_px, why = o[nxt], 'reverse'
            else:                               # 14:55 强平兜底
                ex_px, why = c[j_f], 'force1455'
        net = _leg_pnl(direction, fill, ex_px)
        pairs.append({'bar': ei, 'dir': direction, 'fill': fill, 'exit': ex_px,
                      'reason': why, 'net': net, 'win': net > 0})
    return pairs


def day_type(o, h, l, c, prev_close):
    if prev_close <= 0:
        return 'range'
    rng = h.max() - l.min()
    if rng <= 0:
        return 'range'
    chg = c[-1] / prev_close - 1
    if c[-1] > o[0] and (h.max() - c[-1]) / rng < 0.3 and chg > 0.01:
        return 'up'
    if c[-1] < o[0] and (c[-1] - l.min()) / rng < 0.3 and chg < -0.01:
        return 'down'
    return 'range'


def daily_stats(merged, dates):
    """每日期 line 高/低/收/开 + 前20日振幅中位数 + Dual Thrust Range + 昨日 H/L/C。"""
    st = {}
    closes, highs, lows, opens = {}, {}, {}, {}
    for d in dates:
        b = merged[d]
        highs[d] = max(x['h'] for x in b)
        lows[d] = min(x['l'] for x in b)
        opens[d] = b[0]['o']
        closes[d] = b[-1]['c']
    for k, d in enumerate(dates):
        prev = dates[k - 1] if k > 0 else None
        amps = [(highs[x] - lows[x]) / opens[x] for x in dates[max(0, k - 20):k] if opens[x] > 0]
        rng = None
        if k >= DT_N:
            win = dates[k - DT_N:k]                      # 前 N 日，不含今日
            HH = max(highs[x] for x in win); LL = min(lows[x] for x in win)
            HC = max(closes[x] for x in win); LC = min(closes[x] for x in win)
            rng = max(HH - LC, HC - LL)
        # A8 用：前 20 日 MA20 与前 20 日 ATR（不含今日）
        pri = list(range(max(0, k - 20), k))
        ma20 = float(np.mean([closes[dates[j]] for j in pri])) if pri else 0.0
        trs = []
        for j in pri:
            dd = dates[j]
            pc = closes[dates[j - 1]] if j > 0 else opens[dd]
            trs.append(max(highs[dd] - lows[dd], abs(highs[dd] - pc), abs(lows[dd] - pc)))
        nxt_o = opens[dates[k + 1]] if k + 1 < len(dates) else 0.0
        st[d] = {'next_open': nxt_o,
                 'prev_close': closes[prev] if prev else 0.0,
                 'y_h': highs[prev] if prev else 0.0,
                 'y_l': lows[prev] if prev else 0.0,
                 'y_c': closes[prev] if prev else 0.0,
                 'dt_range': rng,
                 'ma20': ma20, 'atr': float(np.mean(trs)) if trs else 0.0,
                 'vol_med': float(np.median(amps)) if amps else 0.0}
    return st


def a1_disp_table(merged, dates, k, max_look=14):
    """过去 max_look 日（不含今日）同一时刻的 |收/开−1| 均值。"""
    prior = dates[max(0, k - max_look):k]
    if len(prior) < 5:
        return None
    acc = {}
    for d in prior:
        b = merged[d]
        op = b[0]['o']
        if op <= 0:
            continue
        for x in b:
            acc.setdefault(x['t'], []).append(abs(x['c'] / op - 1))
    return {tt: float(np.mean(v)) for tt, v in acc.items() if v}


def market_r30(codes):
    """池内等权「10:30 收益」作市场代理（无未来：只用 ≤10:30 数据）。

    预注册降级：无沪深300分钟数据，故用池内等权代理沪深300 的 5min 动量过滤。
    偏离已记录在 plan 的风险节。
    """
    acc = {}
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        for d in dates:
            day = merged[d]
            if len(day) < MIN_1M or day[0]['o'] <= 0:
                continue
            i30 = None
            for j, b in enumerate(day):
                if b['t'] <= '10:30':
                    i30 = j
                else:
                    break
            if i30 is None:
                continue
            acc.setdefault(d, []).append(day[i30]['c'] / day[0]['o'] - 1)
    return {d: float(np.mean(v)) for d, v in acc.items() if v}


def discover():
    return sorted({os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
                   for f in glob.glob(os.path.join(v2.CSV_DIR, '*_1year_1min.csv'))})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--out', default=os.path.join(OUT, 'results_2026-09-14.json'))
    ap.add_argument('--no-mkt-filter', action='store_true',
                    help='关掉 A7 的市场趋势过滤（消融：Wave 0 归因显示该过滤是 A7 的杀手）')
    args = ap.parse_args()
    global MKT_FILTER
    if args.no_mkt_filter:
        MKT_FILTER = False
    codes = args.codes.split(',') if args.codes else discover()
    v2.END = '2026-08-26'
    mkt = market_r30(codes) if MKT_FILTER else {}

    cells = {}
    rand = {}
    n_days = n_gated = 0
    for code in codes:
        dates, merged, _src = v2.merge_days(code)
        dates = [d for d in dates if '2025-09-14' <= d <= '2026-08-26']
        if len(dates) < 30:
            continue
        st = daily_stats(merged, dates)
        td_map = td_5m_map(merged, dates)
        for k, d in enumerate(dates):
            day = merged[d]
            if len(day) < MIN_1M:
                continue
            n_days += 1
            s = st[d]
            if s['vol_med'] < VOL_GATE:      # 日度波动率门槛：低波日不开T
                continue
            n_gated += 1
            o, h, l, c, v = bars_of(day)
            j_f_local = force_idx([x['t'] for x in day])
            # 当日累计 VWAP（无未来函数：逐 bar 累计 Σamt/Σvol）
            amt = np.array([float(x.get('amt') or 0.0) for x in day], float)
            cum_amt, cum_v = np.cumsum(amt), np.cumsum(v)
            vwap = np.where(cum_v > 0, cum_amt / np.where(cum_v > 0, cum_v, 1.0), np.nan)
            dctx = {'t': [x['t'] for x in day], 'o': o, 'h': h, 'l': l, 'c': c, 'v': v,
                    'lm': lbl_map([x['t'] for x in day]), 'prev_close': s['prev_close'],
                    'y_h': s['y_h'], 'y_l': s['y_l'], 'y_c': s['y_c'],
                    'dt_range': s['dt_range'], 'vwap': vwap,
                    'hist_disp': a1_disp_table(merged, dates, k),
                    'td_day': td_map.get(d, {}), 'next_open': s['next_open']}
            dctx['a8_plans'] = {
                vm: a8_plan_day({'o': o, 'h': h, 'c': c}, dctx['lm'], td_map.get(d, {}),
                                s['prev_close'], j_f_local, s['atr'], s['ma20'], code, vm)
                for vm in ('base', 'noatr', 'noma20', 'bare')}
            dt = day_type(o, h, l, c, s['prev_close'])
            for entry, ex in CELLS:
                    # A7 的市场过滤（池内等权代理）：单边趋势日不开仓
                    if entry.startswith('A7_') and MKT_FILTER:
                        m = mkt.get(d)
                        if m is not None and abs(m) >= A7_TREND_THR:
                            continue
                    key = f'{entry}|{ex}'
                    day_pairs = run_day(dctx, entry, ex)
                    for p in day_pairs:
                        cells.setdefault(key, []).append({**p, 'code': code, 'date': d, 'day_type': dt})
                    # 同格随机基线（达标线②）：同一 (票,日)、同方向分布、随机入场 bar、同出场模块
                    if day_pairs:
                        import random as _rnd
                        _r = _rnd.Random(hash((code, d, key)) & 0xFFFF)
                        _bars = [p['bar'] for p in day_pairs]
                        _dirs = [p['dir'] for p in day_pairs]
                        for _ in range(N_MC):
                            _d2 = _r.choice(_dirs)
                            _b2 = _r.randint(1, j_f_local)
                            _f2 = o[_b2]
                            if _f2 <= 0:
                                continue
                            if ex == 'B0_hold':
                                _x, _w = c[j_f_local], 'hold1455'
                            elif ex == 'B12_next_open':
                                _nx = s['next_open']
                                _x, _w = ((_nx, 'next_open') if _nx > 0
                                          else (c[j_f_local], 'hold1455'))
                            elif ex == 'B10_td9':
                                _dual = code[:2] in ('30', '68')
                                _x, _w = exit_tpsl(_d2, _b2, _f2, o, h, l, c, j_f_local,
                                                   0.035 if _dual else 0.015,
                                                   0.025 if _dual else 0.015)
                            elif ex == 'B11_td9exit':
                                _x, _w = exit_td9(_d2, _b2, _f2, o, h, l, c, j_f_local,
                                                  dctx['lm'], dctx['td_day'])
                            elif ex == 'B9_native':
                                _x, _w = exit_native(_d2, _b2, _f2, o, h, l, c,
                                                     j_f_local, vwap)
                            elif ex == 'B5_rbreaker_rev':
                                _x, _w = exit_rb_rev(_d2, _b2, _f2, o, h, l, c, j_f_local,
                                                     s['y_h'], s['y_l'], s['y_c'])
                            else:
                                _x, _w = exit_tp05(_d2, _b2, _f2, o, h, l, c, j_f_local)
                            if _x is None:
                                _x, _w = c[j_f_local], 'force1455'
                            rand.setdefault(key, []).append(
                                {'net': _leg_pnl(_d2, _f2, _x), 'code': code, 'date': d})
    print(f'[t0] codes={len(codes)} 交易日={n_days} 过波动率门槛={n_gated}')
    print(f"\n{'cell':28}{'n':>6}{'净均%':>9}{'中位%':>9}{'胜率':>8}{'密度/票/日':>11}  出场分布")
    summary = {}
    import collections
    for key in sorted(cells):
        r = cells[key]
        if not r:
            continue
        net = np.array([x['net'] for x in r])
        dist = dict(collections.Counter(x['reason'] for x in r))
        density = len(r) / max(n_gated, 1)
        summary[key] = {'n': len(r), 'avg_net': round(float(net.mean()), 4),
                        'median_net': round(float(np.median(net)), 4),
                        'win_rate': round(float((net > 0).mean()), 4),
                        'density_per_stock_day': round(density, 3),
                        'exit_dist': dist,
                        'by_day_type': {t: {'n': len([x for x in r if x['day_type'] == t]),
                                            'avg': round(float(np.mean([x['net'] for x in r if x['day_type'] == t])), 4)
                                            if any(x['day_type'] == t for x in r) else None}
                                        for t in ('up', 'range', 'down')}}
        rn = np.array([x['net'] for x in rand.get(key, [])]) if rand.get(key) else np.array([])
        delta = round(float(net.mean() - rn.mean()), 4) if len(rn) else None
        summary[key]['random_avg'] = round(float(rn.mean()), 4) if len(rn) else None
        summary[key]['delta_vs_random'] = delta
        print(f"{key:28}{len(r):>6}{net.mean():>9.3f}{np.median(net):>9.3f}"
              f"{(net>0).mean():>8.3f}{density:>11.3f}  rand={summary[key]['random_avg']} Δ={delta}")
    # 配对检验：同一入场下 B0_hold vs B12_next_open 的**逐腿**差（区隔「隔夜 carry」与「入场技巧」）
    # 差值恒等于隔夜那段的价格变动；同时看随机基线是否同步移动（若同步 ⇒ 是 carry 不是 skill）
    paired = {}
    try:
        from scipy import stats as _st
    except Exception:
        _st = None
    for e in EXIT_TEST_ENTRIES:
        a, b = cells.get(f'{e}|B0_hold'), cells.get(f'{e}|B12_next_open')
        if not a or not b:
            continue
        ka = {(x['code'], x['date'], x['bar'], x['dir']): x['net'] for x in a}
        va, vb = [], []
        for x in b:
            k = (x['code'], x['date'], x['bar'], x['dir'])
            if k in ka:
                va.append(ka[k]); vb.append(x['net'])
        if len(va) < 30:
            continue
        d = np.array(vb) - np.array(va)
        sd = float(d.std(ddof=1))
        t = float(d.mean() / (sd / np.sqrt(len(d)))) if sd > 0 else 0.0
        if _st:
            p = float(_st.ttest_rel(vb, va).pvalue)
        else:                                   # 无 scipy：正态近似两侧 p（n≥30 足够）
            import math
            p = math.erfc(abs(t) / math.sqrt(2))
        paired[e] = {'n': len(d), 'overnight_component': round(float(d.mean()), 4),
                     't': round(t, 2), 'p': round(p, 6)}
    if paired:
        print(f"\n{'配对(B0_hold→B12_next_open)':30}{'n':>7}{'隔夜成分%':>11}{'t':>8}{'p':>9}")
        for e, v in paired.items():
            print(f"{e:30}{v['n']:>7}{v['overnight_component']:>11.4f}{v['t']:>8.2f}"
                  f"{str(v['p']):>9}")

    json.dump({'meta': {'codes': len(codes), 'days': n_days, 'gated_days': n_gated,
                        'vol_gate': VOL_GATE, 'dt_N': DT_N, 'dt_K': DT_K},
               'paired_b0_vs_b12': paired,
               'summary': summary}, open(args.out, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(f'\n[t0] -> {args.out}')


if __name__ == '__main__':
    main()
