# -*- coding: utf-8 -*-
"""fix5 backtrace.jsonl 通道级分析：run_id/通道分布/PANIC次数/拒单/趋势状态"""
import json
from collections import Counter, defaultdict

p = r"C:\Users\Lenovo\.goldminer3\projects\e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7\gmcache\backtrace.jsonl"
events = Counter()
run_ids = Counter()
actions = Counter()
rejects = []
channel_by_code = defaultdict(Counter)
panic_events = []
trail_events = []
trend_exit_events = []
dates = set()

with open(p, encoding="utf-8") as f:
    for line in f:
        try:
            d = json.loads(line)
        except Exception:
            continue
        ev = d.get("event", "?")
        events[ev] += 1
        run_ids[d.get("_run_id", "<missing>")] += 1
        if "date" in d:
            dates.add(str(d["date"])[:10])
        if "time" in d:
            dates.add(str(d["time"])[:10])
        act = d.get("action") or d.get("sig_action") or ""
        if act:
            actions[act] += 1
        code = d.get("code", "")
        if ev == "sell":
            channel_by_code[code][act or "unknown"] += 1
            if act == "PANIC_SELL":
                panic_events.append(d)
            elif act == "TRAIL_SELL":
                trail_events.append(d)
            elif act == "TREND_EXIT":
                trend_exit_events.append(d)
        if ev in ("reject", "rejected"):
            rejects.append(d)

print("=== 事件类型 ===")
print(dict(events))
print("\n=== run_id 分布（N12验收）===")
print(dict(run_ids))
print("\n=== action 分布 ===")
print(dict(actions))
print("\n=== 卖出按票按通道 ===")
for c, ct in channel_by_code.items():
    print(" ", c, dict(ct))
print("\n=== PANIC_SELL 事件（commit声称9次）===", len(panic_events))
for d in panic_events[:12]:
    print(" ", d.get("time"), d.get("code"), d.get("qty"), d.get("price"), d.get("reasons", d.get("reason", "")))
print("\n=== TRAIL_SELL 事件 ===", len(trail_events))
for d in trail_events[:12]:
    print(" ", d.get("time"), d.get("code"), d.get("qty"), d.get("price"), d.get("reasons", d.get("reason", "")))
print("\n=== TREND_EXIT 事件 ===", len(trend_exit_events))
for d in trend_exit_events[:12]:
    print(" ", d.get("time"), d.get("code"), d.get("qty"), d.get("price"))
print("\n=== 拒单 ===", len(rejects))
for d in rejects[:8]:
    print(" ", d)
print("\n=== 日期范围 ===", min(dates), "~", max(dates), " 天数:", len(dates))
