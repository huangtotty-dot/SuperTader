# -*- coding: utf-8 -*-
"""批量回测脚本 — 利用 optimizer 已缓存数据，快速评估所有持仓股票"""
import sys, os, json, time
from pathlib import Path
from datetime import datetime
import pandas as pd
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "t_io" / "cache" / "tushare_mins"
OUT_DIR = BASE_DIR / "t_io" / "backtests"

import signal_engine as _se
from config import STOCK_PARAMS

STOCKS = {
    "000988": "华工科技", "600481": "双良节能",
    "600176": "中国巨石", "603667": "五洲新春", "588170": "科创芯片ETF",
}
START, END = "2025-06-01", "2026-07-23"

def run_one(code, name):
    sp = STOCK_PARAMS.get(code, {})
    _se.PARAMS.update(sp)
    _se.MINUTE_FETCH_STATUS[code] = "ok"
    _se.VIRTUAL_TRADES.clear()
    engine = _se.SignalEngine()

    cash = 50000.0
    base_holdings = 1000
    intraday_buy_qty = 0
    all_trades = []
    nav_log = []

    files = sorted((CACHE_DIR / code).glob("*.csv"))
    if not files:
        print(f"  {code} {name}: NO DATA")
        return

    t0 = time.time()
    for f in files:
        ds = f.stem
        if ds < START or ds > END:
            continue
        df = pd.read_csv(f)
        df["time"] = pd.to_datetime(df["time"])
        if "vwap" not in df.columns or len(df) < 30:
            continue

        engine.state_reset_date = ds
        engine.buy_count_per_stock[code] = 0
        engine.sell_count_per_stock[code] = 0
        engine.post_sell_block_until[code] = None

        # Daily context with buy_t_ok=True (required for BUY signals)
        daily_ctx = {
            "daily_status": "ok", "daily_buy_t_ok": True,
            "daily_ma5": float(df.iloc[-1]["close"]), "daily_ma5_state": "above_ma5_trend",
            "daily_above_ma5": True, "daily_breakdown_risk": False,
            "daily_overheated": False, "index_regime": "range",
            "intraday_alerts": [],
        }

        pre_close = float(df.iloc[0].get("prev_close", df.iloc[0]["close"]))
        hold_qty = base_holdings + intraday_buy_qty
        h = {"name": name, "cost": float(df.iloc[0]["close"]), "qty": hold_qty,
             "t_qty": hold_qty, "type": "stock" if not code.startswith("5") else "etf",
             "pre_close": pre_close}

        day_buys, day_sells = [], []
        bc = 0
        for i in range(25, len(df), 5):
            sub = df.iloc[:i+1].copy()
            try:
                bs, ss, sig = engine.evaluate(code, name, sub, h, daily_ctx=daily_ctx)
            except Exception:
                continue
            if sig is None:
                continue
            cp = float(df.iloc[i]["close"])
            t = pd.Timestamp(df.iloc[i]["time"])
            t_val = t.hour * 100 + t.minute

            # Threshold filter (match main.py)
            if sig.action in ("BUY_LOW", "ADD_POS"):
                nth = sp.get("notify_buy_threshold", 68)
            elif t_val >= 1000:
                nth = sp.get("notify_sell_threshold", 65)
            elif t_val < 1000 and sig.action == "SELL_HIGH":
                nth = sp.get("notify_sell_early_threshold", 75)
            else:
                nth = 65
            if sig.score < nth:
                continue

            if sig.action in ("BUY_LOW", "ADD_POS") and bc < 3:
                cost_trade = (cp + 0.01) * 200 * 1.00015
                if cash >= cost_trade:
                    cash -= cost_trade
                    day_buys.append(cp)
                    intraday_buy_qty += 200
                    bc += 1
            elif sig.action == "SELL_HIGH":
                sellable = base_holdings + intraday_buy_qty
                if sellable >= 200:
                    proceeds = (cp - 0.01) * 200 * (1 - 0.00015 - 0.0005)
                    cash += proceeds
                    day_sells.append(cp)
                    if intraday_buy_qty >= 200:
                        intraday_buy_qty -= 200
                    else:
                        remaining = 200 - intraday_buy_qty
                        intraday_buy_qty = 0
                        base_holdings -= remaining

        # Pair trades (record PnL, no holdings restore needed - T0 self-balancing)
        for j in range(min(len(day_buys), len(day_sells))):
            net = (day_sells[j] - day_buys[j]) * 200 - day_buys[j]*200*0.00015 - day_sells[j]*200*(0.00015+0.0005)
            all_trades.append({"date": ds, "net": round(net, 2)})

        total_holdings = base_holdings + intraday_buy_qty
        close = float(df.iloc[-1]["close"])
        nav = cash + total_holdings * close
        nav_log.append({"date": ds, "nav": round(nav, 2)})

    elapsed = time.time() - t0
    n = len(all_trades)
    wins = sum(1 for t in all_trades if t["net"] > 0)
    wr = wins / n * 100 if n > 0 else 0
    total_pnl = sum(t["net"] for t in all_trades)

    # Metrics
    if len(nav_log) >= 3:
        df_nav = pd.DataFrame(nav_log)
        fv, lv = df_nav.iloc[0]["nav"], df_nav.iloc[-1]["nav"]
        total_ret = (lv / fv - 1) * 100
        peak = df_nav["nav"].cummax()
        mdd = float(((df_nav["nav"] / peak) - 1).min() * 100)
        n_days = max(len(nav_log) - 1, 1)
        ann_ret = ((lv / fv) ** (252 / n_days) - 1) * 100 if fv > 0 else 0
        df_nav["ret"] = df_nav["nav"].pct_change()
        dr = df_nav["ret"].dropna()
        sharpe = float(np.mean(dr - 0.03/252) / np.std(dr, ddof=1) * np.sqrt(252)) if len(dr) > 2 and np.std(dr, ddof=1) > 0 else 0
    else:
        total_ret = mdd = ann_ret = sharpe = 0

    print(f"  {code} {name}: {n} trades | Win {wins}/{n} ({wr:.1f}%) | "
          f"T0 PnL {total_pnl:+.0f} | Ret {total_ret:+.2f}% | DD {mdd:.2f}% | "
          f"Sharpe {sharpe:.2f} | {elapsed:.0f}s")
    return {"code": code, "name": name, "n": n, "wins": wins, "wr": wr,
            "total_pnl": total_pnl, "total_ret": total_ret, "mdd": mdd,
            "ann_ret": ann_ret, "sharpe": sharpe, "nav_log": nav_log,
            "trades": all_trades, "elapsed": elapsed}

def main():
    # Parse args
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="")
    args = ap.parse_args()

    codes = [args.code] if args.code else list(STOCKS.keys())
    results = []
    print(f"批量回测 {START} ~ {END} ({len(codes)} 只股票)")
    print("=" * 70)

    for code in codes:
        name = STOCKS.get(code, code)
        r = run_one(code, name)
        if r:
            results.append(r)

    # Summary table
    print("\n" + "=" * 70)
    print(f"{'股票':<12} {'交易':>5} {'胜率':>7} {'T0利润':>8} {'总收益':>8} {'回撤':>7} {'夏普':>6} {'耗时':>6}")
    print("-" * 70)
    for r in sorted(results, key=lambda x: x.get("total_ret", 0), reverse=True):
        print(f"{r['name']}({r['code']}): {r['n']:>4}笔 {r['wr']:>6.1f}% {r['total_pnl']:>+7.0f} {r['total_ret']:>+7.2f}% {r['mdd']:>6.2f}% {r['sharpe']:>5.2f} {r['elapsed']:>5.0f}s")

    # Save report
    out_dir = OUT_DIR / "batch_summary"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"batch_{START}_{END}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in r.items() if k not in ("nav_log", "trades")} for r in results],
                  f, ensure_ascii=False, indent=2)
    print(f"\n报告: {report_path}")

if __name__ == "__main__":
    main()
