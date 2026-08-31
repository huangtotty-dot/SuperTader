#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Renko 做T 每日复盘辅助（V3.1）
===============================
读当日 renko_t trace, 自动汇总做T表现, 供每日复盘清单 §4.6 引用。

数据源: t_io/traces/renko_t_{date}.jsonl (evaluate 自动记录)
汇总:
  - 买入信号数 / 卖出信号数
  - 卖出路径分布: 目标止盈 / 时间止损 / 尾盘强平
  - 做T闭环: 胜率 / 平均收益 / 总收益 (trace 内 BUY→SELL 配对, 手续费双边0.0005)
  - 买入后 +30min 反弹胜率 (读 minute_snapshots 或 backtest_1year_data 1min 计算, 尽力而为)

用法: python scripts/renko_t_daily_review.py --date 2026-08-27
"""
import sys
import os
import glob
import json
from pathlib import Path

if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import pandas as pd
import numpy as np
import argparse

COST = 0.0005
TRACE_DIR = BASE / "t_io" / "traces"
SNAPSHOT_DIR = BASE / "t_io" / "minute_snapshots"
HIST_DIR = BASE / "t_io" / "backtest_1year_data"


def load_trace(date_str):
    """读当日 renko_t trace。兼容两种命名（2026-08-27 起落盘为 renko_t_{date}_{date}.jsonl，
    早前为 renko_t_{date}.jsonl）。均不存在 → 返回 None（调用方须显式报警，防"无声假阴性"）。"""
    p1 = TRACE_DIR / f"renko_t_{date_str}_{date_str}.jsonl"  # 08-27 起实际命名
    p2 = TRACE_DIR / f"renko_t_{date_str}.jsonl"             # 旧命名
    p = p1 if p1.exists() else p2
    if not p.exists():
        return None
    rows = []
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def load_minute(code, date_str):
    """读当日1min数据(优先快照, 回退历史1min缓存)"""
    # 快照: minute_snapshots/{yy}/{mm}/{code}_{date}.json
    dt = pd.Timestamp(date_str)
    cand = SNAPSHOT_DIR / str(dt.year) / f"{dt.month:02d}" / f"{code}_{date_str}.json"
    if cand.exists():
        data = json.load(open(cand, encoding="utf-8"))
        bars = data.get("bars") or data.get("snapshots") or []
        rows = [{"time": b.get("time"), "close": float(b.get("close", 0))} for b in bars if b.get("close")]
        df = pd.DataFrame(rows)
        if not df.empty:
            df["time"] = pd.to_datetime(df["time"])
            return df
    # 历史1min缓存
    matches = glob.glob(str(HIST_DIR / f"{code}*_1year_1min.csv"))
    if matches:
        df = pd.read_csv(matches[0], parse_dates=["time"])
        df = df[df["time"].dt.date == dt.date()]
        return df[["time", "close"]].dropna()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()

    rows = load_trace(args.date)
    print("=" * 100)
    print(f"Renko 做T 每日复盘: {args.date}   (信号数 {len(rows) if rows else 0})")
    print("=" * 100)
    if rows is None:
        print("🔴 未找到 renko_t trace 文件（期望 renko_t_{date}_{date}.jsonl 或 renko_t_{date}.jsonl）"
              "——当日无信号数据，需排查引擎是否写入（P0-5 口径修复）")
        return
    if not rows:
        print("⚠️ 无 renko_t trace（当日无 Renko 做T信号，或引擎未启用）")
        return

    buys = [r for r in rows if r.get("action") == "BUY_LOW"]
    sells = [r for r in rows if r.get("action") == "SELL_HIGH"]

    # ---- 卖出路径分布 ----
    reasons = {}
    for s in sells:
        r = s.get("exit_reason", "未知")
        key = "目标止盈" if "目标止盈" in r else ("时间止损" if "时间止损" in r else ("尾盘强平" if "尾盘强平" in r else "其他"))
        reasons[key] = reasons.get(key, 0) + 1
    print(f"\n卖出信号: {len(sells)}")
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")

    # ---- 做T闭环 (trace 内 BUY→SELL 按 code 配对, 先买后卖) ----
    from collections import defaultdict
    by_code = defaultdict(list)
    for r in sorted(rows, key=lambda x: x.get("ts", "")):
        by_code[r.get("code", "")].append(r)
    trades = []
    for code, recs in by_code.items():
        entry = None
        for r in recs:
            if r.get("action") == "BUY_LOW":
                entry = r.get("price")
            elif r.get("action") == "SELL_HIGH" and entry is not None:
                sell_px = r.get("price")
                pnl = (sell_px * (1 - COST) - entry * (1 + COST)) / (entry * (1 + COST)) * 100
                trades.append(pnl)
                entry = None
    if trades:
        arr = np.array(trades)
        print(f"\n做T闭环: {len(arr)} 笔, 胜率 {(arr > 0).mean() * 100:.1f}%, "
              f"平均 {arr.mean():+.3f}%, 总 {arr.sum():+.2f}%  (手续费双边0.0005)")
        print(f"  对照回测预期: 胜率 75-80% / 均收益 +0.15~0.3%")

    # ---- 买入后 +30min 反弹验证 ----
    print(f"\n买入后 +30min 反弹验证:")
    rebound = []
    for b in buys:
        code = b.get("code")
        price = b.get("price")
        ts = b.get("ts")
        if not (code and price and ts):
            continue
        dfm = load_minute(code, args.date)
        if dfm is None or dfm.empty:
            continue
        t0 = pd.Timestamp(ts)
        t1 = t0 + pd.Timedelta(minutes=30)
        p0 = price
        row1 = dfm[dfm["time"] <= t1]
        if not row1.empty:
            p1 = float(row1.iloc[-1]["close"])
            rebound.append((p1 - p0) / p0 * 100)
    if rebound:
        arr = np.array(rebound)
        print(f"  {len(arr)} 笔可验证, +30min 胜率 {(arr > 0).mean() * 100:.1f}%, "
              f"平均 {arr.mean():+.3f}%  (对照回测预期 60%+)")
        fake = (arr < -1).mean() * 100
        print(f"  假信号(+30min跌>1%)占比: {fake:.1f}%  (目标 <20%)")
    else:
        print("  无分钟数据可验证(次日或补数据后跑)")

    print("\n" + "=" * 100)
    print("✅ 汇总完成, 结果填入复盘清单 §4.6")


if __name__ == "__main__":
    main()
