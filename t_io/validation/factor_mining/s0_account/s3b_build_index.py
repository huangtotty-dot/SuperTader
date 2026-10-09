# -*- coding: utf-8 -*-
"""S3-B 候选改造 · step1: 30min 缓存 → 扩展日级索引（不改 s3_beta_* 原文件）。

在阶段一 min_index_s3.parquet 字段基础上新增（仅用于候选筛选，不进 OGR 触发判定）：
  hi_day   : 当日全部 bar high 最大值
  lo_day   : 当日全部 bar low 最小值
  vwap_day : 当日 Σamount / Σvolume（tushare 缓存 amount 元 / volume 股）

口径见 doc/experiment/2026-10-10_S3b_候选改造.md §2.1（预注册冻结）。
输出 results/s3b/min_index_s3b.parquet + min_index_s3b_meta.json，
并与阶段一 min_index_s3.parquet 逐行对账共有字段（应 0 差异）。
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
OUT = HERE / "results" / "s3b"
CACHE = HERE.parents[2] / "cache" / "tushare_mins"


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(str(CACHE / "*_30min_d540.json")))
    recs = []
    meta = dict(n_files=len(files), bad_files=[], n_partial=0)
    for fp in files:
        base = os.path.basename(fp)
        code6 = base.split("_")[0].split(".")[0]
        try:
            with open(fp, encoding="utf-8") as f:
                rows = json.load(f)["rows"]
        except Exception:
            meta["bad_files"].append(base)
            continue
        o930, c1000, c1500, a930 = {}, {}, {}, {}
        amt, vol, hi, lo, lastc = {}, {}, {}, {}, {}
        for r in rows:
            d = r["time"][:10]
            tm = r["time"][11:16]
            amt[d] = amt.get(d, 0.0) + float(r.get("amount") or 0.0)
            vol[d] = vol.get(d, 0.0) + float(r.get("volume") or 0.0)
            h, l = r.get("high"), r.get("low")
            if h is not None:
                hi[d] = max(hi.get(d, -np.inf), float(h))
            if l is not None:
                lo[d] = min(lo.get(d, np.inf), float(l))
            if tm == "09:30":
                o930[d] = r["open"]
                a930[d] = float(r.get("amount") or 0.0)
            elif tm == "10:00":
                c1000[d] = r["close"]
            elif tm == "15:00":
                c1500[d] = r["close"]
            lastc[d] = r["close"]
        dates = sorted(lastc)
        for i, d in enumerate(dates):
            if d not in o930 or d not in c1000 or d not in c1500:
                meta["n_partial"] += 1
                continue
            pc = lastc[dates[i - 1]] if i > 0 else np.nan
            o = o930[d]
            gap = (o / pc - 1.0) if (np.isfinite(pc) and pc > 0) else np.nan
            vwap = (amt[d] / vol[d]) if vol.get(d, 0.0) > 0 else np.nan
            recs.append((code6, d, o, c1000[d], c1500[d], a930[d], amt[d],
                         pc, gap, hi.get(d, np.nan), lo.get(d, np.nan), vwap))
    df = pd.DataFrame(recs, columns=["code", "date", "o930", "c1000", "c1500",
                                     "a930", "amt_day", "prev_close", "gap",
                                     "hi_day", "lo_day", "vwap_day"])
    df.to_parquet(OUT / "min_index_s3b.parquet", index=False)

    # 与阶段一索引对账共有字段（应 0 差异）
    s3 = pd.read_parquet(HERE / "results" / "s3_beta" / "min_index_s3.parquet")
    m = df.merge(s3, on=["code", "date"], suffixes=("", "_s3"))
    chk = dict(rows_s3b=len(df), rows_s3=len(s3), merged=len(m),
               o930_diff=int((m["o930"] != m["o930_s3"]).sum()),
               c1000_diff=int((m["c1000"] != m["c1000_s3"]).sum()),
               c1500_diff=int((m["c1500"] != m["c1500_s3"]).sum()),
               a930_diff=int((m["a930"] != m["a930_s3"]).sum()),
               gap_diff=int((m["gap"].fillna(-9) != m["gap_s3"].fillna(-9)).sum()))
    meta.update(n_rows=len(df), n_codes=df["code"].nunique(),
                date_min=str(df["date"].min()), date_max=str(df["date"].max()),
                check_vs_s3=chk, elapsed=round(time.time() - t0, 1))
    (OUT / "min_index_s3b_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[min_index_s3b] {meta['n_codes']} codes {meta['n_rows']} rows "
          f"{meta['date_min']}~{meta['date_max']} check={chk} ({meta['elapsed']}s)",
          flush=True)


if __name__ == "__main__":
    main()
