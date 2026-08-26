#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全量拉取候选股 1年 1min 数据（断点续传）
==========================================
拉取 watchlist_buy.json 中除已有数据外的全部股票 1年1min 数据到
t_io/backtest_1year_data/{code}_1year_1min.csv

方法: 按月分段拉取(每月<=8000条限制), 失败重试3次, 已存在文件跳过。
"""
import sys
import os
import time
from pathlib import Path
from datetime import datetime, timedelta

if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import pandas as pd
import json

try:
    import tushare as ts
except ImportError:
    print("tushare not available")
    sys.exit(1)

TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if not TOKEN:
    print("未设置 TUSHARE_TOKEN 环境变量, 无法拉取")
    sys.exit(1)
OUT_DIR = BASE / "t_io" / "backtest_1year_data"
OUT_DIR.mkdir(parents=True, exist_ok=True)

EXISTING = {"000988", "002176", "002202", "002261", "002451"}


def to_api_code(code):
    return f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"


def month_ranges():
    """2025-08-26 ~ 2026-08-26 按月分段"""
    start = datetime(2025, 8, 26)
    end = datetime(2026, 8, 26)
    ranges = []
    current = start
    while current < end:
        if current.month == 12:
            next_month = datetime(current.year + 1, 1, 1)
        else:
            next_month = datetime(current.year, current.month + 1, 1)
        month_end = min(next_month - timedelta(days=1), end)
        ranges.append((current, month_end))
        current = next_month
    return ranges


def fetch_one_month(pro, code, s, e):
    start_str = s.strftime("%Y-%m-%d 09:00:00")
    end_str = e.strftime("%Y-%m-%d 15:00:00")
    for attempt in range(3):
        try:
            df = pro.stk_mins(ts_code=code, freq="1min", start_date=start_str, end_date=end_str)
            return df
        except Exception as ex:
            print(f"    [重试{attempt+1}] {str(ex)[:60]}", flush=True)
            time.sleep(5)
    return None


def fetch_stock(pro, code, api_code):
    out_path = OUT_DIR / f"{code}_1year_1min.csv"
    if out_path.exists():
        n = len(pd.read_csv(out_path, usecols=["time"]))
        print(f"⏭️  跳过 {code} (已存在 {n}条)", flush=True)
        return

    frames = []
    ranges = month_ranges()
    for i, (s, e) in enumerate(ranges, 1):
        df = fetch_one_month(pro, api_code, s, e)
        if df is not None and len(df) > 0:
            frames.append(df)
            print(f"  [{i}/{len(ranges)}] {s.strftime('%Y-%m')} ✅{len(df)}", end="", flush=True)
        else:
            print(f"  [{i}/{len(ranges)}] {s.strftime('%Y-%m')} ⚠️空", end="", flush=True)
        time.sleep(0.6)  # 限流保护

    if not frames:
        print(f"\n❌ {code} 无任何数据", flush=True)
        return

    full = pd.concat(frames, ignore_index=True)
    full = full.rename(columns={"trade_time": "time", "vol": "volume"})
    full["time"] = pd.to_datetime(full["time"], errors="coerce")
    full = full.dropna(subset=["time"]).drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    full.to_csv(out_path, index=False)
    print(f"\n✅ {code}: {len(full)}条 {full['time'].min()}~{full['time'].max()}", flush=True)


def main():
    with open(BASE / "t_io" / "state" / "watchlist_buy.json", encoding="utf-8") as f:
        watchlist = json.load(f)

    codes = [c for c in watchlist["stocks"].keys() if c not in EXISTING]
    print(f"待拉取 {len(codes)} 支(已排除已有5支): {codes}", flush=True)

    pro = ts.pro_api(TOKEN)
    ok = fail = 0
    for i, code in enumerate(codes, 1):
        print(f"\n[{i}/{len(codes)}] {code} ({to_api_code(code)})", flush=True)
        try:
            fetch_stock(pro, code, to_api_code(code))
            if (OUT_DIR / f"{code}_1year_1min.csv").exists():
                ok += 1
            else:
                fail += 1
        except Exception as e:
            print(f"  异常: {str(e)[:60]}", flush=True)
            fail += 1
        time.sleep(0.5)

    print(f"\n\n===== 完成: 成功{ok} 失败{fail} =====", flush=True)


if __name__ == "__main__":
    main()
