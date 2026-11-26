# -*- coding: utf-8 -*-
"""H1/H2 假说深度研究（2026-11-26 16:40 owner 指令）——第二轮细颗粒度用法。

第一轮（h1h2_lever_scan）已判死：温度入场闸（㊷）、H1 窗口加码、温度参数化
pop（㊶）、不 pop。本轮测**细用法**，全部叠加在新基线 B40+G8（日 40 万 /
单票 10 万 / gap≤8% / 本金 33 万）之上：

  Tpop     温度三层 pop：temp≤40→+2% / 40~60→+3% / >60→+5%（弱势快出、强势让利）
  Thold    弱势段（temp≤40）到期 5 日→3 日（弱势不恋战）
  H2fast   H2 窗口（极热转冷）内 pop+2% + 3 日到期（窗口内只吃余温）
  H1wide   H1 窗口（冰点转暖）内信号 top4→top6、日预算 40→60 万（黄金窗多收）
  Tbudget  弱势日的日预算 40→20 万（减量而非禁入——㊷的温和版）
  Srank    弱势日信号排序 gap 降序→dev20 升序（弱势买更深，不追跳得高的）
  Tsl3     弱势段入场止损 -5%只深 → -3%普适（弱势快止损）
  TpopHold Tpop+Thold 组合（弱势 pop2%+3日，强势 pop5%）

另附 **H7 双周期信号质量透视**（投机温度档 × 趋势温度档的纯统计，不改策略）。

产物：`t_io/validation/h1h2_deep_timing/summary.json` + 控制台矩阵。
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
OUT_DIR = os.path.join(_ROOT, "t_io", "validation", "h1h2_deep_timing")

LU_PCT = 0.095
BOARD_TOP_N = 5
DEEP_DEV20 = -0.07
TOPN_KEEP = 6                 # H1wide 需要 top6，扫描期保留 6 个/日
MIN_ALLOC = 20000.0
FEES_MTM = FEE_BUY_STOCK + FEE_SELL_STOCK
CAPITAL = 330218.75           # owner 锚定 33 万
BASE_DAILY, BASE_SINGLE, BASE_GAPCAP = 400000.0, 100000.0, 0.08

BARS = {}                     # code -> (dts,o,h,l,c)（sim_exit 用）


def _limit_of(code: str) -> float:
    return 0.195 if code[:3] in ("300", "688") else 0.095


def _net_ret(b, s):
    return (s * (1 - FEE_SELL_STOCK) - b * (1 + FEE_BUY_STOCK)) / b * 100.0


def _sim_exit(code, i, entry, deep, pop_pct, hold_days, stop_pct, stop_deep_only):
    dts, o, h, l, c = BARS[code]
    for hold in range(1, hold_days + 1):
        j = i + hold
        if j >= len(dts):
            return dts[-1], c[-1] / entry - 1.0, "open_end"
        if pop_pct is not None and hold == 1 and h[j] >= entry * (1 + pop_pct):
            px = max(entry * (1 + pop_pct), o[j])
            return dts[j], px / entry - 1.0, "pop_sell"
        if (not stop_deep_only or deep) and c[j] < entry * (1 + stop_pct):
            return dts[j], c[j] / entry - 1.0, "sl5_deep"
        if hold == hold_days:
            return dts[j], c[j] / entry - 1.0, "horizon_expired"
    return dts[-1], c[-1] / entry - 1.0, "open_end"


def _exit_for(sig, policy):
    """policy(sig) -> (pop, hold, stop, deep_only)；结果带缓存。"""
    pop, hold, stop, deep_only = policy(sig)
    key = (pop, hold, stop, deep_only)
    if key not in sig["_exits"]:
        sig["_exits"][key] = _sim_exit(sig["code"], sig["_i"], sig["entry"],
                                       sig["dev20"] < DEEP_DEV20, *key)
    return sig["_exits"][key]


# ── 各臂策略函数 ──────────────────────────────────────────────
def pol_base(sig):
    return (0.03, 5, -0.05, True)


def pol_tpop(sig):
    t = sig["temp"]
    pop = 0.02 if (t is not None and t <= 40) else (0.05 if (t is not None and t > 60) else 0.03)
    return (pop, 5, -0.05, True)


def pol_thold(sig):
    weak = sig["temp"] is not None and sig["temp"] <= 40
    return (0.03, 3 if weak else 5, -0.05, True)


def pol_h2fast(sig):
    return (0.02, 3, -0.05, True) if sig["h2"] else (0.03, 5, -0.05, True)


def pol_tsl3(sig):
    weak = sig["temp"] is not None and sig["temp"] <= 40
    return (0.03, 5, -0.03, not weak)     # 弱势段 -3% 普适止损


def pol_tpophold(sig):
    t = sig["temp"]
    if t is not None and t <= 40:
        return (0.02, 3, -0.05, True)
    if t is not None and t > 60:
        return (0.05, 5, -0.05, True)
    return (0.03, 5, -0.05, True)


def pol_tcombo(sig):
    """TpopHold + Tsl3 组合：弱势 pop2%+3日+SL3普适；强势 pop5%。"""
    t = sig["temp"]
    if t is not None and t <= 40:
        return (0.02, 3, -0.03, False)
    if t is not None and t > 60:
        return (0.05, 5, -0.05, True)
    return (0.03, 5, -0.05, True)


def pol_tcombo2(sig):
    """只留弱势快字诀，强势不动：弱势 pop2%+3日+SL3；其余 pop3。"""
    t = sig["temp"]
    if t is not None and t <= 40:
        return (0.02, 3, -0.03, False)
    return (0.03, 5, -0.05, True)


ARMS = [
    # (标签, policy, 日预算fn(day_temp,h1), topn_fn(day_temp,h1), 弱势日改排序)
    ("base",    pol_base,     lambda t, h1: BASE_DAILY, lambda t, h1: 4, False),
    ("Tpop",    pol_tpop,     lambda t, h1: BASE_DAILY, lambda t, h1: 4, False),
    ("Thold",   pol_thold,    lambda t, h1: BASE_DAILY, lambda t, h1: 4, False),
    ("H2fast",  pol_h2fast,   lambda t, h1: BASE_DAILY, lambda t, h1: 4, False),
    ("H1wide",  pol_base,     lambda t, h1: 600000.0 if h1 else BASE_DAILY,
     lambda t, h1: 6 if h1 else 4, False),
    ("Tbudget", pol_base,     lambda t, h1: 200000.0 if (t is not None and t <= 40) else BASE_DAILY,
     lambda t, h1: 4, False),
    ("Srank",   pol_base,     lambda t, h1: BASE_DAILY, lambda t, h1: 4, True),
    ("Tsl3",    pol_tsl3,     lambda t, h1: BASE_DAILY, lambda t, h1: 4, False),
    ("TpopHold", pol_tpophold, lambda t, h1: BASE_DAILY, lambda t, h1: 4, False),
    ("Tcombo",  pol_tcombo,   lambda t, h1: BASE_DAILY, lambda t, h1: 4, False),
    ("Tcombo2", pol_tcombo2,  lambda t, h1: BASE_DAILY, lambda t, h1: 4, False),
]


def run_arm(day_map, cal, close_map, policy, daily_fn, topn_fn, weak_resort):
    cash = CAPITAL
    open_pos = []
    equity_curve = []
    taken = skip_cash = skip_budget = 0
    ret_sum = 0.0
    pop_hit = exp_loss = exp_all = 0

    for d in cal:
        if open_pos:
            still = []
            for p in open_pos:
                if p["exit_d"] <= d:
                    cash += p["alloc"] * (1.0 + p["ret"] / 100.0)
                else:
                    still.append(p)
            open_pos = still
        day_sigs = day_map.get(d)
        if day_sigs:
            day_temp, day_h1 = day_sigs[0]["temp"], day_sigs[0]["h1"]
            lst = day_sigs[:topn_fn(day_temp, day_h1)]
            if weak_resort and day_temp is not None and day_temp <= 40:
                lst = sorted(lst, key=lambda s: s["dev20"])   # 更深的优先
            daily_remain = daily_fn(day_temp, day_h1)
            for sig in lst:
                if any(p["code"] == sig["code"] for p in open_pos):
                    continue
                alloc = min(BASE_SINGLE, daily_remain, cash)
                if alloc < MIN_ALLOC:
                    if daily_remain < MIN_ALLOC:
                        skip_budget += 1
                    else:
                        skip_cash += 1
                    continue
                exit_d, gross, why = _exit_for(sig, policy)
                ret = _net_ret(sig["entry"], sig["entry"] * (1.0 + gross))
                cash -= alloc
                daily_remain -= alloc
                ret_sum += ret
                pop_hit += (why == "pop_sell")
                exp_all += (why == "horizon_expired")
                exp_loss += (why == "horizon_expired" and ret < 0)
                open_pos.append({"code": sig["code"], "alloc": alloc,
                                 "entry": sig["entry"], "exit_d": exit_d, "ret": ret})
                taken += 1
        mv = 0.0
        for p in open_pos:
            px = close_map.get(p["code"], {}).get(d)
            mv += p["alloc"] * ((px / p["entry"]) if px else 1.0) * (1.0 - FEES_MTM)
        equity_curve.append(cash + mv)

    n = len(equity_curve)
    final_eq = equity_curve[-1] if n else CAPITAL
    years = n / 252.0
    ann = (final_eq / CAPITAL) ** (1.0 / years) - 1.0 if years > 0 and final_eq > 0 else -1.0
    peak, maxdd = -1.0, 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            maxdd = max(maxdd, 1.0 - eq / peak)
    pnl万 = (final_eq - CAPITAL) / 10000.0
    return {"pnl万": round(pnl万, 1), "ann%": round(ann * 100, 2),
            "maxdd%": round(maxdd * 100, 2),
            "收益回撤比": round(ann * 100 / maxdd, 1) if maxdd > 1e-9 else None,
            "taken": taken, "预算堵": skip_budget, "现金堵": skip_cash,
            "ret均值%": round(ret_sum / max(1, taken), 3),
            "pop率%": round(pop_hit / max(1, taken) * 100, 1),
            "到期亏占比%": round(exp_loss / max(1, exp_all) * 100, 1) if exp_all else None}


def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)

    raw_temp = json.load(open(TEMP_FP, encoding="utf-8"))
    temp_dates = sorted(raw_temp.keys())
    sent = [raw_temp[d].get("sent_ma") for d in temp_dates]
    t2 = [raw_temp[d].get("t2_ma") for d in temp_dates]
    idx_rows = json.load(open(IDX_FP, encoding="utf-8")).get("rows") or []
    cal = sorted(set(temp_dates) | {r["date"] for r in idx_rows if r.get("date")})
    cal_pos = {d: i for i, d in enumerate(cal)}
    sent_map = dict(zip(temp_dates, sent))
    t2_map = dict(zip(temp_dates, t2))

    # H1/H2 触发窗口（slope5 转向 + 近5日极值足迹）
    trig = {}
    for i in range(6, len(temp_dates)):
        w = sent[i - 5:i]
        if any(v is None for v in w) or sent[i] is None or sent[i - 6] is None:
            continue
        sl_prev, sl_now = sent[i - 1] - sent[i - 6], sent[i] - sent[i - 5]
        if sl_prev <= 0 < sl_now and min(w) <= 25:
            trig[temp_dates[i]] = "H1"
        elif sl_prev >= 0 > sl_now and max(w) >= 60:
            trig[temp_dates[i]] = "H2"
    h1_days, h2_days = set(), set()
    for d, k in trig.items():
        if d in cal_pos:
            (h1_days if k == "H1" else h2_days).update(cal[cal_pos[d]:cal_pos[d] + 3])
    print(f"H1 窗口日 {len(h1_days)} / H2 窗口日 {len(h2_days)}")

    meta = full_market_meta()
    board_map = universe_board_map(full_market=True)

    # —— 信号扫描（top6 保留） ——
    lu_cnt = defaultdict(Counter)
    pre = []
    for code in sorted(meta.keys()):
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
        kept = False
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
            kept = True
        if kept:
            BARS[code] = (dts, o, h, l, c)
    print(f"扫描 {time.time()-t0:.0f}s：粗候选 {len(pre)} 条")

    def board_ok(date, board):
        i = cal_pos.get(date)
        if i is None or i < 1 or not board:
            return False
        top = [b for b, _ in lu_cnt.get(cal[i - 1], Counter()).most_common(BOARD_TOP_N)]
        return board in top

    day_map = {}
    close_map = {}
    for r in (r for r in pre if board_ok(r["date"], r["board"])):
        day_map.setdefault(r["date"], []).append(r)
    n_sig = 0
    for d, lst in day_map.items():
        lst.sort(key=lambda r: -r["gap"])
        keep = []
        for r in lst[:TOPN_KEEP]:
            if r["gap"] > BASE_GAPCAP:      # G8 已并入基线
                continue
            d_prev_i = cal_pos.get(d, 1) - 1
            prev_d = cal[d_prev_i] if 0 <= d_prev_i < len(cal) else None
            r["temp"] = sent_map.get(prev_d)
            r["t2"] = t2_map.get(prev_d)
            r["h1"] = d in h1_days
            r["h2"] = d in h2_days
            r["entry"] = BARS[r["code"]][1][r["_i"]]
            r["_exits"] = {}
            keep.append(r)
            n_sig += 1
            if r["code"] not in close_map:
                dts_c = BARS[r["code"]][0]
                close_map[r["code"]] = dict(zip(dts_c, BARS[r["code"]][4]))
        day_map[d] = keep
    print(f"信号 {n_sig} 条（top≤6 + G8 过滤后）")

    sim_cal = [d for d in cal if d >= min(day_map)]
    out = {}
    print(f"\n== H1/H2 细用法矩阵（基线 B40+G8，本金 33 万） ==")
    print(f"{'臂':<9}{'PnL万':>9}{'年化%':>8}{'回撤%':>7}{'收益回撤比':>10}"
          f"{'成交':>6}{'预算堵':>7}{'现金堵':>7}{'ret均值%':>9}{'pop率%':>8}{'到期亏%':>8}")
    for label, pol, dfn, tfn, wr in ARMS:
        r = run_arm(day_map, sim_cal, close_map, pol, dfn, tfn, wr)
        out[label] = r
        print(f"{label:<9}{r['pnl万']:>+9}{r['ann%']:>+8}{r['maxdd%']:>7}"
              f"{(str(r['收益回撤比']) if r['收益回撤比'] else '-'):>10}"
              f"{r['taken']:>6}{r['预算堵']:>7}{r['现金堵']:>7}"
              f"{r['ret均值%']:>+9}{r['pop率%']:>8}"
              f"{(str(r['到期亏占比%']) if r['到期亏占比%'] is not None else '-'):>8}")

    # —— H7 双周期透视：投机温度档 × 趋势温度档（基线出场 pop3） ——
    def band(t):
        if t is None:
            return "无"
        if t <= 20:
            return "≤20"
        if t <= 40:
            return "20~40"
        if t <= 60:
            return "40~60"
        if t <= 70:
            return "60~70"
        return "≥70"

    pivot = defaultdict(list)
    for d, lst in day_map.items():
        for sig in lst[:4]:              # 生产 top4 口径
            exit_d, gross, why = _exit_for(sig, pol_base)
            ret = _net_ret(sig["entry"], sig["entry"] * (1.0 + gross))
            pivot[(band(sig["temp"]), band(sig["t2"]))].append(ret)
    bands = ("≤20", "20~40", "40~60", "60~70", "≥70", "无")
    print("\n== H7 双周期透视：信号费后均值%（行=投机温度，列=趋势温度，n≥30） ==")
    print(f"{'投机↓/趋势→':<14}" + "".join(f"{b:>12}" for b in bands))
    h7 = {}
    for bs in bands:
        row = f"{bs:<14}"
        for bt in bands:
            rs = pivot.get((bs, bt), [])
            if len(rs) >= 30:
                m = sum(rs) / len(rs)
                h7[f"{bs}x{bt}"] = {"n": len(rs), "mean": round(m, 3)}
                row += f"{m:>+8.2f}/{len(rs):<4d}"
            else:
                row += f"{'-':>12}"
        print(row)
    out["_h7_pivot"] = h7

    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"params": {"capital": CAPITAL, "daily": BASE_DAILY,
                              "single": BASE_SINGLE, "gap_cap": BASE_GAPCAP,
                              "signals": n_sig}, "results": out},
                  f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s；产物 {OUT_DIR}/summary.json")


if __name__ == "__main__":
    main()
