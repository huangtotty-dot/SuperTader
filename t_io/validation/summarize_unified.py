# -*- coding: utf-8 -*-
"""
summarize_unified.py — X7 统一口径 summary 重算
在 merge_ab_unified.py 之后运行。复刻 merge_ab_expanded 的 summary 口径:
p1_metrics / closed_loop 用 harness 实际函数重算; day_bars 从 minute_snapshots_ts 构建。
输出: v108_unified/{mode}/summary_{mode}.json + report_{mode}.txt
"""
import json, os, sys
from pathlib import Path

BASE = Path(r"E:\06_T")
os.environ["T_SNAPSHOT_DIR"] = str(BASE / "t_io/minute_snapshots_ts")  # 须在 import harness 前
ROOT = BASE / "t_io/validation/v108_unified"
sys.path.insert(0, str(BASE))
import harness_backtest as hb  # noqa: E402

MODES = ["baseline", "v102"]
CODES = ["000988", "588170", "600176", "600481", "603667"]

def collect_parts(mode):
    """从 merge 产物读 signals/timelines, 从 parts 汇总 daily_stats"""
    signals = [json.loads(l) for l in open(ROOT / mode / f"signals_{mode}.jsonl", encoding="utf-8") if l.strip()]
    timelines = {}
    for l in open(ROOT / mode / f"trend_timeline_{mode}.jsonl", encoding="utf-8"):
        r = json.loads(l)
        timelines[r["key"]] = r["timeline"]
    daily_stats = {}
    for root in (BASE / "t_io/validation/v107_ab_expanded/parts", ROOT / "parts"):  # unified 后遍历, 覆盖同日旧值
        if not root.exists():
            continue
        for d in sorted(root.glob(f"{mode}_*/summary_{mode}.json")):
            s = json.load(open(d, encoding="utf-8"))
            for k, v in s.get("daily_stats", {}).items():
                daily_stats[k] = v
    return signals, timelines, daily_stats

def main():
    holdings = json.load(open(BASE / "holdings.json", encoding="utf-8"))
    hmap = {k.split("_")[0]: v for k, v in holdings.items()}

    all_dates = sorted({s["ts"][:10] for s in collect_parts("v102")[0]} |
                       {s["ts"][:10] for s in collect_parts("baseline")[0]})
    day_bars_cache = {}
    for d in all_dates:
        day_bars = {}
        for c in CODES:
            df = hb.load_snapshots(c, d)
            if not df.empty:
                day_bars[c] = df
        day_bars_cache[d] = day_bars
    print(f"day_bars: {len(day_bars_cache)} days")

    for mode in MODES:
        signals, timelines, daily_stats = collect_parts(mode)
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
                   "p1_metrics": p1, "closed_loop": cl,
                   "data_source": "tushare_unified(minute_snapshots_ts)"}
        with open(ROOT / mode / f"summary_{mode}.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
        with open(ROOT / mode / f"report_{mode}.txt", "w", encoding="ascii", errors="replace") as f:
            f.write(f"=== BACKTEST REPORT ({mode}) UNIFIED TUSHARE ===\n")
            f.write(f"Signals: {len(signals)} WIN={wins} FAIL={fails} VOID={voids} UNSETTLED={unsettled}\n")
            f.write(f"Win rate: {wr:.1%}\n")
            f.write(f"P1 consistency: {p1.get('overall_consistency')} NEUTRAL: {p1.get('neutral_ratio')}\n")
            f.write(f"Closed loop: pairs={cl.get('total_closed_pairs')} pnl={cl.get('total_net_pnl')}\n")
        print(f"[{mode}] sig={len(signals)} wr={wr:.4f} cons={p1.get('overall_consistency')} "
              f"pairs={cl.get('total_closed_pairs')} pnl={cl.get('total_net_pnl')} days={len(daily_stats)}")

if __name__ == "__main__":
    main()
