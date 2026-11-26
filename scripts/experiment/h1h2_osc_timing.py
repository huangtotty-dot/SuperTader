# -*- coding: utf-8 -*-
"""H1/H2 周期特性 × OSC 择时改善实验（2026-11-26 owner 立项）。

研究问题：OSC 现行信号的收益结构是否随**入场时的情绪周期状态**显著变化？
若变化显著 ⇒ 温度状态可成为 OSC 的入场过滤器/出场参数（改善收益的路径）。

## 设计

**信号重建（OSC 日线近似口径，与 h4_d2 B 臂一致 + 补板块替身）**：
  · 预筛（T-1 收盘定）：昨收 < MA20（超跌）∧ 所属板块 ∈ 昨日「涨幅≥9.5% 家数」top-5
  · 确认（T 日）：open>昨收 ∧ low≥昨收（缺口守住）∧ close>昨收（站上昨收）
    —— VWAP 条件日线不可复刻，省略（披露）；一字涨停剔除
  · 入场：T 日开盘价（生产为 10:00 棒收盘，近似——披露）
  · top4 标记：每日确认候选按 gap% 降序 ≤4 打标（生产 top-4 口径），统计分列
**出场（同 D2）**：次日 pop+3%（卖在 max(1.03×入场, open)）/ SL5 只深（dev20<−7% 且
  收盘<入场×0.95 → 收盘卖）/ 满 5 日到期收盘卖。
**温度分桶（T-1 收盘的 sent_ma，入场前可知，无前视）**：
  · 档位：≤20 冰点 / 20~40 弱势 / 40~60 平衡 / 60~70 强势 / ≥70 极热
  · H1 窗口（冰点转暖早期）：slope5 负转正 ∧ 近5日 min≤25 的触发日起 3 日
  · H2 窗口（极热降温早期）：slope5 正转负 ∧ 近5日 max≥60 的触发日起 3 日
**出场择时附加测量**：pop 卖出的票，记录卖后 T+1→T+5 的「卖飞幅度」
  （close(T+5)/pop卖价 −1）——若 H1 窗口内卖飞幅度显著大 ⇒ 「冰点段别急着 +3% 跑」
  的证据（直接指向 pop 目标价参数化）。

产物：`t_io/validation/h1h2_osc_timing/{trades.csv, summary.json}` + 控制台分桶表。
"""
from __future__ import annotations

import bisect
import csv
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
OUT_DIR = os.path.join(_ROOT, "t_io", "validation", "h1h2_osc_timing")

LU_PCT = 0.095          # 板块热度：涨幅 ≥9.5% 计涨停（exp12 替身口径）
BOARD_TOP_N = 5
POP_PCT, STOP_PCT, DEEP_DEV20, HOLD_DAYS = 0.03, -0.05, -0.07, 5
TOP4_CAP = 4


def _limit_of(code: str) -> float:
    return 0.195 if code[:3] in ("300", "688") else 0.095


def _net_ret(b, s):
    return (s * (1 - FEE_SELL_STOCK) - b * (1 + FEE_BUY_STOCK)) / b * 100.0


def _load_temp():
    raw = json.load(open(TEMP_FP, encoding="utf-8"))
    dates = sorted(raw.keys())
    sent = [raw[d].get("sent_ma") for d in dates]
    return dates, sent


def _slope5_flags(dates, sent):
    """H1/H2 触发日（slope5 转向 ∧ 近5日极值足迹），返回 {date: 'H1'/'H2'}。"""
    flags = {}
    for i in range(6, len(dates)):
        w = sent[i - 5:i]              # 近5日（不含今日）
        if any(v is None for v in w) or sent[i] is None or sent[i - 6] is None:
            continue
        sl_prev = sent[i - 1] - sent[i - 6]   # slope5：今值 − 5日前值
        sl_now = sent[i] - sent[i - 5]
        if sl_prev <= 0 < sl_now and min(w) <= 25:
            flags[dates[i]] = "H1"
        elif sl_prev >= 0 > sl_now and max(w) >= 60:
            flags[dates[i]] = "H2"
    return flags


def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)

    temp_dates, sent = _load_temp()
    idx_rows = json.load(open(IDX_FP, encoding="utf-8")).get("rows") or []
    cal = sorted(set(temp_dates) | {r["date"] for r in idx_rows if r.get("date")})
    cal_pos = {d: i for i, d in enumerate(cal)}
    sent_map = dict(zip(temp_dates, sent))
    trig = _slope5_flags(temp_dates, sent)
    # H1/H2 窗口 = 触发日起 3 个交易日
    h1_days, h2_days = set(), set()
    for d, k in trig.items():
        i = cal_pos.get(d)
        if i is None:
            continue
        for d2 in cal[i:i + 3]:
            (h1_days if k == "H1" else h2_days).add(d2)
    print(f"温度触发：H1 {sum(1 for k in trig.values() if k=='H1')} 次 / "
          f"H2 {sum(1 for k in trig.values() if k=='H2')} 次；"
          f"H1窗口日 {len(h1_days)} / H2窗口日 {len(h2_days)}")

    meta = full_market_meta()
    board_map = universe_board_map(full_market=True)
    universe = sorted(meta.keys())
    print(f"宇宙 {len(universe)} 只")

    # —— 单遍扫描：①板块每日涨停家数 ②超跌+跳空候选 ——
    lu_cnt = defaultdict(Counter)          # date -> Counter(board)
    pre = []                               # 粗候选
    rows_keep = {}                         # code -> (dts,o,h,l,c)，仅留有候选的票（控内存）
    files_ok = 0
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
        files_ok += 1
        dts = [r["date"] for r in rows]
        o = [float(r["open"]) for r in rows]
        h = [float(r["high"]) for r in rows]
        l = [float(r["low"]) for r in rows]
        c = [float(r["close"]) for r in rows]
        board = board_map.get(code, "")
        mine = []
        for i in range(21, len(rows)):
            d = dts[i]
            if d < "2023-08-24":
                continue
            # 板块涨停计数（当日涨幅挂当日；预筛时用「信号日前一日」的排名）
            chg = c[i] / c[i - 1] - 1.0
            if board and chg >= LU_PCT:
                lu_cnt[d][board] += 1
            # 候选判定：昨收<MA20 ∧ 今日跳空守住+站上昨收
            ma20_prev = sum(c[i - 20:i]) / 20.0
            if not (c[i - 1] < ma20_prev):
                continue
            if not (o[i] > c[i - 1] and l[i] >= c[i - 1] and c[i] > c[i - 1]):
                continue
            if o[i] == h[i] == l[i] and chg >= _limit_of(code):
                continue                      # 一字涨停买不进
            mine.append({"date": d, "code": code, "board": board,
                         "dev20": c[i - 1] / ma20_prev - 1.0,
                         "gap": o[i] / c[i - 1] - 1.0,
                         "_i": i})
        if mine:
            rows_keep[code] = (dts, o, h, l, c)
            pre.extend(mine)
    print(f"扫描完成 {time.time()-t0:.0f}s（文件 {files_ok}）：粗候选 {len(pre)} 条，"
          f"涉及 {len(rows_keep)} 只")

    # —— 板块 top-5 过滤（用信号日前一交易日的涨停家数排名） ——
    def board_ok(date, board):
        i = cal_pos.get(date)
        if i is None or i < 1 or not board:
            return False
        prev = cal[i - 1]
        top = [b for b, _ in lu_cnt.get(prev, Counter()).most_common(BOARD_TOP_N)]
        return board in top

    cands = [r for r in pre if board_ok(r["date"], r["board"])]
    print(f"板块 top-5 过滤后候选 {len(cands)} 条")

    # —— top4 标记（每日按 gap 降序） ——
    by_day = defaultdict(list)
    for r in cands:
        by_day[r["date"]].append(r)
    for d, lst in by_day.items():
        lst.sort(key=lambda r: -r["gap"])
        for rank, r in enumerate(lst):
            r["top4"] = rank < TOP4_CAP

    # —— 出场仿真（pop 目标参数化：0.03 现行 / 0.05 变体 / None 不 pop） ——
    def sim_exit(dts, o, h, l, c, i, deep, pop_pct):
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

    trades = []
    variants = {}   # (date,code) -> {pop3: ret, pop5: ret, nopop: ret}
    for r in cands:
        dts, o, h, l, c = rows_keep[r["code"]]
        i = r["_i"]
        entry = o[i]
        deep = r["dev20"] < DEEP_DEV20
        exit_d, exit_px, why = sim_exit(dts, o, h, l, c, i, deep, POP_PCT)
        # 变体：pop+5% / 不 pop（同样 SL5+到期纪律）
        v = {}
        for tag, pp in (("pop3", POP_PCT), ("pop5", 0.05), ("nopop", None)):
            ed, ep, ew = sim_exit(dts, o, h, l, c, i, deep, pp)
            v[tag] = {"ret": round(_net_ret(entry, ep), 4), "exit": ew}
        variants[(r["date"], r["code"])] = v
        # 卖飞幅度（pop 卖出售后第 5 日收盘 vs 卖价；数据不足记空）
        fly = None
        if why == "pop_sell":
            j = bisect.bisect_left(dts, exit_d)
            if j + 4 < len(dts):
                fly = (c[j + 4] / exit_px - 1.0) * 100.0
        d_prev_i = cal_pos.get(r["date"], 1) - 1
        tprev = sent_map.get(cal[d_prev_i]) if 0 <= d_prev_i < len(cal) else None
        trades.append({
            "date": r["date"], "code": r["code"], "board": r["board"],
            "dev20": round(r["dev20"], 4), "gap": round(r["gap"], 4),
            "top4": int(r["top4"]),
            "temp": tprev,
            "h1_win": int(r["date"] in h1_days), "h2_win": int(r["date"] in h2_days),
            "exit": why, "ret_net": round(_net_ret(entry, exit_px), 4),
            "fly5": (round(fly, 3) if fly is not None else ""),
        })

    # —— 分桶统计 ——
    def bucket(t):
        if t is None:
            return "无数据"
        if t <= 20:
            return "≤20冰点"
        if t <= 40:
            return "20~40弱势"
        if t <= 60:
            return "40~60平衡"
        if t <= 70:
            return "60~70强势"
        return "≥70极热"

    def agg(rows):
        rs = [x["ret_net"] for x in rows if x["exit"] != "open_end"]
        if not rs:
            return None
        pops = [x for x in rows if x["exit"] == "pop_sell"]
        flys = [x["fly5"] for x in pops if x["fly5"] != ""]
        return {
            "n": len(rs),
            "mean": round(sum(rs) / len(rs), 3),
            "win": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1),
            "pop率": round(len(pops) / len(rs) * 100, 1),
            "到期亏损占比": round(
                sum(1 for x in rows if x["exit"] == "horizon_expired" and x["ret_net"] < 0)
                / max(1, sum(1 for x in rows if x["exit"] == "horizon_expired")) * 100, 1),
            "止损占比": round(
                sum(1 for x in rows if x["exit"] == "sl5_deep") / len(rs) * 100, 1),
            "pop卖飞均值": round(sum(flys) / len(flys), 2) if flys else None,
        }

    groups = defaultdict(list)
    for t in trades:
        groups[bucket(t["temp"])].append(t)
    print("\n== 温度档位分桶（全部候选） ==")
    print(f"{'桶':<10}{'n':>7}{'均值%':>8}{'胜率%':>7}{'pop率%':>8}{'到期亏%':>8}"
          f"{'止损%':>7}{'卖飞%':>8}")
    summary = {"by_temp": {}, "by_h1h2": {}, "top4_by_bucket": {}}
    for b in ("≤20冰点", "20~40弱势", "40~60平衡", "60~70强势", "≥70极热", "无数据"):
        a = agg(groups.get(b, []))
        if not a:
            continue
        summary["by_temp"][b] = a
        print(f"{b:<10}{a['n']:>7}{a['mean']:>+8.3f}{a['win']:>7}{a['pop率']:>8}"
              f"{a['到期亏损占比']:>8}{a['止损占比']:>7}"
              f"{(str(a['pop卖飞均值']) if a['pop卖飞均值'] is not None else '-'):>8}")

    for label, rows in (("H1窗口", [t for t in trades if t["h1_win"]]),
                        ("H2窗口", [t for t in trades if t["h2_win"]]),
                        ("非窗口", [t for t in trades
                                   if not t["h1_win"] and not t["h2_win"]])):
        a = agg(rows)
        if not a:
            continue
        summary["by_h1h2"][label] = a
        print(f"{label:<10}{a['n']:>7}{a['mean']:>+8.3f}{a['win']:>7}{a['pop率']:>8}"
              f"{a['到期亏损占比']:>8}{a['止损占比']:>7}"
              f"{(str(a['pop卖飞均值']) if a['pop卖飞均值'] is not None else '-'):>8}")

    print("\n== 温度档位 × 仅 top4（生产口径） ==")
    for b in ("≤20冰点", "20~40弱势", "40~60平衡", "60~70强势", "≥70极热"):
        a = agg([t for t in groups.get(b, []) if t["top4"]])
        if not a:
            continue
        summary["top4_by_bucket"][b] = a
        print(f"{b:<10}{a['n']:>7}{a['mean']:>+8.3f}{a['win']:>7}{a['pop率']:>8}"
              f"{a['到期亏损占比']:>8}{a['止损占比']:>7}"
              f"{(str(a['pop卖飞均值']) if a['pop卖飞均值'] is not None else '-'):>8}")

    # —— 出场变体对照（pop+3% 现行 / pop+5% / 不 pop） ——
    print("\n== 出场变体对照（费后均值%，全部候选） ==")
    print(f"{'桶':<10}{'n':>7}{'pop3现行':>10}{'pop5':>8}{'不pop':>8}")

    def var_line(rows_):
        out = {}
        for tag in ("pop3", "pop5", "nopop"):
            rets = [variants[(t["date"], t["code"])][tag]["ret"] for t in rows_]
            out[tag] = round(sum(rets) / len(rets), 3) if rets else None
        return out

    var_summary = {}
    for b in ("≤20冰点", "20~40弱势", "40~60平衡", "60~70强势", "≥70极热"):
        rows_b = groups.get(b, [])
        if not rows_b:
            continue
        vl = var_line(rows_b)
        var_summary[b] = {"n": len(rows_b), **vl}
        print(f"{b:<10}{len(rows_b):>7}{vl['pop3']:>+10}{vl['pop5']:>+8}{vl['nopop']:>+8}")
    for label, rows_ in (("H1窗口", [t for t in trades if t["h1_win"]]),
                         ("H2窗口", [t for t in trades if t["h2_win"]]),
                         ("全部", trades)):
        vl = var_line(rows_)
        var_summary[label] = {"n": len(rows_), **vl}
        print(f"{label:<10}{len(rows_):>7}{vl['pop3']:>+10}{vl['pop5']:>+8}{vl['nopop']:>+8}")
    summary["exit_variants"] = var_summary

    # —— 变体尾部风险（p5 / 最差 / 到期亏损占比） ——
    print("\n== 变体尾部（全部候选） ==")
    print(f"{'变体':<8}{'p5%':>8}{'最差%':>8}{'到期亏占比%':>10}")
    tail_summary = {}
    for tag in ("pop3", "pop5", "nopop"):
        rets = sorted(variants[(t["date"], t["code"])][tag]["ret"] for t in trades)
        exps = [variants[(t["date"], t["code"])][tag] for t in trades]
        exp_loss = sum(1 for v in exps
                       if v["exit"] == "horizon_expired" and v["ret"] < 0)
        exp_all = max(1, sum(1 for v in exps if v["exit"] == "horizon_expired"))
        p5 = rets[int(len(rets) * 0.05)]
        tail_summary[tag] = {"p5": round(p5, 2), "worst": round(rets[0], 2),
                             "到期亏损占比": round(exp_loss / exp_all * 100, 1)}
        print(f"{tag:<8}{p5:>+8.2f}{rets[0]:>+8.2f}{exp_loss / exp_all * 100:>10.1f}")
    summary["exit_variant_tails"] = tail_summary

    with open(os.path.join(OUT_DIR, "trades.csv"), "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        wr.writeheader()
        wr.writerows(trades)
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s；产物 {OUT_DIR}/trades.csv ({len(trades)} 行) + summary.json")


if __name__ == "__main__":
    main()
