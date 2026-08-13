# -*- coding: utf-8 -*-
"""
merge_ab_expanded.py — 合并 v107_ab_expanded/parts 为两组全套产物
输出: v107_ab_expanded/{mode}/signals_{mode}.jsonl, summary_{mode}.json,
      trend_timeline_{mode}.jsonl, report_{mode}.txt
汇总指标(p1_metrics/closed_loop)用 harness 实际函数在合并数据上重算一次。
"""
import json, os, sys
from pathlib import Path
from collections import defaultdict

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/v107_ab_expanded"
PARTS = ROOT / "parts"
sys.path.insert(0, str(BASE))
import harness_backtest as hb

MODES = ["baseline", "v102"]

def collect(mode):
    sig_files = sorted(PARTS.glob(f"{mode}_*/signals_{mode}.jsonl"))
    signals, timelines, daily_stats = [], {}, {}
    seg_days = set()
    for f in sig_files:
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line:
                signals.append(json.loads(line))
        d = f.parent
        tl = d / f"trend_timeline_{mode}.jsonl"
        if tl.exists():
            for line in open(tl, encoding="utf-8"):
                r = json.loads(line)
                timelines[r["key"]] = r["timeline"]
        sm = d / f"summary_{mode}.json"
        if sm.exists():
            s = json.load(open(sm, encoding="utf-8"))
            for k, v in s.get("daily_stats", {}).items():
                daily_stats[k] = v  # 段间日期不重叠，直接合并
    signals.sort(key=lambda s: (s["ts"], s["code"]))
    return signals, timelines, daily_stats, len(sig_files)

def main():
    holdings = json.load(open(BASE / "holdings.json", encoding="utf-8"))
    hmap = {k.split("_")[0]: v for k, v in holdings.items()}
    codes = ["000988", "588170", "600176", "600481", "603667"]

    # day_bars 全样本构建一次（p1_metrics 需要）
    all_dates = sorted({s["ts"][:10] for s in collect("v102")[0]} |
                       {s["ts"][:10] for s in collect("baseline")[0]})
    day_bars_cache = {}
    for d in all_dates:
        day_bars = {}
        for c in codes:
            df = hb.load_snapshots(c, d)
            if not df.empty:
                day_bars[c] = df
        day_bars_cache[d] = day_bars
    print(f"day_bars: {len(day_bars_cache)} days")

    for mode in MODES:
        signals, timelines, daily_stats, nparts = collect(mode)
        wins = sum(1 for s in signals if s["settle_result"] == "WIN")
        fails = sum(1 for s in signals if s["settle_result"] == "FAIL")
        voids = sum(1 for s in signals if s["settle_result"] == "VOID")
        unsettled = sum(1 for s in signals if s["settle_result"] is None)
        wr = wins / (wins + fails) if (wins + fails) else 0
        p1 = hb.compute_p1_metrics(timelines, day_bars_cache)
        cl = hb.compute_closed_loop(signals, hmap)
        summary = {"total": len(signals), "wins": wins, "fails": fails, "voids": voids,
                   "unsettled": unsettled, "win_rate": round(wr, 4),
                   "daily_stats": dict(sorted(daily_stats.items())), "ab_mode": mode,
                   "p1_metrics": p1, "closed_loop": cl, "parts_merged": nparts}
        od = ROOT / mode
        od.mkdir(parents=True, exist_ok=True)
        with open(od / f"signals_{mode}.jsonl", "w", encoding="utf-8") as f:
            for s in signals:
                f.write(json.dumps(s, ensure_ascii=False, default=str) + "\n")
        with open(od / f"trend_timeline_{mode}.jsonl", "w", encoding="utf-8") as f:
            for k in sorted(timelines):
                f.write(json.dumps({"key": k, "timeline": timelines[k]}, ensure_ascii=False) + "\n")
        with open(od / f"summary_{mode}.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
        with open(od / f"report_{mode}.txt", "w", encoding="ascii", errors="replace") as f:
            f.write(f"=== BACKTEST REPORT ({mode}) MERGED {nparts} parts ===\n")
            f.write(f"Signals: {len(signals)} WIN={wins} FAIL={fails} VOID={voids} UNSETTLED={unsettled}\n")
            f.write(f"Win rate: {wr:.1%}\n")
            f.write(f"P1 consistency: {p1.get('overall_consistency')} NEUTRAL: {p1.get('neutral_ratio')}\n")
            f.write(f"Closed loop: pairs={cl.get('total_closed_pairs')} pnl={cl.get('total_net_pnl')}\n")
        print(f"[{mode}] parts={nparts} sig={len(signals)} wr={wr:.4f} "
              f"cons={p1.get('overall_consistency')} pairs={cl.get('total_closed_pairs')} pnl={cl.get('total_net_pnl')}")

if __name__ == "__main__":
    main()
