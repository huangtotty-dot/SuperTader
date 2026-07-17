import requests
import json
from datetime import datetime, timedelta
import os
import sys

TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"

def fetch_and_analyze(ts_code="600089.SH", months=2):
    """
    统计指定股票最近N个月的每日冲高幅度
    冲高幅度定义：当日最高价相对开盘价的涨幅
    """
    # 计算日期范围
    end_date = datetime.now()
    start_date = end_date - timedelta(days=months * 35)  # 留余量
    
    start_str = start_date.strftime('%Y%m%d')
    end_str = end_date.strftime('%Y%m%d')
    
    print(f"[{ts_code}] 查询区间: {start_str} ~ {end_str}")
    
    # tushare API 获取分钟数据
    url = "https://api.tushare.pro"
    payload = {
        "api_name": "stk_mins",
        "token": TOKEN,
        "params": {
            "ts_code": ts_code,
            "start_date": start_str,
            "end_date": end_str,
            "freq": "1min"
        }
    }
    
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    data = resp.json()
    
    if data.get("code") != 0:
        print(f"请求失败: {data.get('msg', '未知错误')}")
        return None
    
    fields = data["data"]["fields"]
    items = data["data"]["items"]
    print(f"获取到 {len(items)} 条分钟数据")
    
    # 字段索引
    trade_time_idx = fields.index("trade_time")
    open_idx = fields.index("open")
    high_idx = fields.index("high")
    low_idx = fields.index("low")
    close_idx = fields.index("close")
    
    # 按交易日分组
    daily_data = {}
    for item in items:
        trade_time = item[trade_time_idx]
        date = trade_time[:10]  # YYYY-MM-DD
        
        if date not in daily_data:
            daily_data[date] = {"opens": [], "highs": [], "lows": [], "closes": []}
        
        daily_data[date]["opens"].append(item[open_idx])
        daily_data[date]["highs"].append(item[high_idx])
        daily_data[date]["lows"].append(item[low_idx])
        daily_data[date]["closes"].append(item[close_idx])
    
    print(f"共 {len(daily_data)} 个交易日")
    
    # 计算每日冲高幅度
    results = []
    for date in sorted(daily_data.keys()):
        d = daily_data[date]
        day_open = d["opens"][0]
        day_high = max(d["highs"])
        day_low = min(d["lows"])
        day_close = d["closes"][-1]
        
        # 冲高幅度：最高价相对开盘价
        surge_pct = (day_high - day_open) / day_open * 100
        # 日内最大波动
        range_pct = (day_high - day_low) / day_open * 100
        # 收盘回落
        pullback_pct = (day_high - day_close) / day_high * 100
        
        results.append({
            "date": date,
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "close": day_close,
            "冲高幅度": round(surge_pct, 2),
            "日内波动": round(range_pct, 2),
            "收盘回落": round(pullback_pct, 2)
        })
    
    # 统计
    avg_surge = sum(r["冲高幅度"] for r in results) / len(results)
    avg_range = sum(r["日内波动"] for r in results) / len(results)
    avg_pullback = sum(r["收盘回落"] for r in results) / len(results)
    
    # 分布统计
    big_surge = [r for r in results if r["冲高幅度"] >= 3.0]
    mid_surge = [r for r in results if 1.5 <= r["冲高幅度"] < 3.0]
    small_surge = [r for r in results if r["冲高幅度"] < 1.5]
    
    top5 = sorted(results, key=lambda x: x["冲高幅度"], reverse=True)[:5]
    
    report = {
        "summary": {
            "股票": ts_code,
            "统计区间": f"{results[0]['date']} 至 {results[-1]['date']}",
            "交易日数": len(results),
            "平均冲高幅度": round(avg_surge, 2),
            "平均日内波动": round(avg_range, 2),
            "平均收盘回落": round(avg_pullback, 2)
        },
        "distribution": {
            "大冲高(>=3%)": {
                "天数": len(big_surge),
                "平均幅度": round(sum(r["冲高幅度"] for r in big_surge) / len(big_surge), 2) if big_surge else 0,
                "日期": [r["date"] for r in big_surge]
            },
            "中等冲高(1.5%-3%)": {
                "天数": len(mid_surge),
                "平均幅度": round(sum(r["冲高幅度"] for r in mid_surge) / len(mid_surge), 2) if mid_surge else 0,
                "日期": [r["date"] for r in mid_surge]
            },
            "小冲高(<1.5%)": {
                "天数": len(small_surge),
                "平均幅度": round(sum(r["冲高幅度"] for r in small_surge) / len(small_surge), 2) if small_surge else 0,
                "日期": [r["date"] for r in small_surge]
            }
        },
        "top5": top5,
        "daily": results
    }
    
    return report


def print_report(report):
    """打印统计报告"""
    if not report:
        return
    
    s = report["summary"]
    print("\n" + "="*60)
    print(f"  {s['股票']} 冲高幅度统计报告")
    print("="*60)
    print(f"  统计区间: {s['统计区间']}")
    print(f"  交易日数: {s['交易日数']} 天")
    print(f"  平均冲高幅度: {s['平均冲高幅度']}%")
    print(f"  平均日内波动: {s['平均日内波动']}%")
    print(f"  平均收盘回落: {s['平均收盘回落']}%")
    print("-"*60)
    
    d = report["distribution"]
    print(f"\n  【冲高幅度分布】")
    print(f"  大冲高 (>=3%):   {d['大冲高(>=3%)']['天数']} 天, 平均 {d['大冲高(>=3%)']['平均幅度']}%")
    print(f"  中等冲高 (1.5%-3%): {d['中等冲高(1.5%-3%)']['天数']} 天, 平均 {d['中等冲高(1.5%-3%)']['平均幅度']}%")
    print(f"  小冲高 (<1.5%):  {d['小冲高(<1.5%)']['天数']} 天, 平均 {d['小冲高(<1.5%)']['平均幅度']}%")
    print("-"*60)
    
    print(f"\n  【冲高幅度 TOP 5】")
    for i, r in enumerate(report["top5"], 1):
        print(f"  {i}. {r['date']} | 开盘:{r['open']:.2f} -> 最高:{r['high']:.2f} | 冲高 +{r['冲高幅度']}%")
    
    print("="*60)


def main():
    # 默认统计特变电工
    ts_code = sys.argv[1] if len(sys.argv) > 1 else "600089.SH"
    
    report = fetch_and_analyze(ts_code)
    if report:
        print_report(report)
        
        # 保存到文件
        output_file = f"{ts_code.split('.')[0]}_surge_stats.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n  数据已保存: {output_file}")
    
    return report


if __name__ == "__main__":
    main()
