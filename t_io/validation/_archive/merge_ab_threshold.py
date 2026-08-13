# -*- coding: utf-8 -*-
"""
merge_ab_threshold.py — X9 阈值档合并 + summary 重算
用法: python merge_ab_threshold.py [SELL_TH=55]
严格校验(逐行解析/日期⊆周段/去重), 合并 v109_threshold/t{TH}/parts -> t{TH}/{mode}/ 全套,
summary(p1/closed_loop) 用 harness 函数在统一口径 day_bars 上重算。
"""
import json, os, re, sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(r"E:\06_T")
SELL_TH = sys.argv[1] if len(sys.argv) > 1 else "55"
TAG = f"t{SELL_TH.replace('.', '_')}"
ROOT = BASE / f"t_io/validation/v109_threshold/{TAG}"
PARTS = ROOT / "parts"
os.environ["T_SNAPSHOT_DIR"] = str(BASE / "t_io/minute_snapshots_ts")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "t_io/validation"))
import harness_backtest as hb  # noqa: E402
from run_ab_expanded import SEGS  # noqa: E402

MODES = ["baseline", "v102"]
CODES = ["000988", "588170", "600176", "600481", "603667"]

def load_jsonl_strict(fp):
    rows = []
    with open(fp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows

def collect(mode):
    signals, timelines, daily_stats = [], {}, {}
    errors = []
    nparts = 0
    for code in CODES:
        for s, e in SEGS:
            d = PARTS / f"{mode}_{code}_{s.replace('-', '')}"
            sig = d / f"signals_{mode}.jsonl"
            if not sig.exists():
                errors.append(f"MISSING {d.name}")
                continue
            nparts += 1
            sigs = load_jsonl_strict(sig)
            bad = sorted({x["ts"][:10] for x in sigs if not (s <= x["ts"][:10] <= e)})
            if bad:
                errors.append(f"RANGE {d.name}: {bad}")
            signals.extend(sigs)
            tl = d / f"trend_timeline_{mode}.jsonl"
            if tl.exists():
                for r in load_jsonl_strict(tl):
                    timelines[r["key"]] = r["timeline"]
            sm = d / f"summary_{mode}.json"
            if sm.exists():
                for k, v in json.load(open(sm, encoding="utf-8")).get("daily_stats", {}).items():
                    daily_stats[k] = v
    dup = {k: v for k, v in Counter((x["ts"], x["code"]) for x in signals).items() if v > 1}
    if dup:
        errors.append(f"DUP {len(dup)} e.g. {list(dup)[:3]}")
    signals.sort(key=lambda x: (x["ts"], x["code"]))
    return signals, timelines, daily_stats, nparts, errors

def main():
    holdings = json.load(open(BASE / "holdings.json", encoding="utf-8"))
    hmap = {k.split("_")[0]: v for k, v in holdings.items()}
    all_dates = sorted({s["ts"][:10] for s in collect("v102")[0]} |
                       {s["ts"][:10] for s in collect("baseline")[0]})
    day_bars_cache = {}
    for d in all_dates:
        day_bars = {}
        for c in CODES:
            df = hb.load_snapshots(c, d)
            if not df.empty:
                day_bars[c] = df
        day_bars_cache[d] = day_bars
    print(f"day_bars: {len(day_bars_cache)} days")

    all_errors = []
    for mode in MODES:
        signals, timelines, daily_stats, nparts, errors = collect(mode)
        all_errors.extend(errors)
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
                   "p1_metrics": p1, "closed_loop": cl, "parts_merged": nparts,
                   "notify_sell_threshold": SELL_TH,
                   "data_source": "tushare_unified(minute_snapshots_ts)"}
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
        print(f"[{mode}] parts={nparts} sig={len(signals)} wr={wr:.4f} "
              f"pairs={cl.get('total_closed_pairs')} pnl={cl.get('total_net_pnl')} days={len(daily_stats)}")
    if all_errors:
        print("ERRORS:")
        for e in all_errors:
            print(" ", e)
        return 1
    print("MERGE_OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
