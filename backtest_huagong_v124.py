# -*- coding: utf-8 -*-
"""
V1.24: 华工科技压力位/支撑位回测脚本
测试日期: 2026-06-02 ~ 2026-06-22
优化: 精简evaluate调用次数，专注关键时间点，生成含压力/支撑位的PDF报告
"""
import sys, os

# 清除代理，必须在import requests/tushare之前
os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''
os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''
os.environ['ALL_PROXY'] = ''
os.environ['all_proxy'] = ''
os.environ['NO_PROXY'] = '*'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import os as _os, sys as _sys, json as _json, time as _time, logging as _logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import numpy as np, pandas as pd, requests, urllib.request, urllib.error

shared = {
    '__name__': '__main__', '__file__': __file__,
    'os': _os, 'sys': _sys, 'json': _json, 'time': _time, 'logging': _logging,
    'dataclass': dataclass, 'field': field,
    'datetime': datetime, 'timedelta': timedelta, 'dtime': dtime,
    'Dict': Dict, 'List': List, 'Optional': Optional, 'Any': Any,
    'np': np, 'pd': pd, 'requests': requests,
    'urllib': urllib, 'urllib.request': urllib.request, 'urllib.error': urllib.error,
}

class MockAkshare:
    def __getattr__(self, name):
        return lambda *args, **kwargs: pd.DataFrame()

sys.modules['akshare'] = MockAkshare()
sys.modules['ak'] = MockAkshare()
shared['akshare'] = MockAkshare()
shared['ak'] = MockAkshare()

log = _logging.getLogger("backtest_v124")
log.setLevel(_logging.WARNING)
for h in log.handlers[:]:
    log.removeHandler(h)
log.addHandler(_logging.StreamHandler())
shared['log'] = log

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


def fetch_hist_minute(code, start_date, end_date, pro, retries=3):
    ts_code = f"{code}.SH" if code.startswith(("6", "9", "5")) else f"{code}.SZ"
    start_s = f"{start_date} 09:00:00"
    end_s = f"{end_date} 19:00:00"
    for attempt in range(retries):
        try:
            df = pro.stk_mins(ts_code=ts_code, freq='1min', start_date=start_s, end_date=end_s)
            if df is not None and not df.empty:
                df = df.sort_values('trade_time').reset_index(drop=True)
                for col in ['open', 'close', 'high', 'low', 'vol']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                df['time'] = pd.to_datetime(df['trade_time'])
                df['date'] = df['time'].dt.strftime('%Y-%m-%d')
                df['volume'] = df['vol']
                return df[['time', 'date', 'open', 'high', 'low', 'close', 'volume', 'amount']]
        except Exception as e:
            log.warning(f"tushare fetch {code} {start_date}~{end_date} failed (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                _time.sleep(2)
    return None


def aggregate_daily(minute_df):
    if minute_df is None or minute_df.empty:
        return None
    daily = minute_df.groupby("date").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
    }).reset_index()
    daily = daily.sort_values("date").reset_index(drop=True)
    for col in ["open", "high", "low", "close"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    return daily


def build_daily_ctx(daily_df, date_str):
    if daily_df is None or daily_df.empty:
        return _default_daily_context("000988")
    idx_list = daily_df[daily_df["date"] == date_str].index.tolist()
    if not idx_list:
        return _default_daily_context("000988")
    target_idx = idx_list[0]
    if target_idx < 1:
        return _default_daily_context("000988")
    
    pre_close = float(daily_df.iloc[target_idx - 1]["close"])
    pre2_close = float(daily_df.iloc[target_idx - 2]["close"]) if target_idx >= 2 else 0.0
    
    past_start = max(0, target_idx - 10)
    past_10 = daily_df.iloc[past_start:target_idx]
    high_10d = float(past_10["high"].max()) if not past_10.empty else 0.0
    low_10d = float(past_10["low"].min()) if not past_10.empty else 0.0
    
    close_series = daily_df["close"].iloc[:target_idx + 1]
    ma5 = float(close_series.tail(5).mean()) if len(close_series) >= 5 else 0.0
    ma10 = float(close_series.tail(10).mean()) if len(close_series) >= 10 else 0.0
    ma20 = float(close_series.tail(20).mean()) if len(close_series) >= 20 else 0.0
    ma30 = float(close_series.tail(30).mean()) if len(close_series) >= 30 else 0.0
    ma60 = float(close_series.tail(60).mean()) if len(close_series) >= 60 else 0.0
    ma150 = float(close_series.tail(150).mean()) if len(close_series) >= 150 else 0.0
    
    return {
        "daily_status": "ok", "daily_reason": "minute_aggregated", "daily_asof": date_str,
        "daily_price_ref": float(daily_df.iloc[target_idx]["close"]),
        "daily_prev_close": pre_close, "daily_prev_low": float(daily_df.iloc[target_idx - 1]["low"]),
        "daily_day_ret": (float(daily_df.iloc[target_idx]["close"]) - pre_close) / pre_close if pre_close > 0 else 0.0,
        "daily_ma5": ma5, "daily_ma5_slope": 0.0,
        "daily_above_ma5": float(daily_df.iloc[target_idx]["close"]) > ma5 if ma5 > 0 else False,
        "daily_ma5_gap": (float(daily_df.iloc[target_idx]["close"]) - ma5) / ma5 if ma5 > 0 else 0.0,
        "daily_ma5_state": "unknown",
        "daily_ma10": ma10, "daily_ma20": ma20, "daily_ma30": ma30, "daily_ma60": ma60,
        "daily_ma10_slope": 0.0, "daily_ma20_slope": 0.0, "daily_ma30_slope": 0.0, "daily_ma60_slope": 0.0,
        "daily_trend_bg": "unknown", "daily_gate": "neutral",
        "daily_support_name": "", "daily_support_level": 0.0, "daily_support_gap": 0.0,
        "daily_near_support": False, "daily_pullback_support": False,
        "daily_breakdown_risk": False, "daily_hard_breakdown": False,
        "daily_overheated": False, "daily_ma_clustered": False, "daily_bull_aligned": False,
        "daily_high_10d": high_10d, "daily_low_10d": low_10d,
        "pre2_close": pre2_close, "daily_ma150": ma150,
    }


def run_backtest():
    token = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
    ts.set_token(token)
    pro = ts.pro_api()
    
    global SIM_NOW, MINUTE_FETCH_STATUS, MINUTE_FETCH_DETAIL
    
    code = "000988"
    name = "华工科技"
    
    test_dates = []
    start = pd.to_datetime("2026-06-02")
    end = pd.to_datetime("2026-06-22")
    for d in pd.date_range(start, end):
        d_str = d.strftime("%Y-%m-%d")
        if d.weekday() < 5:
            test_dates.append(d_str)
    
    print(f"[INIT] 测试日期: {test_dates}")
    
    hist_start = "2026-05-01"
    hist_end = "2026-06-22"
    print(f"[FETCH] 获取历史分钟数据 {hist_start} ~ {hist_end} ...")
    t0 = _time.time()
    hist_minute = fetch_hist_minute(code, hist_start, hist_end, pro)
    print(f"[FETCH] 耗时 {_time.time()-t0:.1f}s")
    if hist_minute is None or hist_minute.empty:
        print("[!] 无法获取历史分钟数据，退出")
        return []
    
    hist_daily = aggregate_daily(hist_minute)
    if hist_daily is None or hist_daily.empty:
        print("[!] 无法聚合日线数据，退出")
        return []
    
    print(f"[OK] 历史日线数据: {len(hist_daily)} 天, {hist_daily['date'].min()} ~ {hist_daily['date'].max()}")
    
    holding = {"name": name, "qty": 200, "base": 200, "t_qty": 200, "type": "stock", "account": "账户A", "cost": 207.205, "pre_close": 149.700}
    
    all_results = []
    daily_ps_info = {}  # 记录每日压力/支撑位
    
    for date_str in test_dates:
        print(f"\n{'='*60}")
        print(f"回测 {date_str} {name} ({code})")
        print(f"{'='*60}")
        
        daily_ctx = build_daily_ctx(hist_daily, date_str)
        ps = _calc_ps_levels(daily_ctx.get("daily_price_ref", 0.0), daily_ctx)
        daily_ps_info[date_str] = ps
        print(f"  [日线] 压力: {ps.get('pressure_name','')}={ps.get('pressure_level',0):.2f} | 支撑: {ps.get('support_name','')}={ps.get('support_level',0):.2f} | 卖出比例: {ps.get('sell_qty_pct',100)}%")
        
        day_minute = hist_minute[hist_minute["date"] == date_str].copy()
        if day_minute is None or day_minute.empty:
            print(f"  [SKIP] 无当日分钟数据")
            continue
        
        full_df = add_indicators(day_minute) if 'add_indicators' in globals() else day_minute
        if full_df is None or len(full_df) < 15:
            print(f"  [SKIP] 指标计算失败")
            continue
        
        engine = SignalEngine()
        engine.state_reset_date = date_str.replace("-", "")
        engine.buy_count_per_stock[code] = 0
        engine.sell_count_per_stock[code] = 0
        engine.post_sell_block_until[code] = None
        
        state = {"name": name, "t_qty": 200, "qty": 200, "type": "stock", "cost": 207.205}
        MINUTE_FETCH_STATUS[code] = "ok"
        MINUTE_FETCH_DETAIL[code] = "tushare"
        
        # 关键时间点：早盘/午盘/尾盘，加上用户指定的特殊时间点
        eval_indices = [30, 60, 90, 120, 150, 180, 210, 240]
        # 用户关注的特殊时间点
        if date_str == "2026-06-02":
            # 13:42 → 约index 162 (120+42), 13:55 → 约index 165 (120+55)
            eval_indices += [110, 120, 135, 150, 162, 165, 175, 190, 200]
        if date_str == "2026-06-03":
            # 11:26 前高159.95, 13:11 再次冲击159.83
            eval_indices += [110, 116, 120, 125, 131, 135, 150, 175]
        if date_str == "2026-06-22":
            eval_indices += [100, 120, 135, 150, 160, 170, 180]
        
        eval_indices = [i for i in eval_indices if i < len(full_df)]
        eval_indices = sorted(set(eval_indices))
        
        day_signals = 0
        for i in eval_indices:
            sub = full_df.iloc[:i+1].copy()
            current_time = sub.iloc[-1]["time"]
            if isinstance(current_time, str):
                current_time = pd.to_datetime(current_time)
            if hasattr(current_time, 'to_pydatetime'):
                current_time = current_time.to_pydatetime()
            
            SIM_NOW = current_time
            globals()['SIM_NOW'] = current_time
            
            price = float(sub.iloc[-1]["close"]) if "close" in sub.columns else 0.0
            if price <= 0:
                continue
            
            try:
                buy_score, sell_score, sig = engine.evaluate(code, name, sub, state, daily_ctx=daily_ctx)
            except Exception as e:
                continue
            
            if sig:
                t_val = current_time.hour * 100 + current_time.minute
                notify_threshold = 68 if sig.action in ["BUY_LOW", "ADD_POS"] else (65 if t_val >= 1000 else 75)
                is_notify = sig.score >= notify_threshold
                
                sig_ps = sig.indicators.get("pressure_support", {}) if sig.indicators else {}
                
                result = {
                    "date": date_str, "time": current_time.strftime("%H:%M:%S"),
                    "code": code, "name": name, "action": sig.action,
                    "score": sig.score, "price": price, "reasons": sig.reasons,
                    "notify": is_notify,
                    "pressure_name": sig_ps.get("pressure_name", ""),
                    "pressure_level": sig_ps.get("pressure_level", 0.0),
                    "support_name": sig_ps.get("support_name", ""),
                    "support_level": sig_ps.get("support_level", 0.0),
                    "sell_qty_pct": sig.factors.get("sell_qty_pct", 100) if sig.factors else 100,
                }
                all_results.append(result)
                
                if is_notify:
                    day_signals += 1
                    print(f"  [{current_time.strftime('%H:%M')}] {sig.action} 得分{sig.score:.0f} 价格{price:.2f} | 压力:{sig_ps.get('pressure_name','')}={sig_ps.get('pressure_level',0):.2f} | 卖出比例:{sig.factors.get('sell_qty_pct',100) if sig.factors else 100}%")
        
        if day_signals == 0:
            print(f"  [当日无通知信号]")
    
    # 生成报告（包含所有日期，无论是否有信号）
    generate_pdf_report(all_results, daily_ps_info, name, "2026-06-02", "2026-06-22")
    return all_results


def generate_pdf_report(results, daily_ps_info, stock_name, start_date, end_date):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.units import cm
    
    font_path = "C:/Windows/Fonts/simhei.ttf"
    pdfmetrics.registerFont(TTFont('SimHei', font_path))
    
    styles = getSampleStyleSheet()
    style_title = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontName='SimHei', fontSize=18, alignment=1, spaceAfter=12)
    style_heading = ParagraphStyle('CustomHeading', parent=styles['Heading2'], fontName='SimHei', fontSize=14, spaceAfter=6, spaceBefore=12)
    style_normal = ParagraphStyle('CustomNormal', parent=styles['Normal'], fontName='SimHei', fontSize=10, spaceAfter=6)
    style_small = ParagraphStyle('CustomSmall', parent=styles['Normal'], fontName='SimHei', fontSize=9, spaceAfter=4)
    
    output_path = os.path.join(BASE_DIR, f"pressure_support_report_{stock_name}_{start_date}_{end_date}.pdf")
    doc = SimpleDocTemplate(output_path, pagesize=A4, topMargin=1*cm, bottomMargin=1*cm)
    
    elements = []
    elements.append(Paragraph(f"{stock_name} 压力位/支撑位回测报告 (V1.24)", style_title))
    elements.append(Paragraph(f"测试期间: {start_date} ~ {end_date}", style_normal))
    elements.append(Spacer(1, 0.5*cm))
    
    notify_results = [r for r in results if r["notify"]]
    elements.append(Paragraph(f"总信号数: {len(results)} | 飞书通知信号: {len(notify_results)}", style_normal))
    elements.append(Spacer(1, 0.5*cm))
    
    from collections import defaultdict
    by_date = defaultdict(list)
    for r in results:
        by_date[r["date"]].append(r)
    
    # 按日期排序，显示所有日期的压力/支撑位
    for date_str in sorted(daily_ps_info.keys()):
        ps = daily_ps_info[date_str]
        elements.append(Paragraph(f"{date_str}", style_heading))
        
        ps_text = (f"压力位: {ps.get('pressure_name','无')}={ps.get('pressure_level',0):.2f} | "
                   f"支撑位: {ps.get('support_name','无')}={ps.get('support_level',0):.2f} | "
                   f"卖出比例: {ps.get('sell_qty_pct',100)}%")
        elements.append(Paragraph(ps_text, style_small))
        elements.append(Spacer(1, 0.2*cm))
        
        items = [r for r in by_date.get(date_str, []) if r["notify"]]
        if items:
            table_data = [["时间", "动作", "价格", "得分", "卖出比例", "原因"]]
            for r in items:
                action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(r["action"], r["action"])
                sell_pct = f"{r.get('sell_qty_pct', 100):.0f}%" if r["action"] in ["SELL_HIGH", "PANIC_SELL"] else "-"
                reasons = ", ".join(r["reasons"][:3]) if r["reasons"] else ""
                table_data.append([r["time"], action_cn, f"{r['price']:.2f}", f"{r['score']:.0f}", sell_pct, reasons])
            
            table = Table(table_data, colWidths=[2.5*cm, 1.5*cm, 2*cm, 1.5*cm, 2*cm, 6*cm])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4472C4')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('ALIGN', (5, 0), (5, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, -1), 'SimHei'),
                ('FONTSIZE', (0, 0), (-1, -1), 9),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F2F2F2')]),
            ]))
            elements.append(table)
        else:
            elements.append(Paragraph("当日无通知信号", style_small))
        
        elements.append(Spacer(1, 0.3*cm))
    
    doc.build(elements)
    print(f"\n{'='*60}")
    print(f"PDF报告已生成: {output_path}")
    print(f"{'='*60}")
    return output_path


if __name__ == "__main__":
    run_backtest()
