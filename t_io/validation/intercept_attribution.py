# -*- coding: utf-8 -*-
"""
intercept_attribution.py — X8: v108 统一口径"被拦信号"归因分解
被拦信号 = baseline 信号中按 (ts,code) 精确匹配在 v102 无对应的信号(与 churn 口径一致)。
对每条被拦信号还原 v102 当时状态: trend_state/confidence(trend_timeline)、v102 分数与决策(decision_trace)、
时段、日涨幅语境; 分类门控动作; 输出 intercept_attribution.json + 分层胜率表 + 修复影响估计。
"""
import json, glob, os, sys
from collections import Counter, defaultdict
from pathlib import Path
import pandas as pd

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/v108_unified"
TS_DIR = BASE / "t_io/minute_snapshots_ts"

def load_signals(mode):
    return [json.loads(l) for l in open(ROOT / mode / f"signals_{mode}.jsonl", encoding="utf-8") if l.strip()]

def slot(hhmm):
    m = int(hhmm[:2]) * 60 + int(hhmm[3:5])
    if m < 600: return "早盘09:30-10:00"
    if m < 690: return "午前10:00-11:30"
    if m < 870: return "午后13:00-14:30"
    return "尾盘14:30-15:00"

def conf_bucket(state, conf):
    if state == "NEUTRAL": return "NEUTRAL(conf=0)"
    if conf <= 0.55: return "conf<=0.55"
    return "conf>0.55"

def main():
    b_sigs = load_signals("baseline")
    v_sigs = load_signals("v102")
    v_keys = {(s["ts"], s["code"]) for s in v_sigs}
    intercepted = [s for s in b_sigs if (s["ts"], s["code"]) not in v_keys]
    print(f"intercepted: {len(intercepted)} (baseline={len(b_sigs)} v102={len(v_sigs)})")

    # v102 trend_timeline
    timelines = {}
    for l in open(ROOT / "v102/trend_timeline_v102.jsonl", encoding="utf-8"):
        r = json.loads(l)
        d, code = r["key"].split(":")
        timelines[(d, code)] = r["timeline"]

    def trend_at(d, code, hhmm):
        tl = timelines.get((d, code))
        if not tl: return None, None
        cur_s, cur_c = None, None
        for t, s, c in tl:
            if t <= hhmm: cur_s, cur_c = s, c
            else: break
        return cur_s, cur_c

    # v102 decision_trace 索引 (v108 优先, v107 复用块补充)
    trace = {}
    for root_tag, root in (("expanded", BASE / "t_io/validation/v107_ab_expanded/parts"),
                           ("unified", ROOT / "parts")):
        for f in glob.glob(str(root / "v102_*/decision_trace_*.jsonl")):
            for l in open(f, encoding="utf-8"):
                r = json.loads(l)
                k = (r["code"], r["scan_time"])
                if k not in trace or root_tag == "unified":
                    trace[k] = r
    print(f"trace rows indexed: {len(trace)}")

    # 日涨幅语境 (当日首bar open -> 末bar close)
    day_ctx = {}
    for code in {s["code"] for s in intercepted}:
        for f in glob.glob(str(TS_DIR / "2026" / "*" / f"{code}_20*.json")):
            d = os.path.basename(f).replace(".json", "").split("_", 1)[1]  # 已是 2026-06-04 形式
            bars = json.load(open(f, encoding="utf-8"))["bars"]
            if bars:
                day_ctx[(d, code)] = round((bars[-1]["close"] / bars[0]["open"] - 1) * 100, 2)

    # v102 信号同日索引(近邻匹配用)
    from datetime import datetime as _dt
    v_by_code_day = defaultdict(list)
    for s in v_sigs:
        v_by_code_day[(s["code"], s["ts"][:10])].append(s["ts"])

    def near_v102_ts(code, d, ts, win_min=10):
        t0 = _dt.strptime(ts, "%Y-%m-%d %H:%M:%S")
        for t in v_by_code_day.get((code, d), []):
            if abs((_dt.strptime(t, "%Y-%m-%d %H:%M:%S") - t0).total_seconds()) <= win_min * 60:
                return t
        return None

    rows = []
    no_trace = 0
    for s in intercepted:
        d, hhmm = s["ts"][:10], s["ts"][11:16]
        code = s["code"]
        state, conf = trend_at(d, code, hhmm)
        tr = trace.get((code, s["ts"]))
        if tr is None:
            no_trace += 1
            v_score = v_th = None
            reason = "NO_TRACE"
            blocked = False
        else:
            v_score = tr.get("sell_score")
            v_th = tr.get("sell_threshold")
            reason = tr.get("decision_reason") or ""
            blocked = bool(tr.get("sell_block"))
        # 门控动作分类
        if tr is None:
            gate = "no_trace"
        elif tr["decision"] == "SELL_HIGH":
            gate = "v102_also_decided_sell"  # v102 同时刻也想卖(信号可能因段去重落在相邻分钟)
        elif blocked and "strong_uptrend" in reason:
            gate = "trend_gate_block"      # BULL 态强 uptrend 门控拦截
        elif blocked:
            gate = "trend_gate_block_partial"  # 有 block 记录但主因是低于阈值
        elif "BELOW_THRESHOLD" in reason:
            gate = "score_reconstructed_below"  # 分数重构后低于阈值
        else:
            gate = "other"
        near_ts = near_v102_ts(code, d, s["ts"])
        rows.append({
            "ts": s["ts"], "code": code, "settle": s.get("settle_result"),
            "baseline_sell_score": s.get("sell_score"), "baseline_price": s.get("price"),
            "v102_sell_score": v_score, "v102_sell_threshold": v_th,
            "notify_threshold": 75 if hhmm < "10:00" else 65,
            "score_delta": (round(s["sell_score"] - v_score, 1)
                            if v_score is not None and s.get("sell_score") is not None else None),
            "trend_state": state, "trend_confidence": conf,
            "conf_bucket": conf_bucket(state, conf) if state else "no_timeline",
            "slot": slot(hhmm), "gate_action": gate, "v102_reason": reason,
            "v102_near_ts": near_ts,  # ±10min 内 v102 实际发出的同向信号
            "truly_intercepted": near_ts is None,
            "day_pct": day_ctx.get((d, code)),
        })
    print(f"no_trace: {no_trace}")
    json.dump(rows, open(ROOT / "intercept_attribution.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # ---- 分层胜率表 ----
    def strat_table(key_fn, name):
        groups = defaultdict(list)
        for r in rows:
            groups[key_fn(r)].append(r)
        table = []
        for k in sorted(groups):
            g = groups[k]
            w = sum(1 for r in g if r["settle"] == "WIN")
            f = sum(1 for r in g if r["settle"] == "FAIL")
            vo = sum(1 for r in g if r["settle"] == "VOID")
            table.append({"stratum": k, "n": len(g), "WIN": w, "FAIL": f, "VOID": vo,
                          "误拦率(WIN/(WIN+FAIL))": round(w / (w + f), 4) if (w + f) else None})
        return table

    out = {
        "match_rule": "被拦=baseline信号按(ts,code)精确匹配在v102无对应; truly_intercepted=±10min内v102亦无同向信号",
        "n_intercepted": len(rows),
        "n_near_matched": sum(1 for r in rows if not r["truly_intercepted"]),
        "n_truly_intercepted": sum(1 for r in rows for x in [r] if r["truly_intercepted"]),
        "fail_share_P2②_exact_ts口径": round(sum(1 for r in rows if r["settle"] == "FAIL") / len(rows), 4),
        "fail_share_truly口径": round(
            sum(1 for r in rows if r["truly_intercepted"] and r["settle"] == "FAIL") /
            sum(1 for r in rows if r["truly_intercepted"]), 4)
            if any(r["truly_intercepted"] for r in rows) else None,
        "by_trend_state": strat_table(lambda r: r["trend_state"] or "none", "trend_state"),
        "by_conf_bucket": strat_table(lambda r: r["conf_bucket"], "conf_bucket"),
        "by_slot": strat_table(lambda r: r["slot"], "slot"),
        "by_gate_action": strat_table(lambda r: r["gate_action"], "gate_action"),
        "by_state_x_gate": strat_table(lambda r: f'{r["trend_state"]}|{r["gate_action"]}', "state_x_gate"),
        "by_day_pct": strat_table(
            lambda r: ("无数据" if r["day_pct"] is None
                       else "当日涨>1%" if r["day_pct"] > 1
                       else "当日跌<-1%" if r["day_pct"] < -1
                       else "震荡±1%"), "day_pct"),
        # 真被拦子集的分层(核心)
        "truly_by_gate_action": [t for t in strat_table(
            lambda r: r["gate_action"] if r["truly_intercepted"] else "__near__", "g")
            if t["stratum"] != "__near__"],
        "truly_by_state_x_gate": [t for t in strat_table(
            lambda r: f'{r["trend_state"]}|{r["gate_action"]}' if r["truly_intercepted"] else "__near__", "g")
            if t["stratum"] != "__near__"],
        "truly_by_slot": [t for t in strat_table(
            lambda r: r["slot"] if r["truly_intercepted"] else "__near__", "g")
            if t["stratum"] != "__near__"],
        "v102_also_decided_sell_未记录原因": dict(Counter(
            ("score<notify" if (r["v102_sell_score"] is not None and r["v102_sell_score"] < r["notify_threshold"])
             else "段去重/其他")
            for r in rows if r["gate_action"] == "v102_also_decided_sell")),
    }
    json.dump(out, open(ROOT / "intercept_strata.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    for k in ("by_trend_state", "by_slot", "by_gate_action", "by_day_pct",
              "truly_by_gate_action", "truly_by_state_x_gate", "truly_by_slot"):
        print(f"\n== {k} ==")
        for t in out[k]:
            print(" ", json.dumps(t, ensure_ascii=False))

if __name__ == "__main__":
    main()
