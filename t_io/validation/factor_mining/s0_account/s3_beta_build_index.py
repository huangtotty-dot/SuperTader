# -*- coding: utf-8 -*-
"""S3-β 独立账户臂 · step1: 30min 缓存 → 日级执行索引（不改 s1_*/s2_* 文件）。

与 s2_build_min_index.py 的差异：额外抽取
  a930   : 09:30 bar amount（≈集合竞价+开盘瞬间成交额，作 size_cap_by_auction 的竞价代理）
  c1500  : 15:00 bar close（=收盘价，隔夜滚动底仓的买/卖执行价代理）
  amt_day: 当日全部 bar amount 合计（流动性参考）

输出 results/s3_beta/min_index_s3.parquet，每行 (code6, date)：
  code, date, o930, c1000, c1500, a930, amt_day, prev_close(=prev c1500), gap
同时落 min_index_s3_meta.json。与 S2 min_index 的 o930/c1000/prev_close 口径一致性
在 meta 中做行数校验（S2 用「当日最后 bar close」作 prev_close，本索引用 c1500，
二者应恒等，校验不等计数）。
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
OUT = HERE / "results" / "s3_beta"
CACHE = HERE.parents[2] / "cache" / "tushare_mins"


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(str(CACHE / "*_30min_d540.json")))
    recs = []
    meta = dict(n_files=len(files), bad_files=[], n_partial=0, prev_close_mismatch=0)
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
        o930, c1000, c1500, a930, amt, lastc = {}, {}, {}, {}, {}, {}
        for r in rows:
            d = r["time"][:10]
            tm = r["time"][11:16]
            amt[d] = amt.get(d, 0.0) + float(r.get("amount") or 0.0)
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
            if np.isfinite(pc) and abs(pc - c1500[dates[i - 1]]) > 1e-9:
                meta["prev_close_mismatch"] += 1
            o = o930[d]
            gap = (o / pc - 1.0) if (np.isfinite(pc) and pc > 0) else np.nan
            recs.append((code6, d, o, c1000[d], c1500[d], a930[d], amt[d], pc, gap))
    df = pd.DataFrame(recs, columns=["code", "date", "o930", "c1000", "c1500",
                                     "a930", "amt_day", "prev_close", "gap"])
    df.to_parquet(OUT / "min_index_s3.parquet", index=False)

    # 与 S2 min_index 对账（o930/c1000/prev_close 应逐行一致）
    s2 = pd.read_parquet(HERE / "results" / "s2_ogr" / "min_index.parquet")
    m = df.merge(s2, on=["code", "date"], suffixes=("", "_s2"))
    chk = dict(
        rows_s3=len(df), rows_s2=len(s2), merged=len(m),
        o930_diff=int((m["o930"] != m["o930_s2"]).sum()),
        c1000_diff=int((m["c1000"] != m["c1000_s2"]).sum()),
        prev_close_diff=int(
            (m["prev_close"].fillna(-1) != m["prev_close_s2"].fillna(-1)).sum()),
    )
    meta.update(n_rows=len(df), n_codes=df["code"].nunique(),
                date_min=str(df["date"].min()), date_max=str(df["date"].max()),
                check_vs_s2=chk, elapsed=round(time.time() - t0, 1))
    (OUT / "min_index_s3_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[min_index_s3] {meta['n_codes']} codes {meta['n_rows']} rows "
          f"{meta['date_min']}~{meta['date_max']} partial={meta['n_partial']} "
          f"check={chk} ({meta['elapsed']}s)", flush=True)


if __name__ == "__main__":
    main()
