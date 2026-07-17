# -*- coding: utf-8 -*-
"""
V1.26 全量回测对比验证
对比: 正T模式(long) vs 反T模式(short) | 含低点抬高 vs 不含
标的: 华工科技 000988 (10个交易日 2026-07-01~07-14)
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
print(f"[INIT] 发现 {len(snapshot_files)} 天快照数据")

holding = {"name": name, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "cost": 207.205, "pre_close": 149.700}

def run_backtest(t_mode, enable_hl=True):
    """运行回测: t_mode='long'/'short', enable_hl=True/False"""
    
    # 设置T模式
    shared['T_MODE'] = {"mode": t_mode}
    
    results = []
    signal_count = {"buy": 0, "sell": 0, "hl_detected": 0, "alert_downgraded": 0}
    
    for fpath in snapshot_files:
        date_str = os.path.basename(fpath).replace("000988_", "").replace(".json", "")
        with open(fpath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        bars = data.get("bars", []) if isinstance(data, dict) else data
        if not bars or len(bars) < 15:
            continue
        
        df = pd.DataFrame(bars)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        df['time'] = pd.to_datetime(df['time'])
        
        if 'add_indicators' in shared:
            df = shared['add_indicators'](df)
        if df is None or df.empty or len(df) < 15:
            continue
        
        daily_ctx = shared.get('_default_daily_context', lambda c: {})(code)
        
        engine = shared['SignalEngine']()
        engine.state_reset_date = date_str.replace("-", "")
        engine.buy_count_per_stock[code] = 0
        engine.sell_count_per_stock[code] = 0
        engine.post_sell_block_until[code] = None
        
        # 强制启用/禁用HL检测
        if not enable_hl:
            # 通过monkey patch禁用
            engine._check_higher_low_support = lambda c, d, p, v: (False, {})
        
        shared['MINUTE_FETCH_STATUS'][code] = "ok"
        shared['MINUTE_FETCH_DETAIL'][code] = "snapshot"
        
        # 模拟早盘预警
        if date_str in ["2026-07-14", "2026-07-10", "2026-07-06"]:
            engine.morning_alert_state[code] = {
                "level": 2, "rules": [{"name": "test_rule"}], "stats": {},
                "triggered_at": 935, "corrected": False
            }
        
        day_signals = []
        eval_indices = list(range(30, min(241, len(df)), 5))  # 每5分钟采样
        
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
                print(f"  [{date_str}] ERROR: {e}")
                continue
            
            # 获取HL状态
            price = float(sub.iloc[-1]["close"]) if "close" in sub.columns else 0.0
            vwap = float(sub.iloc[-1]["vwap"]) if "vwap" in sub.columns else 0.0
            hl_direct, hl_detail = engine._check_higher_low_support(code, sub, price, vwap)
            
            indicators = sig.indicators if sig else {}
            alert_level = indicators.get("morning_alert_level", 0) if indicators else 0
            alert_downgrade = indicators.get("morning_alert_downgrade_reason", "") if indicators else ""
            
            record = {
                "date": date_str, "time": current_time.strftime('%H:%M'),
                "price": price, "buy_score": buy_score, "sell_score": sell_score,
                "action": sig.action if sig else None, "score": sig.score if sig else 0,
                "hl_detected": hl_direct, "alert_level": alert_level,
                "alert_downgraded": bool(alert_downgrade),
            }
            
            if sig and sig.score >= 60:
                day_signals.append(record)
                signal_count[sig.action] = signal_count.get(sig.action, 0) + 1
                if hl_direct:
                    signal_count["hl_detected"] += 1
                if alert_downgrade:
                    signal_count["alert_downgraded"] += 1
            elif hl_direct:
                day_signals.append(record)
                signal_count["hl_detected"] += 1
        
        results.extend(day_signals)
    
    return results, signal_count

# ==================== 执行4组对比测试 ====================
print("\n" + "="*70)
print("V1.26 全量回测对比: 正T vs 反T × 含HL vs 不含HL")
print("="*70)

test_cases = [
    ("long", True, "正T模式 + 低点抬高"),
    ("long", False, "正T模式 + 无低点抬高"),
    ("short", True, "反T模式 + 低点抬高"),
    ("short", False, "反T模式 + 无低点抬高"),
]

all_reports = []

for t_mode, enable_hl, label in test_cases:
    print(f"\n【{label}】")
    print("-" * 50)
    
    results, counts = run_backtest(t_mode, enable_hl)
    
    # 统计
    buy_signals = [r for r in results if r["action"] == "buy"]
    sell_signals = [r for r in results if r["action"] == "sell"]
    hl_only = [r for r in results if r["hl_detected"] and not r["action"]]
    
    print(f"  买入信号: {len(buy_signals)} 次")
    print(f"  卖出信号: {len(sell_signals)} 次")
    print(f"  HL检测触发: {counts['hl_detected']} 次")
    print(f"  预警降级: {counts['alert_downgraded']} 次")
    
    if buy_signals:
        avg_buy_score = sum(r["score"] for r in buy_signals) / len(buy_signals)
        avg_buy_price = sum(r["price"] for r in buy_signals) / len(buy_signals)
        print(f"  买入均分: {avg_buy_score:.1f} | 买入均价: {avg_buy_price:.2f}")
    
    if sell_signals:
        avg_sell_score = sum(r["score"] for r in sell_signals) / len(sell_signals)
        avg_sell_price = sum(r["price"] for r in sell_signals) / len(sell_signals)
        print(f"  卖出均分: {avg_sell_score:.1f} | 卖出均价: {avg_sell_price:.2f}")
    
    # 07-14详细
    d0714 = [r for r in results if r["date"] == "2026-07-14"]
    if d0714:
        print(f"\n  07-14 详细 ({len(d0714)} 条记录):")
        for r in d0714[:8]:
            hl_mark = " [HL]" if r["hl_detected"] else ""
            alert_mark = f" [A{r['alert_level']}→0]" if r["alert_downgraded"] else ""
            print(f"    {r['time']} 价格{r['price']:.2f} 买{r['buy_score']} 卖{r['sell_score']} {r['action'] or '-'}" + hl_mark + alert_mark)
    
    all_reports.append({
        "label": label, "t_mode": t_mode, "enable_hl": enable_hl,
        "results": results, "counts": counts,
        "buy_signals": buy_signals, "sell_signals": sell_signals
    })

# ==================== 对比分析 ====================
print("\n" + "="*70)
print("对比分析总结")
print("="*70)

# 正T对比
long_with = all_reports[0]
long_without = all_reports[1]
print(f"\n1. 正T模式: 含HL vs 不含HL")
print(f"   买入信号: {len(long_with['buy_signals'])} vs {len(long_without['buy_signals'])} (差: {len(long_with['buy_signals']) - len(long_without['buy_signals'])})")
print(f"   HL触发: {long_with['counts']['hl_detected']} vs {long_without['counts']['hl_detected']}")
print(f"   预警降级: {long_with['counts']['alert_downgraded']} vs {long_without['counts']['alert_downgraded']}")

# 反T对比
short_with = all_reports[2]
short_without = all_reports[3]
print(f"\n2. 反T模式: 含HL vs 不含HL")
print(f"   买入信号: {len(short_with['buy_signals'])} vs {len(short_without['buy_signals'])} (差: {len(short_with['buy_signals']) - len(short_without['buy_signals'])})")
print(f"   卖出信号: {len(short_with['sell_signals'])} vs {len(short_without['sell_signals'])} (差: {len(short_with['sell_signals']) - len(short_without['sell_signals'])})")
print(f"   HL触发: {short_with['counts']['hl_detected']} vs {short_without['counts']['hl_detected']}")

# 正T vs 反T (都含HL)
print(f"\n3. 正T vs 反T (都含HL)")
print(f"   买入信号: {len(long_with['buy_signals'])} vs {len(short_with['buy_signals'])}")
print(f"   卖出信号: {len(long_with['sell_signals'])} vs {len(short_with['sell_signals'])}")
print(f"   说明: 反T模式会反转早盘预警逻辑，影响买卖门控")

print("\n" + "="*70)
print("V1.26 回测验证完成")
print("="*70)
