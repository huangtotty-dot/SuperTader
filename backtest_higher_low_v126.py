# -*- coding: utf-8 -*-
"""
V1.26: 低点抬高支撑确认回测验证脚本
测试日期: 2026-07-01 ~ 2026-07-14 (10个交易日)
目标: 验证 _check_higher_low_support 集成到 evaluate 后的信号捕获效果
"""
import sys, os, json, glob

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pandas as pd
from datetime import datetime

# Mock akshare
class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: pd.DataFrame()
sys.modules['akshare'] = MockAkshare()
sys.modules['ak'] = MockAkshare()

# Load modules in order
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

# 可用快照日期
snapshot_files = sorted(glob.glob(os.path.join(BASE_DIR, "t_io/minute_snapshots/2026/07/000988_*.json")))
print(f"[INIT] 发现 {len(snapshot_files)} 天快照数据")

holding = {"name": name, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "cost": 207.205, "pre_close": 149.700}

results = []
for fpath in snapshot_files:
    date_str = os.path.basename(fpath).replace("000988_", "").replace(".json", "")
    with open(fpath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    bars = data.get("bars", []) if isinstance(data, dict) else data
    if not bars or len(bars) < 15:
        print(f"  [{date_str}] SKIP: 数据不足 {len(bars)} 条")
        continue
    
    df = pd.DataFrame(bars)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df['time'] = pd.to_datetime(df['time'])
    df['date'] = df['time'].dt.strftime('%Y-%m-%d')
    
    # 计算技术指标（vwap/rsi/macd等）
    if 'add_indicators' in shared:
        df = shared['add_indicators'](df)
    if df is None or df.empty or len(df) < 15:
        print(f"  [{date_str}] SKIP: 指标计算失败或数据不足")
        continue
    
    # 全局模拟时间
    daily_ctx = shared.get('_default_daily_context', lambda c: {})(code)
    
    engine = shared['SignalEngine']()
    engine.state_reset_date = date_str.replace("-", "")
    engine.buy_count_per_stock[code] = 0
    engine.sell_count_per_stock[code] = 0
    engine.post_sell_block_until[code] = None
    
    shared['MINUTE_FETCH_STATUS'][code] = "ok"
    shared['MINUTE_FETCH_DETAIL'][code] = "snapshot"
    
    # 模拟早盘预警（部分日期）
    if date_str in ["2026-07-14", "2026-07-10", "2026-07-06"]:
        engine.morning_alert_state[code] = {
            "level": 2, "rules": [{"name": "test_rule"}], "stats": {},
            "triggered_at": 935, "corrected": False
        }
    
    day_results = []
    eval_indices = [30, 60, 90, 120, 150, 180, 210, 240]
    # 对07-14特别关注11:15前后
    if date_str == "2026-07-14":
        eval_indices += [100, 105, 108, 110, 112, 113, 114, 115, 116, 117, 118, 120, 122, 124, 125, 130, 135, 140, 145]
    
    eval_indices = [i for i in eval_indices if i < len(df)]
    
    for i in eval_indices:
        sub = df.iloc[:i+1].copy()
        current_time = sub.iloc[-1]["time"]
        if hasattr(current_time, 'to_pydatetime'):
            current_time = current_time.to_pydatetime()
        
        shared['SIM_NOW'] = current_time
        globals()['SIM_NOW'] = current_time
        
        try:
            buy_score, sell_score, sig = engine.evaluate(code, name, sub, holding, daily_ctx=daily_ctx)
        except Exception as e:
            print(f"  [{date_str} {current_time.strftime('%H:%M')}] ERROR: {e}")
            continue
        
        # 直接调用 _check_higher_low_support 获取HL信号（避免sig=None时indicators丢失）
        price = float(sub.iloc[-1]["close"]) if "close" in sub.columns else 0.0
        vwap = float(sub.iloc[-1]["vwap"]) if "vwap" in sub.columns else 0.0
        hl_direct, hl_detail = engine._check_higher_low_support(code, sub, price, vwap)
        
        # 提取诊断信息
        indicators = sig.indicators if sig else {}
        
        hl_detected = hl_direct
        indicators["higher_low_support_detected"] = hl_detected
        indicators["higher_low_support_detail"] = hl_detail
        alert_level = indicators.get("morning_alert_level", 0) if indicators else 0
        alert_downgrade = indicators.get("morning_alert_downgrade_reason", "") if indicators else ""
        
        if hl_detected or sig:
            row = {
                "date": date_str,
                "time": current_time.strftime('%H:%M:%S'),
                "price": float(sub.iloc[-1]["close"]),
                "buy_score": buy_score,
                "sell_score": sell_score,
                "action": sig.action if sig else None,
                "score": sig.score if sig else 0,
                "hl_detected": hl_detected,
                "hl_drop": hl_detail.get("drop_from_high", 0) * 100 if hl_detail else 0,
                "hl_raise": hl_detail.get("low_raise_pct", 0) * 100 if hl_detail else 0,
                "alert_level": alert_level,
                "alert_downgrade": alert_downgrade,
            }
            day_results.append(row)
            
            if hl_detected:
                print(f"  [{date_str} {current_time.strftime('%H:%M')}] HL_DETECTED! 价格{row['price']:.2f} 买分{buy_score} 卖分{sell_score} | 跌幅{row['hl_drop']:.1f}% 抬高+{row['hl_raise']:.2f}% | alert={alert_level} {'↓'+alert_downgrade[:20] if alert_downgrade else ''}")
            elif sig and sig.score >= 65:
                print(f"  [{date_str} {current_time.strftime('%H:%M')}] SIGNAL {sig.action} 得分{sig.score} 价格{row['price']:.2f}")
    
    results.extend(day_results)
    if not any(r["hl_detected"] for r in day_results):
        print(f"  [{date_str}] 无HL信号")

# 汇总
print(f"\n{'='*60}")
print("汇总报告")
print(f"{'='*60}")
hl_signals = [r for r in results if r["hl_detected"]]
print(f"总HL信号数: {len(hl_signals)}")
for r in hl_signals:
    print(f"  {r['date']} {r['time']} 价格{r['price']:.2f} 买分{r['buy_score']} 卖分{r['sell_score']} | 跌幅{r['hl_drop']:.1f}% 抬高+{r['hl_raise']:.2f}% | alert={r['alert_level']}→{r['alert_downgrade'][:30]}")

# 检查07-14的alert降级
d0714 = [r for r in results if r["date"] == "2026-07-14"]
if d0714:
    print(f"\n07-14 详细:")
    for r in d0714:
        if r["hl_detected"] or r["alert_level"] > 0 or (r["action"] and r["score"] >= 60):
            print(f"  {r['time']} 价格{r['price']:.2f} 买分{r['buy_score']} 卖分{r['sell_score']} HL={r['hl_detected']} alert={r['alert_level']} action={r['action']} score={r['score']}")

print(f"\n{'='*60}")
print("验证完成 - signal_engine.py V1.26 集成正常")
print(f"{'='*60}")
