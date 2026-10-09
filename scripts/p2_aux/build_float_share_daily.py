# -*- coding: utf-8 -*-
"""P2 数据外拉 — 流通股本事件历史 → 日频面板（PIT 前向填充）。

读取 _ckpt_float/{code6}.parquet 事件表（东财股本结构），按 merge_asof(backward)
把「已上市流通A股」填充到面板交易日历（2023-09-01 起，与 F3 窗口对齐）。
口径：float_share 单位=万股（对齐 tushare daily_basic 习惯），float_share_shares 单位=股。
首个事件日之前为 NaN（上市前/数据起点之前，如实留空）。
产物：t_io/validation/xsection/panel_aux/float_share_daily.parquet
"""
import glob
import os
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUX = os.path.join(BASE, "t_io", "validation", "xsection", "panel_aux")
CKPT = os.path.join(AUX, "_ckpt_float")
PANEL = os.path.join(BASE, "t_io", "validation", "xsection", "panel")
WINDOW_START = pd.Timestamp("2023-09-01")


def main():
    # 1) 面板交易日历（用 universe 中的活跃大盘股 eob 即可代表全市场交易日）
    shards = sorted(glob.glob(os.path.join(PANEL, "shards", "shard_*.parquet")))
    dates = None
    for sp in shards:
        d = pd.read_parquet(sp, columns=["eob"])["eob"]
        dates = d if dates is None else pd.concat([dates, d])
    cal = pd.Series(pd.to_datetime(dates).dt.tz_localize(None).unique()).sort_values()
    cal = cal[cal >= WINDOW_START].reset_index(drop=True).astype("datetime64[ns]")
    print(f"[cal] trading days {cal.iloc[0].date()}..{cal.iloc[-1].date()} n={len(cal)}")

    uni = pd.read_parquet(os.path.join(PANEL, "universe.parquet"))
    uni["code6"] = uni["symbol"].str.split(".").str[1]

    frames = []
    n_empty = n_nan_head = 0
    for _, r in uni.iterrows():
        sym, code6 = r["symbol"], r["code6"]
        path = os.path.join(CKPT, f"{code6}.parquet")
        if not os.path.exists(path):
            n_empty += 1
            continue
        ev = pd.read_parquet(path)
        if "_empty" in ev.columns or ev.empty or "已上市流通A股" not in ev.columns:
            n_empty += 1
            continue
        ev = ev[["变更日期", "已上市流通A股", "已流通股份"]].copy()
        ev["变更日期"] = pd.to_datetime(ev["变更日期"]).astype("datetime64[ns]")
        ev = ev.sort_values("变更日期").drop_duplicates("变更日期", keep="last")
        # B股「已上市流通A股」恒为 0（流通股为 B 股），回退用「已流通股份」
        fa = pd.to_numeric(ev["已上市流通A股"], errors="coerce")
        if fa.fillna(0).max() > 0:
            ev["_float"] = fa
            field = "float_a"
        else:
            ev["_float"] = pd.to_numeric(ev["已流通股份"], errors="coerce")
            field = "float_all_bshare"
        ev = ev.dropna(subset=["变更日期", "_float"])
        ev = ev[ev["_float"] > 0]
        if ev.empty:
            n_empty += 1
            continue
        left = pd.DataFrame({"trade_date": cal})
        m = pd.merge_asof(left, ev[["变更日期", "_float"]].rename(columns={"变更日期": "trade_date"}),
                          on="trade_date", direction="backward")
        if m["_float"].isna().all():
            n_nan_head += 1
            continue
        m["symbol"] = sym
        m["float_share_shares"] = m["_float"].astype("float64")
        m["float_share"] = m["float_share_shares"] / 1e4  # 万股
        m["float_field"] = field
        frames.append(m[["symbol", "trade_date", "float_share", "float_share_shares", "float_field"]])

    out = pd.concat(frames, ignore_index=True)
    path = os.path.join(AUX, "float_share_daily.parquet")
    out.to_parquet(path, index=False)
    print(f"[SAVE] {path} rows={len(out)} symbols={out.symbol.nunique()}")
    print(f"[stat] no-data symbols={n_empty}, all-nan symbols={n_nan_head}")
    # 覆盖率：窗口内有面板行情的 symbol 中，拿到非空 float_share 的比例
    print(out.groupby("symbol")["float_share"].apply(lambda s: s.notna().mean()).describe().to_string())


if __name__ == "__main__":
    main()
