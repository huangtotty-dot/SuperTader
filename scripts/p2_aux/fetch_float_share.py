# -*- coding: utf-8 -*-
"""P2 数据外拉 — 流通股本事件历史（断点续拉）。

数据源：akshare stock_zh_a_gbjg_em（东方财富 datacenter「股本结构」），
事件型全历史（变更日期/已上市流通A股/总股本/变动原因），对退市股同样有效。
每只股票原始事件表落 checkpoint：panel_aux/_ckpt_float/{code6}.parquet，
重跑自动跳过已有 checkpoint。后续由 build_float_share_daily.py 前向填充成日频。
"""
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

warnings.filterwarnings("ignore")
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUX = os.path.join(BASE, "t_io", "validation", "xsection", "panel_aux")
CKPT = os.path.join(AUX, "_ckpt_float")
import akshare as ak

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 10**9  # 本轮最多处理多少只（防 300s 超时）


def fetch_one(code6: str):
    path = os.path.join(CKPT, f"{code6}.parquet")
    if os.path.exists(path):
        return code6, "cached", None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_gbjg_em(symbol=code6)
            if df is None or df.empty:
                # 空表也落 checkpoint（带标记），避免反复重拉
                pd.DataFrame({"_empty": [True]}).to_parquet(path, index=False)
                return code6, "empty", None
            df.to_parquet(path, index=False)
            return code6, "ok", len(df)
        except Exception as e:
            time.sleep(1.5 + attempt * 2)
            err = str(e)[:80]
    return code6, "fail", err


def main():
    os.makedirs(CKPT, exist_ok=True)
    uni = pd.read_parquet(os.path.join(BASE, "t_io", "validation", "xsection", "panel", "universe.parquet"))
    uni["code6"] = uni["symbol"].str.split(".").str[1]
    todo = [c for c in uni["code6"] if not os.path.exists(os.path.join(CKPT, f"{c}.parquet"))]
    todo = todo[:LIMIT]
    print(f"[plan] remaining-this-run={len(todo)}", flush=True)
    stat = {"ok": 0, "empty": 0, "fail": 0}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_one, c): c for c in todo}
        for i, f in enumerate(as_completed(futs), 1):
            code6, status, info = f.result()
            stat[status if status in stat else "fail"] = stat.get(status, 0) + 1
            if status == "fail":
                print(f"[fail] {code6} {info}", flush=True)
            if i % 100 == 0:
                print(f"[prog] {i}/{len(todo)} elapsed={time.time()-t0:.0f}s stat={stat}", flush=True)
    done = len([f for f in os.listdir(CKPT) if f.endswith(".parquet")])
    print(f"[done] this-run={len(todo)} stat={stat} total-ckpt={done}/5674 elapsed={time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
