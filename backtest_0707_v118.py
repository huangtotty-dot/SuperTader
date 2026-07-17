# -*- coding: utf-8 -*-
"""
V1.18 回测脚本：使用 2026-07-07 tushare 分钟数据验证 V1.18 改进逻辑
复测标的：特变电工、拓维信息、华工科技、江丰电子、科创半导体ETF
优化：预计算全量指标，只回测关键时间点（9:30-15:00每5分钟）
"""
import sys, os, json, math
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import os as _os, sys as _sys, json as _json, time as _time, logging as _logging, traceback as _traceback, importlib.util as _importlib_util
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
import numpy as np, pandas as pd, requests, urllib.request, urllib.error

_os.environ['http_proxy'] = ''
_os.environ['https_proxy'] = ''
_os.environ['HTTP_PROXY'] = ''
_os.environ['HTTPS_PROXY'] = ''
_os.environ['ALL_PROXY'] = ''
_os.environ['all_proxy'] = ''

class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: pd.DataFrame()

ak_mock = MockAkshare()
sys.modules['akshare'] = ak_mock
sys.modules['ak'] = ak_mock

log = _logging.getLogger("backtest_v118")
log.setLevel(_logging.WARNING)
for h in log.handlers[:]:
    log.removeHandler(h)
log.addHandler(_logging.StreamHandler())

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

COMMISSION_RATE = shared.get('PARAMS', {}).get('commission_rate', 0.0015)

HOLDINGS = {
    "588170": {"name": "科创半导体ETF华夏", "cost": 1.320, "qty": 30000, "base": 30000, "t_qty": 30000, "type": "etf", "account": "账户A", "pre_close": 1.192},
    "600089": {"name": "特变电工", "cost": 26.216, "qty": 1200, "base": 1200, "t_qty": 1200, "type": "stock", "account": "账户A", "pre_close": 21.350},
    "000988": {"name": "华工科技", "cost": 207.205, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "pre_close": 149.700},
    "002261": {"name": "拓维信息", "cost": 47.325, "qty": 300, "base": 300, "t_qty": 300, "type": "stock", "account": "账户A", "pre_close": 30.100},
    "300666": {"name": "江丰电子", "cost": 403.078, "qty": 100, "base": 100, "t_qty": 100, "type": "stock", "account": "账户B", "pre_close": 326.860},
}

def get_tushare_minute(code, date="2026-07-07"):
    import tushare as ts
    pro = ts.pro_api('9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def')
    if code.startswith(("5", "6", "9")):
        ts_code = f"{code}.SH"
    else:
        ts_code = f"{code}.SZ"
    start_date = f"{date} 09:00:00"
    end_date = f"{date} 19:00:00"
    try:
        df = pro.stk_mins(ts_code=ts_code, freq='1min', start_date=start_date, end_date=end_date)
    except Exception as e:
        log.warning(f"⚠️  tushare 获取 {code} 失败: {e}")
        return None
    if df is None or df.empty:
        return None
    df = df.sort_values('trade_time').reset_index(drop=True)
    df['time'] = pd.to_datetime(df['trade_time'])
    df['date'] = df['time'].dt.strftime('%Y-%m-%d')
    df['volume'] = df['vol']
    for col in ['open', 'close', 'high', 'low', 'volume', 'amount']:
        df[col] = pd.to_numeric(df[col])
    return df[['time', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount']]

def precompute_indicators(df):
    """预计算所有指标，避免每次 evaluate 重复计算"""
    add_indicators = shared.get('add_indicators')
    if not add_indicators:
        return None
    return add_indicators(df)

def run_backtest():
    results = {
        "total_trades": 0, "completed_cycles": 0, "incomplete_cycles": 0,
        "daily_pnl": defaultdict(float),
        "by_code": defaultdict(lambda: {
            "trades": 0, "completed": 0, "incomplete": 0,
            "pnl": 0.0, "buy_signals": 0, "sell_signals": 0, "vol_rev_signals": 0,
        }),
        "trade_log": [], "signal_log": [], "feishu_commands": [],
    }
    
    all_data = {}
    for code, holding in HOLDINGS.items():
        print(f"[FETCH] 获取 {code} {holding['name']} ...")
        df = get_tushare_minute(code)
        if df is None or df.empty:
            print(f"  [SKIP] 无数据")
            continue
        print(f"  [OK] {len(df)} 条记录")
        all_data[code] = df
    
    for code, holding in HOLDINGS.items():
        if code not in all_data:
            continue
        print(f"\n{'='*60}")
        print(f"回测 {code} {holding['name']}")
        print(f"{'='*60}")
        
        df = all_data[code]
        shared['MINUTE_FETCH_STATUS'][code] = "ok"
        
        # 预计算全量指标
        full_df = precompute_indicators(df)
        if full_df is None or len(full_df) < 15:
            print(f"  [SKIP] 指标计算失败")
            continue
        
        engine = shared.get('SignalEngine', lambda: None)()
        if engine is None:
            continue
        
        qty = int(holding.get("t_qty") or holding.get("qty") or 1000)
        buy_price = None
        pnl = 0.0
        day_trades = 0
        day_completed = 0
        vol_rev_count = 0
        
        # 每5分钟回测一次（关键时间点），同时覆盖 9:30-15:00
        eval_indices = list(range(15, len(full_df), 5))  # 每5分钟
        # 确保包含关键时间点
        for special_idx in [30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180, 195, 210, 225, 240]:
            if special_idx < len(full_df) and special_idx not in eval_indices:
                eval_indices.append(special_idx)
        eval_indices = sorted(set(eval_indices))
        
        for i in eval_indices:
            sub = full_df.iloc[:i+1].copy()
            current_time = sub.iloc[-1]["time"]
            if isinstance(current_time, str):
                current_time = pd.to_datetime(current_time)
            if hasattr(current_time, 'to_pydatetime'):
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
                    "date": "2026-07-07", "code": code, "time": str(current_time)[11:19],
                    "action": sig.action, "price": price, "score": sig.score,
                    "is_vol_rev": is_vol_rev,
                    "factors": [str(f) for f in list(sig.factors)[:3]] if sig.factors else [],
                })
                
                t = current_time.time()
                if sig.action in ["BUY_LOW", "ADD_POS"]:
                    notify_threshold = 68
                else:
                    if t >= dtime(10, 0):
                        notify_threshold = 65
                    else:
                        notify_threshold = 75
                
                is_feishu = sig.score >= notify_threshold
                
                if is_vol_rev:
                    vol_rev_count += 1
                    print(f"  [VOL_REV] {current_time.strftime('%H:%M:%S')} {sig.action} @ {price:.2f} score={sig.score}")
                
                if sig.action in ["BUY_LOW", "ADD_POS"]:
                    if buy_price is None:
                        buy_price = price
                        results["by_code"][code]["buy_signals"] += 1
                        print(f"  [BUY] {current_time.strftime('%H:%M:%S')} @ {buy_price:.2f} score={sig.score}")
                        if is_feishu:
                            results["feishu_commands"].append({
                                "date": "2026-07-07", "code": code, "name": holding["name"],
                                "time": current_time.strftime("%H:%M:%S"), "action": sig.action,
                                "price": price, "score": sig.score, "qty": qty, "type": "飞书通知"
                            })
                
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
                            "date": "2026-07-07", "code": code, "action": "SELL",
                            "time": current_time.strftime("%H:%M:%S"), "price": price,
                            "score": sig.score, "buy_price": buy_price,
                            "pnl": net_pnl, "qty": qty,
                        })
                        results["by_code"][code]["sell_signals"] += 1
                        results["by_code"][code]["pnl"] += net_pnl
                        buy_price = None
                    else:
                        if is_feishu:
                            results["feishu_commands"].append({
                                "date": "2026-07-07", "code": code, "name": holding["name"],
                                "time": current_time.strftime("%H:%M:%S"), "action": sig.action,
                                "price": price, "score": sig.score, "qty": qty,
                                "type": "飞书通知（底仓卖出）"
                            })
                    
                    if is_feishu and buy_price is None:
                        results["feishu_commands"].append({
                            "date": "2026-07-07", "code": code, "name": holding["name"],
                            "time": current_time.strftime("%H:%M:%S"), "action": sig.action,
                            "price": price, "score": sig.score, "qty": qty,
                            "type": "飞书通知"
                        })
        
        if buy_price is not None:
            results["incomplete_cycles"] += 1
            results["by_code"][code]["incomplete"] += 1
        
        if day_trades > 0:
            results["daily_pnl"]["2026-07-07"] += pnl
            results["total_trades"] += day_trades
            results["completed_cycles"] += day_completed
            results["by_code"][code]["trades"] += day_trades
            results["by_code"][code]["completed"] += day_completed
        
        results["by_code"][code]["vol_rev_signals"] = vol_rev_count
        print(f"  [SUMMARY] trades={day_trades}, PnL=CNY {pnl:+.2f}, vol_rev={vol_rev_count}")
    
    print("\n" + "="*70)
    print("[V1.18 回测报告 - 2026-07-07]")
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
    
    print(f"\n{'='*70}")
    print("[飞书通知命令汇总]")
    print("="*70)
    for cmd in sorted(results['feishu_commands'], key=lambda x: x['time']):
        action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(cmd['action'], cmd['action'])
        print(f"  {cmd['time']} {cmd['name']}({cmd['code']}) {action_cn} @ {cmd['price']:.2f} 得分:{cmd['score']} {cmd['type']}")
    print(f"\n飞书通知总数: {len(results['feishu_commands'])}")
    
    report_path = os.path.join(BASE_DIR, "backtest_0707_v118_report.json")
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
            "feishu_commands": results['feishu_commands'],
        }, f, ensure_ascii=False, indent=2)
    print(f"\nReport saved: {report_path}")
    return results

if __name__ == "__main__":
    run_backtest()
