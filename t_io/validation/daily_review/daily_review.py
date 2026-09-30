# -*- coding: utf-8 -*-
"""
daily_review.py — 三层复盘体系·日复盘系统自动数据项(§1 第1步 + §5 观察项)
分析-only, 冻结参数。用法: python daily_review.py [--date 2026-08-03]
数据源: t_io/traces/{decision_trace,shadow_signals,preopen_trace,data_quality}_DATE.jsonl
        t_io/logs/t_trader_sys_DATE.log, t_io/logs/closure_audit.jsonl
日型口径: harness_backtest.classify_day_type 的 close-only 近似(生产无分钟OHLC落盘, trace仅tick价)
产物: t_io/validation/daily_review/daily_review_DATE.json + 控制台摘要
"""
import argparse, json, math, os, re, shutil, sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]  # 自解析：本文件在 t_io/validation/daily_review/ 下，上级3级=仓库根（生产机=E:\06_T）
# 施工4(2026-09-27 清单V2.0 §八): CODES 改为从 holdings.json 动态读当前持仓票(qty>0)，
# 失败回退原硬编码 8 票池并告警。NAMES 以 holdings 的 name 为准、硬编码兜底。
CODES_FALLBACK = ["000988", "588170", "600176", "600481", "603667", "002639", "300153", "300364"]
NAMES_FALLBACK = {"000988": "华工科技", "588170": "科创半导体ETF华夏", "600176": "中国巨石",
                  "600481": "双良节能", "603667": "五洲新春", "002639": "雪人集团",
                  "300153": "科泰电源", "300364": "中文在线"}

def _load_codes_from_holdings():
    """读 t_io/state/holdings.json，返回 (codes_qty>0, names)；失败返回 (None, {})。"""
    try:
        h = json.load(open(BASE / "t_io" / "state" / "holdings.json", encoding="utf-8"))
        codes = sorted(c for c, v in h.items() if isinstance(v, dict) and (v.get("qty") or 0) > 0)
        if not codes:
            return None, {}
        names = {c: str((h.get(c) or {}).get("name") or c) for c in codes}
        return codes, names
    except Exception:
        return None, {}

_dyn_codes, _dyn_names = _load_codes_from_holdings()
if _dyn_codes:
    CODES = _dyn_codes
    NAMES = dict(NAMES_FALLBACK)
    NAMES.update(_dyn_names)
else:
    CODES = list(CODES_FALLBACK)
    NAMES = dict(NAMES_FALLBACK)
    print("[warn] holdings.json 读取失败或无持仓(qty>0)，CODES 回退硬编码 8 票池")

p = argparse.ArgumentParser()
p.add_argument("--date", default=None)   # P2-3C: default 当天（原硬编码 2026-08-03）
_DATE_ARG = p.parse_args().date
DATE = _DATE_ARG or datetime.now().strftime("%Y-%m-%d")
OUT = BASE / "t_io/validation/daily_review"
OUT.mkdir(parents=True, exist_ok=True)

def fnum(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))

# ---------- 1. decision_trace: 信号/极值/振幅/日型/NaN ----------
trace_fp = BASE / f"t_io/traces/decision_trace_{DATE}.jsonl"
ticks = defaultdict(list)
decisions = defaultdict(list)   # code -> [(ts, action, score)]
nan_ticks = Counter()
# 2026-09-28 容错：Renko/T引擎删除后 decision_trace 不再每日产生，缺失时跳过本段
if trace_fp.exists():
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
else:
    print(f"[daily_review] 无 decision_trace_{DATE}.jsonl（T引擎已删），跳过信号段")

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
    _hold = json.load(open(BASE / "t_io" / "state" / "holdings.json", encoding="utf-8"))
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
re_sup = re.compile(r"^(\d{2}:\d{2}:\d{2}).*📡 (\d{6}) (BUY_LOW|ADD_POS|SELL_HIGH|PANIC_SELL)两点触发\(score=(\d+)分\)但仓控可交易量为0")
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
# F3-1/F3-3(2026-09-08): 主审计(14:50, 无 phase) 为首条；15:02-15:05 尾部(phase=tail_reconcile)为增量叠加。
# 归账口径 = 虚拟/模拟盘视角（实盘 qty/base 仅截图 reconcile 写，见 F-3 施工方案）。
audit_recs = []
for line in open(BASE / "t_io/logs/closure_audit.jsonl", encoding="utf-8"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get("date") == DATE:
        audit_recs.append(r)
closed = {}
_main_recs = [r for r in audit_recs if not r.get("phase")]
base = _main_recs[-1] if _main_recs else (audit_recs[-1] if audit_recs else None)
audit_today = base  # F3 重构后保持既有引用（:745/:802）可用
if base:
    for d in base.get("details", []):
        closed[d["code"]] = {k: d[k] for k in ("sold", "bought", "unrebuilt", "est_pnl", "qty_diff")}
        closed[d["code"]]["_virtual"] = True   # Q-20260904-6/F3: 归账为虚拟口径（实盘仅截图 reconcile）
for r in audit_recs:
    if r.get("phase") != "tail_reconcile":
        continue
    for d in r.get("details") or []:
        c = d.get("code")
        net = int(d.get("net", 0) or 0)
        if c in closed:
            closed[c]["_tail"] = True
            closed[c]["qty_diff"] = int(closed[c].get("qty_diff", 0) or 0) + net
        else:
            closed[c] = {"sold": 0, "bought": 0, "unrebuilt": 0, "est_pnl": 0.0,
                         "qty_diff": net, "_virtual": True, "_tail": True}

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
_s988 = sig_stat.get("000988") or {"buy_signals": 0, "sell_signals": 0}   # 施工4: 动态池可能不含 000988
_p988 = prof.get("000988") or {"day_type": "unknown"}
s988 = _s988["buy_signals"] + _s988["sell_signals"]
watch = {
    "#1_000988_qty冻结": {"suppressed_qty0": len(suppress.get("000988", [])), "pushed": [p for p in pushes if p["code"] == "000988"],
                        "audit": closed.get("000988"),
                        "note": "实盘当日其买信号被仓控0拦截次数; audit sold/bought=0 表示无成交"},
    "#2_000988_bull日买入": {"day_type": _p988["day_type"], "buy_signals": _s988["buy_signals"],
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
HOLDINGS_FP = STATE_DIR / "holdings.json"

def archive_holdings_snapshot(date):
    """P2-3C(2026-09-10) 重写归档规则（旧"已存在则跳过"会被晨跑旧值锁死 reconcile 前状态）：
    - 历史日期：只读护栏——已存在不覆盖（保留当时快照）。
    - date==今日：holdings.json mtime 非今日 → 不建档（防用旧值）；已存在且 holdings.json 更新则覆盖自愈。
    返回 (snap_path, created_or_updated)。
    """
    snap = STATE_DIR / f"holdings_{date}.json"
    if not HOLDINGS_FP.exists():
        return snap, False
    today = datetime.now().strftime("%Y-%m-%d")
    if date != today:
        if snap.exists():
            return snap, False
        shutil.copy2(HOLDINGS_FP, snap)
        return snap, True
    try:
        h_mtime = os.path.getmtime(HOLDINGS_FP)
    except Exception:
        h_mtime = 0
    if datetime.fromtimestamp(h_mtime).strftime("%Y-%m-%d") != today:
        return snap, False      # holdings.json 非今日 → 不建档
    if snap.exists():
        try:
            if h_mtime > os.path.getmtime(snap):   # 比快照新 → 覆盖自愈
                shutil.copy2(HOLDINGS_FP, snap)
                return snap, True
        except Exception:
            pass
        return snap, False
    shutil.copy2(HOLDINGS_FP, snap)
    return snap, True

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


# ---------- 10. OGR 开盘低开反转段（施工4，2026-09-27 清单V2.0 §八；主策略日内T） ----------
# 事件名（gm_main.py _ogr_try_buy/_ogr_try_sell 落盘口径）：
#   成交 ogr_buy / ogr_sell（status==3 回调才落账）；拒单 ogr_buy_rejected / ogr_sell_rejected；
#   下单 ogr_buy_submit / ogr_sell_submit；跳过 ogr_buy_skip / ogr_sell_skip；诊断 ogr_diag。
# 数据源：t_io/bridge/events_{yyyymmdd}.jsonl → t_io/logs/auto_backtrace.jsonl（镜像，按 time 前缀筛当日）
#   → execution/auto/gmcache/backtrace.jsonl；影子期读 t_io/logs/ogr_shadow_{date}.jsonl。
# 费口径：core/cost_model.py（股票往返 0.06908% / ETF 0.01908%）。无 OGR 活动日输出「无触发」不报错。
def _ogr_cost_model():
    import importlib.util as _ilu2
    _spec = _ilu2.spec_from_file_location("cost_model", BASE / "core" / "cost_model.py")
    _m = _ilu2.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    return _m


def _ogr_fee_venue(code):
    try:
        _h = json.load(open(BASE / "t_io" / "state" / "holdings.json", encoding="utf-8"))
        if str((_h.get(code) or {}).get("type") or "").lower() == "etf":
            return "etf"
    except Exception:
        pass
    return "stock"


def _ogr_events(date):
    """汇总当日 OGR 事件（多源去重；镜像/主审计文件为跨日追加，按 time 字段前缀筛当日）。"""
    evts, seen = [], set()

    def _take(fp, filter_date):
        if not fp.exists():
            return
        for line in open(fp, encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not str(e.get("event") or "").startswith("ogr"):
                continue
            if filter_date and not str(e.get("time") or e.get("buy_time") or "").startswith(date):
                continue
            key = json.dumps(e, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            evts.append(e)

    _take(BASE / f"t_io/bridge/events_{date.replace('-', '')}.jsonl", False)
    _take(BASE / "t_io/logs/auto_backtrace.jsonl", True)
    _take(BASE / "execution/auto/gmcache/backtrace.jsonl", True)
    return evts


ogr_evts = _ogr_events(DATE)
ogr_shadow_rows = []
_ogr_shadow_fp = BASE / f"t_io/logs/ogr_shadow_{DATE}.jsonl"
if _ogr_shadow_fp.exists():
    for _line in open(_ogr_shadow_fp, encoding="utf-8", errors="replace"):
        try:
            ogr_shadow_rows.append(json.loads(_line))
        except Exception:
            continue

ogr_stat = None
if ogr_evts:
    _cm = _ogr_cost_model()
    buys = sorted((e for e in ogr_evts if e.get("event") == "ogr_buy"), key=lambda e: str(e.get("time") or ""))
    sells = sorted((e for e in ogr_evts if e.get("event") == "ogr_sell"), key=lambda e: str(e.get("time") or ""))
    rejs = [e for e in ogr_evts if e.get("event") in ("ogr_buy_rejected", "ogr_sell_rejected")]
    skips = [e for e in ogr_evts if e.get("event") in ("ogr_buy_skip", "ogr_sell_skip")]
    submits = [e for e in ogr_evts if e.get("event") in ("ogr_buy_submit", "ogr_sell_submit")]
    _buy_q = defaultdict(list)
    for _b in buys:
        _buy_q[_b.get("code")].append(_b)
    legs = []
    for _s in sells:   # FIFO 配腿：卖成交 pop 同 code 最早买腿；买事件缺失时用卖事件自带 buy_px 兜底
        c = _s.get("code")
        _b = _buy_q[c].pop(0) if _buy_q.get(c) else None
        buy_px = (_b or {}).get("fill_px") or (_b or {}).get("ref_px") or _s.get("buy_px")
        sell_px = _s.get("fill_px") or _s.get("ref_px")
        qty = int(_s.get("qty") or (_b or {}).get("qty") or 0)
        if not (fnum(buy_px) and fnum(sell_px) and qty > 0):
            legs.append({"code": c, "qty": qty or None, "buy_px": buy_px, "sell_px": sell_px,
                         "pnl": None, "ret%": None, "note": "缺价/量，未核算"})
            continue
        buy_px, sell_px = float(buy_px), float(sell_px)
        fee_s, fee_b = _cm.fees(_ogr_fee_venue(c))
        pnl = qty * (sell_px * (1 - fee_s) - buy_px * (1 + fee_b))
        legs.append({"code": c, "qty": qty, "buy_px": round(buy_px, 4), "sell_px": round(sell_px, 4),
                     "pnl": round(pnl, 2), "ret%": round(_cm.leg_pnl("long", buy_px, sell_px, fee_s, fee_b), 4),
                     "note": "" if _b else "买事件缺失(用腿快照价)"})
    unclosed = [b for ql in _buy_q.values() for b in ql]   # 买成交但当日未卖平
    ogr_stat = {"legs": legs, "n_legs": len(buys), "n_fills": len(buys) + len(sells),
                "n_rejected": len(rejs), "n_skip": len(skips), "n_submit": len(submits),
                "pnl_total": round(sum(l["pnl"] for l in legs if fnum(l.get("pnl"))), 2),
                "unclosed": [{"code": b.get("code"), "qty": b.get("qty"),
                              "px": b.get("fill_px") or b.get("ref_px")} for b in unclosed]}


def ogr_md():
    L = ["", "<!-- OGR日段:begin -->", "", f"## OGR 开盘低开反转（{DATE}，触发/成交/拒单/逐腿费后盈亏）", ""]
    if not ogr_evts and not ogr_shadow_rows:
        L.append("- 当日 OGR **无触发**（无实单事件、无影子记录）。")
    else:
        if ogr_stat:
            s = ogr_stat
            L.append(f"- 触发腿 **{s['n_legs']}** ｜ 成交 **{s['n_fills']}** 笔 ｜ 拒单 **{s['n_rejected']}** ｜ "
                     f"跳过 {s['n_skip']} ｜ 下单 {s['n_submit']} ｜ 费后合计盈亏 **{s['pnl_total']:+.2f}** 元"
                     + (f" ｜ ⚠️ 未平腿 {len(s['unclosed'])} 条" if s["unclosed"] else ""))
            if s["legs"]:
                L += ["", "| 代码 | 股数 | 买价 | 卖价 | 费后盈亏(元) | 费后收益% | 备注 |",
                      "|---|---|---|---|---|---|---|"]
                for l in s["legs"]:
                    L.append(f"| {l['code']} | {l.get('qty') or '—'} | {l.get('buy_px') or '—'} "
                             f"| {l.get('sell_px') or '—'} | "
                             + (f"{l['pnl']:+.2f} | {l['ret%']:+.4f} |" if fnum(l.get("pnl")) else "— | — |")
                             + f" {l.get('note') or '—'} |")
            if s["unclosed"]:
                L.append("")
                L.append("- 未平腿：" + "；".join(f"{u['code']} {u.get('qty')}股@{u.get('px')}" for u in s["unclosed"]))
        elif ogr_evts:
            L.append(f"- 当日 OGR 事件 {len(ogr_evts)} 条（无成交腿）。")
        if ogr_shadow_rows:
            _sh = ogr_shadow_rows[-1]
            L.append(f"- 影子记录 {len(ogr_shadow_rows)} 条：池 {_sh.get('pool_n')} 只 ｜ "
                     f"mkt_gap {_sh.get('mkt_gap')} ｜ 触发 {_sh.get('n_tradable')} 只 "
                     f"{_sh.get('tradable') or ''}（影子期，未下单）。")
        L.append("")
        L.append("> 费口径 core/cost_model.py：股票往返 0.06908% / ETF 0.01908%（逐腿费后=卖×(1−费卖)−买×(1+费买)）。")
    L += ["", "<!-- OGR日段:end -->", ""]
    return "\n".join(L)


# ---------- 11. α/β 收益日统计段（施工4，清单 §八 口径钉死） ----------
# 净值法：equity=持仓市值(EOD收盘重估)+现金；account_ret=(equity−flow_adjust)/prev_equity−1；
# alpha=account_ret−benchmark_ret（主基准沪深300 sh000300，辅基准科创50 sh000688）；费后统一；
# 禁止持仓市值法；现金取不到时 equity/alpha 为 null 并标注，不硬算。
def _fmt_pct(x):
    return "—" if not fnum(x) else f"{x * 100:+.3f}%"


def alpha_beta_md():
    fp = BASE / f"t_io/metrics/equity_daily_{DATE}.json"
    L = ["", "<!-- αβKPI:begin -->", "", f"## α/β 收益日统计（{DATE}，净值法·费后）", ""]
    if not fp.exists():
        L.append(f"⚪ **α/β 未落盘**（t_io/metrics/equity_daily_{DATE}.json 不存在）")
    else:
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception as _e:
            d = None
            L.append(f"⚪ **α/β 读取失败**：{_e}")
        if d:
            if d.get("equity") is None:
                L.append(f"- equity **—**（现金缺失，equity/r_t/α 置 null，不硬算；source: {d.get('source') or '—'}）")
            else:
                L.append(f"- equity **{d['equity']:,.2f}**（市值 {d.get('market_value')} ＋ 现金 {d.get('cash')}）")
            # 内层格式化先取出：原来写成嵌套同引号 f-string（f'...{d['x']}...'），
            # 那是 PEP 701 语法，**只有 3.12+ 能编译**，3.11 直接 SyntaxError。
            _t0v = d.get('t0_realized')
            _t0s = '—' if not fnum(_t0v) else f"{_t0v:+,.2f}元"
            L.append(f"- r_t(账户) **{_fmt_pct(d.get('account_ret'))}** ｜ β(沪深300) **{_fmt_pct(d.get('benchmark_ret'))}** ｜ "
                     f"β_aux(科创50) {_fmt_pct(d.get('benchmark_aux_ret'))} ｜ **α {_fmt_pct(d.get('alpha'))}** ｜ "
                     f"t0_realized {_t0s}")
            # 累计 α：历遍 metrics/equity_daily_*.json（≤当日，alpha 非 null 求和）
            cum, n_cum = 0.0, 0
            for _f in sorted((BASE / "t_io/metrics").glob("equity_daily_*.json")):
                _fd = _f.stem.replace("equity_daily_", "")
                if _fd > DATE:
                    continue
                try:
                    _a = json.load(open(_f, encoding="utf-8")).get("alpha")
                except Exception:
                    continue
                if fnum(_a):
                    cum += float(_a)
                    n_cum += 1
            L.append(f"- 累计 α（{n_cum} 个有效落盘日）: **{cum * 100:+.3f}%**")
    L += ["", "<!-- αβKPI:end -->", ""]
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
    # 施工4: OGR 开盘低开反转段（无活动日也注入「无触发」，幂等护栏沿用既有模式）
    og = ogr_md()
    if "<!-- OGR日段:begin -->" in txt:
        txt = re.sub(r"<!-- OGR日段:begin -->.*?<!-- OGR日段:end -->",
                     og.strip().replace("\n\n<!-- OGR日段:end -->", "\n<!-- OGR日段:end -->")
                     .replace("<!-- OGR日段:begin -->\n\n", "<!-- OGR日段:begin -->\n"),
                     txt, flags=re.S)
    else:
        txt = txt.rstrip() + "\n" + og
    # 施工4: α/β 收益日统计段（报告尾部新段；未落盘输出 ⚪ 不报错）
    ab = alpha_beta_md()
    if "<!-- αβKPI:begin -->" in txt:
        txt = re.sub(r"<!-- αβKPI:begin -->.*?<!-- αβKPI:end -->",
                     ab.strip().replace("\n\n<!-- αβKPI:end -->", "\n<!-- αβKPI:end -->")
                     .replace("<!-- αβKPI:begin -->\n\n", "<!-- αβKPI:begin -->\n"),
                     txt, flags=re.S)
    else:
        txt = txt.rstrip() + "\n" + ab
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
          "resonance": {"rows": resonance_rows, "groups": resonance_groups},
          "ogr": {"events": len(ogr_evts), "stat": ogr_stat, "shadow_n": len(ogr_shadow_rows)},
          "alpha_beta_file": str(BASE / f"t_io/metrics/equity_daily_{DATE}.json")}
with open(OUT / f"daily_review_{DATE}.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

print(f"== {DATE} 日复盘数据摘要 ==")
print(f"CODES(动态持仓池 {len(CODES)}): {CODES}")
print(f"OGR: 事件{len(ogr_evts)} 影子{len(ogr_shadow_rows)} "
      + (f"腿{ogr_stat['n_legs']} 成交{ogr_stat['n_fills']} 拒单{ogr_stat['n_rejected']} 费后{ogr_stat['pnl_total']:+.2f}元" if ogr_stat else "无触发"))
print(f"α/β: {'已落盘' if (BASE / f't_io/metrics/equity_daily_{DATE}.json').exists() else '⚪ 未落盘'}")
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

# ---------- F-8(2026-09-04): 自动复盘推飞书摘要（W-20260904-1 owner 拍板） ----------
try:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))
    try:
        from config import send_feishu_payload as _sfp, FEISHU_WEBHOOK as _fbh, FEISHU_KEYWORD as _fkw
    except Exception:
        _sfp, _fbh, _fkw = None, None, "做T猎手预警"
    if _sfp and _fbh:
        _rfp = BASE / "t_io/state/review_pushed.json"
        try:
            _pd = json.loads(_rfp.read_text(encoding="utf-8")) if _rfp.exists() else {}
        except Exception:
            _pd = {}
        if _pd.get(DATE) != "pushed":
            _tb = sum((v or {}).get("buy_signals", 0) for v in sig_stat.values())
            _ts = sum((v or {}).get("sell_signals", 0) for v in sig_stat.values())
            _tcode = sum(1 for v in sig_stat.values() if (v or {}).get("buy_signals") or (v or {}).get("sell_signals"))
            _cl = len(closed) if isinstance(closed, (list, dict)) else 0
            _prob = len((audit_today or {}).get("problems", [])) if isinstance(audit_today, dict) else 0
            _sample = 0
            try:
                _so = json.loads((BASE / "t_io/validation/signal_outcomes.json").read_text(encoding="utf-8"))
                _recs = _so.get("records") or [] if isinstance(_so, dict) else []
                _sample = len(_recs)
            except Exception:
                pass
            _txt = "\n".join([
                f"**{DATE} 日复盘自动摘要**", "",
                f"🔴 做T信号 **{_tb}买/{_ts}卖** · {_tcode} 只触发",
                f"✅ 做T闭环 {_cl} 笔 · 审计问题 {_prob} 条",
                f"📈 前瞻样本 {_sample} 条",
                f"📄 报告: doc/每日复盘/{DATE}_复盘.md",
            ])
            _card = {"msg_type": "interactive", "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": f"📊 日复盘摘要 - {_fkw}"}, "template": "blue"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": _txt}}]}}
            _ok = _sfp(_card, success_log=f"日复盘摘要已推送: {DATE}", error_prefix="日复盘摘要推送")
            if _ok:
                try:
                    _pd[DATE] = "pushed"
                    _rfp.write_text(json.dumps(_pd, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
except Exception as _e:
    print(f"[warn] 日复盘推飞书失败: {_e}")
