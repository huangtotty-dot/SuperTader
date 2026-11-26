# -*- coding: utf-8 -*-
"""H1/H2 择时 · 资金周转模型（2026-11-26 owner 拍板「直接开始」）。

回答的问题：「不 pop 每笔多赚 +1.03pp」换成**账户口径**还成立吗？
不 pop 持仓 ~1.5 天 → 5 天，资金占用拉长 ⇒ 20 万日预算 + 有限本金下
新信号可能被旧仓堵住。本脚本把出场变体放进资金队列仿真，输出
**资金年化** 对比——这是候选㊶（pop 温度参数化）投产裁决的前置闸。

## 模型（对齐生产口径）
  · 信号：OSC 日线近似 top-4（与 h1h2_osc_timing.py 同口径重建）
  · 预算：日 20 万 / 单票 10 万（executor.py 现行参数）；日内按 gap 降序
    依次下单，min(单票, 日剩余, 现金) < 2 万 ⇒ 放弃该信号（记 skip）
  · 回款：当日可用（owner 已拍板口径）——当日卖出回款当日可买
  · 持仓：一票一仓（持有中同票新信号跳过）
  · 净值：现金 + 持仓市值（close 按日盯市，扣除双边费用近似）

## 实验臂
  pop3   现行（次日 +3% pop / SL5 只深 / 5 日到期）
  pop5   全量 pop+5%
  nopop  全量不 pop
  mix5   ㊶a：temp≥60 或 H1 窗口 → pop5，其余 pop3
  mixNP  ㊶b：temp≥60 或 H1 窗口 → 不 pop，其余 pop3

## 本金档位
  50 万 / 100 万 / 200 万 / 1 亿（无阻塞对照）——找出阻塞临界点。

产物：`t_io/validation/h1h2_capital_turnover/summary.json` + 控制台对照表。
"""
from __future__ import annotations

import bisect
import json
import os
import sys
import time
from collections import Counter, defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location(
    "osc_prescreen_offline",
    os.path.join(_ROOT, "execution", "auto", "oversold_gap_confirm", "prescreen.py"))
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
full_market_meta = _mod.full_market_meta
universe_board_map = _mod.universe_board_map

from core.cost_model import FEE_BUY_STOCK, FEE_SELL_STOCK  # noqa: E402

TEMP_FP = os.path.join(_ROOT, "t_io", "index_regime", "composite_temp.json")
CACHE_DIR = os.path.join(_ROOT, "t_io", "cache", "daily_kline")
IDX_FP = os.path.join(CACHE_DIR, "index_sh000300.json")
OUT_DIR = os.path.join(_ROOT, "t_io", "validation", "h1h2_capital_turnover")

LU_PCT = 0.095
BOARD_TOP_N = 5
POP_PCT, STOP_PCT, DEEP_DEV20, HOLD_DAYS = 0.03, -0.05, -0.07, 5
TOP4_CAP = 4

DAILY_BUDGET = 200000.0     # executor.py 现行
SINGLE_BUDGET = 100000.0    # executor.py 现行
MIN_ALLOC = 20000.0         # 低于 2 万放弃该信号（避免碎单失真）
FEES_MTM = FEE_BUY_STOCK + FEE_SELL_STOCK   # 盯市清算价值近似

ARMS = ("pop3", "pop5", "nopop", "mix5", "mixNP")
CAPITALS = (500000.0, 1000000.0, 2000000.0, 100000000.0)


def _limit_of(code: str) -> float:
    return 0.195 if code[:3] in ("300", "688") else 0.095


def _net_ret(b, s):
    return (s * (1 - FEE_SELL_STOCK) - b * (1 + FEE_BUY_STOCK)) / b * 100.0


def _load_temp():
    raw = json.load(open(TEMP_FP, encoding="utf-8"))
    dates = sorted(raw.keys())
    return dates, [raw[d].get("sent_ma") for d in dates]


def _slope5_flags(dates, sent):
    flags = {}
    for i in range(6, len(dates)):
        w = sent[i - 5:i]
        if any(v is None for v in w) or sent[i] is None or sent[i - 6] is None:
            continue
        if sent[i - 1] - sent[i - 6] <= 0 < sent[i] - sent[i - 5] and min(w) <= 25:
            flags[dates[i]] = "H1"
        elif sent[i - 1] - sent[i - 6] >= 0 > sent[i] - sent[i - 5] and max(w) >= 60:
            flags[dates[i]] = "H2"
    return flags


def _sim_exit(dts, o, h, l, c, i, deep, pop_pct):
    entry = o[i]
    for hold in range(1, HOLD_DAYS + 1):
        j = i + hold
        if j >= len(dts):
            return dts[-1], c[-1], "open_end"
        if pop_pct is not None and hold == 1 and h[j] >= entry * (1 + pop_pct):
            return dts[j], max(entry * (1 + pop_pct), o[j]), "pop_sell"
        if deep and c[j] < entry * (1 + STOP_PCT):
            return dts[j], c[j], "sl5_deep"
        if hold == HOLD_DAYS:
            return dts[j], c[j], "horizon_expired"
    return dts[-1], c[-1], "open_end"


def _arm_tag(arm, sig):
    """㊶ 混合臂的温度路由：temp≥60 或 H1 窗口 → 激进出场；否则现行 pop3。"""
    if arm == "mix5":
        return "pop5" if (sig["temp"] is not None and sig["temp"] >= 60) or sig["h1"] else "pop3"
    if arm == "mixNP":
        return "nopop" if (sig["temp"] is not None and sig["temp"] >= 60) or sig["h1"] else "pop3"
    return arm


def run_capital(signals_by_day, close_map, cal, arm, init_cash):
    cash = init_cash
    open_pos = []           # {code, alloc, entry, exit_d, ret}
    equity_curve = []
    taken = skipped_cash = skipped_held = skipped_budget = 0
    skip_hot = skip_all = 0          # 阻塞信号中 temp≥60 的占比（机制验证）
    ret_taken_sum = 0.0
    buy_notional = 0.0
    util_sum = 0.0

    for d in cal:
        # ① 当日到期卖出（回款当日可用）
        if open_pos:
            still = []
            for p in open_pos:
                if p["exit_d"] <= d:
                    cash += p["alloc"] * (1.0 + p["ret"] / 100.0)
                else:
                    still.append(p)
            open_pos = still
        # ② 当日信号入场（gap 降序）
        daily_remain = DAILY_BUDGET
        for sig in signals_by_day.get(d, ()):
            if any(p["code"] == sig["code"] for p in open_pos):
                skipped_held += 1
                continue
            alloc = min(SINGLE_BUDGET, daily_remain, cash)
            if alloc < MIN_ALLOC:
                skip_all += 1
                if sig["temp"] is not None and sig["temp"] >= 60:
                    skip_hot += 1
                if daily_remain < MIN_ALLOC:
                    skipped_budget += 1
                else:
                    skipped_cash += 1
                continue
            v = sig["variants"][_arm_tag(arm, sig)]
            cash -= alloc
            daily_remain -= alloc
            buy_notional += alloc
            ret_taken_sum += v["ret"]
            open_pos.append({"code": sig["code"], "alloc": alloc,
                             "entry": sig["entry"], "exit_d": v["exit_d"],
                             "ret": v["ret"]})
            taken += 1
        # ③ 盯市
        mv = 0.0
        for p in open_pos:
            px = close_map.get(p["code"], {}).get(d)
            if px:
                mv += p["alloc"] * (px / p["entry"]) * (1.0 - FEES_MTM)
            else:   # 停牌等：按成本估
                mv += p["alloc"] * (1.0 - FEES_MTM)
        eq = cash + mv
        equity_curve.append((d, eq))
        util_sum += (mv / eq) if eq > 0 else 0.0

    n = len(equity_curve)
    final_eq = equity_curve[-1][1] if n else init_cash
    total_ret = final_eq / init_cash - 1.0
    years = n / 252.0
    ann = (final_eq / init_cash) ** (1.0 / years) - 1.0 if years > 0 and final_eq > 0 else -1.0
    peak, maxdd = -1.0, 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            maxdd = max(maxdd, 1.0 - eq / peak)
    return {
        "final": round(final_eq, 0), "total_ret%": round(total_ret * 100, 2),
        "ann%": round(ann * 100, 2), "maxdd%": round(maxdd * 100, 2),
        "taken": taken, "skip_cash": skipped_cash, "skip_budget": skipped_budget,
        "skip_held": skipped_held,
        "taken_ret均值%": round(ret_taken_sum / max(1, taken), 3),
        "阻塞中高温占比%": round(skip_hot / max(1, skip_all) * 100, 1),
        "util%": round(util_sum / max(1, n) * 100, 1),
        "turnover/年": round(buy_notional / max(1e-9, (init_cash + final_eq) / 2)
                          / max(1e-9, years), 2),
    }


def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)

    temp_dates, sent = _load_temp()
    idx_rows = json.load(open(IDX_FP, encoding="utf-8")).get("rows") or []
    cal = sorted(set(temp_dates) | {r["date"] for r in idx_rows if r.get("date")})
    cal_pos = {d: i for i, d in enumerate(cal)}
    sent_map = dict(zip(temp_dates, sent))
    trig = _slope5_flags(temp_dates, sent)
    h1_days = set()
    for d, k in trig.items():
        if k == "H1" and d in cal_pos:
            h1_days.update(cal[cal_pos[d]:cal_pos[d] + 3])

    meta = full_market_meta()
    board_map = universe_board_map(full_market=True)
    universe = sorted(meta.keys())
    print(f"宇宙 {len(universe)} 只；H1 窗口日 {len(h1_days)}")

    # —— 信号扫描（同 h1h2_osc_timing 口径） ——
    lu_cnt = defaultdict(Counter)
    pre = []
    rows_keep = {}
    for code in universe:
        fp = os.path.join(CACHE_DIR, f"{code}.json")
        if not os.path.exists(fp):
            continue
        try:
            rows = (json.load(open(fp, encoding="utf-8")) or {}).get("rows") or []
        except Exception:
            continue
        rows = [r for r in rows if r.get("date")]
        rows.sort(key=lambda r: r["date"])
        if len(rows) < 30:
            continue
        dts = [r["date"] for r in rows]
        o = [float(r["open"]) for r in rows]
        h = [float(r["high"]) for r in rows]
        l = [float(r["low"]) for r in rows]
        c = [float(r["close"]) for r in rows]
        board = board_map.get(code, "")
        for i in range(21, len(rows)):
            d = dts[i]
            if d < "2023-08-24":
                continue
            chg = c[i] / c[i - 1] - 1.0
            if board and chg >= LU_PCT:
                lu_cnt[d][board] += 1
            ma20_prev = sum(c[i - 20:i]) / 20.0
            if not (c[i - 1] < ma20_prev):
                continue
            if not (o[i] > c[i - 1] and l[i] >= c[i - 1] and c[i] > c[i - 1]):
                continue
            if o[i] == h[i] == l[i] and chg >= _limit_of(code):
                continue
            pre.append({"date": d, "code": code, "board": board,
                        "dev20": c[i - 1] / ma20_prev - 1.0,
                        "gap": o[i] / c[i - 1] - 1.0, "_i": i})
            rows_keep[code] = (dts, o, h, l, c)
    print(f"扫描 {time.time()-t0:.0f}s：粗候选 {len(pre)} 条")

    def board_ok(date, board):
        i = cal_pos.get(date)
        if i is None or i < 1 or not board:
            return False
        top = [b for b, _ in lu_cnt.get(cal[i - 1], Counter()).most_common(BOARD_TOP_N)]
        return board in top

    cands = [r for r in pre if board_ok(r["date"], r["board"])]

    by_day = defaultdict(list)
    for r in cands:
        by_day[r["date"]].append(r)
    signals_by_day = {}
    close_map = {}
    for d, lst in by_day.items():
        lst.sort(key=lambda r: -r["gap"])
        day_sigs = []
        for r in lst[:TOP4_CAP]:          # 生产 top-4 口径
            dts, o, h, l, c = rows_keep[r["code"]]
            i = r["_i"]
            entry = o[i]
            deep = r["dev20"] < DEEP_DEV20
            variants = {}
            for tag, pp in (("pop3", POP_PCT), ("pop5", 0.05), ("nopop", None)):
                ed, ep, _w = _sim_exit(dts, o, h, l, c, i, deep, pp)
                variants[tag] = {"exit_d": ed, "ret": round(_net_ret(entry, ep), 4)}
            d_prev_i = cal_pos.get(d, 1) - 1
            tprev = sent_map.get(cal[d_prev_i]) if 0 <= d_prev_i < len(cal) else None
            day_sigs.append({"code": r["code"], "gap": r["gap"], "entry": entry,
                             "temp": tprev, "h1": d in h1_days, "variants": variants})
            if r["code"] not in close_map:
                close_map[r["code"]] = dict(zip(dts, c))
        signals_by_day[d] = day_sigs
    n_sig = sum(len(v) for v in signals_by_day.values())
    print(f"top4 信号 {n_sig} 条（{len(signals_by_day)} 个交易日），开始资金仿真")

    # —— 资金队列仿真：5 臂 × 4 本金 ——
    start_d = min(signals_by_day)
    sim_cal = [d for d in cal if d >= start_d]
    out = {}
    print(f"\n{'臂':<7}{'本金':>10}{'终值':>14}{'总收益%':>9}{'年化%':>8}"
          f"{'最大回撤%':>10}{'成交':>7}{'预算阻塞':>9}{'现金阻塞':>9}"
          f"{'持仓冲突':>9}{'成交均 ret%':>12}{'阻塞高温%':>10}"
          f"{'利用率%':>9}{'年周转':>8}")
    for arm in ARMS:
        for cap in CAPITALS:
            r = run_capital(signals_by_day, close_map, sim_cal, arm, cap)
            out.setdefault(arm, {})[f"{cap/10000:.0f}万" if cap < 1e7 else "无限"] = r
            cap_label = f"{cap/10000:.0f}万" if cap < 1e7 else "1亿(无阻塞)"
            print(f"{arm:<7}{cap_label:>10}{r['final']:>14,.0f}{r['total_ret%']:>+9}"
                  f"{r['ann%']:>+8}{r['maxdd%']:>10}{r['taken']:>7}"
                  f"{r['skip_budget']:>9}{r['skip_cash']:>9}{r['skip_held']:>9}"
                  f"{r['taken_ret均值%']:>+12}{r['阻塞中高温占比%']:>10}"
                  f"{r['util%']:>9}{r['turnover/年']:>8}")

    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"params": {"daily_budget": DAILY_BUDGET, "single_budget": SINGLE_BUDGET,
                              "min_alloc": MIN_ALLOC, "top4_cap": TOP4_CAP,
                              "signals": n_sig, "cal_days": len(sim_cal)},
                   "results": out}, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s；产物 {OUT_DIR}/summary.json")


if __name__ == "__main__":
    main()
