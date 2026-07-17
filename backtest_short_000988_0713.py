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

for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine']:
    mod_path = _os.path.join(BASE_DIR, f"{mod_name}.py")
    with open(mod_path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), mod_path, 'exec'), shared)

globals().update(shared)

SNAP_PATH = os.path.join(BASE_DIR, "t_io", "minute_snapshots", "2026", "07", "000988_2026-07-13.json")
TEST_CODE = "000988"
TEST_NAME = "华工科技"
QTY = 100

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

state = {"name": TEST_NAME, "t_qty": QTY, "qty": QTY, "type": "stock", "cost": 212.197}
MINUTE_FETCH_STATUS[TEST_CODE] = "ok"
MINUTE_FETCH_DETAIL[TEST_CODE] = "snapshot"
daily_ctx = snap.get("daily_context", {})
if not isinstance(daily_ctx, dict):
    daily_ctx = _default_daily_context(TEST_CODE)

def run_mode(t_mode_value):
    global T_MODE
    T_MODE = {TEST_CODE: t_mode_value}
    shared['T_MODE'] = T_MODE

    engine = SignalEngine()
    engine.state_reset_date = "2026-07-13"
    engine.buy_count_per_stock[TEST_CODE] = 0
    engine.sell_count_per_stock[TEST_CODE] = 0
    engine.post_sell_block_until[TEST_CODE] = None

    signals = []
    # 采样: 每15分钟 + 收盘
    for i in list(range(30, len(df), 15)) + [len(df)-1]:
        sub_df = df.iloc[:i+1].copy()
        ct = sub_df.iloc[-1]["time"]
        shared['SIM_NOW'] = ct.to_pydatetime() if hasattr(ct, 'to_pydatetime') else ct
        t_val = shared['SIM_NOW'].hour * 100 + shared['SIM_NOW'].minute
        try:
            buy_score, sell_score, sig = engine.evaluate(TEST_CODE, TEST_NAME, sub_df, state, daily_ctx=daily_ctx)
            if sig and sig.action in ["BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"]:
                signals.append({
                    "time": shared['SIM_NOW'].strftime("%H:%M"),
                    "t_val": t_val,
                    "action": sig.action,
                    "score": sig.score,
                    "price": sig.price,
                    "vwap": float(sub_df.iloc[-1]["vwap"]) if "vwap" in sub_df.columns else sig.price,
                })
                if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                    engine.record_trade_action(TEST_CODE, sig.action, sig.hold_qty)
                elif sig.action in ["BUY_LOW", "ADD_POS"]:
                    engine.record_trade_action(TEST_CODE, sig.action, sig.hold_qty)
        except Exception:
            pass
    return signals

def pnl(signals, mode):
    if mode == "long":
        pnl_val = 0.0; matched = 0; bq = []
        for s in signals:
            if s["action"] in ["BUY_LOW", "ADD_POS"]: bq.append(s["price"])
            elif s["action"] in ["SELL_HIGH", "PANIC_SELL"] and bq:
                pnl_val += (s["price"] - bq.pop(0)) * QTY; matched += 1
        return pnl_val, matched, len([s for s in signals if s["action"] in ["BUY_LOW", "ADD_POS"]]), len([s for s in signals if s["action"] in ["SELL_HIGH", "PANIC_SELL"]]), len(bq)
    else:
        pnl_val = 0.0; matched = 0; sq = []
        for s in signals:
            if s["action"] in ["SELL_HIGH", "PANIC_SELL"]: sq.append(s["price"])
            elif s["action"] in ["BUY_LOW", "ADD_POS"] and sq:
                pnl_val += (sq.pop(0) - s["price"]) * QTY; matched += 1
        return pnl_val, matched, len([s for s in signals if s["action"] in ["BUY_LOW", "ADD_POS"]]), len([s for s in signals if s["action"] in ["SELL_HIGH", "PANIC_SELL"]]), len(sq)

print("="*60)
print("HuaGong 07-13 Long vs Short Backtest")
print("="*60)

print("\n[LONG]")
sig_long = run_mode("long")
pnl_l, m_l, b_l, s_l, u_l = pnl(sig_long, "long")
print(f"  buys={b_l} sells={s_l} matched={m_l} unmatched_buys={u_l} pnl={pnl_l:+.2f}")
for s in sig_long:
    a = {"BUY_LOW":"B","ADD_POS":"B+","SELL_HIGH":"S","PANIC_SELL":"S!"}[s["action"]]
    print(f"    {s['time']} {a} score={s['score']:.0f} price={s['price']:.2f}")

print("\n[SHORT]")
sig_short = run_mode("short")
pnl_s, m_s, b_s, s_s, u_s = pnl(sig_short, "short")
print(f"  sells={s_s} buys={b_s} matched={m_s} unmatched_sells={u_s} pnl={pnl_s:+.2f}")
for s in sig_short:
    a = {"BUY_LOW":"B","ADD_POS":"B+","SELL_HIGH":"S","PANIC_SELL":"S!"}[s["action"]]
    print(f"    {s['time']} {a} score={s['score']:.0f} price={s['price']:.2f}")

print(f"\nLong={pnl_l:+.2f} Short={pnl_s:+.2f} Diff={pnl_s-pnl_l:+.2f}")
