# -*- coding: utf-8 -*-
"""一次性：2024-09-01 至今「站上 365 日均线（年限）」的股票占比。

背景：本地日线缓存最多 800 根（腾讯 fqkline 上限），起点约 2023-06 ⇒ 2024-09 那几天
凑不满 MA365 的回看窗口。本脚本用 **GM 批量日线（900 根）** 补足历史，在**同一批样本**
（本地缓存 ≥365 根且末行=最新交易日的个股）上逐日统计 above/total。

输出：t_io/cache/ma365_breadth_20240901.json
      {computed_at, start, latest_date, source, series:[{date,above,total,pct}], coverage}

运行：python -u scripts/experiment/ma365_breadth_hist_20240901.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.market_data import get_provider  # noqa: E402

START = "2024-09-01"
DAYS = 900            # ≈ 从 2023-01 起；足够覆盖 2024-09 的 MA365 回看
BATCH = 100           # GM 单批 100 只 × 900 行 ≈ 9 万行，避开总行数悬崖
CACHE_DIR = ROOT / "t_io" / "cache" / "daily_kline"
OUT = ROOT / "t_io" / "cache" / "ma365_breadth_20240901.json"


def latest_date():
    for n in ("index_sh000001.json", "index_sz399001.json"):
        try:
            rows = json.loads((CACHE_DIR / n).read_text(encoding="utf-8")).get("rows") or []
            if rows:
                return str(rows[-1]["date"])[:10]
        except Exception:
            pass
    return time.strftime("%Y-%m-%d")


def cohort(latest):
    out = []
    for p in CACHE_DIR.glob("*.json"):
        if not (p.stem.isdigit() and len(p.stem) == 6):
            continue
        try:
            rows = json.loads(p.read_text(encoding="utf-8")).get("rows") or []
        except Exception:
            continue
        if len(rows) >= 365 and str(rows[-1].get("date"))[:10] == latest:
            out.append(p.stem)
    return out


def main():
    latest = latest_date()
    codes = cohort(latest)
    print(f"latest={latest} cohort={len(codes)}", flush=True)
    prov = get_provider()
    counts = {}
    got = 0
    rows_seen = []
    t_all = time.time()
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        t0 = time.time()
        try:
            data = prov.daily_many(chunk, days=DAYS) or {}
        except Exception as e:
            data = {}
            print(f"  batch ERR {type(e).__name__}: {str(e)[:80]}", flush=True)
        for c, df in data.items():
            if df is None or getattr(df, "empty", True) or len(df) < 365:
                continue
            try:
                rows = [(str(d)[:10], float(v)) for d, v in
                        zip(df["date"], df["close"]) if v is not None and float(v) > 0]
            except Exception:
                continue
            if len(rows) < 365:
                continue
            got += 1
            rows_seen.append(len(rows))
            closes = [v for _, v in rows]
            dates = [d for d, _ in rows]
            n = len(closes)
            s = sum(closes[:365])
            for idx in range(364, n):
                if idx > 364:
                    s += closes[idx] - closes[idx - 365]
                d = dates[idx]
                if d < START:
                    continue
                c_cnt = counts.setdefault(d, [0, 0])
                c_cnt[1] += 1
                if closes[idx] > s / 365.0:
                    c_cnt[0] += 1
        print(f"  {min(i + BATCH, len(codes))}/{len(codes)} got={got} "
              f"{time.time() - t0:.1f}s (total {time.time() - t_all:.0f}s)", flush=True)

    series = [{"date": d, "above": a, "total": t, "pct": round(a / t * 100, 2)}
              for d, (a, t) in sorted(counts.items()) if t]
    rec = {"computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "start": START, "latest_date": latest,
           "source": f"gm daily_batch days={DAYS}",
           "series": series,
           "coverage": {"requested": len(codes), "got": got,
                        "rows_median": sorted(rows_seen)[len(rows_seen) // 2] if rows_seen else 0}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    if series:
        vals = [x["pct"] for x in series]
        mx = max(series, key=lambda x: x["pct"])
        mn = min(series, key=lambda x: x["pct"])
        print(f"series={len(series)} {series[0]['date']} -> {series[-1]['date']}", flush=True)
        print(f"last={series[-1]}", flush=True)
        print(f"max={mx}", flush=True)
        print(f"min={mn}", flush=True)
        print(f"mean={sum(vals) / len(vals):.2f}%", flush=True)
    print(f"saved {OUT}", flush=True)


if __name__ == "__main__":
    main()
