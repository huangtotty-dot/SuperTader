# -*- coding: utf-8 -*-
"""regime 闸门 A/B 对照（2026-11-26 owner 指令）：大盘 uni_down 熔断买侧，帮忙还是帮倒忙？

生产口径（gm_main.py:1087 起 / hooks.py:566）：每交易日 morning 检测大盘 regime，
`last_index_regime == "uni_down"` ⇒ 当日零 BUY（不读 10:00 个股行情、不补单）。
历史序列：t_io/index_regime/traces/index_regime_*.jsonl（morning 优先、eod 兜底，
2026-08-12 ~ 2026-11-26 共 71 个交易日；与 state.json history 对账 0 不一致）。

实验窗口 = 有 regime 记录的 71 天。窗口外无生产口径序列（离线复刻需重放
QVIX/宽度/涨跌停池等输入，超范围），故本实验只回答「最近 3 个月这个闸门的
真实反事实」，不外推。

两臂（其余完全相同 = 生产 pack_v2：B40 日 40 万 / 单票 10 万 / G8 gap≤8% /
Tcombo 温度分层出场 / 本金 33 万）：
  闸OFF   每天 top4 照买（反事实：如果没闸门）
  闸ON    uni_down 日零 BUY（生产行为）
附加对照：
  逆闸    只在 uni_down 日买、其余日不买（直接暴露被闸信号的事后质量）

另输出：按当日 regime 分组的信号质量表（uni_down/range/uni_up 日子的信号
费后收益均值/pop率/到期亏占比）——闸门合理性的最直接证据。

产物：t_io/validation/regime_gate_ab/summary.json + 控制台矩阵。
"""
from __future__ import annotations

import glob
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
TRACE_GLOB = os.path.join(_ROOT, "t_io", "index_regime", "traces", "index_regime_*.jsonl")
CACHE_DIR = os.path.join(_ROOT, "t_io", "cache", "daily_kline")
IDX_FP = os.path.join(CACHE_DIR, "index_sh000300.json")
OUT_DIR = os.path.join(_ROOT, "t_io", "validation", "regime_gate_ab")

LU_PCT = 0.095
BOARD_TOP_N = 5
TOPN_KEEP = 6
MIN_ALLOC = 20000.0
FEES_MTM = FEE_BUY_STOCK + FEE_SELL_STOCK
CAPITAL = 330218.75
BASE_DAILY, BASE_SINGLE, BASE_GAPCAP = 400000.0, 100000.0, 0.08

BARS = {}


def _limit_of(code: str) -> float:
    return 0.195 if code[:3] in ("300", "688") else 0.095


def _net_ret(b, s):
    return (s * (1 - FEE_SELL_STOCK) - b * (1 + FEE_BUY_STOCK)) / b * 100.0


def _sim_exit(code, i, entry, pop_pct, hold_days, stop_pct, stop_universal):
    dts, o, h, l, c = BARS[code]
    for hold in range(1, hold_days + 1):
        j = i + hold
        if j >= len(dts):
            return dts[-1], c[-1] / entry - 1.0, "open_end"
        if pop_pct is not None and hold == 1 and h[j] >= entry * (1 + pop_pct):
            px = max(entry * (1 + pop_pct), o[j])
            return dts[j], px / entry - 1.0, "pop_sell"
        if stop_universal and c[j] < entry * (1 + stop_pct):
            return dts[j], c[j] / entry - 1.0, "sl_universal"
        if hold == hold_days:
            return dts[j], c[j] / entry - 1.0, "horizon_expired"
    return dts[-1], c[-1] / entry - 1.0, "open_end"


def pol_tcombo(sig):
    """生产 Tcombo（executor.layered_exit_params 同口径）：
    temp≤40 → pop+2%/3日/-3%普适止损；temp>60 → pop+5%；其余 pop+3%/5日/-5%深票。"""
    t = sig["temp"]
    if t is not None and t <= 40:
        return (0.02, 3, -0.03, True)
    if t is not None and t > 60:
        return (0.05, 5, -0.05, False)
    return (0.03, 5, -0.05, False)


def _exit_for(sig):
    key = pol_tcombo(sig)
    if key not in sig["_exits"]:
        sig["_exits"][key] = _sim_exit(sig["code"], sig["_i"], sig["entry"], *key)
    return sig["_exits"][key]


def load_regime_map():
    """traces → {date: regime}，morning 优先（生产买时口径），eod 兜底。"""
    reg = {}
    for fp in glob.glob(TRACE_GLOB):
        day = os.path.basename(fp)[len("index_regime_"):len("index_regime_") + 10]
        morn = eod = None
        try:
            for ln in open(fp, encoding="utf-8"):
                r = json.loads(ln)
                if r.get("mode") == "morning" and morn is None:
                    morn = r
                elif r.get("mode") == "eod":
                    eod = r
        except Exception:
            continue
        pick = morn or eod
        if pick and pick.get("regime") in ("range", "uni_up", "uni_down"):
            reg[day] = pick["regime"]
    return reg


# 闸门模式：OFF=全买 / ON=uni_down日不买（生产） / REV=只在uni_down日买
def run_arm(day_map, cal, close_map, gate_mode, regime_map):
    cash = CAPITAL
    open_pos = []
    equity_curve = []
    taken = skip_cash = skip_budget = skip_gate = 0
    ret_sum = 0.0
    pop_hit = exp_loss = exp_all = 0
    blocked_rets = []          # 闸ON 被跳过信号的事后费后收益（反事实）

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
            rg = regime_map.get(d)
            gate_block = (gate_mode == "ON" and rg == "uni_down") or \
                         (gate_mode == "REV" and rg != "uni_down")
            lst = day_sigs[:4]
            if gate_block:
                skip_gate += len(lst)
                if gate_mode == "ON":   # 记录被闸信号的事后质量
                    for sig in lst:
                        exit_d, gross, why = _exit_for(sig)
                        blocked_rets.append({"date": d, "code": sig["code"],
                                             "ret": _net_ret(sig["entry"], sig["entry"] * (1.0 + gross)),
                                             "why": why})
            else:
                daily_remain = BASE_DAILY
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
                    exit_d, gross, why = _exit_for(sig)
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
    peak, maxdd = -1.0, 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            maxdd = max(maxdd, 1.0 - eq / peak)
    pnl万 = (final_eq - CAPITAL) / 10000.0
    out = {"pnl万": round(pnl万, 2), "maxdd%": round(maxdd * 100, 2),
           "taken": taken, "gate_skip": skip_gate,
           "预算堵": skip_budget, "现金堵": skip_cash,
           "ret均值%": round(ret_sum / max(1, taken), 3),
           "pop率%": round(pop_hit / max(1, taken) * 100, 1),
           "到期亏占比%": round(exp_loss / max(1, exp_all) * 100, 1) if exp_all else None}
    if blocked_rets:
        brs = [b["ret"] for b in blocked_rets]
        out["被闸信号"] = {"n": len(brs), "费后均值%": round(sum(brs) / len(brs), 3),
                          "中位%": round(sorted(brs)[len(brs) // 2], 3),
                          "pop率%": round(sum(1 for b in blocked_rets if b["why"] == "pop_sell") / len(brs) * 100, 1),
                          "明细": blocked_rets}
    return out


def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)

    regime_map = load_regime_map()
    win_lo, win_hi = min(regime_map), max(regime_map)
    rg_cnt = Counter(regime_map.values())
    print(f"regime 覆盖 {len(regime_map)} 天 [{win_lo} ~ {win_hi}]：{dict(rg_cnt)}")

    raw_temp = json.load(open(TEMP_FP, encoding="utf-8"))
    temp_dates = sorted(raw_temp.keys())
    sent_map = {d: raw_temp[d].get("sent_ma") for d in temp_dates}
    idx_rows = json.load(open(IDX_FP, encoding="utf-8")).get("rows") or []
    cal = sorted(set(temp_dates) | {r["date"] for r in idx_rows if r.get("date")})
    cal_pos = {d: i for i, d in enumerate(cal)}

    meta = full_market_meta()
    board_map = universe_board_map(full_market=True)

    # —— 信号扫描（与 h1h2_deep_timing 同口径：粗候选→板块过滤→top6→G8） ——
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
            if r["gap"] > BASE_GAPCAP:
                continue
            d_prev_i = cal_pos.get(d, 1) - 1
            prev_d = cal[d_prev_i] if 0 <= d_prev_i < len(cal) else None
            r["temp"] = sent_map.get(prev_d)
            r["entry"] = BARS[r["code"]][1][r["_i"]]
            r["_exits"] = {}
            keep.append(r)
            n_sig += 1
            if r["code"] not in close_map:
                dts_c = BARS[r["code"]][0]
                close_map[r["code"]] = dict(zip(dts_c, BARS[r["code"]][4]))
        day_map[d] = keep

    # 限窗：只保留 regime 记录窗口内的信号日
    win_map = {d: v for d, v in day_map.items() if win_lo <= d <= win_hi and v}
    sim_cal = [d for d in cal if win_lo <= d <= win_hi]
    sig_days = sorted(win_map)
    win_sig = sum(len(v) for v in win_map.values())
    print(f"窗口内信号 {win_sig} 条 / 信号日 {len(sig_days)} 天")

    # —— 三臂对照 ——
    print(f"\n== regime 闸门 A/B（pack_v2 基线，窗口 {win_lo}~{win_hi}，本金 33 万） ==")
    print(f"{'臂':<8}{'PnL万':>9}{'回撤%':>7}{'成交':>6}{'闸跳':>6}{'ret均值%':>9}{'pop率%':>8}{'到期亏%':>8}")
    out = {"window": [win_lo, win_hi], "regime_days": dict(rg_cnt),
           "signals_in_window": win_sig, "results": {}}
    for mode, label in (("OFF", "闸OFF"), ("ON", "闸ON"), ("REV", "逆闸")):
        r = run_arm(win_map, sim_cal, close_map, mode, regime_map)
        out["results"][label] = r
        print(f"{label:<8}{r['pnl万']:>+9}{r['maxdd%']:>7}{r['taken']:>6}{r['gate_skip']:>6}"
              f"{r['ret均值%']:>+9}{r['pop率%']:>8}"
              f"{(str(r['到期亏占比%']) if r['到期亏占比%'] is not None else '-'):>8}")

    # —— 按当日 regime 分组的信号质量表（Tcombo 出场，top4 口径） ——
    pivot = defaultdict(list)
    for d, lst in win_map.items():
        rg = regime_map.get(d, "无记录")
        for sig in lst[:4]:
            exit_d, gross, why = _exit_for(sig)
            pivot[rg].append((_net_ret(sig["entry"], sig["entry"] * (1.0 + gross)), why))
    print("\n== 信号质量 × 当日 regime（费后，top4 口径） ==")
    print(f"{'regime':<10}{'n':>5}{'均值%':>8}{'中位%':>8}{'胜率%':>7}{'pop率%':>8}{'到期亏%':>8}")
    qual = {}
    for rg in ("uni_down", "range", "uni_up", "无记录"):
        rs = pivot.get(rg, [])
        if not rs:
            continue
        rets = [x[0] for x in rs]
        n = len(rets)
        mean = sum(rets) / n
        med = sorted(rets)[n // 2]
        win = sum(1 for x in rets if x > 0) / n * 100
        pop = sum(1 for x in rs if x[1] == "pop_sell") / n * 100
        exps = [x for x in rs if x[1] == "horizon_expired"]
        exp_loss = sum(1 for x in exps if x[0] < 0)
        exp_pct = round(exp_loss / len(exps) * 100, 1) if exps else None
        qual[rg] = {"n": n, "mean%": round(mean, 3), "med%": round(med, 3),
                    "win%": round(win, 1), "pop%": round(pop, 1), "到期亏占比%": exp_pct}
        print(f"{rg:<10}{n:>5}{mean:>+8.2f}{med:>+8.2f}{win:>7.1f}{pop:>8.1f}"
              f"{(str(exp_pct) if exp_pct is not None else '-'):>8}")
    out["signal_quality_by_regime"] = qual

    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s；产物 {OUT_DIR}/summary.json")


if __name__ == "__main__":
    main()
