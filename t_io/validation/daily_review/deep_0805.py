# -*- coding: utf-8 -*-
"""2026-08-05 深度复盘数据聚合 v2（分析-only）"""
import json, collections, re
from pathlib import Path

BASE = Path(r"E:\06_T")
DATE = "2026-08-05"
out = {}

# 1) closure_audit 今日记录
audits = []
with open(BASE / "t_io/logs/closure_audit.jsonl", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("date") == DATE or str(e.get("ts", "")).startswith(DATE) or str(e.get("close_date", "")) == DATE:
            audits.append(e)
out["closure_audit_today_n"] = len(audits)
out["closure_audit_today"] = audits[-10:]

# 2) decision_trace 聚合
trace_fp = BASE / f"t_io/traces/decision_trace_{DATE}.jsonl"
factor_sum = collections.defaultdict(lambda: collections.defaultdict(float))
factor_n = collections.defaultdict(int)
sig_events = collections.defaultdict(list)
decision_reason_c = collections.Counter()
buy_block_c = collections.Counter()
sell_block_c = collections.Counter()
decision_c = collections.Counter()

with open(trace_fp, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        code = str(e.get("code", ""))
        dec = e.get("decision", "")
        decision_c[dec] += 1
        if e.get("decision_reason") and dec != "HOLD":
            decision_reason_c[f"{dec}:{e['decision_reason']}"] += 1
        for b in (e.get("buy_block") or []):
            buy_block_c[str(b)] += 1
        for b in (e.get("sell_block") or []):
            sell_block_c[str(b)] += 1
        for key in ("sell_factors", "buy_factors"):
            facs = e.get(key)
            if isinstance(facs, dict) and code:
                for fk, fv in facs.items():
                    if isinstance(fv, (int, float)):
                        factor_sum[code][fk] += fv
                        factor_n[(code, fk)] += 1
        if dec in ("SELL_HIGH", "BUY_LOW", "SELL", "BUY", "NOTIFY_SELL", "NOTIFY_BUY") or "SELL" in dec or "BUY" in dec:
            sig_events[code].append({
                "ts": e.get("scan_time"),
                "dec": dec,
                "reason": e.get("decision_reason"),
                "sell_score": e.get("sell_score"),
                "buy_score": e.get("buy_score"),
                "qty": e.get("qty"),
                "sell_block": e.get("sell_block"),
                "buy_block": e.get("buy_block"),
            })

out["decision_counter"] = dict(decision_c)
out["decision_reason_nonHOLD"] = decision_reason_c.most_common(20)
out["buy_block_counter"] = buy_block_c.most_common(15)
out["sell_block_counter"] = sell_block_c.most_common(15)
out["events_002639"] = sig_events.get("002639", [])[:40]
out["events_603667"] = sig_events.get("603667", [])[:40]
out["events_300153"] = sig_events.get("300153", [])[:15]
out["events_588170_hi"] = [x for x in sig_events.get("588170", []) if (x.get("sell_score") or 0) >= 55][:20]
out["events_000988"] = sig_events.get("000988", [])[:15]

factor_avg = {}
for code, facs in factor_sum.items():
    factor_avg[code] = sorted(
        ((fk, round(fv / max(factor_n[(code, fk)], 1), 2), factor_n[(code, fk)]) for fk, fv in facs.items()),
        key=lambda x: -abs(x[1]))[:8]
out["factor_avg_top"] = factor_avg

# 3) shadow_signals：冻结股全部 + 其他高分
shadow_fp = BASE / f"t_io/traces/shadow_signals_{DATE}.jsonl"
sh_focus, sh_high = [], []
shadow_keys_sample = None
with open(shadow_fp, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        if shadow_keys_sample is None:
            shadow_keys_sample = list(e.keys())
        code = str(e.get("code", ""))
        sc = e.get("score") or e.get("sell_score") or e.get("buy_score")
        rec = {"ts": e.get("scan_time") or e.get("ts"), "code": code,
               "sig": e.get("signal") or e.get("action") or e.get("decision"),
               "score": sc,
               "reason": e.get("miss_reason") or e.get("reason") or e.get("decision_reason"),
               "qty": e.get("qty")}
        if code in ("603667", "002639"):
            sh_focus.append(rec)
        elif isinstance(sc, (int, float)) and sc >= 52:
            sh_high.append(rec)
out["shadow_keys"] = shadow_keys_sample
out["shadow_focus_n"] = len(sh_focus)
out["shadow_focus"] = sh_focus[:60]
out["shadow_high_n"] = len(sh_high)
out["shadow_high"] = sh_high[:40]

# 4) 买入熔断证据：日志关键词
fuse_hits = []
log_dir = BASE / "t_io/logs"
for lf in sorted(log_dir.glob("*.log")) + sorted(log_dir.glob(f"*{DATE}*.jsonl")):
    try:
        txt = lf.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    if DATE not in txt[:500000] and lf.stat().st_size > 5_000_000:
        pass
    for m in re.finditer(r".{0,50}(熔断|uni_down|BUY_FUSE|buy_fuse|fuse_buy|BUY_BLOCKED).{0,50}", txt):
        if DATE in m.group(0) or lf.name.endswith(DATE + ".jsonl") or "2026-08-05" in m.group(0):
            fuse_hits.append({"file": lf.name, "hit": m.group(0)[:140]})
        if len(fuse_hits) >= 20:
            break
    if len(fuse_hits) >= 20:
        break
out["buy_fuse_evidence"] = fuse_hits[:20]

with open(BASE / "t_io/validation/daily_review/deep_2026-08-05.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2, default=str)
print("OK", {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in out.items()})
