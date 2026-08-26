# -*- coding: utf-8 -*-
"""
regress_rsi_nan.py — V1.1.2 RSI NaN 兜底修复（C 语义）回归验证（2026-08-03 实盘缓存）

C 语义（父代理裁决）：
  - 0/0 钉平窗（gain==0 & loss==0）→ 填 50 中性
  - 纯上涨窗（loss==0 & gain>0）→ 保持 NaN（与现网一致盲）
  - 预热 leading NaN → 不变

验证项：
  a) 健康行为不变：修复前后 rsi / rsi_5m / rsi_15m 序列中，旧有效值零差异、
     无"旧有效→新NaN"回退；14:13 前填充仅允许 0/0 钉平窗（盘中钉平属同一 bug 修复范围）。
  b) 尾盘原 NaN 钉平窗现在产出有效数值（==50.0 中性）。
  c) 无副作用：填充值全部 == 50.0；纯上涨窗仍保持 NaN（逐一核对）；
     决策层面以 harness 双世界 diff 为准（见 regression_report_2026-08-03.md）。

数据源：生产分钟缓存 t_io/cache/minute_{code}_2026-08-03.csv。
旧公式在脚本内联复刻（不依赖被修复的模块），新公式调用 indicators.py 生产函数。

用法：python t_io/validation/rsi_nan_guard/regress_rsi_nan.py [--date 2026-08-03]
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, BASE_DIR)

from config import CACHE_DIR, PARAMS  # noqa: E402
from analysis import indicators  # noqa: E402

CODES = ["600176", "600481", "000988", "588170", "603667"]


def old_rsi_parts(close: pd.Series, period: int):
    """修复前公式（内联复刻 V1.1.1 及以前），返回 (old_rsi, gain, loss)。"""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period, min_periods=1).mean()
    loss = -delta.clip(upper=0).rolling(period, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs), gain, loss


def compare_series(name, times, close, period, new, results):
    old, gain, loss = old_rsi_parts(close, period)
    old = old.reset_index(drop=True)
    new = new.reset_index(drop=True)
    times = pd.Series(times).reset_index(drop=True)
    flat_win = ((gain == 0) & (loss == 0)).reset_index(drop=True)
    up_win = ((gain > 0) & (loss == 0)).reset_index(drop=True)

    both_valid = old.notna() & new.notna()
    n_mismatch = int((~np.isclose(old[both_valid], new[both_valid], atol=1e-12)).sum())
    n_new_nan = int((old.notna() & new.isna()).sum())          # 旧有效 → 新 NaN（不允许）
    filled = old.isna() & new.notna()                          # 旧 NaN → 新有效（应全部为钉平窗）
    up_still_nan = int((up_win & old.isna() & new.isna()).sum())   # 纯上涨窗保持 NaN
    up_filled = int((up_win & filled).sum())                   # 纯上涨窗被填（C 语义下不允许）
    flat_not_filled = int((flat_win & old.isna() & new.isna()).sum())  # 钉平窗未填（不允许）

    tail_start = pd.Timestamp(f"{times.iloc[0].date()} 14:13:00") if len(times) else None
    results.append({
        "series": name,
        "len": len(old),
        "old_nan": int(old.isna().sum()),
        "filled": int(filled.sum()),
        "filled_pre_1413": int((filled & (times < tail_start)).sum()),
        "filled_tail": int((filled & (times >= tail_start)).sum()),
        "filled_all_50": bool((new[filled] == 50.0).all()) if filled.any() else True,
        "filled_nonflat": int((filled & ~flat_win).sum()),     # 填充落在非钉平窗（不允许）
        "up_win_kept_nan": up_still_nan,
        "up_win_filled_bad": up_filled,
        "flat_win_unfilled_bad": flat_not_filled,
        "valid_value_mismatch": n_mismatch,
        "new_nan_regression": n_new_nan,
    })

    rows = []
    if filled.any():
        for i in np.where(filled.values)[0]:
            rows.append({"series": name, "time": times.iloc[i], "new": new.iloc[i]})
    return pd.DataFrame(rows)


def run(date: str) -> int:
    results, details = [], []
    for code in CODES:
        path = os.path.join(CACHE_DIR, f"minute_{code}_{date}.csv")
        if not os.path.exists(path):
            print(f"[SKIP] {code} 缓存缺失: {path}")
            continue
        df = pd.read_csv(path)
        df["time"] = pd.to_datetime(df["time"])

        # ── 1 分 RSI ──
        new1 = indicators.add_indicators(df.copy())["rsi"]
        details.append(compare_series(f"{code}.rsi_1m", df["time"], df["close"],
                                      PARAMS.get("rsi_period", 6), new1, results))

        # ── 5 分 RSI(14) ──
        df5 = indicators.resample_to_5min(df)
        if not df5.empty:
            new5 = indicators.add_5min_indicators(df5.copy())["rsi_5m"]
            details.append(compare_series(f"{code}.rsi_5m", df5["time"], df5["close"],
                                          PARAMS.get("rsi_period_5m", 14), new5, results))

        # ── 15 分 RSI(6) ──
        df15 = indicators.resample_to_15min(df)
        if not df15.empty:
            new15 = indicators.add_15min_indicators(df15.copy())["rsi_15m"]
            details.append(compare_series(f"{code}.rsi_15m", df15["time"], df15["close"],
                                          6, new15, results))

    detail_df = pd.concat([d for d in details if not d.empty], ignore_index=True) \
        if any(not d.empty for d in details) else pd.DataFrame()
    summary = pd.DataFrame(results)

    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print("\n===== 修复前后 RSI 序列对比汇总（C 语义） =====")
    print(summary.to_string(index=False))
    if not detail_df.empty:
        print(f"\n===== 填充明细（共 {len(detail_df)} 条，应全部 == 50.0 且全部为 0/0 钉平窗） =====")
        print(detail_df.to_string(index=False))

    # ── 判定 ──
    a_ok = bool((summary["valid_value_mismatch"] == 0).all()
                and (summary["new_nan_regression"] == 0).all())
    b_ok = bool(summary["filled_tail"].sum() > 0)
    c_ok = bool((summary["filled_all_50"]).all()
                and (summary["filled_nonflat"] == 0).all()
                and (summary["up_win_filled_bad"] == 0).all()
                and (summary["flat_win_unfilled_bad"] == 0).all())

    print("\n===== 回归判定（C 语义） =====")
    print(f"a) 健康行为不变（有效值零差异 {int(summary['valid_value_mismatch'].sum())} 处 / "
          f"新NaN回退 {int(summary['new_nan_regression'].sum())} 处）: {'PASS' if a_ok else 'FAIL'}")
    print(f"b) 尾盘原 NaN 钉平窗产出有效数值（填充 {int(summary['filled_tail'].sum())} 条）: {'PASS' if b_ok else 'FAIL'}")
    print(f"c) 填充值全部==50且仅钉平窗 / 纯上涨窗保持NaN {int(summary['up_win_kept_nan'].sum())} 条 / "
          f"违规 {int(summary['up_win_filled_bad'].sum() + summary['flat_win_unfilled_bad'].sum() + summary['filled_nonflat'].sum())} 条: "
          f"{'PASS' if c_ok else 'FAIL'}")
    overall = a_ok and b_ok and c_ok
    print(f"\n总体: {'PASS ✅' if overall else 'FAIL ❌'}")
    return 0 if overall else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-08-03")
    args = ap.parse_args()
    sys.exit(run(args.date))
