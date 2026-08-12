# -*- coding: utf-8 -*-
"""probe_0811.py — 08-11 V1.2.1 首日专项取证"""
import json
from collections import Counter
from pathlib import Path

BASE = Path(r"E:\06_T")


def load(fp):
    return [json.loads(l) for l in Path(fp).read_text(encoding="utf-8").splitlines() if l.strip()]


trace = load(BASE / "t_io/traces/decision_trace_2026-08-11.jsonl")

# --- 1) 地板闸：全日 trace 不应再出现 sell_floor_protect ---
floor_hits = [r for r in trace if any("sell_floor_protect" in b for b in (r.get("sell_block") or []))]
print(f"[地板闸] trace 含 sell_floor_protect 的 tick 数 = {len(floor_hits)}（V1.2.1 默认关，预期 0；昨日前身 588170 94 tick）")
ms = [r for r in trace if any("max_sell_times" in b for b in (r.get("sell_block") or []))]
print(f"[轮次上限] max_sell_times 拦截 tick 数 = {len(ms)}（保留项，应正常工作）")

# --- 2) 接回：588170 awaiting_buyback ---
bb = [r for r in trace if r["code"] == "588170" and "接回追踪(已卖待接)" in (r.get("buy_factors") or {})]
combos = Counter((r["decision_reason"], tuple(r.get("buy_block") or [])) for r in bb)
print(f"[接回 588170] ticks={len(bb)}")
for k, v in combos.most_common(6):
    print(f"    {v:4d}x reason={k[0]} buy_block={list(k[1])}")
for r in bb:
    if r["decision"] != "HOLD":
        print(f"    !! 非HOLD: {r['scan_time']} {r['decision']} buy_score={r['buy_score']}")

# --- 3) 600481 买推送场景还原（满仓/保底证据） ---
b481 = [r for r in trace if r["code"] == "600481" and r["decision"] in ("BUY_LOW", "ADD_POS")]
print(f"[600481] 引擎买决策 {len(b481)} 条:")
for r in b481:
    print(f"    {r['scan_time']} score={r['buy_score']} 阈={r['buy_threshold']} block={r['buy_block']}")

# --- 4) 000988 唯一 qty=0 拦截溯源：其买决策 block ---
b988 = [r for r in trace if r["code"] in ("000988", "000988_B") and r["decision"] in ("BUY_LOW", "ADD_POS")]
print(f"[000988/_B] 引擎买决策 {len(b988)} 条: {[(r['code'], r['scan_time'][11:], r['buy_score']) for r in b988]}")

# --- 5) shadow cap 检查 ---
sh = load(BASE / "t_io/traces/shadow_signals_2026-08-11.jsonl")
cap = [r for r in sh if "cap" in str(r.get("miss_reason", ""))]
print(f"[shadow] total={len(sh)} cap拦截={len(cap)} 原因分布={Counter(r.get('miss_reason','?') for r in sh)}")

# --- 6) 300153 买决策（双层阈口径） ---
b153 = [r for r in trace if r["code"] == "300153" and r["decision"] in ("BUY_LOW", "ADD_POS")]
if b153:
    sc = [r["buy_score"] for r in b153 if isinstance(r.get("buy_score"), (int, float))]
    print(f"[300153] 买决策 {len(b153)} 条 分数 {min(sc):.1f}~{max(sc):.1f}（通知阈68 静默口径）")

# --- 7) 收盘（顶背离/K10 + 日型核对） ---
hd = json.loads((BASE / "t_io/state/holdings_daily_2026-08-11.json").read_text(encoding="utf-8"))
for h in hd["holdings"]:
    if h.get("qty"):
        print(f"[收盘] {h['code']} {h['name']} price={h['price']} change={h['change_pct']}% qty={h['qty']} t={h['t_qty']} cost={h['cost']}")

# --- 8) 建仓扫描 ---
pb = load(BASE / "t_io/traces/position_builder_2026-08-11.jsonl")
print(f"[建仓] records={len(pb)} verdict={Counter(r['verdict'] for r in pb)}")
for r in pb:
    if r["verdict"] == "signal":
        print(f"   signal {r['scan_time'][11:]} {r['code']} {r['name']} {r['composite_score']} qty={r['suggested_qty']} in_hold={r['in_holdings']}")
