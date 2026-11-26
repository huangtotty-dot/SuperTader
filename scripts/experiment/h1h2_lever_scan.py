# -*- coding: utf-8 -*-
"""OSC 收益改善 · 杠杆决策矩阵（2026-11-26 周六复盘预读，owner 定主线）。

周六主线：「如何改善 OSC 策略收益，如何用 H1/H2 假说」。本脚本把候选杠杆
全部放进资金队列（同 h1h2_capital_turnover 口径），产出裁决矩阵：
周六复盘只需拍板，不再讨论方向。

## 杠杆清单（每条对应一个已登记候选/假说）
  base        现行：日 20 万 / 单票 10 万 / pop3 / 无闸
  ── 资金结构（今日发现的最大漏：54% 成交率、1,159 笔被日预算挡掉）
  B40s10      日 40 万 / 单票 10 万（每日最多 4 票）
  B20s5       日 20 万 / 单票 5 万（每日 4 票、更分散）
  B40s20      日 40 万 / 单票 20 万（每日 2 票、更重）
  ── 入场温度闸（候选㊷：弱势段到期亏 63.7%）
  T40         temp<40 不入场
  T2040       仅 20<temp≤40 不入场（保留冰点反转票）
  ── gap 上限（10-26 议题：688228 高开 19.4% 追入当天 -8.8%；
  │            且 top4 子集 pop3 均 ret +2.00% 低于全候选 +2.29%）
  G5 / G8     gap>5% / gap>8% 不入场
  ── H1 假说加码（H1 窗口 pop 达成率 65.1% 全样本最高 → 窗口内加仓）
  H1x2        H1 窗口内单票 20 万（日预算仍 20 万）
  ── 出场幸存项（周转模型唯一活口）
  pop5        全量 pop+5%

口径披露同 h1h2_capital_turnover：日线近似、只有相对比较有效。
产物：`t_io/validation/h1h2_lever_scan/summary.json` + 控制台矩阵。
"""
from __future__ import annotations

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
OUT_DIR = os.path.join(_ROOT, "t_io", "validation", "h1h2_lever_scan")

LU_PCT = 0.095
BOARD_TOP_N = 5
POP_PCT, STOP_PCT, DEEP_DEV20, HOLD_DAYS = 0.03, -0.05, -0.07, 5
TOP4_CAP = 4
MIN_ALLOC = 20000.0
FEES_MTM = FEE_BUY_STOCK + FEE_SELL_STOCK

# (标签, 日预算, 单票, 出场, 温度闸, gap上限, H1窗口单票倍率)
LEVERS = [
    ("base",   200000, 100000, "pop3", None,    None, 1),
    ("B40s10", 400000, 100000, "pop3", None,    None, 1),
    ("B20s5",  200000,  50000, "pop3", None,    None, 1),
    ("B40s20", 400000, 200000, "pop3", None,    None, 1),
    ("T40",    200000, 100000, "pop3", "lt40",  None, 1),
    ("T2040",  200000, 100000, "pop3", "2040",  None, 1),
    ("G5",     200000, 100000, "pop3", None,    0.05, 1),
    ("G8",     200000, 100000, "pop3", None,    0.08, 1),
    ("H1x2",   200000, 100000, "pop3", None,    None, 2),
    ("pop5",   200000, 100000, "pop5", None,    None, 1),
    # —— 组合复测（单杠杆赢家叠加） ——
    ("B40+G8",      400000, 100000, "pop3", None,   0.08, 1),
    ("B40+G8+H1x2", 400000, 100000, "pop3", None,   0.08, 2),
    ("B40+G8+p5",   400000, 100000, "pop5", None,   0.08, 1),
    ("B40+G8+H1+p5", 400000, 100000, "pop5", None,  0.08, 2),
    ("B40+T2040+G8", 400000, 100000, "pop3", "2040", 0.08, 1),
    # —— 33 万实盘余额档（owner 2026-11-26 拍板：按现余额操作不升额） ——
    ("B25s5",       250000,  50000, "pop3", None,   None, 1),
    ("B30s5",       300000,  50000, "pop3", None,   None, 1),
    ("B30s10",      300000, 100000, "pop3", None,   None, 1),
    ("B20s5+G8",    200000,  50000, "pop3", None,   0.08, 1),
    ("B25s5+G8",    250000,  50000, "pop3", None,   0.08, 1),
    ("B30s5+G8",    300000,  50000, "pop3", None,   0.08, 1),
    ("B30s10+G8",   300000, 100000, "pop3", None,   0.08, 1),
]
CAPITALS = (500000.0, 1000000.0, 330218.75)


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


def _pass_gate(sig, temp_gate, gap_cap):
    if gap_cap is not None and sig["gap"] > gap_cap:
        return False
    t = sig["temp"]
    if temp_gate == "lt40" and t is not None and t < 40:
        return False
    if temp_gate == "2040" and t is not None and 20 < t <= 40:
        return False
    return True


def run_sim(signals_by_day, close_map, cal, init_cash,
            daily_cap, single_cap, exit_arm, temp_gate, gap_cap, h1_mult):
    cash = init_cash
    open_pos = []
    equity_curve = []
    taken = skipped_budget = skipped_cash = skipped_gate = 0
    ret_sum = 0.0
    pop_hit = exp_loss_n = exp_n = 0

    for d in cal:
        if open_pos:
            still = []
            for p in open_pos:
                if p["exit_d"] <= d:
                    cash += p["alloc"] * (1.0 + p["ret"] / 100.0)
                else:
                    still.append(p)
            open_pos = still
        daily_remain = daily_cap
        for sig in signals_by_day.get(d, ()):
            if any(p["code"] == sig["code"] for p in open_pos):
                continue
            if not _pass_gate(sig, temp_gate, gap_cap):
                skipped_gate += 1
                continue
            cap = single_cap * (h1_mult if sig["h1"] else 1)
            alloc = min(cap, daily_remain, cash)
            if alloc < MIN_ALLOC:
                if daily_remain < MIN_ALLOC:
                    skipped_budget += 1
                else:
                    skipped_cash += 1
                continue
            v = sig["variants"][exit_arm]
            cash -= alloc
            daily_remain -= alloc
            ret_sum += v["ret"]
            if v["why"] == "pop_sell":
                pop_hit += 1
            if v["why"] == "horizon_expired":
                exp_n += 1
                if v["ret"] < 0:
                    exp_loss_n += 1
            open_pos.append({"code": sig["code"], "alloc": alloc,
                             "entry": sig["entry"], "exit_d": v["exit_d"],
                             "ret": v["ret"]})
            taken += 1
        mv = 0.0
        for p in open_pos:
            px = close_map.get(p["code"], {}).get(d)
            mv += p["alloc"] * ((px / p["entry"]) if px else 1.0) * (1.0 - FEES_MTM)
        equity_curve.append(cash + mv)

    n = len(equity_curve)
    final_eq = equity_curve[-1] if n else init_cash
    years = n / 252.0
    ann = (final_eq / init_cash) ** (1.0 / years) - 1.0 if years > 0 and final_eq > 0 else -1.0
    peak, maxdd = -1.0, 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            maxdd = max(maxdd, 1.0 - eq / peak)
    pnl = final_eq - init_cash
    return {
        "pnl万": round(pnl / 10000, 1), "ann%": round(ann * 100, 2),
        "maxdd%": round(maxdd * 100, 2),
        "收益回撤比": round(ann * 100 / maxdd, 1) if maxdd > 1e-9 else None,
        "taken": taken, "gate拦": skipped_gate,
        "预算堵": skipped_budget, "现金堵": skipped_cash,
        "ret均值%": round(ret_sum / max(1, taken), 3),
        "pop率%": round(pop_hit / max(1, taken) * 100, 1),
        "到期亏占比%": round(exp_loss_n / max(1, exp_n) * 100, 1) if exp_n else None,
    }


def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)

    temp_dates, sent = _load_temp()
    idx_rows = json.load(open(IDX_FP, encoding="utf-8")).get("rows") or []
    cal = sorted(set(temp_dates) | {r["date"] for r in idx_rows if r.get("date")})
    cal_pos = {d: i for i, d in enumerate(cal)}
    sent_map = dict(zip(temp_dates, sent))
    h1_days = set()
    for d in _slope5_flags(temp_dates, sent):
        if d in cal_pos:
            h1_days.update(cal[cal_pos[d]:cal_pos[d] + 3])

    meta = full_market_meta()
    board_map = universe_board_map(full_market=True)
    universe = sorted(meta.keys())
    print(f"宇宙 {len(universe)} 只；H1 窗口日 {len(h1_days)}")

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

    by_day = defaultdict(list)
    for r in (r for r in pre if board_ok(r["date"], r["board"])):
        by_day[r["date"]].append(r)
    signals_by_day = {}
    close_map = {}
    for d, lst in by_day.items():
        lst.sort(key=lambda r: -r["gap"])
        day_sigs = []
        for r in lst[:TOP4_CAP]:
            dts, o, h, l, c = rows_keep[r["code"]]
            i = r["_i"]
            entry = o[i]
            deep = r["dev20"] < DEEP_DEV20
            variants = {}
            for tag, pp in (("pop3", POP_PCT), ("pop5", 0.05)):
                ed, ep, ew = _sim_exit(dts, o, h, l, c, i, deep, pp)
                variants[tag] = {"exit_d": ed, "ret": round(_net_ret(entry, ep), 4),
                                 "why": ew}
            d_prev_i = cal_pos.get(d, 1) - 1
            tprev = sent_map.get(cal[d_prev_i]) if 0 <= d_prev_i < len(cal) else None
            day_sigs.append({"code": r["code"], "gap": r["gap"], "entry": entry,
                             "temp": tprev, "h1": d in h1_days, "variants": variants})
            if r["code"] not in close_map:
                close_map[r["code"]] = dict(zip(dts, c))
        signals_by_day[d] = day_sigs
    n_sig = sum(len(v) for v in signals_by_day.values())
    print(f"top4 信号 {n_sig} 条；开始 {len(LEVERS)} 臂 × {len(CAPITALS)} 本金仿真")

    start_d = min(signals_by_day)
    sim_cal = [d for d in cal if d >= start_d]
    out = {}
    for cap in CAPITALS:
        print(f"\n== 本金 {cap/10000:.0f} 万 ==")
        print(f"{'杠杆':<8}{'PnL万':>9}{'年化%':>8}{'回撤%':>7}{'收益回撤比':>10}"
              f"{'成交':>6}{'闸拦':>6}{'预算堵':>7}{'现金堵':>7}"
              f"{'ret均值%':>9}{'pop率%':>8}{'到期亏%':>8}")
        for (label, dcap, scap, exit_arm, tgate, gcap, h1m) in LEVERS:
            r = run_sim(signals_by_day, close_map, sim_cal, cap,
                        dcap, scap, exit_arm, tgate, gcap, h1m)
            out.setdefault(f"{cap/10000:.0f}万", {})[label] = r
            print(f"{label:<8}{r['pnl万']:>+9}{r['ann%']:>+8}{r['maxdd%']:>7}"
                  f"{(str(r['收益回撤比']) if r['收益回撤比'] else '-'):>10}"
                  f"{r['taken']:>6}{r['gate拦']:>6}{r['预算堵']:>7}{r['现金堵']:>7}"
                  f"{r['ret均值%']:>+9}{r['pop率%']:>8}"
                  f"{(str(r['到期亏占比%']) if r['到期亏占比%'] is not None else '-'):>8}")

    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"params": {"signals": n_sig, "cal_days": len(sim_cal),
                              "levers": [l[0] for l in LEVERS]},
                   "results": out}, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s；产物 {OUT_DIR}/summary.json")


if __name__ == "__main__":
    main()
