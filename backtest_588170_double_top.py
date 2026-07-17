# -*- coding: utf-8 -*-
"""
588170 (科创半导体ETF) 双顶回测脚本
验证 DOUBLE_TOP15 因子在 2026-07-01 走势下的触发效果

使用方法:
  cd E:\06_T
  python backtest_588170_double_top.py
"""
import sys, os
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

# ==================== 1. 构造模拟分钟数据 ====================
def build_588170_0701():
    """
    根据用户描述构造 588170 在 2026-07-01 的分钟数据:
      - 开盘 4.236 (pre_close=4.20)
      - 10:25 第一个高点 4.395
      - 10:42 触及日平均线 ~4.24
      - 11:02 第二个高点 4.37 (未创新高)
      - 之后持续下跌到收盘 4.176
    """
    base = datetime(2026, 7, 1, 9, 30, 0)
    rows = []
    pre_close = 4.20
    
    # --- 9:30 ~ 10:00: 缓慢上涨 4.236 -> 4.30 ---
    for i in range(30):
        t = base + timedelta(minutes=i)
        p = 4.236 + 0.0022 * i
        rows.append(_make_bar(t, p, 0.003))
    
    # --- 10:00 ~ 10:25: 加速上涨 4.30 -> 4.395 ---
    for i in range(25):
        t = base + timedelta(minutes=30+i)
        p = 4.30 + 0.0038 * i
        if i == 24:  # 10:24 强制到 4.395
            p = 4.395
        h = p + 0.002
        if i == 24:
            h = 4.398
        rows.append(_make_bar(t, p, 0.003, high=h))
    
    # --- 10:25 ~ 10:41: 回落 4.395 -> 4.24 ---
    for i in range(17):
        t = base + timedelta(minutes=55+i)
        p = 4.395 - 0.0091 * i
        if i == 16:  # 10:41
            p = 4.24
        rows.append(_make_bar(t, p, 0.003))
    
    # --- 10:42 ~ 11:02: 二次冲顶 4.24 -> 4.37 ---
    for i in range(21):
        t = base + timedelta(minutes=72+i)
        p = 4.24 + 0.0062 * i
        if i == 20:  # 11:02
            p = 4.37
        h = p + 0.002
        if i == 20:
            h = 4.372
        rows.append(_make_bar(t, p, 0.003, high=h))
    
    # --- 11:02 ~ 11:30: 下跌 4.37 -> 4.176 ---
    for i in range(28):
        t = base + timedelta(minutes=93+i)
        p = 4.37 - 0.0070 * i
        if i == 27:
            p = 4.176
        rows.append(_make_bar(t, p, 0.003))
    
    # --- 13:00 ~ 15:00: 低位震荡 4.176 -> 4.22 ---
    # 13:00 对应第 151 分钟 (9:30 到 13:00 共 210 分钟? 不对，A股是 9:30-11:30, 13:00-15:00)
    # 上午 120 分钟 (9:30-11:30)，下午从 13:00 开始
    for i in range(120):
        t = base + timedelta(minutes=150+i)  # 13:00 开始
        p = 4.176 + 0.0005 * i + np.random.normal(0, 0.002)
        if p < 4.15:
            p = 4.15
        if p > 4.25:
            p = 4.25
        rows.append(_make_bar(t, p, 0.003))
    
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)
    return df, pre_close

def _make_bar(t, close, spread, high=None, low=None, vol=10000):
    o = close - spread * 0.3
    h = high if high is not None else close + spread
    l = low if low is not None else close - spread
    return {
        "time": t.strftime("%Y-%m-%d %H:%M:%S"),
        "open": round(o, 3),
        "high": round(h, 3),
        "low": round(l, 3),
        "close": round(close, 3),
        "volume": vol,
        "amount": round(close * vol, 2),
    }


# ==================== 2. 计算指标 ====================
def calc_indicators(df, pre_close):
    df = df.copy()
    c = df["close"].astype(float)
    
    # VWAP
    tp = (df["high"].astype(float) + df["low"].astype(float) + c) / 3
    df["tp_vol"] = tp * df["volume"].astype(float)
    df["date"] = pd.to_datetime(df["time"]).dt.date
    df["vwap"] = df.groupby("date")["tp_vol"].cumsum() / df.groupby("date")["volume"].cumsum()
    df["vwap"] = df["vwap"].ffill().fillna(c)
    
    # RSI(14)
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)
    
    # MACD
    exp1 = c.ewm(span=12, adjust=False).mean()
    exp2 = c.ewm(span=26, adjust=False).mean()
    df["macd"] = exp1 - exp2
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = (df["macd"] - df["macd_signal"]) * 2
    
    # EMA spread
    ema_fast = c.ewm(span=12, adjust=False).mean()
    ema_slow = c.ewm(span=26, adjust=False).mean()
    df["ema_spread"] = (ema_fast - ema_slow) / ema_slow.replace(0, np.nan)
    
    # BB
    ma20 = c.rolling(20, min_periods=1).mean()
    sd20 = c.rolling(20, min_periods=1).std()
    df["bb_up"] = ma20 + 2 * sd20
    df["bb_dn"] = ma20 - 2 * sd20
    band = (df["bb_up"] - df["bb_dn"]).replace(0, np.nan)
    df["bb_pct"] = (c - df["bb_dn"]) / band
    
    # 日内统计
    day_high = df.groupby("date")["high"].transform("max")
    day_low = df.groupby("date")["low"].transform("min")
    df["day_amplitude"] = (day_high.astype(float) - day_low.astype(float)) / day_low.astype(float)
    df["range_pos"] = (c - day_low.astype(float)) / (day_high.astype(float) - day_low.astype(float) + 1e-9)
    
    # 成交量比
    df["vol_ma10"] = df["volume"].rolling(10, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma10"].replace(0, np.nan)
    
    # 5分钟动量
    df["mom5"] = c.pct_change(5)
    
    # 影线
    k_len = df["high"].astype(float) - df["low"].astype(float) + 1e-5
    df["upper_shadow"] = (df["high"].astype(float) - df[["open", "close"]].max(axis=1)) / k_len
    df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"].astype(float)) / k_len
    
    # 前高
    df["prev_high"] = c.rolling(120).max()
    
    # 时间字段
    df["dt"] = pd.to_datetime(df["time"])
    df["t_val"] = df["dt"].dt.hour * 100 + df["dt"].dt.minute
    
    return df


# ==================== 3. 卖出评分引擎（复现 signal_engine 核心逻辑） ====================
def evaluate_sell(df, pre_close, code="588170"):
    """逐分钟计算卖出评分，包含 DOUBLE_TOP15"""
    results = []
    
    for i in range(15, len(df)):
        row = df.iloc[i]
        price = float(row["close"])
        vwap = float(row["vwap"])
        t_val = int(row["t_val"])
        rsi = float(row["rsi"]) if pd.notna(row["rsi"]) else 50
        bb_pct = float(row["bb_pct"]) if pd.notna(row["bb_pct"]) else 0.5
        macd_hist = float(row["macd_hist"]) if pd.notna(row["macd_hist"]) else 0.0
        prev_macd_hist = float(df.iloc[i-1]["macd_hist"]) if i >= 1 else 0.0
        ema_spread = float(row["ema_spread"]) if pd.notna(row["ema_spread"]) else 0.0
        prev_ema_spread = float(df.iloc[i-1]["ema_spread"]) if i >= 1 else 0.0
        range_pos = float(row["range_pos"]) if pd.notna(row["range_pos"]) else 0.5
        vol_ratio = float(row["vol_ratio"]) if pd.notna(row["vol_ratio"]) else 1.0
        mom5 = float(row["mom5"]) if pd.notna(row["mom5"]) else 0.0
        upper_shadow = float(row["upper_shadow"]) if pd.notna(row["upper_shadow"]) else 0.0
        today_open = float(df.iloc[0]["open"])
        today_ret = (price - pre_close) / pre_close
        sell_profit_space = (price - vwap) / vwap if vwap else 0.0
        
        sell_score = 0
        factors = []
        
        # --- 时间分 ---
        if 1000 <= t_val <= 1045 or 1400 <= t_val <= 1445:
            sell_score += 15
            factors.append("时间窗口+15")
        elif 930 <= t_val <= 935:
            sell_score += 8
            factors.append("早盘机会+8")
        
        # --- VWAP偏离 ---
        if sell_profit_space > 0:
            sell_score += 15
            factors.append(f"回吐空间+15({sell_profit_space*100:.2f}%)")
        
        # --- RSI超买 ---
        if rsi >= 70:
            sell_score += 15
            factors.append(f"RSI超买+15({rsi:.1f})")
        
        # --- MACD拐头 ---
        if macd_hist < prev_macd_hist and macd_hist > 0:
            sell_score += 10
            factors.append(f"MACD拐头+10({macd_hist:.4f})")
        
        # --- 量能确认 ---
        if vol_ratio >= 1.3:
            sell_score += 6
            factors.append(f"量能确认+6({vol_ratio:.2f})")
        
        # --- 长上影 ---
        if upper_shadow >= 0.5:
            sell_score += 15
            factors.append(f"长上影+15({upper_shadow:.2f})")
        
        # --- EMA转弱 ---
        if ema_spread < prev_ema_spread and ema_spread < 0.002:
            sell_score += 4
            factors.append(f"EMA转弱+4")
        
        # --- 区间高位 ---
        if range_pos >= 0.75 and mom5 < 0.01:
            sell_score += 4
            factors.append(f"区间高位+4({range_pos:.2f})")
        
        # --- 早盘冲高 ---
        if 930 <= t_val <= 935 and today_ret > 0.006 and price > vwap * 1.005:
            surge = min(18, int(today_ret * 1000))
            sell_score += surge
            factors.append(f"早盘冲高+{surge}")
        
        # ==================== V1.15fix: DOUBLE_TOP15 ====================
        current_idx = i
        first_major_peak = 0
        for j in range(max(1, current_idx - 60), current_idx):
            h = float(df.iloc[j]["high"])
            if h <= first_major_peak:
                continue
            low_after = float(df.iloc[j:current_idx + 1]["low"].min())
            if (h - low_after) / h >= 0.005 and (current_idx - j) >= 15:
                first_major_peak = h
        
        if first_major_peak > 0 and price >= first_major_peak * 0.99 and price < first_major_peak:
            sell_score += 15
            factors.append(f"双顶保护+15(前高{first_major_peak:.3f})")
        
        # --- 门槛判断 ---
        # ETF 门槛: 10:00前 75, 10:00后 65
        threshold = 75 if t_val < 1000 else 65
        triggered = sell_score >= threshold
        
        results.append({
            "time": row["dt"].strftime("%H:%M"),
            "price": price,
            "sell_score": sell_score,
            "threshold": threshold,
            "triggered": triggered,
            "factors": factors,
            "rsi": rsi,
            "vwap": vwap,
            "range_pos": range_pos,
            "first_major_peak": first_major_peak if first_major_peak > 0 else None,
        })
    
    return pd.DataFrame(results)


# ==================== 4. 主程序 ====================
def main():
    print("=" * 70)
    print("588170 (科创半导体ETF) 双顶回测 - DOUBLE_TOP15 验证")
    print("=" * 70)
    
    df, pre_close = build_588170_0701()
    print(f"\n数据构造完成: {len(df)} 条分钟线")
    print(f"日期: 2026-07-01, 前收: {pre_close}")
    print(f"开盘: {df.iloc[0]['close']:.3f}, 最高: {df['high'].max():.3f}, 最低: {df['low'].min():.3f}")
    
    # 计算指标
    df = calc_indicators(df, pre_close)
    
    # 运行卖出评分
    results = evaluate_sell(df, pre_close)
    
    # 找出触发信号的时点
    triggered = results[results["triggered"] == True]
    
    print(f"\n{'='*70}")
    print("卖出信号触发汇总")
    print(f"{'='*70}")
    print(f"总扫描分钟数: {len(results)}")
    print(f"触发信号次数: {len(triggered)}")
    
    if not triggered.empty:
        print(f"\n{'时间':<8} {'价格':<8} {'分数':<6} {'门槛':<6} {'触发因子'}")
        print("-" * 70)
        for _, r in triggered.iterrows():
            factors_str = " | ".join(r["factors"])
            print(f"{r['time']:<8} {r['price']:<8.3f} {r['sell_score']:<6} {r['threshold']:<6} {factors_str}")
    else:
        print("\n⚠️ 无任何时点触发卖出信号")
    
    # 重点检查 10:25 和 11:02 附近
    print(f"\n{'='*70}")
    print("关键时间点详细分析")
    print(f"{'='*70}")
    
    key_times = ["10:24", "10:25", "10:30", "10:41", "10:50", "11:01", "11:02", "11:10"]
    for kt in key_times:
        match = results[results["time"] == kt]
        if not match.empty:
            r = match.iloc[0]
            status = "✅ 触发" if r["triggered"] else "❌ 未触发"
            peak_info = f" 前高={r['first_major_peak']:.3f}" if r["first_major_peak"] else ""
            print(f"{kt} | 价格{r['price']:.3f} | 卖分{r['sell_score']}/{r['threshold']} | {status}{peak_info}")
            if r["factors"]:
                print(f"      因子: {' | '.join(r['factors'])}")
    
    # 输出 DOUBLE_TOP15 首次触发时间
    dt_triggered = results[results["factors"].apply(lambda x: any("双顶保护" in f for f in x))]
    print(f"\n{'='*70}")
    print("DOUBLE_TOP15 因子专项分析")
    print(f"{'='*70}")
    if not dt_triggered.empty:
        first = dt_triggered.iloc[0]
        print(f"✅ DOUBLE_TOP15 首次触发: {first['time']} 价格 {first['price']:.3f}")
        print(f"   当时卖出总分: {first['sell_score']}, 门槛: {first['threshold']}")
        print(f"   {'已触发卖出信号' if first['triggered'] else '未达到门槛，需要其他因子配合'}")
        
        # 列出所有触发 DOUBLE_TOP15 的时点
        print(f"\n所有 DOUBLE_TOP15 触发时点:")
        for _, r in dt_triggered.iterrows():
            status = "✅ 卖" if r["triggered"] else "·"
            print(f"  {status} {r['time']} 价格{r['price']:.3f} 总分{r['sell_score']}")
    else:
        print("❌ DOUBLE_TOP15 全天未触发")
    
    # 保存结果
    out_path = r"E:\06_T\backtest_588170_double_top_result.csv"
    results.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n📁 详细结果已保存: {out_path}")

if __name__ == "__main__":
    main()
