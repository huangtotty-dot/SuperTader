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

BASE = Path(__file__).resolve().parents[3]  # 自解析：本文件在 t_io/validation/daily_review/ 下，上级3级=仓库根（生产机=E:\06_T）
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

def day_profile(rs, prev_close=None):
    """close-only 近似 classify_day_type(口径: harness_backtest.py:170-196)

    C23修复(2026-08-19): 日ret% 保留"开→收"口径(harness 可比性)，
    新增 day_ret_pc% = 相对前收的市场惯例口径——跳空日两者差异巨大
    (08-19 600176 开收-4.3% vs 前收-9.86%)，复盘/水位线以后者为准。
    """
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
    out = {"open": round(o, 3), "close": round(cl, 3), "high": round(H, 3), "low": round(L, 3),
           "day_ret%": round(day_ret * 100, 2), "振幅%": round((H - L) / o * 100, 2),
           "day_type": dtype, "above_avg_ratio": round(above, 3)}
    if fnum(prev_close) and float(prev_close) > 0:
        out["day_ret_pc%"] = round((cl / float(prev_close) - 1) * 100, 2)
    return out

# C23: 前收优先取竞价采集(当日真实前收)，回退 holdings.json(eod_sync 滚动前有效)
_prev_close_map = {}
try:
    _auc = json.load(open(BASE / f"t_io/preopen/auction_{DATE}.json", encoding="utf-8"))
    for _slot, _snap in sorted((_auc.get("snapshots") or {}).items()):
        for _c, _row in (_snap.get("rows") or {}).items():
            if fnum(_row.get("pre_close")):
                _prev_close_map.setdefault(_c, float(_row["pre_close"]))
except Exception:
    pass
try:
    _hold = json.load(open(BASE / "holdings.json", encoding="utf-8"))
    for _c in CODES:
        if _c not in _prev_close_map and fnum((_hold.get(_c) or {}).get("pre_close")):
            _prev_close_map[_c] = float(_hold[_c]["pre_close"])
except Exception:
    pass

prof = {c: day_profile(ticks[c], _prev_close_map.get(c)) for c in CODES}

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

# --- K1 已按 W33 G4 移除（做T闭环盈亏非加仓/建仓主轴） ---

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

# --- K4/K5 已按 W33 G4 移除（做T滚动胜率/qty=0拦截非加仓/建仓主轴） ---

kpi = {"date": DATE,
       "snapshot": {"file": str(snap_fp.relative_to(BASE)) if snap_fp.exists() else None,
                    "created_now": snap_created,
                    "prev": str(prev_fp.relative_to(BASE)) if prev_fp else None},
       "K2_cost_change": k2, "K3_base_drift": k3}
with open(OUT / f"kpi_{DATE}.json", "w", encoding="utf-8") as f:
    json.dump(kpi, f, ensure_ascii=False, indent=2, default=str)

# --- 日复盘报告追加/替换「KPI 日快照」段（幂等：标记内替换） ---
def kpi_report_md():
    # W33 G4 (V2.1 对齐): 移除做T质量段 K1 闭环盈亏 / K4 滚动胜率 / K5 拦截计数，保留 K2 成本/K3 底仓漂移
    L = ["", "<!-- KPI日快照:begin -->", "", f"## 持仓准确性日快照（{DATE}，喂每日 Review §1）", ""]
    L.append("| KPI | 值 | 明细 |")
    L.append("|-----|-----|------|")
    if k2["baseline"]:
        L.append(f"| K2 持仓成本变化 | 基线日 | 无前日快照，仅建档："
                 + "；".join(f"{c} cost={v['cost_now']}" for c, v in k2["by_code"].items()) + " |")
    else:
        L.append(f"| K2 持仓成本变化 | {k2['note']} | "
                 + "；".join(f"{c} {v['cost_prev']}→{v['cost_now']}（{v['delta']:+}）" if v["delta"] is not None else f"{c} 无变动"
                            for c, v in k2["by_code"].items()) + " |")
    k3s = "；".join(f"{c} {v['drift']:+d}股 {v['attribution']}" for c, v in k3["by_code"].items() if v["drift"]) or "全仓无漂移"
    L.append(f"| K3 底仓漂移 | **{k3['drift_total']:+d}** | {k3s} |")
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

# ---------- 8. 加仓观察（§1 第1步·重点考察点；分析-only，周六周复盘立项设计可改） ----------
# 支撑位定义（观察期 v0）：
#   MA10/MA20/MA60 = 腾讯 qfq 日线（web.ifzq.gtimg.cn fqkline，直连；akshare/eastmoney 在本机复盘环境 SSL 不可达）
#   日内VWAP       = decision_trace 最后一个有效 tick 的 vwap（盘后定值）
#   近20日平台低点  = 日线 low 的 20 日最小值
# 回踩事件判定（v0）：日低 ≤ 支撑×1.005 记"触及"；收盘 ≥ 支撑=守住、收盘 < 支撑=破位；
#   1.005 < 日低/支撑 ≤ 1.02 记"临近未触"（候选素材，不计事件）。
import urllib.request as _urlreq

def _tx_daily(code, n=80):
    """腾讯 qfq 日线 [[date,open,close,high,low,...],...]；失败返回 []"""
    mkt = ("sh" if code.startswith(("5", "6")) else "sz") + code
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={mkt},day,,,{n},qfq"
    try:
        req = _urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(_urlreq.urlopen(req, timeout=15).read().decode())
        node = d["data"][mkt]
        return node.get("qfqday") or node.get("day") or []
    except Exception:
        return []

add_watch = {}
for c in CODES:
    p = prof.get(c) or {}
    if p.get("day_type") in (None, "unknown") or not fnum(p.get("low")):
        continue
    rows = _tx_daily(c)
    closes = [float(r[2]) for r in rows]
    lows_d = [float(r[3]) for r in rows]
    vwap = None
    for r in reversed(ticks[c]):
        if fnum(r.get("vwap")):
            vwap = round(float(r["vwap"]), 4)
            break
    sups = {}
    if len(closes) >= 60:
        sups["MA10"] = round(sum(closes[-10:]) / 10, 4)
        sups["MA20"] = round(sum(closes[-20:]) / 20, 4)
        sups["MA60"] = round(sum(closes[-60:]) / 60, 4)
        sups["近20日低点"] = round(min(lows_d[-20:]), 4)
    if vwap:
        sups["日内VWAP"] = vwap
    day_low, day_close = float(p["low"]), float(p["close"])
    events, near = [], []
    for lv, sv in sups.items():
        if not sv:
            continue
        dist = (day_low / sv - 1) * 100   # 日低相对支撑的偏离%（负=刺穿）
        if abs(dist) <= 0.5:
            # 回踩事件（父代理口径：盘中最低价距支撑 ≤0.5%）
            events.append({"level": lv, "support": sv, "day_low": day_low,
                           "dist%": round(dist, 2),
                           "status": "守住" if day_close >= sv else "破位"})
        elif -3.0 <= dist < -0.5:
            # 宽幅刺穿素材：日低刺穿支撑 0.5%~3%；收盘收回=刺穿收回，收盘在下=破位
            near.append({"level": lv, "support": sv, "dist%": round(dist, 2),
                         "type": "刺穿收回" if day_close >= sv else "刺穿破位"})
        elif 0.5 < dist <= 2.0:
            # 临近未触素材：日低在支撑上方 0.5%~2%
            near.append({"level": lv, "support": sv, "dist%": round(dist, 2), "type": "临近未触"})
        # |dist| > 3%：长期偏离（下跌趋势中 MA 悬于头顶），非回踩，不记录
    add_watch[c] = {"name": NAMES[c], "day_low": day_low, "close": day_close, "vwap": vwap,
                    "daily_rows": len(rows), "supports": sups, "events": events, "near": near}

def add_watch_md():
    n_hold = sum(1 for v in add_watch.values() for e in v["events"] if e["status"] == "守住")
    n_break = sum(1 for v in add_watch.values() for e in v["events"] if e["status"] == "破位")
    n_buy = sum(len([d for d in decisions[c] if d[1] == "BUY_LOW"]) for c in CODES)
    L = ["", "<!-- 加仓观察:begin -->", "",
         f"## 加仓观察（{DATE}，回踩事件扫描 v0 · 重点考察点，喂周六加仓逻辑设计）", ""]
    L.append("> 支撑位口径 v0（周六立项设计可改）：MA10/MA20/MA60=腾讯 qfq 日线；日内VWAP=trace 尾盘定值；"
             "近20日平台低点=日线 low 20 日最小。回踩事件=日低距支撑 ≤0.5%（守住=收盘≥支撑，破位=收盘<支撑）；"
             "素材带：刺穿 0.5%~3%（收盘收回=刺穿收回/在下=刺穿破位）、临近未触（上方 0.5%~2%）；"
             "偏离 >3% 属长期下方运行，非回踩不记录。")
    L.append("")
    L.append("| 代码 | 名称 | 日低 | 收盘 | 回踩事件（支撑@值，距%，守/破） | 素材（类型：支撑@值，距%） |")
    L.append("|---|---|---|---|---|---|")
    for c, v in add_watch.items():
        ev = "；".join(f"{e['level']}@{e['support']}（{e['dist%']:+}%，{e['status']}）" for e in v["events"]) or "—"
        nr = "；".join(f"{n['type']}:{n['level']}@{n['support']}（{n['dist%']:+}%）" for n in v["near"]) or "—"
        L.append(f"| {c} | {v['name']} | {v['day_low']} | {v['close']} | {ev} | {nr} |")
    if not add_watch:
        L.append("| — | — | — | — | 当日无数据 | — |")
    L.append("")
    L.append(f"- 回踩事件合计 **{n_hold + n_break}** 起（守住 {n_hold} / 破位 {n_break}）；"
             f"当日全池买入信号 **{n_buy}** 条（回踩位置买信号样本积累中）。")
    L += ["", "<!-- 加仓观察:end -->", ""]
    return "\n".join(L)

# ---------- W33 G1/G4: sizing_advice 当日汇总段（读取 main.py 落盘，喂每日 Review §3 加仓逐笔） ----------
def sizing_advice_md():
    fp = BASE / f"t_io/traces/sizing_advice_{DATE}.jsonl"
    if not fp.exists():
        return ""
    rows = []
    for line in open(fp, encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if not rows:
        return ""
    act_cn = {"BUY_LOW": "低吸", "SELL_HIGH": "高抛", "ADD_POS": "加仓", "PANIC_SELL": "恐慌卖"}
    L = ["", "<!-- sizing汇总:begin -->", "", f"## 加仓建议逐笔（{DATE}，sizing_advice 落盘）", ""]
    L.append("| 时间 | 代码 | 动作 | 类型 | 建议价 | VWAP | 建议股数 | 推送 | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        kind = {"rebuild": "接回", "first_add": "首加"}.get(r.get("buy_kind"), r.get("buy_kind") or "—")
        L.append(f"| {str(r.get('ts',''))[11:19]} | {r['code']} | {act_cn.get(r.get('action'), r.get('action'))} "
                 f"| {kind} | {r.get('price','')} | {r.get('vwap','')} | {r.get('suggested_qty','')} "
                 f"| {'✅' if r.get('pushed') else '❌'} | {r.get('note') or '—'} |")
    n_rebuild = sum(1 for r in rows if r.get("buy_kind") == "rebuild")
    n_first = sum(1 for r in rows if r.get("buy_kind") == "first_add")
    n_pushed = sum(1 for r in rows if r.get("pushed"))
    L += ["", f"- sizing 调用 **{len(rows)}** 次（接回 {n_rebuild} / 首加 {n_first}；推送 {n_pushed} / 静默 {len(rows) - n_pushed}）。"
             f"逐笔画像喂每日 Review §3 加仓质量跟踪（建议价 vs VWAP 判定买卖优劣）。",
          "", "<!-- sizing汇总:end -->", ""]
    return "\n".join(L)


# ---------- 指数5分钟共振段（2026-08-14 新增）：读 index_resonance trace，按门控分组算命中率 ----------
def _time_to_sec(s):
    """'HH:MM:SS' 或 'YYYY-MM-DD HH:MM:SS' → 当日秒数；失败返回 None。"""
    try:
        t = str(s).split(" ")[-1].split(".")[0].split(":")
        return int(t[0]) * 3600 + int(t[1]) * 60 + int(t[2])
    except Exception:
        return None


def _resonance_settle(code, ts, action, price):
    """共振判定信号结算（口径与 settle 一致：+0.5%/-0.4%/30tick；按 ±90s 最近决策轨迹点对齐）。"""
    rs = ticks.get(code, [])
    t_sec = _time_to_sec(ts)
    if t_sec is None or price in (None, 0):
        return "VOID"
    best = None
    for i, r in enumerate(rs):
        s = _time_to_sec(r.get("scan_time", ""))
        if s is None or abs(s - t_sec) > 90:
            continue
        if best is None or abs(s - t_sec) < abs(_time_to_sec(rs[best]["scan_time"]) - t_sec):
            best = i
    if best is None:
        return "VOID"
    for r in rs[best + 1: best + 31]:
        p = float(r["price"])
        if action in ("BUY_LOW", "ADD_POS"):
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


resonance_rows = []
res_fp = BASE / f"t_io/traces/index_resonance_{DATE}.jsonl"
if res_fp.exists():
    for _line in open(res_fp, encoding="utf-8"):
        try:
            _r = json.loads(_line)
        except Exception:
            continue
        _group = "data_missing" if _r.get("missing") else ("pass" if _r.get("gate_pass") else "block")
        resonance_rows.append({
            "ts": str(_r.get("scan_time", ""))[11:16], "code": _r.get("code"), "name": _r.get("name"),
            "action": _r.get("action"), "price": _r.get("price"),
            "group": _group, "gate": _r.get("gate", ""), "index_code": _r.get("index_code", ""),
            "res": _resonance_settle(_r.get("code"), _r.get("scan_time", ""), _r.get("action"), _r.get("price")),
        })


def _resonance_group_stats(rows):
    out = {}
    for g in ("pass", "block", "data_missing"):
        sub = [x for x in rows if x["group"] == g]
        w = sum(1 for x in sub if x["res"] == "WIN")
        f = sum(1 for x in sub if x["res"] == "FAIL")
        out[g] = {"n": len(sub), "wins": w, "fails": f,
                  "void": sum(1 for x in sub if x["res"] == "VOID"),
                  "hit_rate": round(w / (w + f), 4) if (w + f) else None}
    return out


resonance_groups = _resonance_group_stats(resonance_rows)


def _fmt_wr(v):
    return "—" if v is None else f"{v:.2%}"


def resonance_md():
    if not resonance_rows:
        return ""
    g = resonance_groups
    L = ["", "<!-- 指数共振:begin -->", "", f"## 指数5分钟共振过滤（{DATE}）", ""]
    L.append(f"- 共振通过 **{g['pass']['n']}** 条（命中率 {_fmt_wr(g['pass']['hit_rate'])}）｜ "
             f"共振拦截 **{g['block']['n']}** 条（命中率 {_fmt_wr(g['block']['hit_rate'])}）｜ "
             f"数据缺失拦截 **{g['data_missing']['n']}** 条")
    if g["pass"]["hit_rate"] is not None and g["block"]["hit_rate"] is not None:
        gap = g["pass"]["hit_rate"] - g["block"]["hit_rate"]
        if gap > 0.05:
            verdict = "有效（通过组命中率更高，过滤出更优信号）"
        elif gap < -0.05:
            verdict = "有害（拦截组命中率反而更高，需放宽口径）"
        else:
            verdict = "暂无效（两组差异不大，继续积累样本）"
        L.append(f"- 命中率差 = 通过 − 拦截 = **{gap:+.2%}** → 共振过滤**{verdict}**")
        L.append("> 口径：+0.5%/-0.4%/30tick，与 settle 一致；样本 < 20 时结论仅供参考。")
    L.append("")
    L.append("| 时间 | 代码 | 动作 | 分组 | 指数 | 结算 |")
    L.append("|---|---|---|---|---|---|")
    for x in resonance_rows:
        g_cn = {"pass": "共振通过", "block": "共振拦截", "data_missing": "数据缺失"}[x["group"]]
        L.append(f"| {x['ts']} | {x['code']} | {x['action']} | {g_cn} | {x['index_code']} | {x['res']} |")
    L += ["", "<!-- 指数共振:end -->", ""]
    return "\n".join(L)


# ---------- 9. 建仓信号扫描（§1 第1步·user 2026-08-05 新增；读取 position_builder 日志） ----------
def position_builder_md():
    trace_fp = BASE / f"t_io/traces/position_builder_{DATE}.jsonl"
    if not trace_fp.exists():
        return ""

    entries = []
    for line in open(trace_fp, encoding="utf-8"):
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    if not entries:
        return ""

    # 按股票聚合：取当日最高分
    best = {}
    scan_times = set()
    for e in entries:
        code = e["code"]
        scan_times.add(e.get("scan_time", "")[:16])  # 精确到分钟
        if code not in best or e["composite_score"] > best[code]["composite_score"]:
            best[code] = e

    n_intraday = sum(1 for e in entries if e.get("scan_type") == "intraday")
    n_eod = sum(1 for e in entries if e.get("scan_type") == "eod")
    sorted_best = sorted(best.values(), key=lambda e: -e["composite_score"])

    L = ["", "<!-- 建仓扫描:begin -->", "",
         f"## 建仓信号扫描（{DATE}，position_builder 日志聚合）", "",
         f"- 当日扫描: 盘中 **{n_intraday // max(1, len(best))}** 轮 / 盘后 **{min(n_eod // max(1, len(best)), 1)}** 次",
         f"- 候选股: **{len(best)}** 只（有快照数据的纳入统计）",
         ""]

    # W33 A1: 双通道 8 键（与 position_builder.CHANNEL_COND_KEYS 同序）
    _PB_COND_KEYS = ["c1_turn_confirm", "c1_boll_lower", "c1_volume_shrink", "c1_rsi_oversold",
                     "c1_m5_iceberg", "c2_box_breakout", "c2_volume_confirm", "c2_trend_bull"]
    _CH_TXT = {"iceberg": "🧊", "breakout": "🚀", "both": "🧊🚀"}

    signals = [e for e in sorted_best if e["verdict"] == "signal"]
    approaching = [e for e in sorted_best if e["verdict"] == "approaching"]
    if signals:
        L.append(f"🔴 **满足建仓条件（双通道 signal）: {len(signals)} 只**")
        for e in signals:
            cond_str = " ".join("●" if e["conditions"].get(k) else "○" for k in _PB_COND_KEYS)
            _ch = _CH_TXT.get(e.get("channel"), "—")
            L.append(f"  - {e['code']} {e['name']}: 得分 **{e['composite_score']}** [{_ch}] "
                     f"价 {e.get('price')} 建议 {e.get('suggested_qty', 0)}股  {cond_str}")
        L.append("")

    if approaching:
        L.append(f"🟡 **接近条件（approaching）: {len(approaching)} 只**")
        # W33 G4: 缺口条件列（从 conditions 提取未过的计分条件，如"差缩量"）
        _SCORED_CN = {"c1_turn_confirm": "转向", "c1_boll_lower": "BOLL", "c1_volume_shrink": "缩量",
                      "c2_box_breakout": "突破", "c2_volume_confirm": "放量", "c2_trend_bull": "多头"}
        for e in approaching[:6]:  # 最多显示 6 只
            cond_str = " ".join("●" if e["conditions"].get(k) else "○" for k in _PB_COND_KEYS)
            _ch = _CH_TXT.get(e.get("channel"), "—")
            _ap = {"immediate": "即时", "intraday_pending": "待日内", "next_day_pending": "待次日"}.get(e.get("approach_status"), "")
            _gap = "，".join(f"差{n}" for k, n in _SCORED_CN.items()
                             if k in e["conditions"] and not e["conditions"][k]) or "—"
            L.append(f"  - {e['code']} {e['name']}: 得分 **{e['composite_score']}** [{_ch}{('·' + _ap) if _ap else ''}] "
                     f"价 {e.get('price')}  **缺口:{_gap}**  {cond_str}")
        L.append("")

    L.append("●=通过  ○=未通过  (转向/BOLL/缩量/RSI/5分冰点/突破/放量/多头)")
    L += ["", "<!-- 建仓扫描:end -->", ""]
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
    aw = add_watch_md()
    if "<!-- 加仓观察:begin -->" in txt:
        txt = re.sub(r"<!-- 加仓观察:begin -->.*?<!-- 加仓观察:end -->",
                     aw.strip().replace("\n\n<!-- 加仓观察:end -->", "\n<!-- 加仓观察:end -->")
                     .replace("<!-- 加仓观察:begin -->\n\n", "<!-- 加仓观察:begin -->\n"),
                     txt, flags=re.S)
    else:
        txt = txt.rstrip() + "\n" + aw
    pb = position_builder_md()
    if pb:
        if "<!-- 建仓扫描:begin -->" in txt:
            txt = re.sub(r"<!-- 建仓扫描:begin -->.*?<!-- 建仓扫描:end -->",
                         pb.strip().replace("\n\n<!-- 建仓扫描:end -->", "\n<!-- 建仓扫描:end -->")
                         .replace("<!-- 建仓扫描:begin -->\n\n", "<!-- 建仓扫描:begin -->\n"),
                         txt, flags=re.S)
        else:
            txt = txt.rstrip() + "\n" + pb
    # W33 G4: sizing_advice 当日汇总段
    sa = sizing_advice_md()
    if sa:
        if "<!-- sizing汇总:begin -->" in txt:
            txt = re.sub(r"<!-- sizing汇总:begin -->.*?<!-- sizing汇总:end -->",
                         sa.strip().replace("\n\n<!-- sizing汇总:end -->", "\n<!-- sizing汇总:end -->")
                         .replace("<!-- sizing汇总:begin -->\n\n", "<!-- sizing汇总:begin -->\n"),
                         txt, flags=re.S)
        else:
            txt = txt.rstrip() + "\n" + sa
    # 指数5分钟共振段（2026-08-14）
    rs_md = resonance_md()
    if rs_md:
        if "<!-- 指数共振:begin -->" in txt:
            txt = re.sub(r"<!-- 指数共振:begin -->.*?<!-- 指数共振:end -->",
                         rs_md.strip().replace("\n\n<!-- 指数共振:end -->", "\n<!-- 指数共振:end -->")
                         .replace("<!-- 指数共振:begin -->\n\n", "<!-- 指数共振:begin -->\n"),
                         txt, flags=re.S)
        else:
            txt = txt.rstrip() + "\n" + rs_md
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
          "add_watch": add_watch,
          "watch": watch,
          "resonance": {"rows": resonance_rows, "groups": resonance_groups}}
with open(OUT / f"daily_review_{DATE}.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

print(f"== {DATE} 日复盘数据摘要 ==")
for c in CODES:
    s = sig_stat[c]
    print(f"{c} {NAMES[c]}: 买{s['buy_signals']}/卖{s['sell_signals']} 买max{s['max_buy_score']} 卖max{s['max_sell_score']} "
          f"振幅{s.get('振幅%')}% 日ret{s.get('day_ret%')}%(开收) 日ret_pc{s.get('day_ret_pc%')}%(前收) {s['day_type']} nan={s['nan_ticks']}")
print("shadow_near:", {c: len(v) for c, v in shadow_near.items()})
print("suppressed:", {c: len(v) for c, v in suppress.items()}, "pushes:", pushes)
print("silent_sell:", {c: (len(v), max((e['score'] for e in v), default=None)) for c, v in silent_sell.items() if v})
print("closed:", closed)
print("== 持仓准确性快照 (W33 G4 口径) ==")
print(f"K2 cost对照: {'基线日' if k2['baseline'] else k2['note']}  snapshot={kpi['snapshot']['file']} created={snap_created} prev={kpi['snapshot']['prev']}")
print(f"K3 底仓漂移: total={k3['drift_total']:+d} ", {c: (v['drift'], v['attribution']) for c, v in k3['by_code'].items() if v['drift']})
print("KPI JSON:", OUT / f"kpi_{DATE}.json")
print("settle_by_code:", json.dumps(settle_by_code, ensure_ascii=False))
print("watch:", json.dumps(watch, ensure_ascii=False, default=str)[:600])
print("JSON:", OUT / f"daily_review_{DATE}.json")

# ---------- A-1: 每日结算信号前瞻（signal_outcomes.json），接入每日管线 ----------
try:
    import importlib.util as _ilu
    _p = BASE / "t_io" / "validation" / "signal_outcome_tracker.py"
    _spec = _ilu.spec_from_file_location("signal_outcome_tracker", _p)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod.run_settle(days=None)
    print("[tracker] signal_outcomes 已结算（A-1，含退出侧 max_drawdown）")
except Exception as _e:
    print(f"[warn] signal_outcomes 结算失败: {_e}")
