# -*- coding: utf-8 -*-
"""bootstrap_b7ff_hist.py — B7 择时过滤层因子历史冷启动回填（2026-09-22 施工）。

方案：doc/solutions/2026-09-22_B7择时过滤层生产集成方案.md §4.2。
生产 z 值需要每票过去 ≥14 个交易日的 F(14:30) 标量（gp_miner fast_zscore 口径，
逐票时序、非横截面）。本脚本一次性回填到 t_io/state/b7ff_factor_hist.json：

  数据源（双源合并，整日照搬根数多者，同 minute_data.merge_frames 口径）：
    A. t_io/backtest_1year_data/*1min.csv（gm 缓存，至 ~2026-08-26）
    B. tushare stk_mins 实时拉取 CSV 截止日之后的缺口（标签口径已实测与 gm 一致：
       首日 09:30、全日 241 根、14:30 = 索引 210；2026-09-22 实测 000988.SZ）
  每票每天经生产模块 core/b7_factor_filter.factor_at_1430 计算（含 211 根对齐守卫），
  守卫失败的日写 f=None 留痕（计入 hist_days，不进窗口统计——与 fast_zscore 语义一致）。

⚠️ 运行环境：需用户 Python（tushare），managed python 无 tushare：
  "$DAIMON_USER_PYTHON" scripts/bootstrap_b7ff_hist.py
  可选参数：--end 2026-09-21（回填截止日，默认昨天）；--days 40（每票保留天数）
幂等：整文件重写（以本次计算为准），可重复跑。
"""
import argparse
import glob
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np                       # noqa: E402
import pandas as pd                      # noqa: E402
import tushare as ts                     # noqa: E402  仅用户 python 有
from core import b7_factor_filter as b7ff  # noqa: E402  ★ 生产模块（同一实现，防漂移）

CSV_DIR = os.path.join(ROOT, "t_io", "backtest_1year_data")
HIST_PATH = os.path.join(ROOT, "t_io", "state", "b7ff_factor_hist.json")
TUSHARE_TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"  # 同 fetch_warmup_minutes.py:15

BAR_COLS = ["time", "open", "high", "low", "close", "volume", "amount"]


def pool_codes():
    """CSV 全集 6 位代码（同 minute_data.pool_symbols 口径）。"""
    codes = set()
    for f in glob.glob(os.path.join(CSV_DIR, "*1min.csv")):
        m = re.match(r"^(\d{6})", os.path.basename(f))
        if m:
            codes.add(m.group(1))
    return sorted(codes)


def csv_path(code):
    for f in sorted(glob.glob(os.path.join(CSV_DIR, code + "*1min.csv"))):
        rest = os.path.basename(f)[len(code):]
        if rest.startswith(("_", ".")):
            return f
    return None


def ts_code_of(code):
    return f"{code}.SH" if code.startswith(("6", "5")) else f"{code}.SZ"


def load_csv_days(code):
    """CSV → {date: day_df}（标准 BAR_COLS，time 为 str）。"""
    f = csv_path(code)
    if f is None:
        return {}, None
    df = pd.read_csv(f)
    df["time"] = pd.to_datetime(df["time"])
    days = {}
    for d, g in df.groupby(df["time"].dt.strftime("%Y-%m-%d")):
        days[d] = g
    last = max(days) if days else None
    return days, last


def fetch_tushare_days(code, start_date, end_date):
    """tushare stk_mins 拉 [start_date, end_date] → {date: day_df}。失败/空 → {}。"""
    if start_date > end_date:
        return {}
    try:
        df = pro.stk_mins(ts_code=ts_code_of(code), freq="1min",
                          start_date=start_date + " 09:00:00",
                          end_date=end_date + " 19:00:00")
    except Exception as e:
        print(f"    {code} tushare {start_date}~{end_date}: FETCH_ERROR {repr(e)[:100]}")
        return {}
    if df is None or df.empty:
        return {}
    df = df.sort_values("trade_time")
    out = pd.DataFrame({
        "time": pd.to_datetime(df["trade_time"]),
        "open": df["open"].astype(float), "high": df["high"].astype(float),
        "low": df["low"].astype(float), "close": df["close"].astype(float),
        "volume": df["vol"].astype(float), "amount": df["amount"].astype(float),
    })
    return {d: g for d, g in out.groupby(out["time"].dt.strftime("%Y-%m-%d"))}


def day_bars_of(day_df):
    return [{"time": str(t), "close": float(c), "volume": float(v), "amount": float(a)}
            for t, c, v, a in zip(day_df["time"], day_df["close"],
                                  day_df["volume"], day_df["amount"])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=None, help="回填截止日 YYYY-MM-DD（默认昨天）")
    ap.add_argument("--days", type=int, default=b7ff.HIST_KEEP_DAYS,
                    help="每票保留最近 N 个条目（默认=模块 HIST_KEEP_DAYS）")
    args = ap.parse_args()
    end = args.end or pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
    end = str(end)[:10]

    codes = pool_codes()
    print(f"[bootstrap] 池={len(codes)} 票，回填截止={end}，每票保留={args.days} 条")

    hist, report = {}, []
    for i, code in enumerate(codes):
        csv_days, csv_last = load_csv_days(code)
        ts_days = {}
        if csv_last is None or csv_last < end:
            ts_days = fetch_tushare_days(
                code, "2026-08-27" if csv_last is None else
                (pd.Timestamp(csv_last) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                end)
            time.sleep(1.2)                  # tushare 限流
        # 双源合并（同日根数多者胜，平手取 CSV；同 minute_data.merge_frames 口径）
        merged = dict(csv_days)
        for d, g in ts_days.items():
            if d not in csv_days or len(g) > len(csv_days[d]):
                merged[d] = g
        # 逐日算 F（生产模块，含对齐守卫）；只保留 ≤end 且最近 days 条
        entries, n_guard_fail = [], 0
        for d in sorted(merged):
            if d > end:
                continue
            f, diag = b7ff.factor_at_1430(day_bars_of(merged[d]))
            if f is None:
                n_guard_fail += 1
            entries.append({"date": d, "f": f})
        entries = entries[-args.days:]
        hist[code] = entries
        valid = sum(1 for e in entries if e["f"] is not None)
        report.append({"code": code, "csv_last": csv_last,
                       "ts_days": len(ts_days), "entries": len(entries),
                       "valid_f": valid, "guard_fail": n_guard_fail,
                       "last_date": entries[-1]["date"] if entries else None})
        print(f"  [{i+1}/{len(codes)}] {code}: csv至{csv_last} tushare+{len(ts_days)}日 "
              f"条目={len(entries)} 有效F={valid} 守卫失败={n_guard_fail} "
              f"末日={entries[-1]['date'] if entries else '--'}")

    ok = b7ff.save_hist(hist, HIST_PATH)
    n_ready = sum(1 for r in report
                  if (r["valid_f"] or 0) >= b7ff.Z_MIN_HIST
                  and r["last_date"] == end)
    print(f"\n[bootstrap] 落盘 {'成功' if ok else '失败'}: {HIST_PATH}")
    print(f"[bootstrap] 就绪票（有效F≥{b7ff.Z_MIN_HIST} 且覆盖到 {end}）: "
          f"{n_ready}/{len(report)}")
    not_ready = [r for r in report
                 if not ((r["valid_f"] or 0) >= b7ff.Z_MIN_HIST
                         and r["last_date"] == end)]
    for r in not_ready:
        print(f"  未就绪: {r['code']} csv_last={r['csv_last']} "
              f"ts_days={r['ts_days']} valid_f={r['valid_f']} last={r['last_date']}")
    rep_path = os.path.join(ROOT, "t_io", "state", "b7ff_bootstrap_report.json")
    with open(rep_path, "w", encoding="utf-8") as fp:
        json.dump({"end": end, "days": args.days, "n_ready": n_ready,
                   "hist_path": HIST_PATH, "stocks": report},
                  fp, ensure_ascii=False, indent=1)
    print(f"[bootstrap] 报告 -> {rep_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
    raise SystemExit(main())
