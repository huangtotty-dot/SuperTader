import sys, os, json
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import os as _os
from datetime import datetime, timedelta, time as dtime
import pandas as pd

_os.environ['http_proxy'] = ''
_os.environ['https_proxy'] = ''

shared = {
    '__name__': '__main__', '__file__': __file__,
    'os': _os, 'sys': sys, 'json': json,
    'datetime': datetime, 'timedelta': timedelta, 'dtime': dtime,
    'Dict': dict, 'List': list, 'Optional': type(None), 'Any': object,
    'np': __import__('numpy'), 'pd': pd,
}

try:
    import akshare as ak
    shared['ak'] = ak
except:
    pass
try:
    import log_enhancer as _le
    shared['_log_enhancer'] = _le
except:
    shared['_log_enhancer'] = None

for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']:
    mod_path = _os.path.join(BASE_DIR, f"{mod_name}.py")
    with open(mod_path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), mod_path, 'exec'), shared)

globals().update(shared)

SNAP_PATH = os.path.join(BASE_DIR, "t_io", "minute_snapshots", "2026", "07", "000988_2026-07-13.json")
with open(SNAP_PATH, 'r', encoding='utf-8') as f:
    snap = json.load(f)

bars = snap.get("bars", [])
df = pd.DataFrame(bars)
df['time'] = pd.to_datetime(df['time'])
df['date'] = df['time'].dt.date
for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors='coerce')
df = df.sort_values('time').reset_index(drop=True)
df = add_indicators(df)

state = {"name": "华工科技", "t_qty": 100, "qty": 100, "type": "stock", "cost": 212.197}
MINUTE_FETCH_STATUS["000988"] = "ok"
MINUTE_FETCH_DETAIL["000988"] = "snapshot"
daily_ctx = snap.get("daily_context", {})
if not isinstance(daily_ctx, dict):
    daily_ctx = _default_daily_context("000988")

# 只测试最关键的几个时间点
# 09:46(冲高), 10:00, 10:30, 11:30, 13:30, 14:00, 14:30, 14:59
# 对应的索引大约是: 16, 30, 60, 120, 150, 180, 210, 240
test_indices = [16, 30, 60, 90, 120, 150, 180, 210, 240]

results = {"long": [], "short": []}

for t_mode_value in ["long", "short"]:
    print(f"\n=== T_MODE = {t_mode_value} ===")
    T_MODE = {"000988": t_mode_value}
    shared['T_MODE'] = T_MODE

    engine = SignalEngine()
    engine.state_reset_date = "2026-07-13"
    engine.buy_count_per_stock["000988"] = 0
    engine.sell_count_per_stock["000988"] = 0

    for idx in test_indices:
        if idx >= len(df):
            continue
        sub_df = df.iloc[:idx+1].copy()
        current_time = sub_df.iloc[-1]["time"]
        shared['SIM_NOW'] = current_time.to_pydatetime() if hasattr(current_time, 'to_pydatetime') else current_time

        try:
            buy_score, sell_score, sig = engine.evaluate("000988", "华工科技", sub_df, state, daily_ctx=daily_ctx)
            t_val = shared['SIM_NOW'].hour * 100 + shared['SIM_NOW'].minute
            if sig:
                print(f"  {shared['SIM_NOW'].strftime('%H:%M')} {sig.action} score={sig.score:.0f} price={sig.price:.2f}")
                results[t_mode_value].append({
                    "time": shared['SIM_NOW'].strftime("%H:%M"),
                    "action": sig.action,
                    "score": sig.score,
                    "price": sig.price,
                    "vwap": float(sub_df.iloc[-1]["vwap"]) if "vwap" in sub_df.columns else sig.price,
                })
                if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                    engine.record_trade_action("000988", sig.action, sig.hold_qty)
                elif sig.action in ["BUY_LOW", "ADD_POS"]:
                    engine.record_trade_action("000988", sig.action, sig.hold_qty)
            else:
                print(f"  {shared['SIM_NOW'].strftime('%H:%M')} buy={buy_score:.0f} sell={sell_score:.0f} NO SIGNAL")
        except Exception as e:
            import traceback
            print(f"  {shared['SIM_NOW'].strftime('%H:%M')} ERROR: {e}")

# 保存结果到文件
output = {"long": results["long"], "short": results["short"]}
out_path = os.path.join(BASE_DIR, "backtest_000988_0713_raw.json")
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)
print(f"\nResults saved to {out_path}")

# 计算盈亏对比
for mode in ["long", "short"]:
    sigs = results[mode]
    if mode == "long":
        pnl = 0.0; bq = []
        for s in sigs:
            if s["action"] in ["BUY_LOW", "ADD_POS"]: bq.append(s["price"])
            elif s["action"] in ["SELL_HIGH", "PANIC_SELL"] and bq:
                pnl += (s["price"] - bq.pop(0)) * 100
    else:
        pnl = 0.0; sq = []
        for s in sigs:
            if s["action"] in ["SELL_HIGH", "PANIC_SELL"]: sq.append(s["price"])
            elif s["action"] in ["BUY_LOW", "ADD_POS"] and sq:
                pnl += (sq.pop(0) - s["price"]) * 100
    print(f"{mode}: {len(sigs)} signals, PnL={pnl:+.2f}")
