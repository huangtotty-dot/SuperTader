# -*- coding: utf-8 -*-
import sys, os, json
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path: sys.path.insert(0, BASE_DIR)
import pandas as pd
class MockAkshare:
    def __getattr__(self, name):
        return lambda *a, **k: pd.DataFrame()
sys.modules['akshare'] = MockAkshare()
sys.modules['ak'] = MockAkshare()
shared = {'__name__': '__main__', '__file__': __file__}
for mod_name in ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']:
    mod_path = os.path.join(BASE_DIR, f"{mod_name}.py")
    if not os.path.exists(mod_path): continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        exec(compile(f.read(), mod_path, 'exec'), shared)
globals().update(shared)

code, name = "000988", "华工科技"
holding = {"name": name, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "cost": 207.205, "pre_close": 149.700}

def run(date_str, t_mode):
    fpath = os.path.join(BASE_DIR, f"t_io/minute_snapshots/2026/07/000988_{date_str}.json")
    with open(fpath, 'r', encoding='utf-8') as f: data = json.load(f)
    df = pd.DataFrame(data.get("bars", []))
    for col in ['open', 'high', 'low', 'close', 'volume']:
        if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
    df['time'] = pd.to_datetime(df['time'])
    if 'add_indicators' in shared: df = shared['add_indicators'](df)
    
    shared['T_MODE'] = {"mode": t_mode}
    daily_ctx = shared.get('_default_daily_context', lambda c: {})(code)
    engine = shared['SignalEngine']()
    engine.state_reset_date = date_str.replace("-", "")
    engine.buy_count_per_stock[code] = 0
    engine.sell_count_per_stock[code] = 0
    engine.post_sell_block_until[code] = None
    shared['MINUTE_FETCH_STATUS'][code] = "ok"
    shared['MINUTE_FETCH_DETAIL'][code] = "snapshot"
    engine.morning_alert_state[code] = {"level": 2, "rules": [{"name": "test"}], "stats": {}, "triggered_at": 935, "corrected": False}
    
    indices = [60, 90, 100, 105, 108, 110, 112, 114, 116, 118, 120, 122, 124, 126, 130, 150]
    indices = [i for i in indices if i < len(df)]
    
    out = []
    for i in indices:
        sub = df.iloc[:i+1].copy()
        t = sub.iloc[-1]["time"]
        if hasattr(t, 'to_pydatetime'): t = t.to_pydatetime()
        shared['SIM_NOW'] = t
        globals()['SIM_NOW'] = t
        
        buy_score, sell_score, sig = engine.evaluate(code, name, sub, holding, daily_ctx=daily_ctx)
        price = float(sub.iloc[-1]["close"])
        vwap = float(sub.iloc[-1]["vwap"])
        hl, hl_detail = engine._check_higher_low_support(code, sub, price, vwap)
        
        ind = sig.indicators if sig else {}
        alert = ind.get("morning_alert_level", 0) if ind else 0
        alert_dg = ind.get("morning_alert_downgrade_reason", "") if ind else ""
        
        out.append({
            "time": t.strftime('%H:%M'), "price": price,
            "buy": buy_score, "sell": sell_score,
            "action": sig.action if sig else "-", "score": sig.score if sig else 0,
            "hl": hl, "hl_drop": round(hl_detail.get("drop_from_high",0)*100,1) if hl_detail else 0,
            "alert": alert, "dg": bool(alert_dg)
        })
    return out

print("=" * 70)
print("07-14 华工科技 正T vs 反T 详细对比")
print("=" * 70)

for t_mode, label in [("long", "正T模式"), ("short", "反T模式")]:
    print(f"\n【{label}】")
    recs = run("2026-07-14", t_mode)
    for r in recs:
        if r["action"] != "-" or r["hl"] or r["dg"]:
            marks = []
            if r["hl"]: marks.append(f"HL(跌{r['hl_drop']}%)")
            if r["action"] != "-": marks.append(f"{r['action'].upper()}:{r['score']}")
            if r["dg"]: marks.append(f"ALERT↓({r['alert']}→0)")
            print(f"  {r['time']} 价{r['price']:.2f} 买{r['buy']} 卖{r['sell']} {' | '.join(marks)}")

print("\n" + "=" * 70)
print("完成")
print("=" * 70)
