# -*- coding: utf-8 -*-
"""
V1.17 回测脚本：使用2026-07-06本地快照数据验证缩量止跌+放量反攻信号
"""
import csv
import sys, os, json
from datetime import datetime, timedelta
from collections import defaultdict

BASE_DIR = r"e:\06_T"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Mock akshare
class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: __import__('pandas').DataFrame()

import pandas as pd, numpy as np
ak_mock = MockAkshare()
sys.modules['akshare'] = ak_mock

shared = {'akshare': ak_mock, 'ak': ak_mock}
shared.update({
    '__name__': '__main__', '__file__': __file__,
    'os': os, 'sys': sys, 'json': json, 'time': __import__('time'),
    'logging': __import__('logging'), 'traceback': __import__('traceback'),
    'pd': pd, 'np': np, 'requests': __import__('requests'),
})

module_order = ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']
for mod_name in module_order:
    mod_path = os.path.join(BASE_DIR, f"{mod_name}.py")
    if not os.path.exists(mod_path):
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)

globals().update(shared)

# 设置 holdings
HOLDINGS = {
    "002261": {"name": "拓维信息", "cost": 33.787, "qty": 1200, "base": 1200, "t_qty": 1200, "type": "stock", "account": "账户A", "pre_close": 29.07/0.9962},
    "588170": {"name": "科创半导体ETF", "cost": 1.252, "qty": 54000, "base": 54000, "t_qty": 54000, "type": "etf", "account": "账户A", "pre_close": 1.214/1.0176},
    "600089": {"name": "特变电工", "cost": 26.216, "qty": 1200, "base": 1200, "t_qty": 1200, "type": "stock", "account": "账户A", "pre_close": 21.74/1.0042},
    "000988": {"name": "华工科技", "cost": 207.205, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "pre_close": 153.95},
    "300666": {"name": "江丰电子", "cost": 397.317, "qty": 100, "base": 100, "t_qty": 100, "type": "stock", "account": "账户B", "pre_close": 338.97/1.0204},
}

# 更新 shared 中的 HOLDINGS
shared['HOLDINGS'] = HOLDINGS

COMMISSION_RATE = 0.0015

def read_minute_csv(code, date):
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

def build_df_up_to(minute_df, idx):
    """构建到当前索引的df（包含技术指标）"""
    sub = minute_df.iloc[:idx+1].copy()
    if sub.empty:
        return sub
    sub = shared['add_indicators'](sub)
    return sub

def run_backtest_for_code(code, date="2026-07-06"):
    df = read_minute_csv(code, date)
    if df is None or df.empty:
        return None
    
    holding = HOLDINGS.get(code, {})
    if not holding:
        return None
    
    name = holding.get("name", code)
    engine = shared['SignalEngine']()
    
    signals = []
    trades = []
    virtual_buys = []
    virtual_sells = []
    
    for i in range(15, len(df)):
        sub_df = build_df_up_to(df, i)
        if sub_df.empty or len(sub_df) < 15:
            continue
        
        # 设置虚拟交易记录
        shared['VIRTUAL_TRADES'] = {code: {"BUY_LOW": virtual_buys, "SELL_HIGH": virtual_sells}}
        
        price = float(df.iloc[i]["close"])
        
        try:
            buy_score, sell_score, sig = engine.evaluate(code, name, sub_df, holding)
        except Exception as e:
            continue
        
        if sig:
            signals.append({
                "time": df.iloc[i]["time"],
                "action": sig.action,
                "price": sig.price,
                "score": sig.score,
                "factors": sig.factors,
                "buy_score": buy_score,
                "sell_score": sell_score,
            })
            
            # 模拟交易
            if sig.action in ["BUY_LOW", "ADD_POS"]:
                qty = int(holding.get("t_qty", 0) * 0.2)
                if qty > 0:
                    virtual_buys.append({"price": sig.price, "qty": qty, "time": df.iloc[i]["time"]})
                    trades.append({"time": df.iloc[i]["time"], "action": sig.action, "price": sig.price, "qty": qty})
            elif sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                total_buy = sum(t["qty"] for t in virtual_buys)
                total_sell = sum(t["qty"] for t in virtual_sells)
                available = total_buy - total_sell
                if available > 0:
                    qty = min(available, int(holding.get("t_qty", 0) * 0.3))
                    if qty > 0:
                        virtual_sells.append({"price": sig.price, "qty": qty, "time": df.iloc[i]["time"]})
                        trades.append({"time": df.iloc[i]["time"], "action": sig.action, "price": sig.price, "qty": qty})
    
    # 计算收益
    total_buy_cost = sum(t["price"] * t["qty"] for t in virtual_buys)
    total_sell_rev = sum(t["price"] * t["qty"] for t in virtual_sells)
    total_buy_qty = sum(t["qty"] for t in virtual_buys)
    total_sell_qty = sum(t["qty"] for t in virtual_sells)
    
    avg_buy = total_buy_cost / total_buy_qty if total_buy_qty > 0 else 0
    avg_sell = total_sell_rev / total_sell_qty if total_sell_qty > 0 else 0
    
    gross_profit = total_sell_rev - total_buy_cost
    commission = (total_buy_cost + total_sell_rev) * COMMISSION_RATE
    net_profit = gross_profit - commission
    
    return {
        "code": code,
        "name": name,
        "signals": signals,
        "trades": trades,
        "virtual_buys": virtual_buys,
        "virtual_sells": virtual_sells,
        "summary": {
            "buy_signals": len([s for s in signals if s["action"] in ["BUY_LOW", "ADD_POS"]]),
            "sell_signals": len([s for s in signals if s["action"] in ["SELL_HIGH", "PANIC_SELL"]]),
            "trades_executed": len(trades),
            "avg_buy": round(avg_buy, 4) if avg_buy > 0 else 0,
            "avg_sell": round(avg_sell, 4) if avg_sell > 0 else 0,
            "gross_profit": round(gross_profit, 2),
            "commission": round(commission, 2),
            "net_profit": round(net_profit, 2),
        }
    }

def main():
    results = {}
    for code in ["002261", "588170", "600089", "000988", "300666"]:
        print(f"回测 {code} ...")
        res = run_backtest_for_code(code)
        if res:
            results[code] = res
            print(f"  ✓ 买入信号:{res['summary']['buy_signals']} 卖出信号:{res['summary']['sell_signals']} 净收益:{res['summary']['net_profit']}")
        else:
            print(f"  ✗ 无数据")
    
    # 保存结果
    output = os.path.join(BASE_DIR, "backtest_0706_v117.json")
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output}")
    return results

if __name__ == "__main__":
    main()
