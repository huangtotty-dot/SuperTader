# -*- coding: utf-8 -*-
"""
华工科技(000988) 10日回测脚本 — 基于真实分钟数据验证65/75/68分门槛
使用方法：在本地 t_trader 环境中运行
  python backtest_huagong_minute.py
"""
import sys, os, json, math
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict

import numpy as np
import pandas as pd
import requests

# 东财历史分钟数据接口
def fetch_minute_eastmoney(code, date_str):
    """获取指定日期的1分钟K线数据"""
    secid = f"0.{code}"
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13,f14,f15,f17"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        f"&ndate={date_str}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()
        if data.get("rc") != 0 or not data.get("data"):
            return pd.DataFrame()
        trends = data["data"].get("trends", [])
        rows = []
        for t in trends:
            parts = t.split(",")
            if len(parts) < 7:
                continue
            rows.append({
                "time": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": int(parts[5]) if parts[5].isdigit() else 0,
                "amount": float(parts[6]) if parts[6] else 0.0,
            })
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[ERROR] 获取 {code} {date_str} 分钟数据失败: {e}")
        return pd.DataFrame()


def calc_indicators(df):
    """计算分钟线指标"""
    if df.empty or len(df) < 5:
        return df
    df = df.copy()
    df["vwap"] = (df["amount"].cumsum() / df["volume"].cumsum()).fillna(df["close"].iloc[0])
    df["day_high"] = df["high"].cummax()
    df["day_low"] = df["low"].cummin()
    df["range"] = df["day_high"] - df["day_low"]
    df["range_pos"] = df["range"].apply(lambda x: 0.5 if x == 0 else (df.loc[df.index[-1], "close"] - df.loc[df.index[-1], "day_low"]) / x)
    df["mom5"] = df["close"].diff(5) / df["close"].shift(5)
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()
    return df


def evaluate_signal(df, pre_close):
    """简化版信号评估，贴近 signal_engine V1.14 核心逻辑"""
    if df.empty or len(df) < 10:
        return []
    
    signals = []
    for i in range(10, len(df)):
        row = df.iloc[i]
        price = row["close"]
        vwap = row["vwap"]
        high = row["day_high"]
        low = row["day_low"]
        vol_ratio = row["vol_ratio"] if not pd.isna(row["vol_ratio"]) else 1.0
        mom5 = row["mom5"] if not pd.isna(row["mom5"]) else 0
        
        minute = row["time"].minute
        hour = row["time"].hour
        t_val = hour * 100 + minute
        
        range_pos = (price - low) / (high - low) if (high - low) > 0 else 0.5
        today_ret = (price - pre_close) / pre_close if pre_close > 0 else 0
        vwap_gap = (price - vwap) / vwap if vwap > 0 else 0
        
        # ========== 高抛评分 ==========
        sell_score = 0
        sell_factors = []
        
        # 时间分
        if 930 <= t_val < 1000:
            sell_score += 15
            sell_factors.append("早盘+15")
        elif 1000 <= t_val < 1100:
            sell_score += 8
            sell_factors.append("上午+8")
        elif 1300 <= t_val < 1430:
            sell_score += 5
            sell_factors.append("下午+5")
        elif 1430 <= t_val <= 1500:
            sell_score += 10
            sell_factors.append("尾盘+10")
        
        # 区间位置
        if range_pos > 0.85:
            sell_score += 15
            sell_factors.append(f"区间高位({range_pos:.2f})+15")
        elif range_pos > 0.6:
            sell_score += 8
            sell_factors.append(f"区间中上({range_pos:.2f})+8")
        
        # VWAP偏离
        if vwap_gap > 0.02:
            sell_score += 12
            sell_factors.append(f"VWAP上方+12")
        elif vwap_gap > 0.01:
            sell_score += 6
            sell_factors.append(f"VWAP微上+6")
        
        # 正收益
        if today_ret > 0.03:
            sell_score += 10
            sell_factors.append(f"大涨+10")
        elif today_ret > 0:
            sell_score += 5
            sell_factors.append(f"正收益+5")
        
        # 成交量
        if vol_ratio > 1.5:
            sell_score += 8
            sell_factors.append("放量+8")
        elif vol_ratio > 1.0:
            sell_score += 4
            sell_factors.append("微放量+4")
        
        # 5分钟动量负
        if mom5 < -0.005:
            sell_score += 8
            sell_factors.append("动量转负+8")
        
        # 冲高回落（当前价格低于近期最高）
        if i >= 3:
            recent_high = df.iloc[max(0, i-10):i+1]["high"].max()
            if price < recent_high * 0.995:
                sell_score += 10
                sell_factors.append("冲高回落+10")
        
        # ========== 低吸评分 ==========
        buy_score = 0
        buy_factors = []
        
        # 时间分
        if 1300 <= t_val < 1500:
            buy_score += 15
            buy_factors.append("下午+15")
        elif 930 <= t_val < 1000:
            buy_score += 5
            buy_factors.append("早盘+5")
        elif 1000 <= t_val < 1130:
            buy_score += 8
            buy_factors.append("上午+8")
        
        # 区间位置
        if range_pos < 0.15:
            buy_score += 15
            buy_factors.append(f"区间低位({range_pos:.2f})+15")
        elif range_pos < 0.4:
            buy_score += 8
            buy_factors.append(f"区间中下({range_pos:.2f})+8")
        
        # VWAP偏离
        if vwap_gap < -0.02:
            buy_score += 12
            buy_factors.append(f"VWAP下方+12")
        elif vwap_gap < -0.01:
            buy_score += 6
            buy_factors.append(f"VWAP微下+6")
        
        # 负收益
        if today_ret < -0.03:
            buy_score += 10
            buy_factors.append(f"大跌+10")
        elif today_ret < 0:
            buy_score += 5
            buy_factors.append(f"负收益+5")
        
        # 成交量
        if vol_ratio > 1.5:
            buy_score += 8
            buy_factors.append("放量+8")
        elif vol_ratio > 1.0:
            buy_score += 4
            buy_factors.append("微放量+4")
        
        # 5分钟动量正
        if mom5 > 0.005:
            buy_score += 8
            buy_factors.append("动量转正+8")
        
        # 探底回升
        if i >= 3:
            recent_low = df.iloc[max(0, i-10):i+1]["low"].min()
            if price > recent_low * 1.005 and price < recent_low * 1.02:
                buy_score += 10
                buy_factors.append("探底回升+10")
        
        # 记录信号
        if sell_score > 0 or buy_score > 0:
            signals.append({
                "time": row["time"].strftime("%H:%M"),
                "price": price,
                "vwap": vwap,
                "sell_score": sell_score,
                "buy_score": buy_score,
                "sell_factors": sell_factors,
                "buy_factors": buy_factors,
                "range_pos": range_pos,
                "today_ret": today_ret,
                "vwap_gap": vwap_gap,
            })
    
    return signals


def run_day(code, date_str, pre_close):
    """回测单天"""
    print(f"\n{'='*60}")
    print(f"回测 {code} {date_str} (前收 {pre_close})")
    print(f"{'='*60}")
    
    df = fetch_minute_eastmoney(code, date_str)
    if df.empty:
        print("[WARN] 无数据")
        return None
    
    df = calc_indicators(df)
    signals = evaluate_signal(df, pre_close)
    
    if not signals:
        print("[INFO] 无信号")
        return None
    
    # 找出最高分的卖和买信号
    best_sell = max([s for s in signals if s["sell_score"] > 0], key=lambda x: x["sell_score"], default=None)
    best_buy = max([s for s in signals if s["buy_score"] > 0], key=lambda x: x["buy_score"], default=None)
    
    # 旧门槛 (45)
    old_sell = best_sell and best_sell["sell_score"] >= 45
    old_buy = best_buy and best_buy["buy_score"] >= 45
    
    # 新门槛
    # 卖: 10:00前>=75, 10:00后>=65
    new_sell = False
    sell_time = int(best_sell["time"].replace(":", "")) if best_sell else 0
    if best_sell:
        if sell_time < 1000:
            new_sell = best_sell["sell_score"] >= 75
        else:
            new_sell = best_sell["sell_score"] >= 65
    
    # 买: >=68
    new_buy = best_buy and best_buy["buy_score"] >= 68
    
    print(f"[最佳卖信号] 时间:{best_sell['time'] if best_sell else 'N/A'} 分数:{best_sell['sell_score'] if best_sell else 0} {'[触发]' if old_sell else ''} {'[新门槛触发]' if new_sell else ''}")
    if best_sell:
        print(f"  价格:{best_sell['price']:.2f} VWAP:{best_sell['vwap']:.2f} 区间位置:{best_sell['range_pos']:.2f} 今日收益:{best_sell['today_ret']*100:.1f}%")
        print(f"  因子: {' | '.join(best_sell['sell_factors'])}")
    
    print(f"[最佳买信号] 时间:{best_buy['time'] if best_buy else 'N/A'} 分数:{best_buy['buy_score'] if best_buy else 0} {'[触发]' if old_buy else ''} {'[新门槛触发]' if new_buy else ''}")
    if best_buy:
        print(f"  价格:{best_buy['price']:.2f} VWAP:{best_buy['vwap']:.2f} 区间位置:{best_buy['range_pos']:.2f} 今日收益:{best_buy['today_ret']*100:.1f}%")
        print(f"  因子: {' | '.join(best_buy['buy_factors'])}")
    
    return {
        "date": date_str,
        "best_sell_score": best_sell["sell_score"] if best_sell else 0,
        "best_buy_score": best_buy["buy_score"] if best_buy else 0,
        "old_sell": old_sell,
        "old_buy": old_buy,
        "new_sell": new_sell,
        "new_buy": new_buy,
        "sell_time": best_sell["time"] if best_sell else "",
        "buy_time": best_buy["time"] if best_buy else "",
    }


def main():
    code = "000988"
    # 10个交易日 + 前一日用于计算前收
    dates = [
        ("20260622", 177.55),  # 前收来自日K数据
        ("20260623", 174.67),
        ("20260624", 163.66),
        ("20260625", 167.89),
        ("20260626", 173.58),
        ("20260629", 160.94),
        ("20260630", 177.03),
        ("20260701", 184.61),
        ("20260702", 173.55),
        ("20260703", 156.20),
    ]
    
    results = []
    for date_str, pre_close in dates:
        r = run_day(code, date_str, pre_close)
        if r:
            results.append(r)
    
    if not results:
        print("\n[ERROR] 未获取到任何数据")
        return
    
    # 统计
    old_total = sum(1 for r in results if r["old_sell"] or r["old_buy"])
    new_total = sum(1 for r in results if r["new_sell"] or r["new_buy"])
    
    print(f"\n{'='*60}")
    print("统计汇总")
    print(f"{'='*60}")
    print(f"回测天数: {len(results)}")
    print(f"旧门槛(>=45): 触发 {old_total} 天")
    print(f"新门槛(65/75/68): 触发 {new_total} 天")
    print(f"过滤比例: {(1 - new_total/max(old_total,1))*100:.1f}%")
    print(f"\n{'='*60}")
    
    # 保存JSON
    out = f"backtest_huagong_{code}_10d.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"结果已保存: {out}")


if __name__ == "__main__":
    main()
