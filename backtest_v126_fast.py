# -*- coding: utf-8 -*-
"""
V1.26 快速对比回测 - 关键时间点采样
"""
import sys, os, json, glob

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pandas as pd
from datetime import datetime

class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: pd.DataFrame()
sys.modules['akshare'] = MockAkshare()
sys.modules['ak'] = MockAkshare()

shared = {'__name__': '__main__', '__file__': __file__}
for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']:
    mod_path = os.path.join(BASE_DIR, f"{mod_name}.py")
    if not os.path.exists(mod_path):
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)

globals().update(shared)

code = "000988"
name = "华工科技"

snapshot_files = sorted(glob.glob(os.path.join(BASE_DIR, "t_io/minute_snapshots/2026/07/000988_*.json")))
print(f"[INIT] {len(snapshot_files)} days")

holding = {"name": name, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "cost": 207.205, "pre_close": 149.700}

def run_single_day(date_str, t_mode, enable_hl):
    """回测单日 - 只评估关键时间点"""
    fpath = os.path.join(BASE_DIR, f"t_io/minute_snapshots/2026/07/000988_{date_str}.json")
    if not os.path.exists(fpath):
        return []
    
    with open(fpath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    bars = data.get("bars", []) if isinstance(data, dict) else data
    if not bars or len(bars) < 15:
        return []
    
    df = pd.DataFrame(bars)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df['time'] = pd.to_datetime(df['time'])
    
    if 'add_indicators' in shared:
        df = shared['add_indicators'](df)
    if df is None or df.empty or len(df) < 15:
        return []
    
    daily_ctx = shared.get('_default_daily_context', lambda c: {})(code)
    engine = shared['SignalEngine']()
    engine.state_reset_date = date_str.replace("-", "")
    engine.buy_count_per_stock[code] = 0
    engine.sell_count_per_stock[code] = 0
    engine.post_sell_block_until[code] = None
    
    if not enable_hl:
        engine._check_higher_low_support = lambda c, d, p, v: (False, {})
    
    shared['MINUTE_FETCH_STATUS'][code] = "ok"
    shared['MINUTE_FETCH_DETAIL'][code] = "snapshot"
    
    if date_str in ["2026-07-14", "2026-07-10", "2026-07-06"]:
        engine.morning_alert_state[code] = {
            "level": 2, "rules": [{"name": "test_rule"}], "stats": {},
            "triggered_at": 935, "corrected": False
        }
    
    # 关键时间点: 10:00, 10:30, 11:00, 11:15, 11:30, 13:00, 13:30, 14:00
    key_times = [30, 60, 90, 105, 120, 150, 180, 210]
    # 07-14 加强 11:15 附近
    if date_str == "2026-07-14":
        key_times += [100, 102, 104, 106, 108, 110, 112, 114, 116, 118]
    key_times = [i for i in sorted(set(key_times)) if i < len(df)]
    
    results = []
    for i in key_times:
        sub = df.iloc[:i+1].copy()
        current_time = sub.iloc[-1]["time"]
        if hasattr(current_time, 'to_pydatetime'):
            current_time = current_time.to_pydatetime()
        shared['SIM_NOW'] = current_time
        globals()['SIM_NOW'] = current_time
        
        try:
            buy_score, sell_score, sig = engine.evaluate(code, name, sub, holding, daily_ctx=daily_ctx)
        except Exception as e:
            continue
        
        price = float(sub.iloc[-1]["close"]) if "close" in sub.columns else 0.0
        vwap = float(sub.iloc[-1]["vwap"]) if "vwap" in sub.columns else 0.0
        hl_direct, hl_detail = engine._check_higher_low_support(code, sub, price, vwap)
        
        indicators = sig.indicators if sig else {}
        alert_level = indicators.get("morning_alert_level", 0) if indicators else 0
        alert_downgrade = indicators.get("morning_alert_downgrade_reason", "") if indicators else ""
        
        results.append({
            "time": current_time.strftime('%H:%M'),
            "price": price, "buy_score": buy_score, "sell_score": sell_score,
            "action": sig.action if sig else None, "score": sig.score if sig else 0,
            "hl": hl_direct, "hl_drop": hl_detail.get("drop_from_high", 0)*100 if hl_detail else 0,
            "hl_raise": hl_detail.get("low_raise_pct", 0)*100 if hl_detail else 0,
            "alert_level": alert_level, "alert_downgraded": bool(alert_downgrade),
        })
    
    return results

# ==================== 执行对比 ====================
test_cases = [
    ("long", True, "正T+HL"),
    ("long", False, "正T无HL"),
    ("short", True, "反T+HL"),
    ("short", False, "反T无HL"),
]

all_results = {}
for t_mode, enable_hl, label in test_cases:
    print(f"\n=== {label} ===")
    shared['T_MODE'] = {"mode": t_mode}
    
    day_summary = {}
    for fpath in snapshot_files:
        date_str = os.path.basename(fpath).replace("000988_", "").replace(".json", "")
        day_results = run_single_day(date_str, t_mode, enable_hl)
        if not day_results:
            continue
        
        buys = [r for r in day_results if r["action"] == "buy"]
        sells = [r for r in day_results if r["action"] == "sell"]
        hls = [r for r in day_results if r["hl"]]
        downgrades = [r for r in day_results if r["alert_downgraded"]]
        
        day_summary[date_str] = {
            "records": day_results, "buys": buys, "sells": sells,
            "hls": hls, "downgrades": downgrades
        }
        
        hl_str = ""
        if hls:
            last_hl = hls[-1]
            hl_str = f" | HL@{last_hl['time']} 跌{last_hl['hl_drop']:.1f}% 抬{last_hl['hl_raise']:.2f}%"
        
        print(f"  {date_str}: 买{len(buys)} 卖{len(sells)} HL{len(hls)} 降级{len(downgrades)}{hl_str}")
    
    all_results[label] = day_summary

# ==================== 对比报告 ====================
print("\n" + "="*60)
print("对比报告")
print("="*60)

# 1. 正T: 有HL vs 无HL
print("\n1. 正T模式: 低点抬高影响")
long_dates = set(all_results["正T+HL"].keys()) | set(all_results["正T无HL"].keys())
for date in sorted(long_dates):
    r1 = all_results["正T+HL"].get(date, {})
    r2 = all_results["正T无HL"].get(date, {})
    b1 = len(r1.get("buys", []))
    b2 = len(r2.get("buys", []))
    h1 = len(r1.get("hls", []))
    d1 = len(r1.get("downgrades", []))
    diff = "↑" if b1 > b2 else ("↓" if b1 < b2 else "=")
    print(f"  {date}: 买{b1}vs{b2}{diff} HL{h1} 降级{d1}")

# 2. 反T: 有HL vs 无HL  
print("\n2. 反T模式: 低点抬高影响")
short_dates = set(all_results["反T+HL"].keys()) | set(all_results["反T无HL"].keys())
for date in sorted(short_dates):
    r1 = all_results["反T+HL"].get(date, {})
    r2 = all_results["反T无HL"].get(date, {})
    b1 = len(r1.get("buys", []))
    b2 = len(r2.get("buys", []))
    s1 = len(r1.get("sells", []))
    s2 = len(r2.get("sells", []))
    h1 = len(r1.get("hls", []))
    diff = "↑" if b1 > b2 else ("↓" if b1 < b2 else "=")
    print(f"  {date}: 买{b1}vs{b2}{diff} 卖{s1}vs{s2} HL{h1}")

# 3. 07-14 详细
print("\n3. 07-14 华工科技详细对比")
for label in ["正T+HL", "正T无HL", "反T+HL", "反T无HL"]:
    r = all_results[label].get("2026-07-14", {})
    if not r:
        continue
    print(f"\n  [{label}]")
    for rec in r["records"]:
        if rec["hl"] or rec["action"] or rec["alert_downgraded"]:
            markers = []
            if rec["hl"]: markers.append(f"HL(跌{rec['hl_drop']:.1f}%)")
            if rec["action"]: markers.append(f"{rec['action'].upper()}:{rec['score']}")
            if rec["alert_downgraded"]: markers.append(f"ALERT↓")
            print(f"    {rec['time']} 价{rec['price']:.2f} 买{rec['buy_score']} 卖{rec['sell_score']} {' | '.join(markers)}")

print("\n" + "="*60)
print("V1.26 回测完成")
print("="*60)
