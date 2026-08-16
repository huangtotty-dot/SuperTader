# -*- coding: utf-8 -*-
"""
w34_factor_experiment.py — 做T信号·个股侧因子分离实验（2026-08-15 新增）

在无未来函数的数据上，为当前做T信号（5分 bb 触轨 + RSI6 极值）逐信号计算一组
个股侧因子（VWAP偏离/时间/日内位置/动量/量能/ATR等，全部用 entry_ts 之前数据），
分桶统计命中率，找出能分离"信号胜/负"的因子；再测结算窗口(30/60/90min)影响。

命中率口径：+0.5%/-0.4% 先触胜率（与 daily_review 一致），窗口可测 30/60/90 根 1 分钟。
所有因子严格无未来函数：只用 df1[df1.time <= entry_ts]。

用法：
    python t_io/validation/w34_factor_experiment.py
    python t_io/validation/w34_factor_experiment.py --start 2024-08-15 --end 2025-08-14
输出：t_io/replay/factor_experiment_{start}_{end}/  signals.jsonl + factors.md
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

# ── Windows 终端 UTF-8 编码修复 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from indicators import resample_to_5min, add_5min_indicators  # noqa: E402
from index_regime_intraday import _iri_tushare_pro  # noqa: E402
from index_resonance import resolve_index  # noqa: E402
from w34_resonance_backtest_year import _day_df, _stock_code_to_ts, _trading_days, WATCHLIST_FILE, HOLDINGS_FILE  # noqa: E402

WIN_TARGET = 0.005
STOP = 0.004
FACTORS = ["vwap_dev", "time_bucket", "range_pos", "mom30", "vol_ratio", "atr_rel", "gap_pct", "stock_ma5_dev"]


def _outcome(df1, entry_idx, action, price, ticks):
    tgt, stp = 1 + WIN_TARGET, 1 - STOP
    for i in range(entry_idx + 1, min(entry_idx + ticks + 1, len(df1))):
        p = float(df1.iloc[i]["close"])
        if action in ("BUY_LOW", "ADD_POS"):
            if p <= price * stp:
                return "FAIL"
            if p >= price * tgt:
                return "WIN"
        else:
            if p >= price * (1 + STOP):
                return "FAIL"
            if p <= price * (1 - WIN_TARGET):
                return "WIN"
    return "VOID"


def _compute_factors(df1, entry_ts, entry_idx, price):
    """用 entry_ts 之前的数据算因子（无未来函数）。返回 dict。"""
    sub = df1.iloc[: entry_idx + 1]
    f = {}
    c = sub["close"]
    v = sub["volume"]
    # VWAP 偏离
    tp = (sub["high"] + sub["low"] + sub["close"]) / 3
    vwap = (tp * sub["volume"]).sum() / (sub["volume"].sum() + 1e-9)
    f["vwap_dev"] = (price - vwap) / vwap if vwap else 0.0
    # 日内位置
    dh = float(sub["high"].max()); dl = float(sub["low"].min())
    f["range_pos"] = (price - dl) / (dh - dl) if dh > dl else 0.5
    # 30 分钟动量（若足够数据）
    t0 = pd.Timestamp(entry_ts) - pd.Timedelta(minutes=30)
    past = sub[sub["time"] <= t0]
    f["mom30"] = (price / float(past.iloc[-1]["close"]) - 1) if (not past.empty and float(past.iloc[-1]["close"]) > 0) else 0.0
    # 量比（近5分钟 vs 全天均量）
    recent = sub.tail(5)
    f["vol_ratio"] = float(recent["volume"].mean()) / (float(sub["volume"].mean()) + 1e-9) if len(sub) > 5 else 1.0
    # ATR 相对偏移
    h_l = (sub["high"] - sub["low"]).tail(14)
    atr = float(h_l.mean()) if not h_l.empty else 0.0
    f["atr_rel"] = (price - float(sub.iloc[-1]["open"])) / (atr + 1e-9) if atr > 0 else 0.0
    # 跳空
    pre = float(df1.iloc[0]["open"])
    f["gap_pct"] = (float(sub.iloc[0]["open"]) / pre - 1) if pre > 0 else 0.0
    # 个股5分钟MA5偏离
    ma5 = float(c.tail(5).mean())
    f["stock_ma5_dev"] = (price - ma5) / ma5 if ma5 > 0 else 0.0
    # 时间桶
    f["time_bucket"] = pd.Timestamp(entry_ts).hour  # 9/10/11/13/14
    return f


def main():
    ap = argparse.ArgumentParser(description="做T信号·个股侧因子分离实验")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--codes", nargs="*", default=None)
    args = ap.parse_args()

    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")
    start = args.start or (pd.Timestamp(end) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    if args.codes:
        codes = list(args.codes)
    else:
        wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8")) if WATCHLIST_FILE.exists() else {}
        codes = [c for c, v in (wl.get("stocks", {}) or {}).items()
                 if isinstance(v, dict) and not c.startswith("_example") and v.get("status") in ("monitoring", "signal")]
    codes = sorted(set(codes))
    days = _trading_days(start, end)
    print(f"[universe] {len(codes)} 候选 ｜ {start}~{end}（{len(days)} 工作日）")

    pro = _iri_tushare_pro()
    index_codes = {resolve_index(c)[0] for c in codes}
    signals = []
    day_idx = 0
    for day in days:
        day_idx += 1
        # 指数5分钟预计算（无未来：index as-of 取 label<entry_ts 已收盘根）
        idx_5min_day = {}
        for ic in index_codes:
            ic_ts = (ic[2:] + ".SH") if ic.startswith("sh") else ((ic[2:] + ".SZ") if ic.startswith("sz") else ic)
            df1i = _day_df(ic_ts, day, pro)
            if df1i.empty:
                continue
            df5i = resample_to_5min(df1i)
            if df5i is not None and not df5i.empty:
                df5i = add_5min_indicators(df5i)
                df5i["idx_ma5"] = df5i["close"].rolling(5).mean()
                idx_5min_day[ic] = df5i
        for code in codes:
            df1 = _day_df(_stock_code_to_ts(code), day, pro)
            if df1.empty or len(df1) < 60:
                continue
            df5 = add_5min_indicators(resample_to_5min(df1))
            if df5 is None or df5.empty or len(df5) < 13:
                continue
            times = df1["time"].values
            ic = resolve_index(code)[0]
            idx5 = idx_5min_day.get(ic)
            idx_times = idx5["time"].values if (idx5 is not None and not idx5.empty) else np.array([])
            for b in range(12, len(df5)):
                row = df5.iloc[b]
                bb = row.get("bb_pct_5m"); rsi = row.get("rsi_5m_p6")
                if bb is None or rsi is None or pd.isna(bb) or pd.isna(rsi):
                    continue
                bb, rsi = float(bb), float(rsi)
                if bb >= 1.0 and rsi > 75:
                    action = "SELL_HIGH"
                elif bb <= 0.0 and rsi < 35:
                    action = "BUY_LOW"
                else:
                    continue
                entry_ts = row["time"] + pd.Timedelta(minutes=5)
                idx = int(np.searchsorted(times, np.datetime64(entry_ts), side="right")) - 1
                if idx < 0 or idx >= len(df1):
                    continue
                price = float(df1.iloc[idx]["close"])
                f = _compute_factors(df1, entry_ts, idx, price)
                # 指数特征 as-of（label<entry_ts 已收盘根，无未来）
                if len(idx_times) > 0:
                    j = int(np.searchsorted(idx_times, np.datetime64(entry_ts), side="left")) - 1
                    if j >= 0:
                        il = idx5.iloc[j]
                        icl = float(il.get("close")) if pd.notna(il.get("close")) else None
                        ima = float(il.get("idx_ma5")) if pd.notna(il.get("idx_ma5")) else None
                        ibb = float(il.get("bb_pct_5m")) if pd.notna(il.get("bb_pct_5m")) else None
                        irs = float(il.get("rsi_5m_p6")) if pd.notna(il.get("rsi_5m_p6")) else None
                        f["idx_close"] = icl; f["idx_ma5"] = ima; f["idx_bb"] = ibb; f["idx_rsi"] = irs
                        f["idx_ok"] = (icl is not None and ima is not None)
                    else:
                        f["idx_ok"] = False
                else:
                    f["idx_ok"] = False
                signals.append({
                    "date": day, "ts": str(entry_ts)[11:16], "code": code, "action": action, "price": round(price, 3),
                    "o30": _outcome(df1, idx, action, price, 30),
                    "o60": _outcome(df1, idx, action, price, 60),
                    "o90": _outcome(df1, idx, action, price, 90),
                    **f,
                })
        if day_idx % 30 == 0 or day_idx == len(days):
            print(f"  [{day_idx}/{len(days)}] {day} 累计信号 {len(signals)}")
    print(f"信号总数 {len(signals)}")

    def _hr(rows, key):
        w = sum(1 for r in rows if r[key] == "WIN"); f = sum(1 for r in rows if r[key] == "FAIL")
        return (len(rows), w / (w + f) if (w + f) else None)

    # ── 因子分桶分离 ──
    lines = [f"# 做T信号·个股侧因子分离（{start}~{end}，{len(signals)} 信号，+0.5%/-0.4%）", ""]
    print(f"\n{'因子':14s} {'分桶':20s} {'n':>6} {'hr30':>7} {'hr60':>7} {'hr90':>7}")
    print("-" * 70)
    bucket_specs = {
        "vwap_dev": [(-9, -0.005, "<-0.5%"), (-0.005, 0, "-0.5~0"), (0, 0.005, "0~0.5%"), (0.005, 9, ">0.5%")],
        "range_pos": [(-9, 0.25, "<0.25(近低)"), (0.25, 0.5, "0.25~0.5"), (0.5, 0.75, "0.5~0.75"), (0.75, 9, ">0.75(近高)")],
        "mom30": [(-9, -0.01, "<-1%"), (-0.01, 0, "-1~0"), (0, 0.01, "0~1%"), (0.01, 9, ">1%")],
        "vol_ratio": [(-9, 0.8, "<0.8缩量"), (0.8, 1.5, "0.8~1.5"), (1.5, 3, "1.5~3"), (3, 9, ">3放量")],
        "atr_rel": [(-9, -1, "<-1"), (-1, 0, "-1~0"), (0, 1, "0~1"), (1, 9, ">1")],
        "gap_pct": [(-9, -0.01, "<-1%低开"), (-0.01, 0, "-1~0"), (0, 0.01, "0~1%"), (0.01, 9, ">1%高开")],
        "stock_ma5_dev": [(-9, -0.01, "<-1%"), (-0.01, 0, "-1~0"), (0, 0.01, "0~1%"), (0.01, 9, ">1%")],
        "time_bucket": [(9, 10, "9-10"), (10, 11, "10-11"), (11, 12, "11-12"), (13, 14, "13-14"), (14, 15, "14-15")],
    }
    for fact in FACTORS:
        if fact == "time_bucket":
            specs = bucket_specs[fact]
            for lo, hi, name in specs:
                sub = [r for r in signals if lo <= r[fact] < hi]
                n, h30 = _hr(sub, "o30"); _, h60 = _hr(sub, "o60"); _, h90 = _hr(sub, "o90")
                fmt = lambda h: f"{h:.1%}" if h else "—"  # noqa: E731
                print(f"{fact:14s} {name:20s} {n:6d} {fmt(h30):>7} {fmt(h60):>7} {fmt(h90):>7}")
        else:
            specs = bucket_specs[fact]
            for lo, hi, name in specs:
                sub = [r for r in signals if lo <= r[fact] < hi]
                n, h30 = _hr(sub, "o30"); _, h60 = _hr(sub, "o60"); _, h90 = _hr(sub, "o90")
                fmt = lambda h: f"{h:.1%}" if h else "—"  # noqa: E731
                print(f"{fact:14s} {name:20s} {n:6d} {fmt(h30):>7} {fmt(h60):>7} {fmt(h90):>7}")

    # 按买卖分开看关键因子（VWAP偏离、日内位置）
    print("\n== 按动作 × VWAP偏离 ==")
    for act in ("BUY_LOW", "SELL_HIGH"):
        sub = [r for r in signals if r["action"] == act]
        for lo, hi, name in bucket_specs["vwap_dev"]:
            b = [r for r in sub if lo <= r["vwap_dev"] < hi]
            n, h = _hr(b, "o30")
            print(f"  {act:9s} {name:20s} n={n:5d} hr30={h:.1%}" if h else f"  {act:9s} {name:20s} n=0")

    out_dir = BASE / "t_io" / "replay" / f"factor_experiment_{start}_{end}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "signals.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in signals) + "\n", encoding="utf-8")
    (out_dir / "factors.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[OK] 输出 → {out_dir}")


if __name__ == "__main__":
    main()
