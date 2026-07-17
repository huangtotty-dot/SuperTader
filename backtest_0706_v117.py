# -*- coding: utf-8 -*-
"""
V1.17 回测脚本：使用2026-07-06本地CSV快照验证缩量止跌+放量反攻信号
"""
import sys, os, csv, json, math
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

# 获取 logger
log = _logging.getLogger("backtest_v117")

shared = {'akshare': ak_mock, 'ak': ak_mock, 'log': log}
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

# 当前持仓（2026-07-06）
HOLDINGS = {
    "002261": {"name": "拓维信息", "cost": 33.787, "qty": 1200, "base": 1200, "t_qty": 1200, "type": "stock", "account": "账户A", "pre_close": 29.07/0.9962},
    "588170": {"name": "科创半导体ETF", "cost": 1.252, "qty": 54000, "base": 54000, "t_qty": 54000, "type": "etf", "account": "账户A", "pre_close": 1.214/1.0176},
    "600089": {"name": "特变电工", "cost": 26.216, "qty": 1200, "base": 1200, "t_qty": 1200, "type": "stock", "account": "账户A", "pre_close": 21.74/1.0042},
    "000988": {"name": "华工科技", "cost": 207.205, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "pre_close": 153.95},
    "300666": {"name": "江丰电子", "cost": 397.317, "qty": 100, "base": 100, "t_qty": 100, "type": "stock", "account": "账户B", "pre_close": 338.97/1.0204},
}

def read_csv_snapshot(code, date="2026-07-06"):
    """读取本地CSV分钟数据"""
    path = os.path.join(BASE_DIR, "t_io", "cache", f"minute_{code}_{date}.csv")
    if not os.path.exists(path):
        return None
    data = []
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            data.append({
                "time": row["time"],
                "date": row["time"][:10],
                "open": float(row["open"]),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "volume": float(row["volume"]),
                "amount": float(row["amount"]),
            })
    return pd.DataFrame(data)

def run_backtest():
    results = {
        "total_trades": 0,
        "completed_cycles": 0,
        "incomplete_cycles": 0,
        "daily_pnl": defaultdict(float),
        "by_code": defaultdict(lambda: {
            "trades": 0, "completed": 0, "incomplete": 0,
            "pnl": 0.0, "buy_signals": 0, "sell_signals": 0,
            "vol_rev_signals": 0,
        }),
        "trade_log": [],
        "signal_log": [],
    }
    
    for code, holding in HOLDINGS.items():
        print(f"\n{'='*60}")
        print(f"回测 {code} {holding['name']}")
        print(f"{'='*60}")
        
        df = read_csv_snapshot(code)
        if df is None or df.empty:
            print(f"  [SKIP] 无数据")
            continue
        
        # 设置分钟数据状态
        shared['MINUTE_FETCH_STATUS'][code] = "ok"
        
        engine = shared.get('SignalEngine', lambda: None)()
        if engine is None:
            continue
        
        qty = int(holding.get("t_qty") or holding.get("qty") or 1000)
        buy_price = None
        buy_time = None
        pnl = 0.0
        day_trades = 0
        day_completed = 0
        vol_rev_count = 0
        
        # 逐分钟模拟
        for i in range(15, len(df)):
            sub = df.iloc[:i+1].copy()
            if sub.empty or len(sub) < 15:
                continue
            
            # 添加指标
            try:
                add_indicators = shared.get('add_indicators')
                if not add_indicators:
                    continue
                sub = add_indicators(sub)
            except Exception:
                continue
            
            current_time = sub.iloc[-1]["time"]
            if isinstance(current_time, str):
                current_time = pd.to_datetime(current_time)
            if hasattr(current_time, 'to_pydatatime'):
                current_time = current_time.to_pydatetime()
            
            shared['SIM_NOW'] = current_time
            globals()['SIM_NOW'] = current_time
            
            price = float(sub.iloc[-1]["close"]) if "close" in sub.columns else 0.0
            if price <= 0:
                continue
            
            try:
                buy_score, sell_score, sig = engine.evaluate(
                    code, holding.get("name", code), sub, holding
                )
            except Exception as e:
                continue
            
            if sig:
                is_vol_rev = any("5分量能反转" in str(f) for f in (sig.factors or []))
                
                results["signal_log"].append({
                    "date": "2026-07-06", "code": code, "time": str(current_time)[11:19],
                    "action": sig.action, "price": price, "score": sig.score,
                    "is_vol_rev": is_vol_rev,
                    "factors": [str(f) for f in list(sig.factors)[:3]] if sig.factors else [],
                })
                
                if is_vol_rev:
                    vol_rev_count += 1
                    print(f"  [VOL_REV] {current_time.strftime('%H:%M:%S')} {sig.action} @ {price:.2f} score={sig.score}")
                
                if sig.action in ["BUY_LOW", "ADD_POS"]:
                    if buy_price is None:
                        buy_price = price
                        buy_time = current_time.strftime("%H:%M:%S")
                        results["by_code"][code]["buy_signals"] += 1
                        print(f"  [BUY] {buy_time} @ {buy_price:.2f} score={sig.score}")
                
                elif sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                    if buy_price is not None:
                        raw_pnl = (price - buy_price) * qty
                        commission = (buy_price + price) * qty * COMMISSION_RATE
                        net_pnl = raw_pnl - commission
                        pnl += net_pnl
                        day_trades += 1
                        day_completed += 1
                        
                        print(f"  [SELL] {current_time.strftime('%H:%M:%S')} @ {price:.2f} PnL={net_pnl:+.2f}")
                        results["trade_log"].append({
                            "date": "2026-07-06", "code": code, "action": "SELL",
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
            results["daily_pnl"]["2026-07-06"] += pnl
            results["total_trades"] += day_trades
            results["completed_cycles"] += day_completed
            results["by_code"][code]["trades"] += day_trades
            results["by_code"][code]["completed"] += day_completed
        
        results["by_code"][code]["vol_rev_signals"] = vol_rev_count
        print(f"  [SUMMARY] trades={day_trades}, PnL=CNY {pnl:+.2f}, vol_rev={vol_rev_count}")
    
    # 打印报告
    print("\n" + "="*70)
    print("[V1.17 回测报告 - 2026-07-06]")
    print("="*70)
    print(f"\nCompleted T-cycles: {results['completed_cycles']}")
    print(f"Incomplete T-cycles: {results['incomplete_cycles']}")
    print(f"Total trades: {results['total_trades']}")
    
    total_pnl = sum(results['daily_pnl'].values())
    print(f"\nTotal PnL: CNY {total_pnl:.2f}")
    if results['completed_cycles'] > 0:
        print(f"Avg per cycle: CNY {total_pnl/results['completed_cycles']:.2f}")
    
    print(f"\n[By Stock]")
    for code, stats in sorted(results['by_code'].items(), key=lambda x: x[1]['pnl'], reverse=True):
        print(f"  {code}: buy={stats['buy_signals']}, sell={stats['sell_signals']}, vol_rev={stats['vol_rev_signals']}, completed={stats['completed']}, PnL=CNY {stats['pnl']:+.2f}")
    
    # 保存报告
    report_path = os.path.join(BASE_DIR, "backtest_0706_v117_report.json")
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump({
            "summary": {
                "completed_cycles": results['completed_cycles'],
                "incomplete_cycles": results['incomplete_cycles'],
                "total_trades": results['total_trades'],
                "total_pnl": total_pnl,
            },
            "daily_pnl": dict(results['daily_pnl']),
            "by_code": {k: dict(v) for k, v in results['by_code'].items()},
            "trade_log": results['trade_log'],
            "signal_log": results['signal_log'],
        }, f, ensure_ascii=False, indent=2)
    print(f"\nReport saved: {report_path}")
    return results

if __name__ == "__main__":
    run_backtest()
