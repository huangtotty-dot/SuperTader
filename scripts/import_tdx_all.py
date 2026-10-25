# -*- coding: utf-8 -*-
"""把通达信本地**全部日线**导成一个 parquet（+ 每只覆盖情况清单）。

数据源：`{TDX_HOME}/vipdoc/{sh,sz,bj}/lday/*.day`（32 字节/条，不复权）
产出：
    t_io/tdx/tdx_daily.parquet       列：code,date,open,high,low,close,volume(股),amount(元)
    t_io/tdx/tdx_manifest.csv        每只：代码/市场/根数/首日/末日

⚠️ 与 `t_io/cache/daily_kline` 的区别（别混用）：
  · 通达信 = **不复权**原始价，成交量单位 **股**；
  · 仓库缓存 = 腾讯 **前复权**，成交量单位 **手**（=股/100）。
  需要前复权时用 `scripts/import_tdx_daily.py`（套新浪 qfq 因子），不要直接拿这份覆盖缓存。

用法：``python scripts/import_tdx_all.py [--out DIR]``
"""
import argparse
import csv
import os
import sys
import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.stdout.reconfigure(encoding="utf-8")

TDX_ROOT = os.environ.get("TDX_HOME", r"C:\new_tdx")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "t_io", "tdx")

DT = np.dtype([("date", "<u4"), ("open", "<u4"), ("high", "<u4"), ("low", "<u4"),
               ("close", "<u4"), ("amount", "<f4"), ("volume", "<u4"), ("_r", "<u4")])
SCHEMA = pa.schema([
    ("code", pa.string()), ("date", pa.string()),
    ("open", pa.float32()), ("high", pa.float32()), ("low", pa.float32()), ("close", pa.float32()),
    ("volume", pa.int64()), ("amount", pa.float64()),
])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--root", default=TDX_ROOT)
    a = ap.parse_args()
    out_dir = a.out or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    fp_parquet = os.path.join(out_dir, "tdx_daily.parquet")
    fp_manifest = os.path.join(out_dir, "tdx_manifest.csv")

    files = []
    for mkt in ("sh", "sz", "bj"):
        d = os.path.join(a.root, "vipdoc", mkt, "lday")
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.endswith(".day"):
                # 文件名是 sh600000/sz000001/bj920xxx，**只留 6 位代码**（与仓库其它数据一致）
                files.append((mkt, fn[2:-4], os.path.join(d, fn)))
    files.sort()
    print(f"发现 {len(files)} 个 .day 文件", flush=True)

    writer = pq.ParquetWriter(fp_parquet, SCHEMA, compression="zstd")
    manifest, total_rows, empty, t0 = [], 0, 0, time.time()
    try:
        for i, (mkt, code, fp) in enumerate(files, 1):
            raw = open(fp, "rb").read()
            n = len(raw) // DT.itemsize
            if n == 0:
                empty += 1
                write_manifest(manifest, code, mkt, 0, "", "")
                continue
            arr = np.frombuffer(raw[: n * DT.itemsize], dtype=DT)
            d = arr["date"]
            date_s = np.array([f"{x//10000:04d}-{x//100%100:02d}-{x%100:02d}" for x in d], dtype=object)
            tbl = pa.table({
                "code": pa.array([code] * n, pa.string()),
                "date": pa.array(date_s.astype(str), pa.string()),
                "open": arr["open"].astype(np.float32) / 100.0,
                "high": arr["high"].astype(np.float32) / 100.0,
                "low": arr["low"].astype(np.float32) / 100.0,
                "close": arr["close"].astype(np.float32) / 100.0,
                "volume": arr["volume"].astype(np.int64),
                "amount": arr["amount"].astype(np.float64),
            })
            writer.write_table(tbl)
            total_rows += n
            write_manifest(manifest, code, mkt, n, date_s[0], date_s[-1])
            if i % 1000 == 0:
                print(f"  {i}/{len(files)}  行数 {total_rows:,}  {time.time()-t0:.0f}s", flush=True)
    finally:
        writer.close()

    with open(fp_manifest, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["代码", "市场", "根数", "首日", "末日"])
        w.writerows(manifest)

    size = os.path.getsize(fp_parquet) / 1e6
    print(f"\n完成：{len(files)} 只 · {total_rows:,} 行 · parquet {size:.1f}MB · 空文件 {empty}")
    print(f"  → {fp_parquet}")
    print(f"  → {fp_manifest}")
    bars = [m[2] for m in manifest if m[2]]
    if bars:
        import statistics
        print(f"  每只根数：中位 {statistics.median(bars):.0f} / 最少 {min(bars)} / 最多 {max(bars)}")


def write_manifest(manifest, code, mkt, n, first, last):
    manifest.append([code, mkt, n, first, last])


if __name__ == "__main__":
    main()
