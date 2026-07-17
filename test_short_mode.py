# -*- coding: utf-8 -*-
"""
V1.26 反T模式验证脚本
测试标的：华工科技 000988 2026-07-13
对比正T(long) vs 反T(short) 的信号差异
"""
import sys, os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 预加载共享命名空间（与 main.py 一致）
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

try:
    import akshare as ak
except Exception:
    ak = None

shared = {
    '__name__': '__main__',
    '__file__': __file__,
    'os': _os,
    'sys': _sys,
    'json': _json,
    'time': _time,
    'logging': _logging,
    'traceback': _traceback,
    'importlib': _importlib_util,
    'importlib.util': _importlib_util,
    'dataclass': dataclass,
    'field': field,
    'datetime': datetime,
    'timedelta': timedelta,
    'dtime': dtime,
    'Dict': Dict,
    'List': List,
    'Optional': Optional,
    'Any': Any,
    'np': np,
    'pd': pd,
    'requests': requests,
    'urllib': urllib,
    'urllib.request': urllib.request,
    'urllib.error': urllib.error,
}
if ak:
    shared['akshare'] = ak
    shared['ak'] = ak

try:
    import log_enhancer as _log_enhancer
    shared['_log_enhancer'] = _log_enhancer
except Exception:
    shared['_log_enhancer'] = None

# 加载模块
module_order = ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']
for mod_name in module_order:
    mod_path = _os.path.join(BASE_DIR, f"{mod_name}.py")
    if not _os.path.exists(mod_path):
        print(f"[WARN] 模块不存在: {mod_path}")
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)
    print(f"[OK] 模块已加载: {mod_name}.py")

globals().update(shared)

# 导入 tushare
import tushare as ts
TS_TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
ts.set_token(TS_TOKEN)
pro = ts.pro_api()

TEST_CODE = "000988"
TEST_NAME = "华工科技"
TEST_DATE = "2026-07-13"

def fetch_and_test(t_mode_value="long"):
    """获取数据并运行回测"""
    ts_code = f"{TEST_CODE}.SZ"
    
    try:
        df = pro.stk_mins(ts_code=ts_code, freq='1min',
                          start_date=f"{TEST_DATE} 09:00:00",
                          end_date=f"{TEST_DATE} 19:00:00")
        if df is None or df.empty:
            print(f"[ERROR] {TEST_CODE} 无分钟数据")
            return []
    except Exception as e:
        print(f"[ERROR] {TEST_CODE} 获取失败: {e}")
        return []
    
    df = df.rename(columns={'trade_time': 'time', 'vol': 'volume', 'amount': 'amount'})
    df['time'] = pd.to_datetime(df['time'])
    df['date'] = df['time'].dt.date
    df = df.sort_values('time').reset_index(drop=True)
    for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    
    df = add_indicators(df)
    
    state = {
        "name": TEST_NAME,
        "t_qty": 100,
        "qty": 100,
        "type": "stock",
        "cost": 212.197,
    }
    
    engine = SignalEngine()
    engine.state_reset_date = TEST_DATE
    engine.buy_count_per_stock[TEST_CODE] = 0
    engine.sell_count_per_stock[TEST_CODE] = 0
    engine.post_sell_block_until[TEST_CODE] = None
    
    # 设置 T_MODE
    global T_MODE
    T_MODE = {TEST_CODE: t_mode_value}
    shared['T_MODE'] = T_MODE
    
    # 设置分钟状态
    MINUTE_FETCH_STATUS[TEST_CODE] = "ok"
    MINUTE_FETCH_DETAIL[TEST_CODE] = "tushare"
    
    results = []
    
    for i in range(25, len(df) + 1):
        sub_df = df.iloc[:i].copy()
        if len(sub_df) < 25:
            continue
        
        current_time = sub_df.iloc[-1]["time"]
        if hasattr(current_time, 'to_pydatetime'):
            SIM_NOW = current_time.to_pydatetime()
        else:
            SIM_NOW = current_time
        
        t_val = SIM_NOW.hour * 100 + SIM_NOW.minute
        
        daily_ctx = _default_daily_context(TEST_CODE)
        
        try:
            buy_score, sell_score, sig = engine.evaluate(
                TEST_CODE, TEST_NAME, sub_df, state, daily_ctx=daily_ctx
            )
        except Exception as e:
            print(f"[WARN] {TEST_CODE} {SIM_NOW.strftime('%H:%M')} evaluate 失败: {e}")
            continue
        
        if sig and sig.action in ["BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"]:
            notify_threshold = 68 if sig.action in ["BUY_LOW", "ADD_POS"] else (65 if t_val >= 1000 else 75)
            
            result = {
                "time": SIM_NOW.strftime("%H:%M:%S"),
                "action": sig.action,
                "score": sig.score,
                "price": sig.price,
                "notify": sig.score >= notify_threshold,
                "vwap": float(sub_df.iloc[-1]["vwap"]) if "vwap" in sub_df.columns else sig.price,
                "reasons": sig.reasons[:5],
            }
            results.append(result)
            
            if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                engine.record_trade_action(TEST_CODE, sig.action, sig.hold_qty)
            elif sig.action in ["BUY_LOW", "ADD_POS"]:
                engine.record_trade_action(TEST_CODE, sig.action, sig.hold_qty)
    
    return results


def print_results(mode, results):
    print(f"\n{'='*60}")
    print(f"【{mode}模式】{TEST_NAME} ({TEST_CODE}) {TEST_DATE}")
    print(f"{'='*60}")
    
    notify_results = [r for r in results if r["notify"]]
    print(f"总信号数: {len(results)} | 飞书通知信号数: {len(notify_results)}")
    
    if notify_results:
        print(f"\n【飞书通知信号】")
        for r in notify_results:
            action_cn = {"BUY_LOW": "🟢 低吸", "ADD_POS": "🟢 加仓", "SELL_HIGH": "🔴 高抛", "PANIC_SELL": "🔴 恐慌卖出"}.get(r["action"], r["action"])
            print(f"  {r['time']} {action_cn} 得分{r['score']:.0f} 价格{r['price']:.2f} VWAP{r['vwap']:.2f}")
            print(f"    原因: {', '.join(r['reasons'])}")
    
    # 统计买卖次数
    buys = [r for r in results if r["action"] in ["BUY_LOW", "ADD_POS"]]
    sells = [r for r in results if r["action"] in ["SELL_HIGH", "PANIC_SELL"]]
    print(f"\n【统计】买入 {len(buys)} 次 | 卖出 {len(sells)} 次")
    
    if sells and buys:
        # 简单盈亏估算（假设每笔交易100股）
        sell_prices = [r["price"] for r in sells]
        buy_prices = [r["price"] for r in buys]
        avg_sell = sum(sell_prices) / len(sell_prices) if sell_prices else 0
        avg_buy = sum(buy_prices) / len(buy_prices) if buy_prices else 0
        print(f"平均卖出价: {avg_sell:.2f} | 平均买入价: {avg_buy:.2f}")
        if len(sells) == len(buys):
            profit = (avg_sell - avg_buy) * 100
            print(f"估算盈亏: {profit:+.2f} 元（假设每笔100股）")
    
    print(f"{'='*60}\n")


if __name__ == "__main__":
    print("="*60)
    print("V1.26 反T模式验证脚本")
    print("="*60)
    
    # 测试正T模式
    results_long = fetch_and_test("long")
    print_results("正T(long)", results_long)
    
    # 测试反T模式
    results_short = fetch_and_test("short")
    print_results("反T(short)", results_short)
