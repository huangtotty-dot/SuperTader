# -*- coding: utf-8 -*-
"""08-10 复盘批量取证：接回场景/300153口径/顶背离/建仓扫描/shadow类型"""
import json
from collections import Counter
from pathlib import Path

BASE = Path(r"E:\06_T")

def load(fp):
    return [json.loads(l) for l in Path(fp).read_text(encoding="utf-8").splitlines() if l.strip()]

trace = load(BASE / "t_io/traces/decision_trace_2026-08-10.jsonl")

# --- 1) 接回场景：588170 / 600176 ---
for code in ("588170", "600176"):
    bb = [r for r in trace if r["code"] == code and "接回追踪(已卖待接)" in (r.get("buy_factors") or {})]
    combos = Counter((r["decision_reason"], tuple(r.get("buy_block") or [])) for r in bb)
    scores = [r["buy_score"] for r in bb if isinstance(r.get("buy_score"), (int, float))]
    print(f"[接回 {code}] ticks={len(bb)} 首={bb[0]['scan_time'] if bb else '-'} 末={bb[-1]['scan_time'] if bb else '-'} "
          f"买max={max(scores) if scores else None} 阈={bb[0]['buy_threshold'] if bb else '-'}")
    for k, v in combos.most_common():
        print(f"    {v:4d}x reason={k[0]} buy_block={list(k[1])}")
    for r in bb:
        if r["decision"] != "HOLD":
            print(f"    !! 非HOLD: {r['scan_time']} {r['decision']} score={r['buy_score']}")

# --- 2) 300153 买决策口径 ---
b153 = [r for r in trace if r["code"] == "300153" and r["decision"] in ("BUY_LOW", "ADD_POS")]
if b153:
    scores = [r["buy_score"] for r in b153 if isinstance(r.get("buy_score"), (int, float))]
    print(f"[300153] 引擎买决策={len(b153)} 分数 {min(scores):.1f}~{max(scores):.1f} "
          f"trace阈={b153[0]['buy_threshold']} 首={b153[0]['scan_time']} 末={b153[-1]['scan_time']}")
    print(f"    reason分布={Counter(r['decision_reason'] for r in b153)}")
# 600481 买决策
b481 = [r for r in trace if r["code"] == "600481" and r["decision"] in ("BUY_LOW", "ADD_POS")]
if b481:
    print(f"[600481] 引擎买决策={len(b481)} 分数={[round(r['buy_score'],1) for r in b481]} 阈={b481[0]['buy_threshold']}")

# --- 3) shadow 类型分布 ---
sh = load(BASE / "t_io/traces/shadow_signals_2026-08-10.jsonl")
print(f"[shadow] total={len(sh)} 类型={Counter(r.get('reason', r.get('type', '?')) for r in sh)}")
near = [r for r in sh if r.get("near_threshold")]
print(f"[shadow near±3] {len(near)} 条: " + "; ".join(
    f"{r['code']}@{r.get('ts','?')[11:]} {r.get('side','?')}{r.get('score',0):.1f}" for r in near[:20]))

# --- 4) 顶背离跟踪：588170 08-07 11:05 报警价 vs 今日 ---
hd = json.loads((BASE / "t_io/state/holdings_daily_2026-08-10.json").read_text(encoding="utf-8"))
def _find(d, code):
    if isinstance(d, dict):
        for k, v in d.items():
            if k == code:
                return v
            r = _find(v, code)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = _find(v, code)
            if r is not None:
                return r
    return None
row = _find(hd, "588170")
print(f"[顶背离 588170] holdings_daily 行={json.dumps(row, ensure_ascii=False)[:200] if row else '未找到'}")

# --- 5) 建仓扫描 ---
pb = load(BASE / "t_io/traces/position_builder_2026-08-10.jsonl")
print(f"[建仓扫描] records={len(pb)}")
for r in pb[:3]:
    print("   ", json.dumps(r, ensure_ascii=False)[:300])

# --- 6) 全日推送核对：trace 里 score>=36 的买/>=55 卖 决策数（对照推送3笔） ---
print("[K4] 见 kpi json（脚本外已输出）")
