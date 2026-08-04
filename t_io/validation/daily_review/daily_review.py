# -*- coding: utf-8 -*-
"""
daily_review.py — 三层复盘体系·日复盘系统自动数据项(§1 第1步 + §5 观察项)
分析-only, 冻结参数。用法: python daily_review.py [--date 2026-08-03]
数据源: t_io/traces/{decision_trace,shadow_signals,preopen_trace,data_quality}_DATE.jsonl
        t_io/logs/t_trader_sys_DATE.log, t_io/logs/closure_audit.jsonl
日型口径: harness_backtest.classify_day_type 的 close-only 近似(生产无分钟OHLC落盘, trace仅tick价)
产物: t_io/validation/daily_review/daily_review_DATE.json + 控制台摘要
"""
import argparse, json, math, re, shutil, sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(r"E:\06_T")
CODES = ["000988", "588170", "600176", "600481", "603667", "002639", "300153", "300364"]
NAMES = {"000988": "华工科技", "588170": "科创半导体ETF华夏", "600176": "中国巨石",
         "600481": "双良节能", "603667": "五洲新春", "002639": "雪人集团",
         "300153": "科泰电源", "300364": "中文在线"}

p = argparse.ArgumentParser()
p.add_argument("--date", default="2026-08-03")
DATE = p.parse_args().date
OUT = BASE / "t_io/validation/daily_review"
OUT.mkdir(parents=True, exist_ok=True)

def fnum(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))

# ---------- 1. decision_trace: 信号/极值/振幅/日型/NaN ----------
trace_fp = BASE / f"t_io/traces/decision_trace_{DATE}.jsonl"
ticks = defaultdict(list)
decisions = defaultdict(list)   # code -> [(ts, action, score)]
nan_ticks = Counter()
for line in open(trace_fp, encoding="utf-8"):
    r = json.loads(line)
    c = r["code"]
    ticks[c].append(r)
    bs, ss = r.get("buy_score"), r.get("sell_score")
    if not fnum(bs) and not fnum(ss):
        nan_ticks[c] += 1
    d = r.get("decision")
    if d in ("BUY_LOW", "SELL_HIGH"):
        decisions[c].append((r["scan_time"], d, bs if d == "BUY_LOW" else ss,
                             r.get("price"), r.get("buy_block") or [], r.get("sell_block") or []))

def day_profile(rs):
    """close-only 近似 classify_day_type(口径: harness_backtest.py:170-196)"""
    prices = [float(r["price"]) for r in rs if fnum(r.get("price"))]
    if len(prices) < 30:
        return {"day_type": "unknown"}
    o, cl = prices[0], prices[-1]
    H, L = max(prices), min(prices)
    day_ret = (cl - o) / o
    avg = sum(prices) / len(prices)
    above = sum(1 for x in prices if x > avg) / len(prices)
    fh = prices[: max(1, len(prices) // 4)]  # 近似首小时(生产tick约30s一根≈全天1/8, 取1/4保守)
    fh_ret = (fh[-1] - fh[0]) / fh[0]
    reversed_dir = (fh_ret > 0.003 and day_ret < -0.005) or (fh_ret < -0.003 and day_ret > 0.005)
    if reversed_dir and abs(day_ret) >= 0.008:
        dtype = "reversal_day"
    elif day_ret >= 0.01 and above >= 0.55:
        dtype = "bull_day"
    elif day_ret <= -0.01 and above <= 0.45:
        dtype = "bear_day"
    else:
        dtype = "chop_day"
    return {"open": round(o, 3), "close": round(cl, 3), "high": round(H, 3), "low": round(L, 3),
            "day_ret%": round(day_ret * 100, 2), "振幅%": round((H - L) / o * 100, 2),
            "day_type": dtype, "above_avg_ratio": round(above, 3)}

prof = {c: day_profile(ticks[c]) for c in CODES}

def valid_max(rs, key):
    vals = [r[key] for r in rs if fnum(r.get(key))]
    return round(max(vals), 1) if vals else None

sig_stat = {}
for c in CODES:
    dec = decisions[c]
    buys = [d for d in dec if d[1] == "BUY_LOW"]
    sells = [d for d in dec if d[1] == "SELL_HIGH"]
    sig_stat[c] = {"ticks": len(ticks[c]), "nan_ticks": nan_ticks[c],
                   "buy_signals": len(buys), "sell_signals": len(sells),
                   "buy_first_last": (buys[0][0][11:16], buys[-1][0][11:16]) if buys else None,
                   "sell_first_last": (sells[0][0][11:16], sells[-1][0][11:16]) if sells else None,
                   "max_buy_score": valid_max(ticks[c], "buy_score"),
                   "max_sell_score": valid_max(ticks[c], "sell_score"),
                   **prof[c]}

# ---------- 2. shadow_signals: ±3 分近阈漏单 ----------
shadow_fp = BASE / f"t_io/traces/shadow_signals_{DATE}.jsonl"
shadow_near = defaultdict(list)
shadow_total = 0
if shadow_fp.exists():
    for line in open(trace_fp if False else shadow_fp, encoding="utf-8"):
        r = json.loads(line)
        shadow_total += 1
        db, ds = r.get("distance_to_buy_threshold"), r.get("distance_to_sell_threshold")
        near_buy = fnum(db) and abs(db) <= 3
        near_sell = fnum(ds) and abs(ds) <= 3
        if near_buy or near_sell:
            shadow_near[r["code"]].append({
                "ts": r["scan_time"][11:16], "action": r.get("action"),
                "buy_score": r.get("buy_score"), "sell_score": r.get("sell_score"),
                "dist_buy": db, "dist_sell": ds, "miss_reason": r.get("miss_reason")})

def shadow_compact(evts):
    """按 (action, miss_reason) 聚合: 时段范围+条数+最近距离"""
    out = []
    for (act, mr), grp in defaultdict(list, {}).items():
        pass
    groups = defaultdict(list)
    for e in evts:
        groups[(e["action"], e["miss_reason"])].append(e)
    for (act, mr), g in groups.items():
        dists = [abs(e["dist_buy"]) for e in g if fnum(e["dist_buy"]) and abs(e["dist_buy"]) <= 3] + \
                [abs(e["dist_sell"]) for e in g if fnum(e["dist_sell"]) and abs(e["dist_sell"]) <= 3]
        out.append({"action": act, "miss_reason": mr, "n": len(g),
                    "span": f"{g[0]['ts']}~{g[-1]['ts']}", "min_dist": round(min(dists), 1) if dists else None})
    return sorted(out, key=lambda x: (x["min_dist"] if x["min_dist"] is not None else 99))

shadow_report = {c: shadow_compact(shadow_near[c]) for c in CODES if shadow_near[c]}

# ---------- 3. 日志: 仓控拦截/静默/实际推送/收盘同步 ----------
log_fp = BASE / f"t_io/logs/t_trader_sys_{DATE}.log"
suppress, silent_sell, pushes, eod = defaultdict(list), defaultdict(list), [], []
re_sup = re.compile(r"^(\d{2}:\d{2}:\d{2}).*🛑 (\d{6}) (BUY_LOW|SELL_HIGH)信号达标\((\d+)分\)但仓控可交易量为0")
re_sil = re.compile(r"^(\d{2}:\d{2}:\d{2}).*📉 (\d{6}) 卖出信号得分(\d+)分，低于.*阈值(\d+)分，静默")
re_push = re.compile(r"^(\d{2}:\d{2}:\d{2}).*飞书消息已成功送达: .+?\((\d{6})\) (BUY_LOW|SELL_HIGH)")
re_sync = re.compile(r"收盘同步 (.+?\((\d{6})\)): qty (\d+)→(\d+), t_qty (\d+)→(\d+)")
for line in open(log_fp, encoding="utf-8", errors="replace"):
    m = re_sup.search(line)
    if m:
        suppress[m.group(2)].append({"ts": m.group(1), "action": m.group(3), "score": int(m.group(4))})
        continue
    m = re_sil.search(line)
    if m:
        silent_sell[m.group(2)].append({"ts": m.group(1), "score": int(m.group(3)), "th": int(m.group(4))})
        continue
    m = re_push.search(line)
    if m:
        pushes.append({"ts": m.group(1), "code": m.group(2), "action": m.group(3)})
        continue
    m = re_sync.search(line)
    if m:
        eod.append({"code": m.group(2), "qty_from": int(m.group(3)), "qty_to": int(m.group(4))})
suppress, silent_sell = dict(suppress), dict(silent_sell)

# ---------- 4. 闭环(closure_audit 当日) ----------
audit_today = None
for line in open(BASE / "t_io/logs/closure_audit.jsonl", encoding="utf-8"):
    r = json.loads(line)
    if r.get("date") == DATE:
        audit_today = r
closed = {}
if audit_today:
    for d in audit_today["details"]:
        closed[d["code"]] = {k: d[k] for k in ("sold", "bought", "unrebuilt", "est_pnl", "qty_diff")}

# ---------- 5. 当日信号结算(close-only近似: +0.5%/-0.4%, 30 tick窗口) ----------
def settle(code, ts, action, price):
    rs = ticks[code]
    idx = next((i for i, r in enumerate(rs) if r["scan_time"] == ts), None)
    if idx is None or price in (None, 0):
        return "VOID"
    for r in rs[idx + 1: idx + 31]:
        p = float(r["price"])
        if action == "BUY_LOW":
            if p <= price * 0.996:
                return "FAIL"
            if p >= price * 1.005:
                return "WIN"
        else:
            if p >= price * 1.004:
                return "FAIL"
            if p <= price * 0.995:
                return "WIN"
    return "VOID"

settle_rows = []
for c in CODES:
    for ts, act, score, price, bb, sb in decisions[c]:
        res = settle(c, ts, act, price)
        settle_rows.append({"code": c, "ts": ts[11:16], "action": act, "score": score,
                            "price": price, "res": res, "day_type": prof[c]["day_type"]})
settle_by_code = {}
for c in CODES:
    rows = [r for r in settle_rows if r["code"] == c]
    for act in ("BUY_LOW", "SELL_HIGH"):
        sub = [r for r in rows if r["action"] == act]
        w = sum(1 for r in sub if r["res"] == "WIN")
        f = sum(1 for r in sub if r["res"] == "FAIL")
        settle_by_code.setdefault(c, {})[act] = {"n": len(sub), "wins": w, "fails": f,
                                                 "wr": round(w / (w + f), 4) if (w + f) else None}

# ---------- 6. 观察项 ----------
total_sigs = sum(v["buy_signals"] + v["sell_signals"] for v in sig_stat.values())
s988 = sig_stat["000988"]["buy_signals"] + sig_stat["000988"]["sell_signals"]
watch = {
    "#1_000988_qty冻结": {"suppressed_qty0": len(suppress.get("000988", [])), "pushed": [p for p in pushes if p["code"] == "000988"],
                        "audit": closed.get("000988"),
                        "note": "实盘当日其买信号被仓控0拦截次数; audit sold/bought=0 表示无成交"},
    "#2_000988_bull日买入": {"day_type": prof["000988"]["day_type"], "buy_signals": sig_stat["000988"]["buy_signals"],
                            "note": "D+E未上线, bull日买入应仍触发"},
    "#3_588170_买wr": settle_by_code.get("588170", {}).get("BUY_LOW"),
    "#4_阴跌日股": {c: {"day_type": prof[c]["day_type"], "buy_settle": settle_by_code.get(c, {}).get("BUY_LOW")}
                   for c in CODES if prof[c]["day_type"] == "bear_day"},
    "#5_000988信号占比": {"n": s988, "total": total_sigs, "ratio": round(s988 / total_sigs, 4) if total_sigs else None,
                        "alert": (s988 / total_sigs) > 0.55 if total_sigs else None},
}

# ---------- 7. KPI 日快照（喂周复盘 §1.5 周 KPI 表 K1-K5） ----------
STATE_DIR = BASE / "t_io/state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
HOLDINGS_FP = BASE / "holdings.json"

def archive_holdings_snapshot(date):
    """幂等归档: 当前 holdings.json → t_io/state/holdings_DATE.json（已存在则跳过）。
    收盘同步会覆盖 holdings.json，必须先归档才能做跨日 K2/K3 对照。"""
    snap = STATE_DIR / f"holdings_{date}.json"
    created = False
    if not snap.exists() and HOLDINGS_FP.exists():
        shutil.copy2(HOLDINGS_FP, snap)
        created = True
    return snap, created

def prev_holdings_snapshot(date):
    """前一交易日归档快照（取日期 < date 的最新一份）。"""
    cands = [s for s in STATE_DIR.glob("holdings_*.json")
             if s.stem.replace("holdings_", "") < date]
    return sorted(cands)[-1] if cands else None

snap_fp, snap_created = archive_holdings_snapshot(DATE)
prev_fp = prev_holdings_snapshot(DATE)
hold_now = json.load(open(snap_fp, encoding="utf-8")) if snap_fp.exists() else {}
hold_prev = json.load(open(prev_fp, encoding="utf-8")) if prev_fp else {}
k2_baseline = prev_fp is None

# --- K1 当日闭环净盈亏（按股分解，复用 §4 closure_audit 当日数据） ---
k1 = {"total_est_pnl": round(sum(v["est_pnl"] for v in closed.values()), 2) if closed else None,
      "by_code": {c: dict(v) for c, v in closed.items()} if closed else {},
      "source": "t_io/logs/closure_audit.jsonl"}

# --- K2 持仓成本变化（对照前一交易日快照；无快照=基线日） ---
k2_by = {}
for c in CODES:
    now_h = hold_now.get(c, {})
    prev_h = hold_prev.get(c, {})
    cost_now, cost_prev = now_h.get("cost"), prev_h.get("cost")
    k2_by[c] = {"cost_now": cost_now,
                "cost_prev": cost_prev if not k2_baseline else None,
                "delta": round(cost_now - cost_prev, 4) if (fnum(cost_now) and fnum(cost_prev)) else None}
k2 = {"baseline": k2_baseline,
      "note": "基线日（无前日快照，仅记录当前 cost）" if k2_baseline else f"对照 {prev_fp.stem.replace('holdings_', '')}",
      "by_code": k2_by, "snapshot": str(snap_fp.relative_to(BASE))}

# --- K3 底仓漂移（base/t_qty 净变动 + 归因；优先快照对照，基线日用收盘同步+成交记录推断） ---
eod_by_code = {e["code"]: e for e in eod}
def k3_attrib(c, drift, audit):
    """归因: 未接回/加仓/减仓/无漂移/需人工确认"""
    if drift == 0:
        return "无漂移"
    if audit:
        sold, bought = audit.get("sold", 0), audit.get("bought", 0)
        if sold > bought and drift == -(sold - bought):
            return f"未接回（卖{sold}/接回{bought}，{sold - bought}股未接回）"
        if bought > sold and drift == bought - sold:
            return f"加仓（买{bought}/卖{sold}，净{drift:+d}股）"
        if sold > 0 and bought == 0 and drift == -sold:
            return f"减仓（卖{sold}股未接回）"
    return "需人工确认"

k3_by = {}
for c in CODES:
    now_h, prev_h = hold_now.get(c, {}), hold_prev.get(c, {})
    audit = closed.get(c)
    if not k2_baseline:
        drift = (now_h.get("t_qty") or now_h.get("qty") or 0) - (prev_h.get("t_qty") or prev_h.get("qty") or 0)
        src = "snapshot_diff"
    elif c in eod_by_code:
        e = eod_by_code[c]
        drift = e["qty_to"] - e["qty_from"]
        src = "eod_sync+closure_audit"
    else:
        drift, src = 0, "eod_sync（无同步记录=无漂移）"
    k3_by[c] = {"t_qty_now": now_h.get("t_qty") or now_h.get("qty"),
                "t_qty_prev": (prev_h.get("t_qty") or prev_h.get("qty")) if not k2_baseline else None,
                "drift": drift, "attribution": k3_attrib(c, drift, audit),
                "audit": audit, "source": src}
k3 = {"baseline": k2_baseline, "by_code": k3_by,
      "drift_total": sum(v["drift"] for v in k3_by.values())}

# --- K4 滚动 20 条买/卖胜率（decision_trace 向前回溯，close-only 结算口径同 §5） ---
def _settle_in(rs, ts, action, price):
    idx = next((i for i, r in enumerate(rs) if r["scan_time"] == ts), None)
    if idx is None or price in (None, 0):
        return "VOID"
    for r in rs[idx + 1: idx + 31]:
        p = float(r.get("price") or 0)
        if p <= 0:
            continue
        if action == "BUY_LOW":
            if p <= price * 0.996:
                return "FAIL"
            if p >= price * 1.005:
                return "WIN"
        else:
            if p >= price * 1.004:
                return "FAIL"
            if p <= price * 0.995:
                return "WIN"
    return "VOID"

def rolling_wr(date, window=20):
    """最近 window 条已结算（WIN/FAIL）买/卖信号胜率；不足 window 如实给 n。"""
    evts = {"BUY_LOW": [], "SELL_HIGH": []}
    days = []
    for fp in sorted((BASE / "t_io/traces").glob("decision_trace_*.jsonl")):
        d = fp.stem.replace("decision_trace_", "")
        if d > date:
            continue
        days.append(d)
        rs_by = defaultdict(list)
        decs = []
        for line in open(fp, encoding="utf-8"):
            r = json.loads(line)
            c = r.get("code")
            rs_by[c].append(r)
            if r.get("decision") in ("BUY_LOW", "SELL_HIGH") and fnum(r.get("price")):
                decs.append(r)
        for r in decs:
            res = _settle_in(rs_by[r["code"]], r["scan_time"], r["decision"], float(r["price"]))
            if res in ("WIN", "FAIL"):
                evts[r["decision"]].append({"date": d, "code": r["code"],
                                            "ts": r["scan_time"], "res": res})
    out = {}
    for act, key in (("BUY_LOW", "buy"), ("SELL_HIGH", "sell")):
        tail = evts[act][-window:]
        w = sum(1 for e in tail if e["res"] == "WIN")
        f = sum(1 for e in tail if e["res"] == "FAIL")
        out[key] = {"n": len(tail), "wins": w, "fails": f,
                    "wr": round(w / len(tail), 4) if tail else None,
                    "window": window, "short": len(tail) < window,
                    "note": f"n={len(tail)}<{window}，可得样本胜率" if len(tail) < window else "满窗",
                    "events_tail": tail}
    out["days_covered"] = [days[0], days[-1]] if days else []
    return out

k4 = rolling_wr(DATE)

# --- K5 qty=0 冻结拦截数（按股，复用 §3 仓控拦截正则） ---
k5 = {"total": sum(len(suppress.get(c, [])) for c in CODES),
      "by_code": {c: len(suppress[c]) for c in CODES if suppress.get(c)},
      "source": "t_io/logs 仓控可交易量为0"}

kpi = {"date": DATE,
       "snapshot": {"file": str(snap_fp.relative_to(BASE)) if snap_fp.exists() else None,
                    "created_now": snap_created,
                    "prev": str(prev_fp.relative_to(BASE)) if prev_fp else None},
       "K1_closed_pnl": k1, "K2_cost_change": k2, "K3_base_drift": k3,
       "K4_rolling_wr": k4, "K5_qty0_suppressed": k5}
with open(OUT / f"kpi_{DATE}.json", "w", encoding="utf-8") as f:
    json.dump(kpi, f, ensure_ascii=False, indent=2, default=str)

# --- 日复盘报告追加/替换「KPI 日快照」段（幂等：标记内替换） ---
def kpi_report_md():
    L = ["", "<!-- KPI日快照:begin -->", "", f"## KPI 日快照（{DATE}，喂周复盘 §1.5）", ""]
    L.append("| KPI | 值 | 明细 |")
    L.append("|-----|-----|------|")
    k1s = "；".join(f"{c} {v['est_pnl']:+.2f}" for c, v in k1["by_code"].items() if v["est_pnl"]) or "全日无成交"
    L.append(f"| K1 闭环净盈亏 | **{k1['total_est_pnl']:+.2f}** | {k1s} |")
    if k2["baseline"]:
        L.append(f"| K2 持仓成本变化 | 基线日 | 无前日快照，仅建档："
                 + "；".join(f"{c} cost={v['cost_now']}" for c, v in k2["by_code"].items()) + " |")
    else:
        L.append(f"| K2 持仓成本变化 | {k2['note']} | "
                 + "；".join(f"{c} {v['cost_prev']}→{v['cost_now']}（{v['delta']:+}）" if v["delta"] is not None else f"{c} 无变动"
                            for c, v in k2["by_code"].items()) + " |")
    k3s = "；".join(f"{c} {v['drift']:+d}股 {v['attribution']}" for c, v in k3["by_code"].items() if v["drift"]) or "全仓无漂移"
    L.append(f"| K3 底仓漂移 | **{k3['drift_total']:+d}** | {k3s} |")
    b, s = k4["buy"], k4["sell"]
    L.append(f"| K4 滚动20条胜率 | 买 **{b['wr']}**（{b['wins']}W/{b['fails']}F，n={b['n']}{'，不满窗' if b['short'] else ''}）"
             f" / 卖 **{s['wr']}**（{s['wins']}W/{s['fails']}F，n={s['n']}{'，不满窗' if s['short'] else ''}） | "
             f"覆盖 {k4['days_covered']} |")
    L.append(f"| K5 qty=0 拦截 | **{k5['total']}** | "
             + "；".join(f"{c}×{n}" for c, n in k5["by_code"].items()) + " |")
    L += ["", "<!-- KPI日快照:end -->", ""]
    return "\n".join(L)

report_fp = BASE / f"doc/每日复盘/{DATE}_复盘.md"

# --- 日复盘报告追加/替换「系统阶段看板」段（幂等：标记内替换；单一事实源 stage_board.json） ---
def stage_board_md():
    fp = OUT / "stage_board.json"
    if not fp.exists():
        return ""
    board = json.loads(fp.read_text(encoding="utf-8"))
    zones = board.get("_meta", {}).get("zones", ["已验收", "观察中", "优化管线中", "待启动"])
    upd = board.get("_meta", {}).get("updated", DATE)
    L = ["", "<!-- 阶段看板:begin -->", "",
         f"## 系统阶段看板（截至 {upd}；数据源 stage_board.json，阶段变更只在验收事件时由周/月复盘维护）", ""]
    for zone in zones:
        items = [s for s in board.get("stages", []) if s.get("zone") == zone]
        if not items:
            continue
        L += [f"**{zone}（{len(items)}）**", "", "| 事项 | since | 备注 |", "|---|---|---|"]
        for s in items:
            L.append(f"| {s.get('name','')} | {s.get('since','')} | {s.get('note','')} |")
        L.append("")
    L += ["<!-- 阶段看板:end -->", ""]
    return "\n".join(L)

if report_fp.exists():
    txt = report_fp.read_text(encoding="utf-8")
    if "<!-- KPI日快照:begin -->" in txt:
        txt = re.sub(r"<!-- KPI日快照:begin -->.*?<!-- KPI日快照:end -->",
                     kpi_report_md().strip().replace("\n\n<!-- KPI日快照:end -->", "\n<!-- KPI日快照:end -->")
                     .replace("<!-- KPI日快照:begin -->\n\n", "<!-- KPI日快照:begin -->\n"),
                     txt, flags=re.S)
    else:
        txt = txt.rstrip() + "\n" + kpi_report_md()
    sb = stage_board_md()
    if sb:
        if "<!-- 阶段看板:begin -->" in txt:
            txt = re.sub(r"<!-- 阶段看板:begin -->.*?<!-- 阶段看板:end -->",
                         sb.strip().replace("\n\n<!-- 阶段看板:end -->", "\n<!-- 阶段看板:end -->")
                         .replace("<!-- 阶段看板:begin -->\n\n", "<!-- 阶段看板:begin -->\n"),
                         txt, flags=re.S)
        else:
            txt = txt.rstrip() + "\n" + sb
    report_fp.write_text(txt, encoding="utf-8")

# ---------- 输出 ----------
result = {"date": DATE, "sig_stat": sig_stat, "shadow_total": shadow_total,
          "shadow_near_±3": shadow_report,
          "qty_freeze": {"suppressed": {c: suppress[c] for c in CODES if suppress.get(c)},
                          "silent_sell": {c: {"n": len(silent_sell[c]), "max_score": max((e["score"] for e in silent_sell[c]), default=None)}
                                          for c in CODES if c in silent_sell and silent_sell[c]},
                          "pushes": pushes, "eod_sync": eod},
          "closed_loop": closed, "audit_problems": audit_today["problems"] if audit_today else None,
          "settle": {"rows": settle_rows, "by_code": settle_by_code},
          "kpi": kpi,
          "watch": watch}
with open(OUT / f"daily_review_{DATE}.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

print(f"== {DATE} 日复盘数据摘要 ==")
for c in CODES:
    s = sig_stat[c]
    print(f"{c} {NAMES[c]}: 买{s['buy_signals']}/卖{s['sell_signals']} 买max{s['max_buy_score']} 卖max{s['max_sell_score']} "
          f"振幅{s.get('振幅%')}% 日ret{s.get('day_ret%')}% {s['day_type']} nan={s['nan_ticks']}")
print("shadow_near:", {c: len(v) for c, v in shadow_near.items()})
print("suppressed:", {c: len(v) for c, v in suppress.items()}, "pushes:", pushes)
print("silent_sell:", {c: (len(v), max((e['score'] for e in v), default=None)) for c, v in silent_sell.items() if v})
print("closed:", closed)
print("== KPI 日快照 ==")
print(f"K1 闭环净盈亏: {k1['total_est_pnl']}  by_code:", {c: v['est_pnl'] for c, v in k1['by_code'].items()})
print(f"K2 cost对照: {'基线日' if k2['baseline'] else k2['note']}  snapshot={kpi['snapshot']['file']} created={snap_created} prev={kpi['snapshot']['prev']}")
print(f"K3 底仓漂移: total={k3['drift_total']:+d} ", {c: (v['drift'], v['attribution']) for c, v in k3['by_code'].items() if v['drift']})
print(f"K4 滚动胜率: 买 n={k4['buy']['n']} wr={k4['buy']['wr']} / 卖 n={k4['sell']['n']} wr={k4['sell']['wr']}  覆盖{k4['days_covered']}")
print(f"K5 qty=0拦截: total={k5['total']} ", k5['by_code'])
print("KPI JSON:", OUT / f"kpi_{DATE}.json")
print("settle_by_code:", json.dumps(settle_by_code, ensure_ascii=False))
print("watch:", json.dumps(watch, ensure_ascii=False, default=str)[:600])
print("JSON:", OUT / f"daily_review_{DATE}.json")
