# -*- coding: utf-8 -*-
"""backfill_minutes.py — 回补历史1分钟数据到快照存储 (2026-08-24)

目的: 把做T参数寻优窗口从 16 日扩到 3 年。本地 minute_snapshots 仅覆盖 2026-03-16 起，
本脚本用 tushare stk_mins 回补 2023-08 ~ 2026-03-13 的 1min OHLCV 到同一快照格式，
使 load_snapshots(code, date) 透明覆盖 3 年窗口。

口径:
- 不复权真实成交价（与实时腾讯分时同口径; stk_mins 无 adj_factor 依赖）
- 每个交易日一个 JSON, 路径 t_io/minute_snapshots/{year}/{month}/{code}_{date}.json
- 已存在的文件跳过（不覆盖 2026-03-16 起的实时快照）
- vol/amount 直接透传; 做T胜率结算只用 close, vol_ratio 为日内比值, 单位无影响

用法: python t_io/validation/optuna_swing/backfill_minutes.py [--start 2023-08-01] [--end 2026-03-13]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, date

import tushare as ts

BASE = Path(__file__).resolve().parent.parent.parent.parent  # e:/superTrader
SNAP_DIR = BASE / "t_io" / "minute_snapshots"
TOKEN = os.environ.get("TUSHARE_TOKEN") or ""
if not TOKEN:
    raise SystemExit("未设置 TUSHARE_TOKEN 环境变量（tushare token 不硬编码入库以防泄露）；"
                     "请先 export TUSHARE_TOKEN=你的token")

# 代码 -> (tushare ts_code, 最早可用数据起点)
CODES = [
    ("588170", "588170.SH", "2025-04-01"),   # ETF, tushare 分钟数据 2025-04 起
    ("600176", "600176.SH", "2023-08-01"),
    ("600481", "600481.SH", "2023-08-01"),
    ("000988", "000988.SZ", "2023-08-01"),
    ("002639", "002639.SZ", "2023-08-01"),
]
RETRY = 3
SLEEP = 0.4


def trade_days(s: date, e: date):
    out, d = [], s
    while d <= e:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bar_from_row(r) -> dict:
    return {
        "time": r["trade_time"],
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
        "volume": float(r["vol"]), "amount": float(r["amount"]),
    }


def fetch_month(pro, ts_code: str, ym: str) -> list:
    """拉整月 1min 数据, 重试; 返回 (date_str, row) 列表。
    注: tushare stk_mins 会丢弃当月最后一个交易日(end 在当月内时), 故 end 跨界到下月1日强制包含。"""
    y, m = int(ym[:4]), int(ym[4:])
    first = date(y, m, 1)
    ed_ext = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1))  # 跨到下一月1日
    sd, ed = first.strftime("%Y%m%d"), ed_ext.strftime("%Y%m%d")
    for attempt in range(1, RETRY + 1):
        try:
            df = pro.stk_mins(ts_code=ts_code, freq="1min", start_date=sd, end_date=ed)
            if df is None or df.empty:
                return []
            df = df.sort_values("trade_time")
            return list(df.to_dict("records"))
        except Exception as ex:
            if attempt < RETRY:
                time.sleep(SLEEP * 3 * attempt)
            else:
                print(f"  !! {ts_code} {ym} 失败: {str(ex)[:120]}")
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-08-01")
    ap.add_argument("--end", default="2026-03-13")
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    pro = ts.pro_api(TOKEN)
    months = sorted({(d.year, d.month) for d in trade_days(start, end)})
    total_written = total_skipped = 0
    started = time.time()

    for code, ts_code, avail_from in CODES:
        c_start = max(start, datetime.strptime(avail_from, "%Y-%m-%d").date())
        if c_start > end:
            print(f"[{code}] 起点 {avail_from} 晚于 end, 跳过")
            continue
        c_months = sorted({(d.year, d.month) for d in trade_days(c_start, end)})
        print(f"\n=== {code} ({ts_code}) {c_start} ~ {end}  {len(c_months)} 个月 ===")
        for (y, m) in c_months:
            ym = f"{y}{m:02d}"
            rows = fetch_month(pro, ts_code, ym)
            if not rows:
                continue
            # 按日聚合
            by_day = {}
            for r in rows:
                ds = str(r["trade_time"])[:10]
                by_day.setdefault(ds, []).append(r)
            w = s = 0
            for ds in sorted(by_day):
                if ds < str(c_start) or ds > str(end):
                    continue
                d = date.fromisoformat(ds)
                out_dir = SNAP_DIR / f"{d.year:04d}" / f"{d.month:02d}"
                fpath = out_dir / f"{code}_{ds}.json"
                if fpath.exists():
                    s += 1
                    continue
                out_dir.mkdir(parents=True, exist_ok=True)
                bars = [_bar_from_row(r) for r in by_day[ds]]
                snap = {
                    "code": code, "name": code, "date": ds,
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "tushare_stk_mins_backfill",
                    "row_count": len(bars),
                    "bars": bars,
                }
                fpath.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
                w += 1
            total_written += w
            total_skipped += s
            print(f"  {ym}: 写{w} 跳过{s}")
            time.sleep(SLEEP)

    print(f"\n完成: 写{total_written} 跳过{total_skipped} 耗时{(time.time()-started)/60:.1f}min")


if __name__ == "__main__":
    main()
