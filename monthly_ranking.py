# -*- coding: utf-8 -*-
"""
月度收益排名模块
"""
import os
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Tuple, List, Dict

import pandas as pd

from config import log, MONTHLY_GAIN_TOP_N, MONTHLY_GAIN_MIN_TRADING_DAYS, SCAN_WORKERS, RESULTS_DIR

from data_fetcher import (
    fetch_from_tencent_kline_final, fetch_from_akshare_hist, fetch_from_akshare_hist_alt,
    apply_qt_snapshot_amount, ensure_amount_column
)
from stock_pool import load_a_share_pool, is_a_share_code, is_st_stock, resolve_stock_concept, resolve_business_summary
from utils import truncate_reason
from feishu import send_monthly_gain_to_feishu

def parse_month_input(month_input: str) -> tuple:
    month_input = str(month_input or "").strip()
    if not month_input:
        return "", "", ""
    try:
        if "-" in month_input:
            dt = datetime.strptime(month_input + "-01", "%Y-%m-%d")
        else:
            dt = datetime.strptime(month_input, "%Y%m")
        month_start = dt.replace(day=1)
        if month_start.year == datetime.now().year and month_start.month == datetime.now().month:
            month_end = datetime.now()
        else:
            next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            month_end = next_month - timedelta(days=1)
        return month_start.strftime("%Y-%m"), month_start.strftime("%Y%m%d"), month_end.strftime("%Y%m%d")
    except Exception:
        return "", "", ""


def fetch_monthly_daily_data(code: str, month_start: str, month_end: str) -> pd.DataFrame:
    start_dt = datetime.strptime(month_start, "%Y%m%d")
    end_dt = datetime.strptime(month_end, "%Y%m%d")
    lookback_days = max(45, (end_dt - start_dt).days + 15)
    start_date = (start_dt - timedelta(days=lookback_days)).strftime("%Y%m%d")
    df = fetch_from_tencent_kline_final(code, month_end, start_date, require_target_date=False)
    if df.empty:
        df = fetch_from_akshare_hist(code, month_end, start_date)
    if df.empty:
        df = fetch_from_akshare_hist_alt(code, month_end, start_date)
    if df.empty or "date" not in df.columns:
        return pd.DataFrame()
    df = apply_qt_snapshot_amount(df, code, month_end)
    df = ensure_amount_column(df, code=code)
    df = df.copy()
    df["date"] = df["date"].astype(str).str.slice(0, 10).str.replace("-", "", regex=False)
    return df[(df["date"] >= month_start) & (df["date"] <= month_end)].copy()


def calculate_month_gain(df: pd.DataFrame, month_start: str, month_end: str) -> Dict[str, object]:
    if df.empty or "date" not in df.columns:
        return {}
    dfx = df.copy()
    dfx["date"] = dfx["date"].astype(str).str.slice(0, 10)
    dfx = dfx.sort_values("date")
    dfx["open"] = pd.to_numeric(dfx["open"], errors="coerce")
    dfx["close"] = pd.to_numeric(dfx["close"], errors="coerce")
    dfx["high"] = pd.to_numeric(dfx["high"], errors="coerce")
    dfx["low"] = pd.to_numeric(dfx["low"], errors="coerce")
    dfx["volume"] = pd.to_numeric(dfx["volume"], errors="coerce")
    dfx["amount"] = pd.to_numeric(dfx.get("amount", 0), errors="coerce")
    month_mask = (dfx["date"] >= month_start) & (dfx["date"] <= month_end)
    month_df = dfx.loc[month_mask].copy()
    if month_df.empty or len(month_df) < MONTHLY_GAIN_MIN_TRADING_DAYS:
        return {}
    first_day = month_df.iloc[0]
    prev_df = dfx[dfx["date"] < month_start]
    if not prev_df.empty:
        base_price = float(prev_df.iloc[-1]["close"])
        base_source = f"{prev_df.iloc[-1]['date']} 前收盘"
    else:
        base_price = float(first_day["open"])
        base_source = f"{first_day['date']} 月初开盘"
    end_row = month_df.iloc[-1]
    end_price = float(end_row["close"])
    gain_pct = ((end_price - base_price) / base_price * 100.0) if base_price > 0 else 0.0
    month_amount_sum = float(month_df["amount"].fillna(0).sum()) if "amount" in month_df.columns else 0.0
    month_amount_avg = float(month_df["amount"].fillna(0).mean()) if "amount" in month_df.columns else 0.0
    return {
        "gain_pct": gain_pct,
        "base_price": base_price,
        "base_source": base_source,
        "end_price": end_price,
        "high_price": float(month_df["high"].max()),
        "low_price": float(month_df["low"].min()),
        "trading_days": int(len(month_df)),
        "month_amount_sum": month_amount_sum,
        "month_amount_avg": month_amount_avg,
        "month_start": month_start,
        "month_end": month_end,
        "end_date": str(end_row["date"]),
    }


def send_monthly_gain_to_feishu(month_std: str, month_start: str, month_end: str, total_scanned: int, success_count: int, failed_count: int, top_results: List[Dict], top_n: int) -> None:
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK:
        return

    header_text = (
        f"📅 月度涨幅统计 {month_std}\n"
        f"区间：{month_start} ~ {month_end}\n"
        f"总扫描：{total_scanned} | 成功：{success_count} | 失败：{failed_count}\n"
        f"TOP{min(top_n, len(top_results))}"
    )
    post_feishu_payload({"msg_type": "text", "content": {"text": header_text}})

    if not top_results:
        post_feishu_payload({"msg_type": "text", "content": {"text": "暂无结果"}})
        return

    chunk_size = 5
    total_chunks = (len(top_results) + chunk_size - 1) // chunk_size
    for chunk_idx in range(total_chunks):
        start = chunk_idx * chunk_size
        chunk = top_results[start:start + chunk_size]
        lines = []
        for idx, item in enumerate(chunk, start + 1):
            business = str(item.get("business_summary", "")).replace("\n", " ").strip()
            sector_text = str(item.get("sector", "")).replace("\n", " ").strip()
            lines.append(
                f"{idx}. {item['name']}({item['code']}) {item['gain_pct']:.2f}%\n"
                f"   起点 {item['base_price']:.2f} → 终点 {item['end_price']:.2f}\n"
                f"   板块：{sector_text}\n"
                f"   业务：{business if business else '未知'}"
            )
        payload = {
            "msg_type": "text",
            "content": {"text": f"TOP明细（{chunk_idx + 1}/{total_chunks}）\n" + "\n".join(lines)},
        }
        post_feishu_payload(payload)


def run_monthly_gain_ranking() -> bool:
    month_input = os.getenv("MONTHLY_STATS_MONTH", "").strip()
    month_std, month_start, month_end = parse_month_input(month_input)
    if not month_std:
        print("❌ 月份格式错误，请使用 YYYYMM 或 YYYY-MM")
        return False
    if month_end > datetime.now().strftime("%Y%m%d"):
        month_end = datetime.now().strftime("%Y%m%d")
    print(f"\n📅 月度涨幅统计：{month_std}（{month_start} ~ {month_end}）\n")
    print(f"🔧 月度统计参数：TOP{max(1, MONTHLY_GAIN_TOP_N)} | 最少交易日 {MONTHLY_GAIN_MIN_TRADING_DAYS}")

    pool = load_a_share_pool()
    if not pool:
        print("❌ 无法加载全A股股票清单")
        return False

    items = [(code, info) for code, info in pool.items() if is_a_share_code(code) and not is_st_stock(code, info.get("name", ""))]
    scan_limit_env = os.getenv("SCAN_LIMIT", "").strip()
    scan_test_mode = os.getenv("SCAN_TEST_MODE", "0").strip() == "1"
    if scan_test_mode and scan_limit_env:
        try:
            scan_limit = max(1, int(scan_limit_env))
            items = items[:scan_limit]
            print(f"🧪 测试模式：仅统计前 {len(items)} 只标的")
        except ValueError:
            pass

    print(f"🔧 统计并发数: {max(1, min(SCAN_WORKERS, 16, len(items) if items else 1))} | 有效标的: {len(items)}")
    results = []
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(SCAN_WORKERS, 16, len(items) if items else 1))) as executor:
        future_map = {
            executor.submit(fetch_monthly_daily_data, code, month_start, month_end): (code, info)
            for code, info in items
        }
        for idx, future in enumerate(as_completed(future_map), 1):
            code, info = future_map[future]
            try:
                df = future.result()
                if df.empty:
                    failed += 1
                    continue
                stat = calculate_month_gain(df, month_start, month_end)
                if not stat:
                    failed += 1
                    continue
                results.append({
                    "code": code,
                    "name": info.get("name", code),
                    "sector": resolve_stock_concept(code, info.get("sector", "未知板块")),
                    "business_summary": str(info.get("business_summary", "")).strip(),
                    **stat,
                })
            except Exception as e:
                failed += 1
                log.debug(f"月度统计异常 {code}: {str(e)}")

    results.sort(key=lambda x: x.get("gain_pct", 0), reverse=True)
    top_n = max(1, MONTHLY_GAIN_TOP_N)
    top_results = results[:top_n]
    print(f"\n🏆 {month_std} 涨幅 TOP{top_n}\n")
    if not top_results:
        print("⚠️  本月没有可展示的涨幅结果，可能是全部标的都被数据源或交易日条件过滤掉了。")
    for idx, item in enumerate(top_results, 1):
        print(
            f"{idx:>3}. {item['name']} ({item['code']}) | 涨幅 {item['gain_pct']:.2f}% | "
            f"起点 {item['base_price']:.2f} ({item['base_source']}) | 终点 {item['end_price']:.2f} | "
            f"交易日 {item['trading_days']} | 板块 {item['sector']} | 业务 {truncate_reason(item.get('business_summary', ''), limit=20) if item.get('business_summary') else '未知'}"
        )

    output_dir = os.path.join(RESULTS_DIR, "monthly_gain")
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{month_std}.json")
    payload = {
        "month": month_std,
        "month_start": month_start,
        "month_end": month_end,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_scanned": len(items),
        "success_count": len(results),
        "failed_count": failed,
        "top_n": top_n,
        "results": top_results,
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已保存月度排行: {output_file}\n")
    print(f"📣 月度排行结果已准备推送飞书（{len(top_results)} 条）")
    send_monthly_gain_to_feishu(month_std, month_start, month_end, len(items), len(results), failed, top_results, top_n)
    return True


