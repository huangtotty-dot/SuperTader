# -*- coding: utf-8 -*-
"""
做T回测脚本（快速版）：只处理当前持仓，每5分钟步长
"""
import sys, os, json, glob, math
from datetime import datetime, timedelta
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 共享命名空间加载模块
import os as _os, sys as _sys, json as _json, time as _time, logging as _logging, traceback as _traceback, importlib.util as _importlib_util
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import numpy as np, pandas as pd, requests, urllib.request, urllib.error

_os.environ['http_proxy'] = ''
_os.environ['https_proxy'] = ''
_os.environ['HTTP_PROXY'] = ''
_os.environ['HTTPS_PROXY'] = ''
_os.environ['ALL_PROXY'] = ''
_os.environ['all_proxy'] = ''

# Mock akshare
class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: pd.DataFrame()

ak_mock = MockAkshare()
sys.modules['akshare'] = ak_mock

shared = {'akshare': ak_mock, 'ak': ak_mock}
shared.update({
    '__name__': '__main__', '__file__': __file__,
    'os': _os, 'sys': _sys, 'json': _json, 'time': _time, 'logging': _logging, 'traceback': _traceback,
    'importlib': _importlib_util, 'importlib.util': _importlib_util,
    'dataclass': dataclass, 'field': field, 'datetime': datetime, 'timedelta': timedelta, 'dtime': dtime,
    'Dict': Dict, 'List': List, 'Optional': Optional, 'Any': Any,
    'np': np, 'pd': pd, 'requests': requests, 'urllib': urllib,
    'urllib.request': urllib.request, 'urllib.error': urllib.error,
})

for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen']:
    mod_path = _os.path.join(BASE_DIR, f"{mod_name}.py")
    if not _os.path.exists(mod_path):
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)

globals().update(shared)

log.setLevel(logging.WARNING)
for h in log.handlers[:]:
    log.removeHandler(h)
log.addHandler(logging.StreamHandler())

COMMISSION_RATE = shared.get('PARAMS', {}).get('commission_rate', 0.0015)

# 当前持仓
HOLDINGS = {
    "588000": {"name": "科创50ETF华夏", "t_qty": 40000, "qty": 40000, "type": "etf"},
    "600089": {"name": "特变电工", "t_qty": 400, "qty": 400, "type": "stock"},
    "600176": {"name": "中国巨石", "t_qty": 600, "qty": 600, "type": "stock"},
    "600481": {"name": "双良节能", "t_qty": 1400, "qty": 1400, "type": "stock"},
    "603667": {"name": "五洲新春", "t_qty": 200, "qty": 200, "type": "stock"},
    "002261": {"name": "拓维信息", "t_qty": 300, "qty": 300, "type": "stock"},
    "688102": {"name": "陕西斯瑞新材", "t_qty": 800, "qty": 800, "type": "stock"},
    "002837": {"name": "英维克", "t_qty": 400, "qty": 400, "type": "stock"},
}

TARGET_CODES = set(HOLDINGS.keys())

def run_backtest():
    snapshot_dir = os.path.join(BASE_DIR, "t_io", "minute_snapshots")
    if not os.path.exists(snapshot_dir):
        print(f"[ERROR] Snapshot dir missing: {snapshot_dir}")
        return

    # 只处理最近60天的数据（快照主要是5月份的，需要回溯）
    cutoff = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
    
    snapshot_files = glob.glob(os.path.join(snapshot_dir, "**/*.json"), recursive=True)
    
    # 过滤：只保留当前持仓 + 最近10天
    filtered = []
    for path in snapshot_files:
        try:
            fname = os.path.basename(path)
            parts = fname.replace('.json', '').split('_')
            if len(parts) < 2:
                continue
            code = parts[0]
            date = parts[-1]
            if code in TARGET_CODES and date >= cutoff:
                filtered.append(path)
        except Exception:
            continue
    
    # 显示所有可用快照的股票
    available_codes = set()
    for path in snapshot_files:
        try:
            fname = os.path.basename(path)
            parts = fname.replace('.json', '').split('_')
            if len(parts) >= 2:
                available_codes.add(parts[0])
        except Exception:
            continue
    print(f"[INFO] Available stocks in snapshots: {sorted(available_codes)}")
    print(f"[INFO] Target holdings: {sorted(TARGET_CODES)}")
    print(f"[INFO] Overlap: {sorted(available_codes & TARGET_CODES)}")
    
    if not filtered:
        print("[WARN] No matching snapshots found")
        return

    results = {
        "total_days": 0,
        "total_trades": 0,
        "completed_cycles": 0,
        "incomplete_cycles": 0,
        "daily_pnl": defaultdict(float),
        "by_code": defaultdict(lambda: {
            "trades": 0, "completed": 0, "incomplete": 0,
            "pnl": 0.0, "buy_signals": 0, "sell_signals": 0
        }),
        "trade_log": [],
    }

    for path in sorted(filtered):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                snap = json.load(f)
        except Exception:
            continue

        code = snap.get("code")
        date = snap.get("date")
        bars = snap.get("bars", [])
        if not bars or len(bars) < 30:
            continue

        holding = HOLDINGS.get(code, {"name": snap.get("name", code), "t_qty": 1000, "qty": 1000, "type": "stock"})
        qty = int(holding.get("t_qty") or holding.get("qty") or 1000)

        engine = shared.get('SignalEngine', lambda: None)()
        if engine is None:
            continue

        daily_ctx = snap.get("daily_context", {})
        if not isinstance(daily_ctx, dict):
            daily_ctx = {}

        buy_price = None
        buy_time = None
        pnl = 0.0
        day_trades = 0
        day_completed = 0

        # 每5分钟步长模拟（关键优化）
        for i in range(25, len(bars) + 1, 5):
            df = pd.DataFrame(bars[:i])
            if df.empty or len(df) < 25:
                continue

            for col in ["open", "high", "low", "close", "volume", "amount"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["time", "open", "high", "low", "close"]).reset_index(drop=True)
            if df.empty or len(df) < 25:
                continue

            try:
                add_indicators = shared.get('add_indicators')
                if not add_indicators:
                    continue
                df = add_indicators(df)
            except Exception:
                continue

            current_time = df.iloc[-1]["time"]
            if isinstance(current_time, str):
                current_time = pd.to_datetime(current_time)
            if hasattr(current_time, 'to_pydatetime'):
                current_time = current_time.to_pydatetime()

            shared['SIM_NOW'] = current_time
            globals()['SIM_NOW'] = current_time

            price = float(df.iloc[-1]["close"]) if "close" in df.columns else 0.0
            if price <= 0:
                continue

            try:
                buy_score, sell_score, sig = engine.evaluate(
                    code, holding.get("name", code), df, holding, daily_ctx=daily_ctx
                )
            except Exception:
                continue

            if sig and sig.action in ["BUY_LOW", "ADD_POS"]:
                if buy_price is None:
                    buy_price = price
                    buy_time = current_time.strftime("%H:%M:%S")
                    results["by_code"][code]["buy_signals"] += 1
                    results["trade_log"].append({
                        "date": date, "code": code, "action": "BUY",
                        "time": buy_time, "price": buy_price, "score": sig.score,
                    })

            elif sig and sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                if buy_price is not None:
                    raw_pnl = (price - buy_price) * qty
                    commission = (buy_price + price) * qty * COMMISSION_RATE
                    net_pnl = raw_pnl - commission
                    pnl += net_pnl
                    day_trades += 1
                    day_completed += 1

                    results["trade_log"].append({
                        "date": date, "code": code, "action": "SELL",
                        "time": current_time.strftime("%H:%M:%S"), "price": price,
                        "score": sig.score, "buy_price": buy_price,
                        "pnl": net_pnl, "qty": qty,
                    })
                    results["by_code"][code]["sell_signals"] += 1
                    results["by_code"][code]["pnl"] += net_pnl
                    buy_price = None
                    buy_time = None

        if buy_price is not None:
            results["incomplete_cycles"] += 1
            results["by_code"][code]["incomplete"] += 1

        if day_trades > 0:
            results["daily_pnl"][date] += pnl
            results["total_trades"] += day_trades
            results["completed_cycles"] += day_completed
            results["by_code"][code]["trades"] += day_trades
            results["by_code"][code]["completed"] += day_completed

        print(f"[DONE] {code} {date}: trades={day_trades}, PnL=CNY {pnl:+.2f}")

    results["total_days"] = len(results["daily_pnl"])

    print("\n" + "="*70)
    print("[Backtest Report]")
    print("="*70)
    print(f"\nBacktest days: {results['total_days']}")
    print(f"Completed T-cycles: {results['completed_cycles']}")
    print(f"Incomplete T-cycles: {results['incomplete_cycles']}")
    print(f"Total trades: {results['total_trades']}")

    total_pnl = sum(results['daily_pnl'].values())
    print(f"\nTotal PnL: CNY {total_pnl:.2f}")
    if results['completed_cycles'] > 0:
        print(f"Avg per cycle: CNY {total_pnl/results['completed_cycles']:.2f}")

    print(f"\n[Daily PnL]")
    for date in sorted(results['daily_pnl'].keys()):
        pnl = results['daily_pnl'][date]
        print(f"  {date}: CNY {pnl:+.2f}")

    print(f"\n[By Stock]")
    for code, stats in sorted(results['by_code'].items(), key=lambda x: x[1]['pnl'], reverse=True):
        print(f"  {code}: trades={stats['trades']}, completed={stats['completed']}, incomplete={stats['incomplete']}, PnL=CNY {stats['pnl']:+.2f}")

    report_path = os.path.join(BASE_DIR, "backtest_report.json")
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump({
            "summary": {
                "total_days": results['total_days'],
                "completed_cycles": results['completed_cycles'],
                "incomplete_cycles": results['incomplete_cycles'],
                "total_trades": results['total_trades'],
                "total_pnl": total_pnl,
            },
            "daily_pnl": dict(results['daily_pnl']),
            "by_code": {k: dict(v) for k, v in results['by_code'].items()},
            "trade_log": results['trade_log'][-100:],
        }, f, ensure_ascii=False, indent=2)
    print(f"\nReport saved: {report_path}")

if __name__ == "__main__":
    run_backtest()
