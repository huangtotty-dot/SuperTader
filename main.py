# -*- coding: utf-8 -*-
"""
做T盯盘脚本主入口（拆分版）
通过共享命名空间加载所有模块，确保原始代码中的跨函数引用无需修改。
"""
import sys, os

# 确保运行路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 预加载所有必要的第三方库到共享命名空间
import os as _os, sys as _sys, json as _json, time as _time, logging as _logging, traceback as _traceback, importlib.util as _importlib_util
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import numpy as np, pandas as pd, requests, urllib.request, urllib.error

# 代理修复（与 config.py 保持一致）
_os.environ['http_proxy'] = ''
_os.environ['https_proxy'] = ''
_os.environ['HTTP_PROXY'] = ''
_os.environ['HTTPS_PROXY'] = ''
_os.environ['ALL_PROXY'] = ''
_os.environ['all_proxy'] = ''

# 共享命名空间：所有模块在此空间中执行，共享所有变量和函数
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

# 尝试导入 akshare（某些环境可能没有，但 t_trader 需要）
try:
    import akshare as ak
    shared['akshare'] = ak
    shared['ak'] = ak
except Exception:
    pass

# V1.11: 日志增强模块导入，加入共享命名空间供 signal_engine 使用
try:
    import log_enhancer as _log_enhancer
    shared['_log_enhancer'] = _log_enhancer
except Exception:
    shared['_log_enhancer'] = None

# 按顺序加载模块：后面的模块可以引用前面的模块
module_order = ['config', 'utils', 'data_fetcher', 'multi_timeframe_fetcher', 'signal_engine', 'preopen', 'market_regime', 'position_sizer']
for mod_name in module_order:
    mod_path = _os.path.join(BASE_DIR, f"{mod_name}.py")
    if not _os.path.exists(mod_path):
        print(f"[WARN] 模块不存在: {mod_path}")
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    # 在共享命名空间中执行模块代码
    exec(compile(code, mod_path, 'exec'), shared)
    try:
        print(f"[OK] 模块已加载: {mod_name}.py")
    except UnicodeEncodeError:
        print(f"[OK] 模块已加载: {mod_name}.py")

# 将共享命名空间中的关键变量暴露到当前模块的 globals，使 main.py 的代码可以运行
globals().update(shared)

# ==================== notify 信号通知（拆分后补充） ====================
def notify(sig, holding):
    """当信号触发时发送飞书通知（V1.14 增强版：含市场状态/组合拳/预计接回价位）"""
    try:
        if not sig or not FEISHU_WEBHOOK:
            return
        action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(sig.action, sig.action)
        title_color = {"BUY_LOW": "🟢", "ADD_POS": "🟢", "SELL_HIGH": "🔴", "PANIC_SELL": "🔴"}.get(sig.action, "⚪")
        title = f"{title_color} 【触发】{action_cn}信号({sig.action}) {sig.name}({sig.code}) 得:{sig.score:.0f}分"
        
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        at_all = feishu_cfg.get("at_all_on_signal", True)
        use_strong = feishu_cfg.get("use_strong_notification", True)
        at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""
        
        card_elements = []
        if at_all:
            card_elements.append({"tag": "div", "text": {"content": at_text, "tag": "lark_md"}})
        card_elements.append({"tag": "div", "text": {"content": title, "tag": "lark_md"}})
        
        # V1.14: 增强通知内容
        reasons_text = "\n".join([f"• {r}" for r in (sig.reasons or [])[:5]])
        vwap = float(sig.indicators.get("vwap", sig.price) or sig.price)
        today_ret = float(sig.indicators.get("today_ret", 0) or 0)
        market_state = str(sig.indicators.get("market_state", "unknown"))
        
        # 【V1.14 新增】市场状态识别
        regime_info = ""
        regime = getattr(sig, "regime", None)
        regime_reason = getattr(sig, "regime_reason", "")
        if regime and regime != "normal":
            regime_info = f"\n🚨 **市场状态**：{regime} | {regime_reason}"
        
        # 【V1.14 新增】组合拳交易摘要
        trade_summary = ""
        code = sig.code
        total_sold = 0
        total_bought = 0
        unrebuilt = 0
        if code in VIRTUAL_TRADES:
            total_sold = sum(t.get("qty", 0) for t in VIRTUAL_TRADES[code].get("SELL_HIGH", []))
            total_bought = sum(t.get("qty", 0) for t in VIRTUAL_TRADES[code].get("BUY_LOW", []))
            unrebuilt = max(0, total_sold - total_bought)
        
        # 建议交易股数
        hold_qty = int(sig.hold_qty or 0)
        total_t = int(holding.get("t_qty", 0) or holding.get("qty", 0) or 0)
        
        advice = f"建议{action_cn} {hold_qty} 股/份"
        
        # ETF显示交易份数
        if holding.get("type") == "etf" and hold_qty > 0:
            pct = hold_qty / total_t * 100 if total_t > 0 else 0
            advice += f"（占总T仓{pct:.0f}%）"
        
        # 组合拳信息
        if action_cn in ["高抛", "恐慌卖出"]:
            if total_sold > 0 or total_bought > 0:
                advice += f"\n📦 本日已卖出 {total_sold} | 已接回 {total_bought} | 未接回 {unrebuilt}"
            if unrebuilt > 0:
                advice += f"\n💡 建议尾盘接回价位：{vwap * 0.992:.2f}（VWAP下方0.8%）"
            else:
                advice += f"\n💡 预计接回价位：{vwap * 0.992:.2f}（VWAP下方0.8%）"
            if today_ret > 0.005:
                advice += f"\n📈 早盘已涨{today_ret*100:.1f}%，建议高抛后等回落接回"
            # 风险提醒
            if regime and regime in ["heavy_sell", "distribution"]:
                advice += f"\n⚠️ 风险：当前处于主力出货/重压状态，建议谨慎接回，尾盘仅接回30%"
        elif action_cn in ["低吸", "加仓"]:
            if unrebuilt > 0:
                advice = f"建议接回 {hold_qty} 股/份（未接回 {unrebuilt}）"
            else:
                advice = f"建议买入 {hold_qty} 股/份（首次加仓/建仓）"
            advice += f"\n💡 参考卖出价位：{vwap * 1.008:.2f}（VWAP上方0.8%）"
            # 风险提醒
            if regime and regime in ["heavy_sell", "distribution"]:
                advice += f"\n⚠️ 风险：当前处于主力出货/重压状态，不建议主动加仓，仅接回已卖出部分"
        
        # 【V1.14 新增】支撑位与决策透明化
        support_info = ""
        nearest_support = sig.indicators.get("nearest_support")
        if nearest_support:
            ns_name = nearest_support.get("name", "")
            ns_level = float(nearest_support.get("level", 0))
            ns_gap = float(nearest_support.get("gap_pct", 0))
            if ns_name and ns_level > 0:
                support_info = f"\n📍 **最近支撑**：{ns_name} {ns_level:.2f}（偏离{ns_gap*100:.2f}%）"
        # 旁路原因
        entry_kind = str(sig.indicators.get("entry_kind", ""))
        open_dip_reason = sig.indicators.get("open_dip_reason", "")
        bypass_info = ""
        if entry_kind == "open_dip_support":
            bypass_info = f"\n⚡ **旁路买入**：{open_dip_reason}"
        
        # 【V1.15 新增】均线压力信息
        ma_resistance_info = ""
        ma_resistance = sig.indicators.get("ma_resistance")
        if ma_resistance:
            pressure_count = ma_resistance.get("pressure_count", 0)
            if pressure_count >= 1:
                pressure_mas = ma_resistance.get("pressure_mas", [])
                pressure_names = "/".join([p.get("name", "") for p in pressure_mas]) if pressure_mas else ""
                is_cluster = ma_resistance.get("is_cluster", False)
                fail_note = ma_resistance.get("fail_note", "")
                cluster_text = " 密集区" if is_cluster else ""
                fail_text = f"，{fail_note}" if fail_note else ""
                ma_resistance_info = f"\n📍 **均线压力**：{pressure_names}{cluster_text}（{pressure_count}条）{fail_text}"
        
        # 【V1.15 新增】均线支撑确认信息（低吸用）
        ma_support_info = ""
        ma_support = sig.indicators.get("ma_support")
        if ma_support:
            ms_name = ma_support.get("name", "")
            ms_level = float(ma_support.get("level", 0))
            if ms_name and ms_level > 0:
                ma_support_info = f"\n📍 **均线支撑确认**：{ms_name} {ms_level:.2f}（冲高回落后站稳，理想低吸）"
        
        content = (
            f"【做T猎手预警】{regime_info}{bypass_info}{support_info}{ma_resistance_info}{ma_support_info}\n"
            f"股票：{sig.name} ({sig.code})\n"
            f"动作：{action_cn}\n"
            f"现价：{sig.price:.2f}\n"
            f"VWAP：{vwap:.2f}\n"
            f"评分：{sig.score:.0f}\n"
            f"市场状态：{market_state}\n"
            f"总T仓：{total_t} 股/份\n\n"
            f"**触发原因**：\n{reasons_text}\n\n"
            f"**操作建议**：\n{advice}"
        )
        card_elements.append({"tag": "div", "text": {"content": content, "tag": "lark_md"}})
        
        payload = {
            "msg_type": "interactive",
            "card": {"config": {"wide_screen_mode": True}, "elements": card_elements},
            "notify_type": 1,
        }
        send_feishu_payload(
            payload=payload,
            success_log=f"✅ 飞书消息已成功送达: {sig.name}({sig.code}) {sig.action} - 加急通知已发送",
            error_prefix="飞书推送",
            trigger_urgent_alarm_after_success=use_strong,
        )
    except Exception as e:
        log.warning(f"⚠️ notify 发送异常: {str(e)[:100]}")

# ==================== V1.25: 早盘预警飞书推送 ====================
def build_alert_card(code, name, alert_level, triggered_rules, morning_stats, oneway_ratio="N/A", avg_decline="N/A"):
    """
    构建早盘预警飞书卡片消息
    alert_level: 0=正常(green) / 1=谨慎(orange) / 2=禁止买入(red)
    """
    level_config = {
        0: {"emoji": "✅", "color": "green", "title": "正常交易", "bg": "efffe8"},
        1: {"emoji": "⚠️", "color": "orange", "title": "【谨慎观望】只做减仓不做加仓", "bg": "fff7e6"},
        2: {"emoji": "🚨", "color": "red", "title": "【禁止买入/清仓】早盘单边下行预警", "bg": "ffebeb"},
    }
    cfg = level_config.get(alert_level, level_config[0])

    rules_text = "\n".join([f"• **{r.get('name', '')}**: {r.get('desc', '')} (历史命中率{r.get('precision', 0)*100:.0f}%)" for r in triggered_rules])

    stats_text = (
        f"| 指标 | 数值 |\n"
        f"|---|---|\n"
        f"| 开盘5分钟 | {morning_stats.get('open_5min_ret', 'N/A')}% |\n"
        f"| 开盘30分钟 | {morning_stats.get('open_30min_ret', 'N/A')}% |\n"
        f"| 最高涨幅 | {morning_stats.get('max_gain_after_open', 'N/A')}% |\n"
        f"| 低于VWAP | {morning_stats.get('below_vwap_ratio', 'N/A')}% |\n"
        f"| 连续阴线 | {morning_stats.get('consecutive_bearish', 'N/A')}根 |"
    )

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": cfg["color"],
                "title": {
                    "tag": "plain_text",
                    "content": f"{cfg['emoji']} 【早盘预警】{name}({code}) — {cfg['title']}"
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**预警级别**: <font color='{cfg['color']}' size=4>**{cfg['title']}**</font>\n\n"
                                   f"**触发规则**:\n{rules_text}\n\n"
                                   f"**早盘统计**:\n{stats_text}\n\n"
                                   f"<font color='grey' size=1>该标的历史单边下行占比: {oneway_ratio}% | 平均跌幅: {avg_decline}%</font>"
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<font color='grey' size=1>📊 基于近两年分钟数据训练 | 模型AUC>0.85</font>"
                    }
                }
            ]
        }
    }
    return card

def notify_alert_cleared(code, name, reason, morning_stats):
    """V1.25: 早盘预警纠正解除通知"""
    try:
        if not FEISHU_WEBHOOK:
            return
        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "green",
                    "title": {
                        "tag": "plain_text",
                        "content": f"✅ 【预警解除】{name}({code}) — 早盘弱势已纠正"
                    }
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**纠正原因**: {reason}\n\n"
                                       f"**早盘统计**: 30分钟涨跌幅 {morning_stats.get('open_30min_ret', 'N/A')}% | "
                                       f"最高涨幅 {morning_stats.get('max_gain_after_open', 'N/A')}%\n\n"
                                       f"<font color='green'>已恢复VWAP深V低吸策略</font>"
                        }
                    }
                ]
            }
        }
        send_feishu_payload(
            payload=card,
            success_log=f"✅ 早盘预警解除已推送: {name}({code})",
            error_prefix="早盘预警解除推送",
        )
    except Exception as e:
        log.warning(f"⚠️ notify_alert_cleared 异常: {str(e)[:100]}")

def send_morning_alert(code, name, alert_level, triggered_rules, morning_stats):
    """V1.25: 发送早盘预警到飞书（独立函数，便于在scan循环中调用）"""
    try:
        if not FEISHU_WEBHOOK:
            return
        # 获取历史统计信息
        oneway_ratio = "N/A"
        avg_decline = "N/A"
        alert_cfg = MORNING_ALERT_PARAMS.get(code, {})
        if alert_cfg:
            # 从第一条level_2规则获取precision信息作为参考
            pass
        card = build_alert_card(code, name, alert_level, triggered_rules, morning_stats, oneway_ratio, avg_decline)
        send_feishu_payload(
            payload=card,
            success_log=f"✅ 早盘预警已推送: {name}({code}) Level={alert_level}",
            error_prefix="早盘预警推送",
        )
    except Exception as e:
        log.warning(f"⚠️ send_morning_alert 异常: {str(e)[:100]}")

# ==================== 主循环函数（从原始 t_trader_v1.10.py lines 4970-5363 提取） ====================

def scan_once():
    global _last_idle_log, _scan_count, _scan_lock
    if _scan_lock:
        log.warning("⚠️ 上一轮扫描仍在进行，跳过本轮触发")
        return

    _scan_lock = True
    try:
        now = _now()
        t = now.time()

        if _is_preopen_monitor_window(now):
            preopen_context = _ensure_preopen_context(force=True)
            if preopen_context is not None:
                _send_preopen_monitor_feishu(preopen_context, now=now)
            if (_now() - _last_idle_log).total_seconds() >= 120:
                log.info("📡 盘前集合竞价监控已刷新")
                _last_idle_log = _now()

        if dtime(14, 55) <= t <= dtime(15, 5): log_eod_summary()

        if now.weekday() >= 5 or t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
            if (_now() - _last_idle_log).total_seconds() >= PARAMS["idle_log_minutes"] * 60:
                log.info("⏸ 非交易时段，进入低频保活")
                _last_idle_log = _now()
            return

        log.info(f"🫀 扫描心跳 第{_scan_count + 1}轮开始")

        if not HOLDINGS:
            return
        preopen_context = _ensure_preopen_context(force=False)
        _scan_count += 1
        panel_rows = []
        minute_issue_stats = {}

        for code, holding in HOLDINGS.items():
            _ensure_ai_review_stats(code, holding)
            dec = _ensure_daily_decision_stats(code, holding)

            try:
                time.sleep(0.5)
                df = fetch_minute_bar(code, is_etf=holding.get("type") == "etf")

                dec["minute_status"] = MINUTE_FETCH_STATUS.get(code, "unknown")
                dec["minute_detail"] = MINUTE_FETCH_DETAIL.get(code, "")
                dec["last_scan_time"] = _now().strftime("%H:%M:%S")

                minute_status = MINUTE_FETCH_STATUS.get(code, "unknown")
                minute_detail = MINUTE_FETCH_DETAIL.get(code, "")
                minute_label = _minute_status_label(minute_status, minute_detail)
                if df.empty:
                    dec["last_status"] = f"分钟线断流({minute_label})"
                    dec["last_status_detail"] = minute_detail
                    panel_rows.append([label(code, holding), "-", "-", "-", "-", f"分钟线断流({minute_label})"])
                    bucket = _minute_issue_bucket(minute_status)
                    minute_issue_stats.setdefault(bucket, {})
                    minute_issue_stats[bucket][minute_label] = minute_issue_stats[bucket].get(minute_label, 0) + 1
                    log.warning(f"⚠️  {label(code, holding)} 分钟线为空 [{minute_label}]")
                    continue
                if minute_status not in {"ok", "cache_hit"}:
                    dec["last_status"] = f"分钟线异常({minute_label})"
                    dec["last_status_detail"] = minute_detail
                    panel_rows.append([label(code, holding), "-", "-", "-", "-", f"分钟线异常({minute_label})"])
                    bucket = _minute_issue_bucket(minute_status)
                    minute_issue_stats.setdefault(bucket, {})
                    minute_issue_stats[bucket][minute_label] = minute_issue_stats[bucket].get(minute_label, 0) + 1
                    log.warning(f"⚠️  {label(code, holding)} 分钟线状态异常 [{minute_label}] {minute_detail}")
                    _append_jsonl(_trace_path("data_quality"), {
                        "fetch_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                        "code": code,
                        "source": "scan_gate",
                        "minute_status": minute_status,
                        "minute_detail": minute_detail,
                        "fetch_cost_ms": 0,
                    })
                    continue

                df = add_indicators(df)
                price = float(df.iloc[-1]["close"]) if "close" in df.columns else 0.0
                vwap = float(df.iloc[-1]["vwap"]) if "vwap" in df.columns else price
                amp = float(df.iloc[-1]["day_amplitude"]) if "day_amplitude" in df.columns else 0.0

                dec["last_price"] = price
                dec["last_vwap"] = vwap
                dec["close_price"] = price
                dec["last_amp"] = amp
                if preopen_context is not None:
                    dec["preopen_market_score"] = preopen_context.market_score
                    dec["preopen_market_bias"] = preopen_context.market_bias
                    dec["preopen_note"] = preopen_context.session_note

                if len(df) < 2:
                    dec["last_status"] = "数据预热"
                    panel_rows.append([label(code, holding), f"{price:.2f}", f"{vwap:.2f}", f"{amp*100:.1f}%", "-", "数据预热"])
                    continue

                can_t = holding.get("t_qty", 0) > 0
                daily_ctx = get_daily_context(code, holding, current_price=price)
                dec["daily_status"] = daily_ctx.get("daily_status", "unknown")
                dec["last_daily_gate"] = daily_ctx.get("daily_gate", "neutral")
                dec["last_daily_trend_bg"] = daily_ctx.get("daily_trend_bg", "unknown")
                dec["last_daily_support"] = daily_ctx.get("daily_support_name", "")
                dec["last_daily_support_gap"] = daily_ctx.get("daily_support_gap", 0.0)
                dec["last_daily_overheated"] = daily_ctx.get("daily_overheated", False)
                buy_score, sell_score, sig = engine.evaluate(code, holding.get("name", code), df, holding, daily_ctx=daily_ctx)

                dec["last_benchmark_code"] = sig.indicators.get("benchmark_code", "") if sig else dec.get("last_benchmark_code", "")
                dec["last_benchmark_name"] = sig.indicators.get("benchmark_name", "") if sig else dec.get("last_benchmark_name", "")
                dec["last_benchmark_state"] = sig.indicators.get("benchmark_state", "unknown") if sig else dec.get("last_benchmark_state", "unknown")
                dec["last_benchmark_gate"] = sig.indicators.get("benchmark_gate", "neutral") if sig else dec.get("last_benchmark_gate", "neutral")
                dec["last_benchmark_reason"] = sig.indicators.get("benchmark_reason", "") if sig else dec.get("last_benchmark_reason", "")

                dec["last_buy_score"] = buy_score
                dec["last_sell_score"] = sell_score

                st = AI_REVIEW_STATS[code]
                st["最大多头分"] = max(st["最大多头分"], buy_score)
                st["最大空头分"] = max(st["最大空头分"], sell_score)
                st["最大振幅"] = max(st["最大振幅"], amp)

                best_score = max(buy_score, sell_score)
                if dec.get("last_stand_down_reason"):
                    stat = f"停手:{dec.get('last_stand_down_reason')}"
                elif engine.cycle_count.get(code, 0) >= PARAMS["max_t_cycles_per_stock"]:
                    stat = "停手:当日轮次已满"
                elif dec.get("last_buy_limit_reason"):
                    stat = f"停手:{dec.get('last_buy_limit_reason')}"
                elif amp < PARAMS['min_amplitude']:
                    stat = "无波待涨"
                elif not can_t:
                    stat = "底仓"
                elif best_score >= 65:
                    stat = "强可T"
                elif best_score >= 45:
                    stat = "可T观察"
                elif best_score >= 25:
                    stat = "弱机会"
                else:
                    stat = "无信号"
                if sig and sig.action in {"BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"}:
                    stat = f"{stat}|{sig.action}"
                dec["last_status"] = stat
                panel_rows.append([label(code, holding), f"{price:.2f}", f"{vwap:.2f}", f"{amp*100:.1f}%", f"多{buy_score}/空{sell_score}", stat])

                _snapshot_write(code, holding, df, {
                    "price": price,
                    "vwap": vwap,
                    "market_state": sig.indicators.get("market_state", dec.get("last_market_state", "unknown")) if sig else dec.get("last_market_state", "unknown"),
                    "benchmark_code": dec.get("last_benchmark_code", ""),
                    "benchmark_name": dec.get("last_benchmark_name", ""),
                    "benchmark_state": dec.get("last_benchmark_state", "unknown"),
                    "benchmark_gate": dec.get("last_benchmark_gate", "neutral"),
                    "benchmark_reason": dec.get("last_benchmark_reason", ""),
                    "preopen_market_score": dec.get("preopen_market_score", 0),
                    "preopen_market_bias": dec.get("preopen_market_bias", "unknown"),
                    "preopen_note": dec.get("preopen_note", ""),
                }, {
                    "action": sig.action,
                    "score": sig.score,
                    "reasons": sig.reasons,
                    "entry_kind": sig.factors.get("entry_kind", "") if sig else "",
                } if sig else None, daily_context=daily_ctx)

                if sig and can_t:
                    # V1.14: 新架构 — 市场状态识别 + 动态份数 + 高抛低吸组合拳
                    # 1. 识别当前市场状态
                    regime = None
                    regime_reason = ""
                    try:
                        from market_regime import detect_regime, MarketRegime
                        regime_obj, regime_reason = detect_regime(
                            code, _now().strftime("%Y-%m-%d"), 
                            preopen_data=preopen_context
                        )
                        regime = regime_obj
                        # 将状态注入 sig，供 notify 使用
                        sig.regime = regime.value
                        sig.regime_reason = regime_reason
                        log.info(f"🎯 {code} 市场状态: {regime.value} | {regime_reason}")
                    except Exception as e:
                        sig.regime = "normal"
                        sig.regime_reason = "状态识别失败"
                        log.debug(f"⚠️  {code} 市场状态识别失败: {e}")
                    
                    # 2. 动态份数计算（个股/ETF统一）
                    try:
                        from position_sizer import calc_sell_qty, calc_buy_qty
                        threshold = float(sig.factors.get("threshold", 35))
                        if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                            dynamic_qty = calc_sell_qty(
                                code, holding, regime, 
                                float(sig.score), threshold,
                                used_sells=engine.sell_count_per_stock.get(code, 0),
                                params=PARAMS,
                                virtual_trades=VIRTUAL_TRADES
                            )
                        else:
                            dynamic_qty = calc_buy_qty(
                                code, holding, regime,
                                float(sig.score), threshold,
                                params=PARAMS,
                                virtual_trades=VIRTUAL_TRADES
                            )
                        if dynamic_qty > 0:
                            sig.hold_qty = dynamic_qty
                            total_t = int(holding.get("t_qty", 0) or holding.get("qty", 0) or 0)
                            pct = dynamic_qty / total_t * 100 if total_t > 0 else 0
                            log.info(f"📊 动态份数 {code}: 状态={regime.value if regime else 'normal'} 信号强度{sig.score:.0f}/阈值{threshold:.0f}, 建议交易{dynamic_qty}股/份 ({pct:.0f}%)")
                    except Exception as e:
                        log.warning(f"⚠️  动态份数计算失败 {code}: {e}")
                    
                    # 3. 信号分数门槛 + 通知
                    # 早盘加时：9:30-10:00 设 75 分门槛，10:00 后 65 分
                    # 低吸额外：无论什么时间都 68 分
                    if sig.action in ["BUY_LOW", "ADD_POS"]:
                        notify_threshold = 68
                    else:
                        # 卖信号
                        if t >= dtime(10, 0):
                            notify_threshold = 65
                        else:
                            notify_threshold = 75
                    
                    if sig.score >= notify_threshold:
                        notify(sig, holding)
                    else:
                        action_type = "买入" if sig.action in ["BUY_LOW", "ADD_POS"] else "卖出"
                        time_window = "10:00前" if t < dtime(10, 0) else "10:00后"
                        log.info(f"📉 {code} {action_type}信号得分{sig.score:.0f}分，低于{time_window}阈值{notify_threshold}分，静默处理（不推送飞书）")
                    engine.record_signal(code, sig.action, sig.price, sig.score)
                    engine.record_trade_action(code, sig.action, sig.hold_qty)
                    if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                        engine.cycle_count[code] = engine.cycle_count.get(code, 0) + 1

                # V1.14: 尾盘强制平仓已删除（用户反馈不需要）

            except Exception as e:
                log.warning(f"⚠️  {label(code, holding)} 扫描异常: {str(e)[:120]}")
                continue

        if _scan_count % 4 == 1:
            lines = [f"\n📊 护城河防御面板 第{_scan_count}轮\n" + "─"*70]
            for r in panel_rows:
                lines.append(f"{r[0]:<16}{r[1]:>8}{r[2]:>10}{r[3]:>8} {r[4]:>10}  {r[5]:<8}")
            log.info("\n".join(lines))
    finally:
        _scan_lock = False


def replay_today():
    global T_MODE
    T_MODE = load_t_mode()
    shared['T_MODE'] = T_MODE
    today = get_today_str()
    snapshot_files = []
    snapshot_days = set()
    for root, _, files in os.walk(SNAPSHOT_DIR):
        for name in files:
            if not name.endswith(".json") or "_" not in name:
                continue
            day_part = name.rsplit("_", 1)[-1].removesuffix(".json")
            snapshot_days.add(day_part)
            if day_part == today:
                snapshot_files.append(os.path.join(root, name))
    if not snapshot_files:
        if not snapshot_days:
            log.info(f"未找到当日快照: {today}")
            return
        today = sorted(snapshot_days)[-1]
        snapshot_files = []
        for root, _, files in os.walk(SNAPSHOT_DIR):
            for name in files:
                if name.endswith(f"_{today}.json"):
                    snapshot_files.append(os.path.join(root, name))
        log.info(f"未找到今日快照，改用最近快照日: {today}")

    HOLDINGS_LOCAL = load_holdings()
    stats = {"total": 0, "buy_ok": 0, "sell_ok": 0, "rebuild_buy_ok": 0, "buy_blocked": 0, "sell_blocked": 0, "buy_block_by_reason": {}, "sell_block_by_reason": {}, "preempt_by_sell_fast_path": 0, "buy_candidate_but_rejected": 0, "buy_candidate_preheat": 0, "buy_candidate_preheat_rejected": 0, "by_code": {}}
    global SIM_NOW
    prev_sim_now = SIM_NOW
    try:
        for path in sorted(snapshot_files):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    snap = json.load(f)
            except Exception as e:
                log.warning(f"⚠️  快照加载异常: {str(e)[:120]}")
                continue

            code = str(snap.get("code", "")).strip()
            if not code:
                continue
            bars = snap.get("bars", []) if isinstance(snap, dict) else []
            if not bars:
                continue

            holding = HOLDINGS_LOCAL.get(code, {"name": snap.get("name", code), "t_qty": 0, "qty": 0, "type": "stock", "cost": 0})
            state = {
                "name": snap.get("name", code),
                "t_qty": int(holding.get("t_qty") or holding.get("qty") or 0),
                "qty": int(holding.get("qty") or holding.get("t_qty") or 0),
                "type": holding.get("type", "stock"),
                "cost": float(holding.get("cost") or 0),
            }

            engine_local = SignalEngine()
            engine_local.state_reset_date = today
            engine_local.buy_count_per_stock[code] = 0
            engine_local.sell_count_per_stock[code] = 0
            engine_local.post_sell_block_until[code] = None
            got_buy = False
            got_sell = False
            code_stats = {"buy_ok": 0, "sell_ok": 0, "rebuild_buy_ok": 0, "buy_blocked": 0, "sell_blocked": 0, "buy_block_by_reason": {}, "sell_block_by_reason": {}, "preempt_by_sell_fast_path": 0, "buy_candidate_but_rejected": 0, "buy_candidate_preheat": 0, "buy_candidate_preheat_rejected": 0}
            stats["total"] += 1
            MINUTE_FETCH_STATUS[code] = "ok"

            for i in range(25, len(bars) + 1):
                df = pd.DataFrame(bars[:i])
                if df.empty:
                    continue
                df["time"] = pd.to_datetime(df["time"], errors="coerce")
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["time", "open", "high", "low", "close"]).reset_index(drop=True)
                if df.empty or len(df) < 25:
                    continue

                try:
                    current_time = df.iloc[-1]["time"]
                    if hasattr(current_time, "to_pydatetime"):
                        SIM_NOW = current_time.to_pydatetime()
                    elif isinstance(current_time, datetime):
                        SIM_NOW = current_time
                    daily_ctx = snap.get("daily_context") if isinstance(snap, dict) else None
                    if not isinstance(daily_ctx, dict):
                        daily_ctx = _default_daily_context(code, status="replay_missing", reason="snapshot missing daily_context")
                    buy_score, sell_score, sig = engine_local.evaluate(code, snap.get("name", code), add_indicators(df), state, daily_ctx=daily_ctx)
                except Exception:
                    continue

                if sig and sig.action in ["BUY_LOW", "ADD_POS"]:
                    got_buy = True
                    stats["buy_ok"] += 1
                    code_stats["buy_ok"] += 1
                    if engine_local.post_sell_block_until.get(code):
                        stats["rebuild_buy_ok"] += 1
                        code_stats["rebuild_buy_ok"] += 1
                    engine_local.record_trade_action(code, sig.action, sig.hold_qty)
                elif sig and sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                    got_sell = True
                    stats["sell_ok"] += 1
                    code_stats["sell_ok"] += 1
                    engine_local.record_trade_action(code, sig.action, sig.hold_qty)
                else:
                    diag = getattr(engine_local, "diagnostics", {}).get(code, {}) if isinstance(getattr(engine_local, "diagnostics", None), dict) else {}
                    if diag.get("buy_candidate_preheat") and sig is None:
                        stats["buy_candidate_preheat_rejected"] += 1
                        code_stats["buy_candidate_preheat_rejected"] += 1
                    if diag.get("buy_candidate") and sig is None:
                        stats["buy_candidate_but_rejected"] += 1
                        code_stats["buy_candidate_but_rejected"] += 1
                        for reason in diag.get("buy_block_reasons", []) or ["unknown"]:
                            stats["buy_block_by_reason"][reason] = stats["buy_block_by_reason"].get(reason, 0) + 1
                            code_stats["buy_block_by_reason"][reason] = code_stats["buy_block_by_reason"].get(reason, 0) + 1
                    if diag.get("sell_candidate") and sig is None:
                        for reason in diag.get("sell_block_reasons", []) or ["unknown"]:
                            stats["sell_block_by_reason"][reason] = stats["sell_block_by_reason"].get(reason, 0) + 1
                            code_stats["sell_block_by_reason"][reason] = code_stats["sell_block_by_reason"].get(reason, 0) + 1
                    if diag.get("preempted_by_sell_fast_path"):
                        stats["preempt_by_sell_fast_path"] += 1
                        code_stats["preempt_by_sell_fast_path"] += 1

            if not got_buy:
                stats["buy_blocked"] += 1
                code_stats["buy_blocked"] += 1
            if not got_sell:
                stats["sell_blocked"] += 1
                code_stats["sell_blocked"] += 1
            stats["by_code"][code] = code_stats
    finally:
        SIM_NOW = prev_sim_now

    out = os.path.join(TRACE_DIR, f"replay_compare_{today}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"), "stats": stats}, f, ensure_ascii=False, indent=2)
    log.info(f"回放完成: {out}")
    log.info(f"总快照={stats['total']} 买触发={stats['buy_ok']} 卖触发={stats['sell_ok']} 卖后可买回={stats['rebuild_buy_ok']} 买被挡={stats['buy_blocked']} 卖被挡={stats['sell_blocked']} 买候选预热未成={stats['buy_candidate_preheat_rejected']} 买候选未成交={stats['buy_candidate_but_rejected']} 卖快路径抢占={stats['preempt_by_sell_fast_path']}")
    if stats["buy_block_by_reason"]:
        log.info("买阻塞原因: " + ", ".join(f"{k}:{v}" for k, v in sorted(stats["buy_block_by_reason"].items(), key=lambda kv: -kv[1])[:8]))
    if stats["sell_block_by_reason"]:
        log.info("卖阻塞原因: " + ", ".join(f"{k}:{v}" for k, v in sorted(stats["sell_block_by_reason"].items(), key=lambda kv: -kv[1])[:8]))
    if stats.get("by_code"):
        try:
            with open(out, "r", encoding="utf-8") as f:
                replay_doc = json.load(f)
        except Exception:
            replay_doc = {"generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"), "stats": stats}
        replay_doc["stats"]["by_code"] = stats["by_code"]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(replay_doc, f, ensure_ascii=False, indent=2)
    else:
        log.info("回放未产生按标的统计，自动学习跳过")
    _apply_replay_learning(today)


def tushare_replay():
    """
    V1.19: 使用 Tushare 分钟数据复测今日表现
    仅输出会触发飞书通知的信号（score >= notify_threshold）
    """
    import tushare as ts
    token = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
    ts.set_token(token)
    pro = ts.pro_api()

    global SIM_NOW, HOLDINGS, MINUTE_FETCH_STATUS, MINUTE_FETCH_DETAIL
    global DAILY_DECISION_STATS, AI_REVIEW_STATS, SIGNAL_OUTCOME_TRACKER, T_MODE

    today = get_today_str()
    HOLDINGS = load_holdings()
    shared['HOLDINGS'] = HOLDINGS  # V1.19: 更新共享命名空间中的HOLDINGS
    T_MODE = load_t_mode()
    shared['T_MODE'] = T_MODE
    holdings = HOLDINGS
    
    results = []
    
    for code, holding in holdings.items():
        # 转换代码为 tushare 格式（去除 _A/_B 等账户后缀）
        api_code = code.split("_")[0] if "_" in code else code
        if api_code.startswith(("6", "9", "5")):
            ts_code = f"{api_code}.SH"
        else:
            ts_code = f"{api_code}.SZ"
        
        try:
            df = pro.stk_mins(ts_code=ts_code, freq='1min', 
                              start_date=f"{today} 09:00:00", 
                              end_date=f"{today} 19:00:00")
            if df is None or df.empty:
                print(f"[WARN] {code} 无分钟数据")
                continue
        except Exception as e:
            print(f"[WARN] {code} 获取失败: {e}")
            continue
        
        # 转换列名
        df = df.rename(columns={
            'trade_time': 'time',
            'vol': 'volume',
            'amount': 'amount'
        })
        df['time'] = pd.to_datetime(df['time'])
        df['date'] = df['time'].dt.date
        df = df.sort_values('time').reset_index(drop=True)
        
        # 确保列类型正确
        for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 添加指标
        df = add_indicators(df)
        
        # 模拟状态
        state = {
            "name": holding.get("name", code),
            "t_qty": int(holding.get("t_qty") or holding.get("qty") or 0),
            "qty": int(holding.get("qty") or holding.get("t_qty") or 0),
            "type": holding.get("type", "stock"),
            "cost": float(holding.get("cost") or 0),
        }
        
        # 初始化引擎
        engine = SignalEngine()
        engine.state_reset_date = today
        engine.buy_count_per_stock[code] = 0
        engine.sell_count_per_stock[code] = 0
        engine.post_sell_block_until[code] = None
        
        # 初始化统计
        DAILY_DECISION_STATS[code] = _ensure_daily_decision_stats(code, holding)
        AI_REVIEW_STATS[code] = _ensure_ai_review_stats(code, holding)
        
        # 模拟逐分钟
        for i in range(25, len(df) + 1):
            sub_df = df.iloc[:i].copy()
            if len(sub_df) < 25:
                continue
            
            # 设置模拟时间
            current_time = sub_df.iloc[-1]["time"]
            if hasattr(current_time, "to_pydatetime"):
                SIM_NOW = current_time.to_pydatetime()
            else:
                SIM_NOW = current_time
            
            t_val = SIM_NOW.hour * 100 + SIM_NOW.minute
            
            # 设置分钟状态
            MINUTE_FETCH_STATUS[code] = "ok"
            MINUTE_FETCH_DETAIL[code] = "tushare"
            
            # 获取 daily_ctx（简单版）
            daily_ctx = _default_daily_context(code)
            
            try:
                buy_score, sell_score, sig = engine.evaluate(
                    code, holding.get("name", code), sub_df, state, daily_ctx=daily_ctx
                )
            except Exception as e:
                print(f"[WARN] {code} {SIM_NOW.strftime('%H:%M')} evaluate 失败: {e}")
                continue
            
            if sig and sig.action in ["BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"]:
                # 计算 notify_threshold
                if sig.action in ["BUY_LOW", "ADD_POS"]:
                    notify_threshold = 68
                else:
                    if t_val >= 1000:
                        notify_threshold = 65
                    else:
                        notify_threshold = 75
                
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
    report_lines.append(f"")
    report_lines.append(f"## 总信号统计")
    report_lines.append(f"- 总信号数: {len(results)}")
    report_lines.append(f"- 飞书通知信号数: {sum(1 for r in results if r['notify'])}")
    report_lines.append(f"")
    
    # 按代码分组
    notify_results = [r for r in results if r["notify"]]
    by_code = {}
    for r in notify_results:
        by_code.setdefault(r["code"], []).append(r)
    
    for code in sorted(by_code.keys()):
        items = by_code[code]
        report_lines.append(f"## {items[0]['name']} ({code})")
        report_lines.append(f"")
        for item in items:
            action_cn = {"BUY_LOW": "🟢 低吸", "ADD_POS": "🟢 加仓", "SELL_HIGH": "🔴 高抛", "PANIC_SELL": "🔴 恐慌卖出"}.get(item["action"], item["action"])
            report_lines.append(f"### {item['time']} {action_cn}")
            report_lines.append(f"- 价格: {item['price']:.2f}")
            report_lines.append(f"- 得分: {item['score']:.0f}")
            report_lines.append(f"- VWAP: {item['vwap']:.2f}")
            report_lines.append(f"- 原因: {', '.join(item['reasons'][:5])}")
            report_lines.append(f"")
        report_lines.append(f"---")
        report_lines.append(f"")
    
    # 非通知信号（简要）
    non_notify = [r for r in results if not r["notify"]]
    if non_notify:
        report_lines.append(f"## 未达通知阈值信号（简要）")
        report_lines.append(f"")
        for item in non_notify[:20]:
            action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(item["action"], item["action"])
            report_lines.append(f"- {item['time']} {item['name']} {action_cn} 得分{item['score']:.0f} (阈值未达)")
        report_lines.append(f"")
    
    report_text = "\n".join(report_lines)
    report_path = os.path.join(TRACE_DIR, f"tushare_replay_report_{today}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    
    print(f"\n{'='*60}")
    print(f"报告已保存: {report_path}")
    print(f"总信号: {len(results)} | 飞书通知: {len(notify_results)}")
    print(f"{'='*60}")
    
    # 打印飞书命令摘要
    print(f"\n【飞书通知摘要】")
    for item in notify_results:
        action_cn = {"BUY_LOW": "低吸", "ADD_POS": "加仓", "SELL_HIGH": "高抛", "PANIC_SELL": "恐慌卖出"}.get(item["action"], item["action"])
        print(f"{item['time']} {item['name']}({item['code']}) {action_cn} 得分{item['score']:.0f} 价格{item['price']:.2f}")
    
    return report_path


def _prompt_t_mode_selection(holdings, t_mode):
    """V1.26: 启动时提示用户为每只股票选择正T/反T模式"""
    try:
        import builtins
        input_fn = builtins.input
    except Exception:
        input_fn = lambda x: ""

    mode_names = {"long": "正T(先买后卖)", "short": "反T(先卖后买)"}
    updated = False

    print("\n" + "="*60)
    print("【V1.26 T模式选择】为每只股票选择做T策略")
    print("  l = 正T(long): 先买后卖，适合震荡/上涨趋势")
    print("  s = 反T(short): 先卖后买，适合下跌趋势")
    print("  直接回车 = 保持当前设置")
    print("="*60)

    for code, holding in holdings.items():
        name = holding.get("name", code)
        current = t_mode.get(code, "long")
        prompt = f"  {name}({code}) [{mode_names.get(current, current)}] 选择(l/s/回车): "
        try:
            choice = input_fn(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if choice in ("l", "long", "正", "正t"):
            t_mode[code] = "long"
            updated = True
            print(f"    → 设置为: 正T(先买后卖)")
        elif choice in ("s", "short", "反", "反t"):
            t_mode[code] = "short"
            updated = True
            print(f"    → 设置为: 反T(先卖后买)")
        else:
            print(f"    → 保持: {mode_names.get(current, current)}")

    if updated and 'save_t_mode' in globals():
        save_t_mode(t_mode)
        print("\n✅ T模式已保存到 t_mode.json")
    print("="*60 + "\n")


def run_watch():
    global HOLDINGS, engine, T_MODE
    HOLDINGS = load_holdings()
    shared['HOLDINGS'] = HOLDINGS  # V1.12: 更新共享命名空间中的HOLDINGS，供signal_engine使用

    # V1.26: 加载T模式配置并提示选择
    T_MODE = load_t_mode()
    shared['T_MODE'] = T_MODE
    _prompt_t_mode_selection(HOLDINGS, T_MODE)

    _ensure_preopen_context(force=True)
    engine = SignalEngine()
    log.info("========= 做T终极护城河防御版 (V1.26 正T/反T模式切换版) 启动 =========")
    if PREOPEN_CONTEXT is not None:
        log.info(_format_preopen_brief(PREOPEN_CONTEXT))
    log.info(f"飞书推送: {'✓ 已启用' if FEISHU_WEBHOOK else '✗ 未配置'}")
    log.info(f"飞书关键词: {FEISHU_KEYWORD}")
    if FEISHU_WEBHOOK:
        log.info(f"飞书Webhook: {FEISHU_WEBHOOK[:55]}...")

    # 【2026-06-12 加仓策略】显示在启动日志
    log.info("═" * 70)
    log.info("【2026-06-12 加仓策略执行指南】")
    log.info("  1️⃣ 中国巨石 600176 — ❌ 不追 (已涨5.62%, 资金不足)")
    log.info("  2️⃣ 科创50ETF 588000 — ⚡ 分批加仓 (先加¥15,000)")
    log.info("     └─ 第一批: ¥15,000 (约8,200份)")
    log.info("     └─ 第二批: ¥15,000 (等待盘中回落)")
    log.info("  3️⃣ 中信银行 601998 — ✅ 按计划加 (¥15,000, 约1,900股)")
    log.info("  4️⃣ 特变电工 600089 — ⚡ 加仓 (¥25,000, 约1,090股)")
    log.info("═" * 70)

    if SYS_ALERT_AVAILABLE:
        init_alert(enabled=True)
        log.info("🔊 系统高级报警音效已成功【全自动挂载】！准备执行听声辨位！")
    else:
        log.warning("⚠️ 目录下未检测到 system_alert_v17.3.py，高级报警音效静默禁用。")

    cleanup_expired_minute_cache()
    if should_run_startup_self_test():
        send_startup_self_test()
    log.info(f"⏱ 采用顺序轮询模式：每轮扫描结束后再等待 {PARAMS['poll_interval']} 秒（V1.8 确认型收敛）")

    try:
        while True:
            cycle_start = _now()
            scan_once()
            elapsed = (_now() - cycle_start).total_seconds()
            sleep_seconds = max(0, PARAMS["poll_interval"])
            log.debug(f"⏳ 本轮耗时 {elapsed:.1f}s，等待 {sleep_seconds}s 后进入下一轮")
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        log.info("已停止盯盘")
    except Exception as e:
        log.error(f"❌ 盯盘主循环异常: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--replay-today":
        replay_today()
    elif len(sys.argv) > 1 and sys.argv[1] == "--tushare-replay":
        tushare_replay()
    else:
        run_watch()
