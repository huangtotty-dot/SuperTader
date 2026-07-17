# -*- coding: utf-8 -*-
"""
002261 (拓维信息) 6月26日-7月3日 真实数据回测
尝试多数据源: tushare -> 东财 -> akshare

使用方法:
  cd E:\06_T
  python backtest_002261_real.py
"""
import sys, os, json
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ============== 配置 ==============
CODE = "002261"
NAME = "拓维信息"
DATES = ["20260626", "20260629", "20260630", "20260701", "20260702", "20260703"]

# 日线数据（从公开信息已知）
DAILY_KNOWN = {
    "20260626": {"pre_close": 28.46, "open": 28.32, "high": 28.90, "low": 26.89, "close": 26.89},
    "20260627": {"pre_close": 26.89, "open": 26.89, "high": 27.50, "low": 26.50, "close": 27.00},
    "20260629": {"pre_close": 28.01, "open": 26.80, "high": 27.40, "low": 26.41, "close": 26.86},
    "20260630": {"pre_close": 26.86, "open": 27.15, "high": 28.23, "low": 26.88, "close": 28.01},
    "20260701": {"pre_close": 28.01, "open": 28.10, "high": 28.94, "low": 27.81, "close": 28.47},
    "20260702": {"pre_close": 28.47, "open": 28.10, "high": 31.32, "low": 28.00, "close": 29.89},
    "20260703": {"pre_close": 29.89, "open": 29.74, "high": 30.00, "low": 28.83, "close": 29.18},
}


# ============== 数据源 1: Tushare ==============
def fetch_tushare_minute(code, date, token):
    try:
        import tushare as ts
        ts.set_token(token)
        pro = ts.pro_api()
        df = ts.pro_bar(ts_code=f"{code}.SZ", freq='1min', start_date=date, end_date=date)
        if df is not None and not df.empty:
            df = df.rename(columns={
                "trade_time": "time", "open": "open", "high": "high",
                "low": "low", "close": "close", "vol": "volume", "amount": "amount"
            })
            df["time"] = pd.to_datetime(df["time"])
            return df, "tushare"
    except Exception as e:
        pass
    return None, None


# ============== 数据源 2: 东财 ==============
def fetch_eastmoney_minute(code, date):
    secid = f"0.{code}"
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13,f14,f15,f17"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
        f"&ndate={date}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://quote.eastmoney.com/"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()
        if data.get("rc") != 0 or not data.get("data"):
            return None, None
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
        if df.empty:
            return None, None
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)
        return df, "eastmoney"
    except Exception:
        return None, None


# ============== 数据源 3: akshare ==============
def fetch_akshare_minute(code, date):
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist_min_em(symbol=code, period="1", start_date=date, end_date=date, adjust="")
        if df is not None and not df.empty:
            df = df.rename(columns={
                "时间": "time", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount"
            })
            df["time"] = pd.to_datetime(df["time"])
            return df, "akshare"
    except Exception:
        pass
    return None, None


# ============== 拉取所有日期数据 ==============
def load_all_data(code, dates, token=""):
    all_data = {}
    for date in dates:
        print(f"\n[{date}] 拉取中...")
        df, source = None, None

        # 尝试 tushare
        if token:
            df, source = fetch_tushare_minute(code, date, token)
            if df is not None:
                print(f"  -> Tushare 成功: {len(df)} 条")

        # 尝试东财
        if df is None:
            df, source = fetch_eastmoney_minute(code, date)
            if df is not None:
                print(f"  -> 东财 成功: {len(df)} 条")

        # 尝试 akshare
        if df is None:
            df, source = fetch_akshare_minute(code, date)
            if df is not None:
                print(f"  -> akshare 成功: {len(df)} 条")

        if df is not None:
            all_data[date] = {"df": df, "source": source}
        else:
            print(f"  -> 所有数据源均失败")

    return all_data


# ============== 指标计算 ==============
def calc_indicators(df, pre_close):
    df = df.copy()
    c = df["close"].astype(float)

    # VWAP
    tp = (df["high"].astype(float) + df["low"].astype(float) + c) / 3
    df["tp_vol"] = tp * df["volume"].astype(float)
    df["date"] = pd.to_datetime(df["time"]).dt.date
    df["vwap"] = df.groupby("date")["tp_vol"].cumsum() / df.groupby("date")["volume"].cumsum()
    df["vwap"] = df["vwap"].ffill().fillna(c)

    # RSI
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

    df["vol_ma10"] = df["volume"].rolling(10, min_periods=1).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma10"].replace(0, np.nan)
    df["mom5"] = c.pct_change(5)

    k_len = df["high"].astype(float) - df["low"].astype(float) + 1e-5
    df["upper_shadow"] = (df["high"].astype(float) - df[["open", "close"]].max(axis=1)) / k_len
    df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"].astype(float)) / k_len

    df["prev_high"] = c.rolling(120).max()

    df["dt"] = pd.to_datetime(df["time"])
    df["t_val"] = df["dt"].dt.hour * 100 + df["dt"].dt.minute

    return df


# ============== 信号评估 (复现 signal_engine V1.15) ==============
def evaluate_signals(df, pre_close, is_etf=False):
    results = []
    sell_threshold = 65 if not is_etf else 65  # 个股10:00后65, ETF 10:00前75后65
    buy_threshold = 68

    for i in range(15, len(df)):
        row = df.iloc[i]
        price = float(row["close"])
        vwap = float(row["vwap"])
        t_val = int(row["t_val"])
        rsi = float(row["rsi"]) if pd.notna(row["rsi"]) else 50
        macd_hist = float(row["macd_hist"]) if pd.notna(row["macd_hist"]) else 0.0
        prev_macd_hist = float(df.iloc[i - 1]["macd_hist"]) if i >= 1 else 0.0
        ema_spread = float(row["ema_spread"]) if pd.notna(row["ema_spread"]) else 0.0
        prev_ema_spread = float(df.iloc[i - 1]["ema_spread"]) if i >= 1 else 0.0
        range_pos = float(row["range_pos"]) if pd.notna(row["range_pos"]) else 0.5
        vol_ratio = float(row["vol_ratio"]) if pd.notna(row["vol_ratio"]) else 1.0
        mom5 = float(row["mom5"]) if pd.notna(row["mom5"]) else 0.0
        upper_shadow = float(row["upper_shadow"]) if pd.notna(row["upper_shadow"]) else 0.0
        today_ret = (price - pre_close) / pre_close
        sell_profit_space = (price - vwap) / vwap if vwap else 0.0
        buy_profit_space = (vwap - price) / price if price > 0 else 0.0

        # ===== 卖出评分 =====
        sell_score = 0
        sell_factors = []

        if 1000 <= t_val <= 1045 or 1400 <= t_val <= 1445:
            sell_score += 15
            sell_factors.append("时间窗口+15")
        elif 930 <= t_val <= 935:
            sell_score += 8
            sell_factors.append("早盘机会+8")

        if sell_profit_space > 0:
            sell_score += 15
            sell_factors.append("回吐空间+15")

        if rsi >= 70:
            sell_score += 15
            sell_factors.append("RSI超买+15")

        if macd_hist < prev_macd_hist and macd_hist > 0:
            sell_score += 10
            sell_factors.append("MACD拐头+10")

        if vol_ratio >= 1.3:
            sell_score += 6
            sell_factors.append("量能确认+6")

        if upper_shadow >= 0.5:
            sell_score += 15
            sell_factors.append("长上影+15")

        if ema_spread < prev_ema_spread and ema_spread < 0.002:
            sell_score += 4
            sell_factors.append("EMA转弱+4")

        if range_pos >= 0.75 and mom5 < 0.01:
            sell_score += 4
            sell_factors.append("区间高位+4")

        if 930 <= t_val <= 935 and today_ret > 0.006 and price > vwap * 1.005:
            surge = min(18, int(today_ret * 1000))
            sell_score += surge
            sell_factors.append(f"早盘冲高+{surge}")

        # DOUBLE_TOP15 (+25)
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
            sell_score += 25
            sell_factors.append("双顶保护+25")

        st = sell_threshold
        if is_etf and t_val < 1000:
            st = 75

        sell_triggered = sell_score >= st

        # ===== 买入评分 =====
        buy_score = 0
        buy_factors = []

        if 1300 <= t_val < 1500:
            buy_score += 15
            buy_factors.append("下午+15")
        elif 930 <= t_val < 1000:
            buy_score += 5
            buy_factors.append("早盘+5")
        elif 1000 <= t_val < 1130:
            buy_score += 8
            buy_factors.append("上午+8")

        if range_pos < 0.15:
            buy_score += 15
            buy_factors.append("区间低位+15")
        elif range_pos < 0.4:
            buy_score += 8
            buy_factors.append("区间中下+8")

        if buy_profit_space < -0.02:
            buy_score += 12
            buy_factors.append("VWAP下方+12")
        elif buy_profit_space < -0.01:
            buy_score += 6
            buy_factors.append("VWAP微下+6")

        if today_ret < -0.03:
            buy_score += 10
            buy_factors.append("大跌+10")
        elif today_ret < 0:
            buy_score += 5
            buy_factors.append("负收益+5")

        if vol_ratio > 1.5:
            buy_score += 8
            buy_factors.append("放量+8")

        if mom5 > 0.005:
            buy_score += 8
            buy_factors.append("动量转正+8")

        buy_triggered = buy_score >= buy_threshold

        results.append({
            "time": row["dt"].strftime("%H:%M"),
            "price": price,
            "sell_score": sell_score,
            "sell_threshold": st,
            "sell_triggered": sell_triggered,
            "sell_factors": " | ".join(sell_factors),
            "buy_score": buy_score,
            "buy_threshold": buy_threshold,
            "buy_triggered": buy_triggered,
            "buy_factors": " | ".join(buy_factors),
            "rsi": rsi,
            "vwap": vwap,
            "range_pos": range_pos,
            "today_ret": today_ret,
        })

    return pd.DataFrame(results)


# ============== 主程序 ==============
def main():
    print("=" * 80)
    print(f"002261 {NAME} 回测 - 6月26日 至 7月3日")
    print("=" * 80)

    # 尝试读取 token
    token = ""
    token_path = os.path.join(os.path.dirname(__file__), ".tushare_token")
    if os.path.exists(token_path):
        with open(token_path, "r") as f:
            token = f.read().strip()
        print(f"Tushare token loaded from {token_path}")
    else:
        print("提示: 如需使用 Tushare，请在同目录下创建 .tushare_token 文件写入 token")

    # 拉取数据
    all_data = load_all_data(CODE, DATES, token)

    if not all_data:
        print("\n[错误] 未能获取任何日期的分钟数据，请检查网络或 token")
        return

    # 逐日回测
    summary = []

    for date in DATES:
        if date not in all_data:
            continue

        df_raw = all_data[date]["df"]
        source = all_data[date]["source"]
        daily = DAILY_KNOWN.get(date, {})
        pre_close = daily.get("pre_close", df_raw["close"].iloc[0])

        print(f"\n{'='*80}")
        print(f"[{date}] {NAME} - 数据源: {source}")
        print(f"  前收: {pre_close:.2f}, 实际开盘: {daily.get('open', '?')}, 实际收盘: {daily.get('close', '?')}")
        print(f"  实际最高: {daily.get('high', '?')}, 实际最低: {daily.get('low', '?')}")

        # 计算指标
        df = calc_indicators(df_raw, pre_close)

        # 信号评估
        results = evaluate_signals(df, pre_close, is_etf=False)

        # 提取触发信号
        sell_signals = results[results["sell_triggered"] == True]
        buy_signals = results[results["buy_triggered"] == True]

        print(f"\n  扫描分钟数: {len(results)}")
        print(f"  卖出触发: {len(sell_signals)} 次")
        print(f"  买入触发: {len(buy_signals)} 次")

        if not sell_signals.empty:
            print(f"\n  [卖出信号列表]")
            for _, r in sell_signals.iterrows():
                print(f"    {r['time']} 价格{r['price']:.2f} 分数{r['sell_score']}/{r['sell_threshold']} | {r['sell_factors']}")

        if not buy_signals.empty:
            print(f"\n  [买入信号列表]")
            for _, r in buy_signals.iterrows():
                print(f"    {r['time']} 价格{r['price']:.2f} 分数{r['buy_score']}/{r['buy_threshold']} | {r['buy_factors']}")

        # 保存单日结果
        out_dir = os.path.join(os.path.dirname(__file__), "backtest_output")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"002261_{date}_signals.csv")
        results.to_csv(out_path, index=False, encoding="utf-8-sig")

        # 汇总
        best_sell = sell_signals.iloc[0] if not sell_signals.empty else None
        best_buy = buy_signals.iloc[0] if not buy_signals.empty else None

        summary.append({
            "date": date,
            "pre_close": pre_close,
            "actual_open": daily.get("open"),
            "actual_high": daily.get("high"),
            "actual_low": daily.get("low"),
            "actual_close": daily.get("close"),
            "source": source,
            "sell_count": len(sell_signals),
            "buy_count": len(buy_signals),
            "first_sell_time": best_sell["time"] if best_sell is not None else "",
            "first_sell_price": best_sell["price"] if best_sell is not None else "",
            "first_sell_score": best_sell["sell_score"] if best_sell is not None else "",
            "first_buy_time": best_buy["time"] if best_buy is not None else "",
            "first_buy_price": best_buy["price"] if best_buy is not None else "",
            "first_buy_score": best_buy["buy_score"] if best_buy is not None else "",
        })

    # 总汇总
    print(f"\n{'='*80}")
    print("回测汇总")
    print(f"{'='*80}")

    df_summary = pd.DataFrame(summary)
    print(df_summary.to_string(index=False))

    summary_path = os.path.join(os.path.dirname(__file__), "backtest_output", "002261_summary.csv")
    df_summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    print(f"\n汇总已保存: {summary_path}")


if __name__ == "__main__":
    main()
