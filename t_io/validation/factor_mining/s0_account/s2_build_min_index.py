# -*- coding: utf-8 -*-
"""S2-T1 step1: 30min 缓存 → 日级 OGR 索引（不改 s1_* 文件）。

输出 results/s2_ogr/min_index.parquet，每行 (code6, date)：
  o930       : 09:30 bar open（=集合竞价价，原始未复权）
  c1000      : 10:00 bar close（=规则出场价，原始未复权）
  prev_close : 同一 30min 序列前一交易日最后 bar 的 close（原始未复权；
               ⚠️ 不可混用日线前复权面板，否则除权日伪 gap）
  gap        : o930/prev_close - 1
同时落 results/s2_ogr/min_index_meta.json（票数/日期覆盖/缺 bar 统计）。
"""
from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "s2_ogr"
CACHE = HERE.parents[2] / "cache" / "tushare_mins"


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(str(CACHE / "*_30min_d540.json")))
    recs = []
    meta = dict(n_files=len(files), bad_files=[], n_partial=0)
    for fp in files:
        base = os.path.basename(fp)
        ts = base.split("_")[0]                    # e.g. 000001.SZ
        code6 = ts.split(".")[0]
        try:
            with open(fp, encoding="utf-8") as f:
                rows = json.load(f)["rows"]
        except Exception:
            meta["bad_files"].append(base)
            continue
        o930, c1000, lastc = {}, {}, {}
        for r in rows:
            d = r["time"][:10]
            tm = r["time"][11:16]
            if tm == "09:30":
                o930[d] = r["open"]
            elif tm == "10:00":
                c1000[d] = r["close"]
            lastc[d] = r["close"]                  # 迭代至当日最后 bar
        dates = sorted(lastc)
        for i, d in enumerate(dates):
            if d not in o930 or d not in c1000:
                meta["n_partial"] += 1
                continue
            pc = lastc[dates[i - 1]] if i > 0 else np.nan
            o = o930[d]
            gap = (o / pc - 1.0) if (np.isfinite(pc) and pc > 0) else np.nan
            recs.append((code6, d, o, c1000[d], pc, gap))
    df = pd.DataFrame(recs, columns=["code", "date", "o930", "c1000",
                                     "prev_close", "gap"])
    df.to_parquet(OUT / "min_index.parquet", index=False)
    meta.update(n_rows=len(df), n_codes=df["code"].nunique(),
                date_min=str(df["date"].min()), date_max=str(df["date"].max()),
                elapsed=round(time.time() - t0, 1))
    (OUT / "min_index_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[min_index] {meta['n_codes']} codes {meta['n_rows']} rows "
          f"{meta['date_min']}~{meta['date_max']} "
          f"partial={meta['n_partial']} bad={len(meta['bad_files'])} "
          f"({meta['elapsed']}s)", flush=True)


if __name__ == "__main__":
    main()
