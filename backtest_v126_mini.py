# -*- coding: utf-8 -*-
"""
V1.26 极速回测 - 仅07-14关键时间点 + 仅对比正T/反T差异
"""
import sys, os, json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pandas as pd

class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: pd.DataFrame()
sys.modules['akshare'] = MockAkshare()
sys.modules['ak'] = MockAkshare()

shared = {'__name__': '__main__', '__file__': __file__}
for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']:
    mod_path = os.path.join(BASE_DIR, f"{mod_name}.py")
    if not os.path.exists(mod_path): continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)

globals().update(shared)

code, name = "000988", "华工科技"
holding = {"name": name, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "cost": 207.205, "pre_close": 149.700}

def test_day(date_str, t_mode, enable_hl):
    fpath = os.path.join(BASE_DIR, f"t_io/minute_snapshots/2026/07/000988_{date_str}.json")
    with open(fpath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    bars = data.get("bars", [])
    df = pd.DataFrame(bars)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
    df['time'] = pd.to_datetime(df['time'])
    
    if 'add_indicators' in shared:
        df = shared['add_indicators'](df)
    
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
        engine.morning_alert_state[code] = {"level": 2, "rules": [{"name": "test"}], "stats": {}, "triggered_at": 935, "corrected": False}
    
    key_indices = [30, 60, 90, 100, 105, 108, 110, 112, 114, 116, 118, 120, 150, 180, 210]
    key_indices = [i for i in key_indices if i < len(df)]
    
    records = []
    for i in key_indices:
        sub = df.iloc[:i+1].copy()
        t = sub.iloc[-1]["time"]
        if hasattr(t, 'to_pydatetime'): t = t.to_pydatetime()
        shared['SIM_NOW'] = t
        globals()['SIM_NOW'] = t
        
        buy_score, sell_score, sig = engine.evaluate(code, name, sub, holding, daily_ctx=daily_ctx)
        price = float(sub.iloc[-1]["close"])
        vwap = float(sub.iloc[-1]["vwap"])
        hl, hl_detail = engine._check_higher_low_support(code, sub, price, vwap)
        
        indicators = sig.indicators if sig else {}
        alert_lvl = indicators.get("morning_alert_level", 0) if indicators else 0
        alert_dg = indicators.get("morning_alert_downgrade_reason", "") if indicators else ""
        
        records.append({
            "time": t.strftime('%H:%M'), "price": price,
            "buy": buy_score, "sell": sell_score,
            "action": sig.action if sig else "-", "score": sig.score if sig else 0,
            "hl": hl, "hl_drop": round(hl_detail.get("drop_from_high",0)*100,1) if hl_detail else 0,
            "alert": alert_lvl, "downgraded": bool(alert_dg)
        })
    return records

# ============= 执行测试 =============
print("=" * 70)
print("V1.26 华工科技 07-14 正T vs 反T 对比")
print("=" * 70)

dates = ["2026-07-14", "2026-07-10", "2026-07-06", "2026-07-02"]

for date_str in dates:
    print(f"\n【{date_str}】")
    for t_mode, hl, label in [("long", True, "正T+HL"), ("short", True, "反T+HL"), ("long", False, "正T无HL")]:
        shared['T_MODE'] = {"mode": t_mode}
        try:
            recs = test_day(date_str, t_mode, hl)
        except Exception as e:
            print(f"  {label}: ERROR {e}")
            continue
        
        # 只打印有信号的点
        sig_recs = [r for r in recs if r["action"] != "-" or r["hl"] or r["downgraded"]]
        if sig_recs:
            print(f"  {label}:")
            for r in sig_recs:
                marks = []
                if r["hl"]: marks.append(f"HL(跌{r['hl_drop']}%)")
                if r["action"] != "-": marks.append(f"{r['action'].upper()}:{r['score']}")
                if r["downgraded"]: marks.append("ALERT↓")
                print(f"    {r['time']} 价{r['price']:.2f} 买{r['buy']} 卖{r['sell']} {' | '.join(marks)}")
        else:
            print(f"  {label}: 无信号")

print("\n" + "=" * 70)
print("测试完成")
print("=" * 70)
