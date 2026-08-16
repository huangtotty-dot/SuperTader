# -*- coding: utf-8 -*-
"""
w34_settle_experiment.py — 做T结算参数优化（2026-08-15 新增）

在无未来函数的数据上，对做T信号（5分bb触轨+RSI6，含已实施的高抛放量确认）
扫描【目标/止损/窗口】网格，找期望收益(EV)最优的结算参数，并做个股分层。

结算口径：每信号入场价 P，窗口内目标先触=WIN（赚 target×P）、止损先触=FAIL（亏 stop×P）、
都未触=VOID（记 0）。EV = 命中率×target − 失败率×stop − 单次成本(费)。
单次成本默认 0.15%（佣金+卖出印花税近似），供净 EV 参考。

用法：
    python t_io/validation/w34_settle_experiment.py
输出：t_io/replay/settle_experiment_{start}_{end}/  report.md + by_stock.csv
"""
import argparse
import csv
import json
import sys
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
from w34_resonance_backtest_year import _day_df, _stock_code_to_ts, _trading_days, WATCHLIST_FILE, HOLDINGS_FILE  # noqa: E402

COST_RATE = 0.0015  # 单次交易成本近似（佣金+印花税）
TARGETS = [0.003, 0.005, 0.008]
STOPS = [0.003, 0.004, 0.006]
WINDOWS = [15, 30, 60]  # 分钟


def _first_touch(closes, price, action, target, stop, window_min):
    """在窗口内返回 WIN/FAIL/VOID。closes 为入场后逐分钟收盘（足够长）。"""
    tgt = 1 + target if action in ("BUY_LOW", "ADD_POS") else 1 - target
    stp = 1 - stop if action in ("BUY_LOW", "ADD_POS") else 1 + stop
    for i, p in enumerate(closes[:window_min]):
        if action in ("BUY_LOW", "ADD_POS"):
            if p <= price * stp:
                return "FAIL"
            if p >= price * tgt:
                return "WIN"
        else:
            if p >= price * stp:
                return "FAIL"
            if p <= price * tgt:
                return "WIN"
    return "VOID"


def main():
    ap = argparse.ArgumentParser(description="做T结算参数优化")
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
    signals = []  # 每条含入场价 + 入场后的逐分钟收盘序列（最多60根）
    n_total = n_vol_ok = 0
    day_idx = 0
    for day in days:
        day_idx += 1
        for code in codes:
            df1 = _day_df(_stock_code_to_ts(code), day, pro)
            if df1.empty or len(df1) < 60:
                continue
            df5 = add_5min_indicators(resample_to_5min(df1))
            if df5 is None or df5.empty or len(df5) < 13:
                continue
            times = df1["time"].values
            closes = df1["close"].values
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
                price = float(closes[idx])
                # 高抛放量确认（已实施）：SELL 需近5分钟量≥全天均量×1.5
                vol_5m = float(row.get("volume") or 0)
                vol_avg = float(df5["volume"][: b + 1].mean()) if b + 1 > 0 else 0.0
                vol_ratio = vol_5m / vol_avg if vol_avg > 0 else 0.0
                vol_ok = (action != "SELL_HIGH") or (vol_ratio >= 1.5)
                n_total += 1
                n_vol_ok += int(vol_ok)
                signals.append({
                    "code": code, "action": action, "price": price, "vol_ok": vol_ok,
                    "fwd": list(closes[idx + 1: idx + 61]),  # 最多60分钟
                })
        if day_idx % 30 == 0 or day_idx == len(days):
            print(f"  [{day_idx}/{len(days)}] {day} 信号 {n_total}")

    print(f"信号 {n_total}（高抛放量后 {n_vol_ok}）")
    base = [s for s in signals if s["vol_ok"]]

    # ── 网格寻优 ──
    rows = []
    for target in TARGETS:
        for stop in STOPS:
            for win in WINDOWS:
                w = f = v = 0
                for s in base:
                    r = _first_touch(s["fwd"], s["price"], s["action"], target, stop, win)
                    if r == "WIN":
                        w += 1
                    elif r == "FAIL":
                        f += 1
                    else:
                        v += 1
                n = w + f
                hr = w / n if n else None
                gross_ev = (hr * target - (1 - hr) * stop) if hr is not None else None
                net_ev = gross_ev - COST_RATE if gross_ev is not None else None
                rows.append({"target": target, "stop": stop, "window": win,
                             "n": len(base), "wins": w, "fails": f, "void": v,
                             "hit_rate": round(hr, 4) if hr is not None else None,
                             "gross_ev": round(gross_ev, 5) if gross_ev is not None else None,
                             "net_ev": round(net_ev, 5) if net_ev is not None else None})

    rows.sort(key=lambda r: (r["net_ev"] is None, -(r["net_ev"] or -9)))
    print(f"\n{'目标':>5} {'止损':>5} {'窗口':>4} {'n':>6} {'命中率':>7} {'毛EV':>8} {'净EV':>8}")
    print("-" * 55)
    for r in rows:
        fmt = lambda x: f"{x:.2%}" if x is not None else "—"  # noqa: E731
        print(f"{r['target']:>5.1%} {r['stop']:>5.1%} {r['window']:>4} {r['n']:>6} "
              f"{fmt(r['hit_rate']):>7} {r['gross_ev']:>8.4f} {r['net_ev']:>8.4f}")

    # ── 个股分层 ──
    print("\n== 个股分层（当前口径 target+0.5%/stop-0.4%/30min，高抛放量后）==")
    by_stock = {}
    for s in base:
        r = _first_touch(s["fwd"], s["price"], s["action"], 0.005, 0.004, 30)
        d = by_stock.setdefault(s["code"], {"n": 0, "w": 0, "f": 0})
        d["n"] += 1
        if r == "WIN":
            d["w"] += 1
        elif r == "FAIL":
            d["f"] += 1
    for c in sorted(by_stock, key=lambda c: -(by_stock[c]["w"] / (by_stock[c]["w"] + by_stock[c]["f"]) if (by_stock[c]["w"] + by_stock[c]["f"]) else 0)):
        d = by_stock[c]
        hr = d["w"] / (d["w"] + d["f"]) if (d["w"] + d["f"]) else None
        print(f"  {c} n={d['n']:5d} wins={d['w']:4d} fails={d['f']:4d} hit_rate={hr:.1%}" if hr else f"  {c} n=0")

    out_dir = BASE / "t_io" / "replay" / f"settle_experiment_{start}_{end}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "grid.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] 输出 → {out_dir}")


if __name__ == "__main__":
    main()
