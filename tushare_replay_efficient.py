# -*- coding: utf-8 -*-
"""
V1.19: 高效 Tushare 复测脚本
每5分钟运行一次 evaluate，避免逐分钟的高开销
"""
import sys, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 加载共享命名空间（复制 main.py 的加载逻辑）
import os as _os, sys as _sys, json as _json, time as _time, logging as _logging, traceback as _traceback, importlib.util as _importlib_util
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import numpy as np, pandas as pd, requests, urllib.request, urllib.error

shared = {
    '__name__': '__main__',
    '__file__': __file__,
    'os': _os,
    'sys': _sys,
    'json': _json,
    'time': _time,
    'logging': _logging,
    'traceback': _traceback,
    'importlib': _importlib_util,
    'importlib.util': _importlib_util,
    'dataclass': dataclass,
    'field': field,
    'datetime': datetime,
    'timedelta': timedelta,
    'dtime': dtime,
    'Dict': Dict,
    'List': List,
    'Optional': Optional,
    'Any': Any,
    'np': np,
    'pd': pd,
    'requests': requests,
    'urllib': urllib,
    'urllib.request': urllib.request,
    'urllib.error': urllib.error,
}

try:
    import akshare as ak
    shared['akshare'] = ak
    shared['ak'] = ak
except Exception:
    pass

try:
    import log_enhancer as _log_enhancer
    shared['_log_enhancer'] = _log_enhancer
except Exception:
    shared['_log_enhancer'] = None

module_order = ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']
for mod_name in module_order:
    mod_path = _os.path.join(BASE_DIR, f"{mod_name}.py")
    if not _os.path.exists(mod_path):
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    exec(compile(code, mod_path, 'exec'), shared)

globals().update(shared)

import tushare as ts


def main():
    token = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
    ts.set_token(token)
    pro = ts.pro_api()

    global SIM_NOW, HOLDINGS, MINUTE_FETCH_STATUS, MINUTE_FETCH_DETAIL
    global DAILY_DECISION_STATS, AI_REVIEW_STATS, SIGNAL_OUTCOME_TRACKER

    today = get_today_str()
    HOLDINGS = load_holdings()
    shared['HOLDINGS'] = HOLDINGS
    holdings = HOLDINGS

    results = []

    for code, holding in holdings.items():
        if code.startswith(("6", "9", "5")):
            ts_code = f"{code}.SH"
        else:
            ts_code = f"{code}.SZ"

        try:
            df = pro.stk_mins(ts_code=ts_code, freq='1min',
                              start_date=f"{today} 09:00:00",
                              end_date=f"{today} 19:00:00")
            if df is None or df.empty:
                print(f"[!] {code} no data")
                continue
        except Exception as e:
            print(f"[!] {code} fetch failed: {e}")
            continue

        df = df.rename(columns={'trade_time': 'time', 'vol': 'volume', 'amount': 'amount'})
        df['time'] = pd.to_datetime(df['time'])
        df['date'] = df['time'].dt.date
        df = df.sort_values('time').reset_index(drop=True)

        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        df = add_indicators(df)

        state = {
            "name": holding.get("name", code),
            "t_qty": int(holding.get("t_qty") or holding.get("qty") or 0),
            "qty": int(holding.get("qty") or holding.get("t_qty") or 0),
            "type": holding.get("type", "stock"),
            "cost": float(holding.get("cost") or 0),
        }

        engine = SignalEngine()
        engine.state_reset_date = today
        engine.buy_count_per_stock[code] = 0
        engine.sell_count_per_stock[code] = 0
        engine.post_sell_block_until[code] = None

        DAILY_DECISION_STATS[code] = _ensure_daily_decision_stats(code, holding)
        AI_REVIEW_STATS[code] = _ensure_ai_review_stats(code, holding)

        # 每5分钟运行一次，从第15根开始（V1.19需要120根用于趋势检测，但15根即可运行基础评估）
        # 为捕捉早盘09:30-10:00信号，加入前120根
        # V1.19fix: 前30分钟每分钟运行，之后每5分钟
        early_indices = list(range(4, 15, 1)) + list(range(15, 30, 2)) if len(df) >= 4 else []
        later_indices = list(range(30, min(120, len(df)), 5)) + list(range(120, len(df) + 1, 5))
        indices = early_indices + later_indices
        if len(df) not in indices:
            indices.append(len(df))

        for i in indices:
            sub_df = df.iloc[:i].copy()
            current_time = sub_df.iloc[-1]["time"]
            if hasattr(current_time, "to_pydatetime"):
                SIM_NOW = current_time.to_pydatetime()
            else:
                SIM_NOW = current_time

            t_val = SIM_NOW.hour * 100 + SIM_NOW.minute
            
            # Debug for 特变电工
            if code == "600089" and t_val == 944:
                print(f"[DEBUG] 600089 at {SIM_NOW.strftime('%H:%M')}, i={i}, len(sub_df)={len(sub_df)}")

            MINUTE_FETCH_STATUS[code] = "ok"
            MINUTE_FETCH_DETAIL[code] = "tushare"

            daily_ctx = _default_daily_context(code)

            try:
                buy_score, sell_score, sig = engine.evaluate(
                    code, holding.get("name", code), sub_df, state, daily_ctx=daily_ctx
                )
            except Exception as e:
                print(f"[!] {code} {SIM_NOW.strftime('%H:%M')} evaluate failed: {e}")
                continue
            
            # Debug for 特变电工
            if code == "600089" and t_val == 944:
                print(f"[DEBUG] 600089 result: buy_score={buy_score}, sell_score={sell_score}, sig={sig.action if sig else 'None'}")
                if sig:
                    print(f"[DEBUG] 600089 sig.score={sig.score}, sig.reasons={sig.reasons}")

            if sig and sig.action in ["BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"]:
                if sig.action in ["BUY_LOW", "ADD_POS"]:
                    notify_threshold = 68
                else:
                    notify_threshold = 65 if t_val >= 1000 else 75

                result = {
                    "time": SIM_NOW.strftime("%H:%M:%S"),
                    "code": code,
                    "name": holding.get("name", code),
                    "action": sig.action,
                    "score": sig.score,
                    "price": sig.price,
                    "reasons": sig.reasons,
                    "notify": sig.score >= notify_threshold,
                    "vwap": float(sub_df.iloc[-1]["vwap"]) if "vwap" in sub_df.columns else sig.price,
                }
                results.append(result)

                if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                    engine.record_trade_action(code, sig.action, sig.hold_qty)
                elif sig.action in ["BUY_LOW", "ADD_POS"]:
                    engine.record_trade_action(code, sig.action, sig.hold_qty)

    # 生成报告
    report_lines = []
    report_lines.append(f"# Tushare 复测报告 ({today})")
    report_lines.append(f"## 版本: V1.19")
    report_lines.append("")
    report_lines.append(f"## 总信号统计")
    report_lines.append(f"- 总信号数: {len(results)}")
    report_lines.append(f"- 飞书通知信号数: {sum(1 for r in results if r['notify'])}")
    report_lines.append("")

    notify_results = [r for r in results if r["notify"]]
    by_code = {}
    for r in notify_results:
        by_code.setdefault(r["code"], []).append(r)

    for code in sorted(by_code.keys()):
        items = by_code[code]
        report_lines.append(f"## {items[0]['name']} ({code})")
        report_lines.append("")
        for item in items:
            action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(item["action"], item["action"])
            report_lines.append(f"### {item['time']} {action_cn}")
            report_lines.append(f"- 价格: {item['price']:.2f}")
            report_lines.append(f"- 得分: {item['score']:.0f}")
            report_lines.append(f"- VWAP: {item['vwap']:.2f}")
            report_lines.append(f"- 原因: {', '.join(item['reasons'][:5])}")
            report_lines.append("")
        report_lines.append("---")
        report_lines.append("")

    non_notify = [r for r in results if not r["notify"]]
    if non_notify:
        report_lines.append(f"## 未达通知阈值信号（简要）")
        report_lines.append("")
        for item in non_notify[:20]:
            action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(item["action"], item["action"])
            report_lines.append(f"- {item['time']} {item['name']} {action_cn} 得分{item['score']:.0f} (阈值未达)")
        report_lines.append("")

    report_text = "\n".join(report_lines)
    report_path = os.path.join(TRACE_DIR, f"tushare_replay_report_{today}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(f"\n{'='*60}")
    print(f"报告已保存: {report_path}")
    print(f"总信号: {len(results)} | 飞书通知: {len(notify_results)}")
    print(f"{'='*60}")

    print(f"\n[飞书通知摘要]")
    for item in notify_results:
        action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(item["action"], item["action"])
        print(f"{item['time']} {item['name']}({item['code']}) {action_cn} 得分{item['score']:.0f} 价格{item['price']:.2f}")

    return report_path


if __name__ == "__main__":
    main()
