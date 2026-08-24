# -*- coding: utf-8 -*-
"""
做T盯盘脚本主入口（拆分版）
通过共享命名空间加载所有模块，确保原始代码中的跨函数引用无需修改。
"""
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 确保运行路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# 预加载所有必要的第三方库到共享命名空间
import os as _os, sys as _sys, json as _json, time as _time, logging as _logging, traceback as _traceback, importlib.util as _importlib_util
import urllib.request as _urllib_request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import numpy as np, pandas as pd, requests, urllib.request, urllib.error

# 代理修复（与 config.py 保持一致）
for _k in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','all_proxy']:
    _os.environ.pop(_k, None)
_os.environ['NO_PROXY'] = '*'; _os.environ['no_proxy'] = '*'
# 强制 urllib 全局无代理
_urllib_request.install_opener(_urllib_request.build_opener(_urllib_request.ProxyHandler({})))
# 强制 requests 全局无代理（trust_env=False 覆盖 Windows IE 系统代理）
import requests as _mreq
_MREQ_SESSION = _mreq.Session(); _MREQ_SESSION.trust_env = False
_mreq.post = lambda url, **kw: _MREQ_SESSION.request('POST', url, **kw)
_mreq.get = lambda url, **kw: _MREQ_SESSION.request('GET', url, **kw)

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

# 按顺序加载模块：后面的模块可以引用前面的模块
module_order = ['config', 'utils', 'data_fetcher', 'indicators', 'signal_engine', 'auction_analyzer', 'preopen', 'support_resistance', 'index_regime', 'index_regime_intraday', 'market_regime', 'position_sizer', 'daily_sentiment']
for mod_name in module_order:
    mod_path = _os.path.join(BASE_DIR, f"{mod_name}.py")
    if not _os.path.exists(mod_path):
        print(f"[WARN] 模块不存在: {mod_path}")
        continue
    with open(mod_path, 'r', encoding='utf-8') as f:
        code = f.read()
    # 在共享命名空间中执行模块代码
    # exec 期间把 __name__ 置为模块名，防止模块内 if __name__ == "__main__" 守卫
    # （如 index_regime / index_regime_intraday 的 CLI）在加载时被误触发
    shared['__name__'] = mod_name
    exec(compile(code, mod_path, 'exec'), shared)
    try:
        print(f"[OK] 模块已加载: {mod_name}.py")
    except UnicodeEncodeError:
        print(f"[OK] 模块已加载: {mod_name}.py")
shared['__name__'] = '__main__'  # 恢复：main.py 尾部自身的 __main__ 启动守卫依赖该值

# ── 建仓信号扫描（收盘后自动执行）──
try:
    from position_builder import run_position_scan as _run_position_scan
    from position_builder import push_summary_feishu as _push_summary_feishu
except Exception as _e:
    _run_position_scan = None
    _push_summary_feishu = None
    print(f"[WARN] position_builder 加载失败（建仓扫描不可用）: {_e}")

# ── 指数5分钟共振过滤（做T信号，2026-08-14 新增）──
# 回测（--tushare-replay）不调 _resonance_gate，跳过导入避免其依赖链在本机间歇性挂起
_RESONANCE_MODULE_OK = True
if "--tushare-replay" in sys.argv:
    _RESONANCE_MODULE_OK = False
    _compute_resonance = None
    _write_resonance_trace = None
else:
    try:
        from index_resonance import compute_resonance as _compute_resonance
        from index_resonance import write_resonance_trace as _write_resonance_trace
    except Exception as _e:
        _RESONANCE_MODULE_OK = False
        _compute_resonance = None
        _write_resonance_trace = None
        print(f"[WARN] index_resonance 加载失败（共振过滤不可用，信号按不过滤放行）: {_e}")


def _resonance_gate(code, sig, now):
    """指数5分钟共振门控。返回 (gate_pass, resonance_info)。

    共振模块不可用/计算异常 → 降级放行（代码故障不阻断交易）；
    指数数据缺失/不足 → 按 INDEX_RESONANCE_PARAMS.fail_closed 决定（默认拦截）。
    每次计算都落盘 index_resonance trace（含被拦截的准信号），供复盘优化。
    C-1(2026-08-21): 做T/接回意图分流——SELL_HIGH(日内了结)跳过共振门控直接放行，
    卖侧不受指数 MA5 尺约束（08-19 破线日 0 卖出信号教训）。
    """
    try:
        from config import RESONANCE_GATE as _rg
    except Exception:
        _rg = {"enabled": True, "bypass_sell_high": True}
    if (_rg.get("enabled", True) and _rg.get("bypass_sell_high", True)
            and str(sig.action) == "SELL_HIGH"):
        return True, {"bypass": "sell_high"}
    if not _RESONANCE_MODULE_OK or _compute_resonance is None:
        return True, None
    try:
        _rp = INDEX_RESONANCE_PARAMS if isinstance(globals().get("INDEX_RESONANCE_PARAMS"), dict) else {}
    except Exception:
        _rp = {}
    if not _rp.get("enabled", True):
        return True, None
    try:
        r = _compute_resonance(code, sig.action, float(sig.price or 0), boundary_ts=now)
    except Exception as e:
        log.warning(f"⚠️ 共振计算异常（降级放行）: {code} {str(e)[:80]}")
        return True, None
    try:
        _write_resonance_trace({
            "scan_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": sig.name, "action": sig.action,
            "price": float(sig.price or 0), "score": float(sig.score or 0),
            **r,
        })
    except Exception:
        pass
    if r.get("missing") and not _rp.get("fail_closed", True):
        return True, r  # fail-open：数据缺失放行
    return bool(r.get("gate_pass")), r

# 将共享命名空间中的关键变量暴露到当前模块的 globals，使 main.py 的代码可以运行
globals().update(shared)

# ==================== C-2/C-3: 个股MA5闸 + 拦截可见性（2026-08-21） ====================
def _below_ma5(code):
    """个股日线最新收盘 < MA5（盘中用最新可得收盘，通常为昨日；docstring 注明边界）。
    C-2(2026-08-21): 用户规则"破五日线只卖不买"系统化。数据缺失返回 None（不拦截）。"""
    try:
        from position_builder import fetch_daily_kline
        df = fetch_daily_kline(code)
        if df is None or df.empty or "close" not in df.columns or len(df) < 6:
            return None
        df = df.sort_values("date").reset_index(drop=True)
        c = df["close"].astype(float)
        return bool(float(c.iloc[-1]) < float(c.rolling(5).mean().iloc[-1]))
    except Exception:
        return None


_INTERCEPT_STATE_FILE = os.path.join(BASE_DIR, "t_io", "state", "intercept_notice_pushed.json")


def _push_intercept_notice(code, sig, now, reason):
    """C-3(2026-08-21): 拦截可见性——被 C-1/C-2 拦截且原 score>=推送阈值，推低优飞书（非加急）。
    每股每向每日最多 1 条，去重 json（参照 position_signal_pushed.json 模式）。"""
    try:
        if not FEISHU_WEBHOOK:
            return
        _today = now.strftime("%Y-%m-%d")
        _st = {}
        if os.path.exists(_INTERCEPT_STATE_FILE):
            try:
                _st = json.load(open(_INTERCEPT_STATE_FILE, encoding="utf-8"))
            except Exception:
                _st = {}
        _key = f"{code}:{sig.action}"
        if _key in (_st.get(_today) or {}):
            return
        _st.setdefault(_today, {})[_key] = True
        os.makedirs(os.path.dirname(_INTERCEPT_STATE_FILE), exist_ok=True)
        json.dump(_st, open(_INTERCEPT_STATE_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        _acn = {"BUY_LOW": "买入", "SELL_HIGH": "卖出", "ADD_POS": "加仓",
                "PANIC_SELL": "恐慌卖"}.get(sig.action, sig.action)
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"template": "blue",
                           "title": {"tag": "plain_text", "content": f"🔕 信号已拦截 - {FEISHU_KEYWORD}"}},
                "elements": [{"tag": "markdown", "content":
                    f"🔕 **{sig.name}（{code}）** {_acn} score={sig.score:.0f} 已拦截（{reason}）"}],
            },
        }
        send_feishu_payload(card, success_log=f"拦截可见性飞书: {code} {sig.action}",
                            error_prefix="拦截可见性飞书")
    except Exception:
        pass


_HIGH_OPEN_PUSHED = set()   # C-4: "YYYY-MM-DD:code" 当日去重


def _high_open_spike_check(code, holding, df, preopen_context, now):
    """C-4(2026-08-21): 高开急拉预警——open_gap>3%（preopen 小数口径）且自开盘冲高>2%。
    仅 09:30-09:45 早盘段检查；每股每日 1 条。证据：08-18 600176 gap 4.2% 冲 47.06 无预警。"""
    try:
        if now.time() < dtime(9, 30) or now.time() > dtime(9, 45):
            return
        today = now.strftime("%Y-%m-%d")
        key = f"{today}:{code}"
        if key in _HIGH_OPEN_PUSHED or not FEISHU_WEBHOOK:
            return
        gap = None
        if preopen_context is not None:
            gap = ((preopen_context.code_snapshots or {}).get(code) or {}).get("open_gap")
        if gap is None or not (gap > 0.03):
            return
        if df is None or df.empty or "open" not in df.columns:
            return
        df = df.copy()
        open_px = float(df.iloc[0]["open"])
        if open_px <= 0:
            return
        hi = float(df["high"].max()) if "high" in df.columns else open_px
        spike = (hi - open_px) / open_px
        if spike > 0.02:
            _HIGH_OPEN_PUSHED.add(key)
            _name = holding.get("name", code)
            card = {
                "msg_type": "interactive",
                "card": {
                    "header": _feishu_card_header(f"⚡ 高开急拉预警 - {FEISHU_KEYWORD}", "orange"),
                    "elements": [{"tag": "markdown", "content":
                        f"⚡ **{_name}（{code}）** gap {gap*100:.1f}%，10min 冲高 {spike*100:.1f}%，谨防冲高回落"}],
                },
            }
            send_feishu_payload(card, success_log=f"高开急拉预警: {code}", error_prefix="高开急拉预警")
    except Exception:
        pass


def _signal_whitelist(code, holding):
    """C-5(2026-08-21): 信号评估白名单（C15+C25+C27 合并）。
      qty==0 已清仓/零持仓 → 跳过全部评估与推送（C27：603667 清仓仍吃加急卖信号教训）；
      t_qty==0 且 qty>0 纯底仓股 → 保留评估（日报展示）但跳过买/卖推送（C25：002639 一天 6 条买信号噪音）；
      返回 {eval_buy, eval_sell, push}，集中一处便于以后调。"""
    qty = int(holding.get("qty") or 0)
    t_qty = int(holding.get("t_qty") or 0)
    if qty <= 0:
        return {"eval_buy": False, "eval_sell": False, "push": False}
    if t_qty <= 0:
        return {"eval_buy": True, "eval_sell": True, "push": False}
    return {"eval_buy": True, "eval_sell": True, "push": True}


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
        total_t = int(holding.get("t_qty", 0))  # 纯底仓(t_qty=0)不应用qty回退
        
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
        if hold_qty <= 0:
            advice += "\n⚠️ 仓控可交易量为0(无T仓/大盘熔断)，仅供参考不自动跟单"
            # W33 J6 (2026-08-13 用户拍板选项甲): 满仓股两点触发照推，卡片标注满仓参考
            advice += "\n🔒 满仓参考·可交易量0"

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

# ==================== 大盘态势判定钩子（index_regime 日线 + 分时预警） ====================

_INDEX_INTRADAY_LAST_FETCH_TS = 0.0   # 分时数据拉取节流（至多 300 秒一次）
_index_intraday_alert_cache: Dict[str, float] = {}  # 同 tag 60 分钟不重复推
# V1.30: 盘中预警活动状态（注入引擎 feats["intraday_alerts"] 供展示/回溯）
_INDEX_INTRADAY_ACTIVE_ALERTS: list = []
_INDEX_INTRADAY_ACTIVE_TS: float = 0.0
_BUY_FUSE_NOTIFY_DATE: str = ""   # 买入熔断飞书明示（每日一次）

# 高抛低吸纯两点推送最小防重：同 (code, action, 5分钟桶) 每日只推一次
_SWING_DEDUP_DATE: str = ""
_SWING_PUSH_DEDUP: set = set()

# VWAP 实时快照缓存（akshare stock_zh_a_spot_em 成交额/成交量，每 60s 刷新一次）
_SPOT_VWAP_CACHE: Dict[str, float] = {}  # code -> 实时 VWAP
_LAST_SPOT_VWAP_REFRESH = 0.0


def _refresh_spot_vwap_cache() -> None:
    """拉取 akshare 全市场实时快照，提取持仓股的真实 VWAP（成交额/成交量）。

    akshare stock_zh_a_spot_em 每次返回全市场 ~5000 只，一次请求即可更新所有持仓。
    VWAP = 成交额 / 成交量（交易所官方口径），比腾讯分钟线自算更准确。
    """
    global _LAST_SPOT_VWAP_REFRESH, _SPOT_VWAP_CACHE
    now_ts = time.time()
    if now_ts - _LAST_SPOT_VWAP_REFRESH < 60:
        return
    _LAST_SPOT_VWAP_REFRESH = now_ts
    try:
        import akshare as ak
        import pandas as pd
        spot = ak.stock_zh_a_spot_em()
        if spot is None or spot.empty:
            return
        codes_col = "代码"
        amt_col = "成交额"
        vol_col = "成交量"
        if codes_col not in spot.columns or amt_col not in spot.columns or vol_col not in spot.columns:
            return
        spot[vol_col] = pd.to_numeric(spot[vol_col], errors="coerce").fillna(0)
        spot[amt_col] = pd.to_numeric(spot[amt_col], errors="coerce").fillna(0)
        mask = (spot[vol_col] > 0) & (spot[amt_col] > 0)
        cache = {}
        for _, row in spot[mask].iterrows():
            c = str(row[codes_col]).strip()
            v = float(row[vol_col])
            a = float(row[amt_col])
            cache[c] = round(a / (v * 100), 2)  # 成交量单位是"手"(×100股)，还原为股
        _SPOT_VWAP_CACHE = cache
        log.debug(f"📡 VWAP 实时缓存刷新: {len(cache)} 只")
    except Exception as e:
        log.debug(f"VWAP 缓存刷新失败: {str(e)[:80]}")

_IR_GATE_ADVICE_CN = {
    "trend_up_hold": "单边上涨：正T优先、买入门控放宽、减少卖飞",
    "defensive_t": "单边下行：防守做T、买入收紧、禁止追跌",
    "panic_capitulation_watch": "恐慌末端：停止割肉、暂停反T、关注反向机会",
    "normal_t": "横盘震荡：黄金做T环境，正常执行策略",
    "data_unavailable": "数据不可用：按默认横盘处理",
}


def _index_regime_feishu_enabled() -> bool:
    """读取 config.json 的 feishu.enabled 开关（仿 preopen.py 读法）"""
    try:
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        return bool(feishu_cfg.get("enabled", True))
    except Exception:
        return True


def _build_index_regime_card(ctx: dict, title_prefix: str, switched: bool = False,
                             prev_regime: Optional[str] = None, as_of_note: str = "",
                             recent_days: Optional[list] = None,
                             extra_lines: Optional[list] = None,
                             regime_name_override: Optional[str] = None,
                             banner_lines: Optional[list] = None) -> dict:
    """组装大盘态势飞书卡片：标题模板按状态选 green/red/blue，切换日加急红/绿

    recent_days：morning 模式 detail.recent_days（近3日 [{date,regime,score}]），
    以 "MM-DD regime score → ..." 序列展示；extra_lines：附加说明行（插在基准日期之后）。
    B-2(2026-08-21)：regime_name_override 覆盖显示基调（C20 Level2 推"震荡观察"）；
    banner_lines 追加在卡片顶部（Level1 黄条 / Level2 红条）。
    """
    regime = str(ctx.get("regime", "range"))
    regime_name = regime_name_override or (ctx.get("regime_name") or index_regime_name(regime))
    score = float(ctx.get("score") or 0.0)
    trend_score = ctx.get("trend_score")
    env_score = ctx.get("env_score")
    days_in_regime = ctx.get("days_in_regime", 0)
    degraded = ctx.get("degraded") or []
    gate = ctx.get("gate_advice", "normal_t")
    gate_cn = _IR_GATE_ADVICE_CN.get(gate, str(gate))

    if switched:
        template = "green" if regime == "uni_up" else ("red" if regime == "uni_down" else "blue")
        title = f"⚡ 大盘态势切换 → {regime_name} {score:+.1f}分 - {FEISHU_KEYWORD}"
    else:
        template = {"uni_up": "green", "uni_down": "red"}.get(regime, "blue")
        title = f"{title_prefix} {regime_name} {score:+.1f}分 - {FEISHU_KEYWORD}"

    lines = [
        f"**状态**：{regime_name}（{regime}）｜持续 {days_in_regime} 个交易日",
        f"**综合分 S**：{score:+.2f}（趋势T {trend_score} ／ 环境E {env_score}）",
        f"**基准日期**：{ctx.get('date', '-')}{as_of_note}",
    ]
    if banner_lines:
        for _bl in banner_lines:
            lines.insert(0, _bl)
    if recent_days:
        seq = " → ".join(
            f"{str(r.get('date', '-'))[5:]} {r.get('regime', '-')} {float(r.get('score') or 0.0):+.1f}"
            for r in recent_days[-3:]
        )
        lines.append(f"**近3日态势**：{seq}")
    if extra_lines:
        lines.extend(str(x) for x in extra_lines)
    if switched and prev_regime:
        lines.insert(0, f"**⚡ 状态切换**：{index_regime_name(prev_regime)} → {regime_name}")
    if ctx.get("exhaust_flag"):
        lines.append("**衰竭提示**：斜率/乖离处于近一年极值区，分数已×衰竭系数")
    lines.append(f"**操作建议**：{gate_cn}")
    if degraded:
        lines.append(f"**降级项**：{'、'.join(str(d) for d in degraded[:8])}（已按权重归一化，宁缺毋崩）")

    card_elements = [_feishu_md_div(line) for line in lines]
    card = {"config": {"wide_screen_mode": True},
            "header": _feishu_card_header(title, template),
            "elements": card_elements}
    return {"msg_type": "interactive", "card": card, "notify_type": 1}


# ---------- 竞价采集调度挂载（W32-B2，2026-08-08 用户拍板，周一 08-10 盘前必须生效）----------
_AUCTION_COLLECT_STATE = {}  # 模块级：{slot/"_gap_warned": "YYYY-MM-DD"} 竞价采集防重复 + 断档告警防重复


def _auction_slot_on_disk(date_str: str, slot: str) -> bool:
    """t_io/preopen/auction_{date}.json 是否已落盘该 slot（供断档检查）。"""
    try:
        fp = os.path.join(BASE_DIR, "t_io", "preopen", f"auction_{date_str}.json")
        if not os.path.exists(fp):
            return False
        with open(fp, "r", encoding="utf-8") as f:
            return slot in (json.load(f).get("snapshots") or {})
    except Exception:
        return False


def _launch_auction_collector(slot: str, today: str) -> None:
    """子进程拉起 auction_collector.py --slot（fire-and-forget，仿 _launch_gui 隔离模式）。

    采集器含网络 I/O（单请求 timeout=15s），子进程隔离保证采集失败/超时
    不阻塞盘前主流程；stdout/stderr 追加到 t_io/preopen/logs/auction_collector_{date}.log
    （断档可诊断，呼应 W32-B5 静默失败显式化方向）。"""
    import subprocess
    collector = os.path.join(BASE_DIR, "t_io", "preopen", "auction_collector.py")
    log_dir = os.path.join(BASE_DIR, "t_io", "preopen", "logs")
    os.makedirs(log_dir, exist_ok=True)
    out_fp = open(os.path.join(log_dir, f"auction_collector_{today}.log"), "a", encoding="utf-8")
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [sys.executable, collector, "--slot", slot, "--date", today],
            cwd=BASE_DIR, creationflags=flags,
            stdout=out_fp, stderr=subprocess.STDOUT,
        )
    finally:
        out_fp.close()   # 子进程已持有句柄副本，父进程立即关闭
    log.info(f"📸 竞价采集已触发: slot={slot} (pid={proc.pid}) → t_io/preopen/auction_{today}.json")


def _maybe_collect_auction_snapshot(now: datetime) -> None:
    """09:20/09:22 竞价快照采集调度（每日各一次，W32-B2 用户 2026-08-08 拍板挂载）。

    窗口：09:20 slot=[09:20,09:22)，09:22 slot=[09:22,09:25)；先占位防重复（无论成败）。
    断档显式化：09:26 后检查落盘（待最晚子进程收尾：09:24:59 启动 +15s 超时 → 09:26 检查不误报），
    缺 slot 记 warning 一次——主程序晚于窗口启动或采集子进程失败均在此显式暴露，
    盘后可用 auction_collector.py --backfill 回填 09:25 口径。
    本次只动竞价挂载，不顺手改其他静默失败点。"""
    try:
        if now.weekday() >= 5:
            return
        t = now.time()
        today = now.strftime("%Y-%m-%d")
        windows = (("09:20", dtime(9, 20), dtime(9, 22)),
                   ("09:22", dtime(9, 22), dtime(9, 25)))
        for slot, start, end in windows:
            if start <= t < end and _AUCTION_COLLECT_STATE.get(slot) != today:
                _AUCTION_COLLECT_STATE[slot] = today       # 先占位防重复触发（无论成败）
                _launch_auction_collector(slot, today)
        # 断档检查（每日一次）
        if t >= dtime(9, 26) and _AUCTION_COLLECT_STATE.get("_gap_warned") != today:
            _AUCTION_COLLECT_STATE["_gap_warned"] = today
            missing = [s for s, _, _ in windows if not _auction_slot_on_disk(today, s)]
            if missing:
                log.warning(f"⚠️ 竞价采集断档: {today} 缺 slot {missing}"
                            f"（主程序晚于窗口启动或采集失败）；盘后可用 auction_collector.py --backfill 回填 09:25 口径")
                # C17-3 修复(2026-08-18): 断档告警上飞书，数据缺失当场可见
                try:
                    send_feishu_payload({
                        "msg_type": "interactive",
                        "card": {
                            "header": {"template": "red",
                                       "title": {"tag": "plain_text", "content": f"⚠️ 竞价采集断档 - {FEISHU_SYSTEM_KEYWORD}"}},
                            "elements": [{"tag": "markdown", "content": (
                                f"**{today} 竞价采集断档**：缺 slot {missing}\n"
                                f"> 主程序晚于窗口启动或采集子进程失败；今日竞价分析可能为空壳。\n"
                                f"> 盘后可用 auction_collector.py --backfill 回填 09:25 口径。")}],
                        },
                    }, success_log=f"竞价断档飞书告警已发送: {missing}", error_prefix="竞价断档飞书告警")
                except Exception as _ae:
                    log.warning(f"⚠️ 竞价断档飞书告警发送失败: {str(_ae)[:80]}")
    except Exception as e:
        log.warning(f"⚠️ 竞价采集钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")


# B-2(2026-08-21): C20 竞价现实校验——"只纠乐观错，不纠悲观错"（悲观错成本少赚、乐观错成本实亏）
_BULL_REGIMES = {"uni_up"}   # 看多类基调：单边上涨（index_regime 枚举 uni_up/range/uni_down）


def _c20_auction_check(date_str: str) -> dict:
    """C20 竞价现实校验（双条件与门版，评审通过）。
    读当日 auction_{date}.json 最后 slot 持仓缺口中位数 + preopen_{date}.json auction_summary。
    返回 {level, gap_med, top20_down_ratio, top20_missing, degraded_top20}。
      level=0 不触发 / 1 Level1 降级标注(黄) / 2 Level2 推翻基调(红)
      Top20 缺失(top20_status=empty 或 up+down=0)时退化为缺口单条件并标 degraded_top20。"""
    try:
        from config import C20_AUCTION_CHECK as _c20
    except Exception:
        _c20 = {"enabled": True, "l1_gap": -1.0, "l2_gap": -2.5,
                "l1_top20_down_ratio": 0.60, "l2_top20_down_ratio": 0.75}
    out = {"level": 0, "gap_med": None, "top20_down_ratio": None,
           "top20_missing": True, "degraded_top20": False}
    if not _c20.get("enabled", True):
        return out
    base = os.path.join(BASE_DIR, "t_io", "preopen")
    # 1) 持仓缺口中位数（剔除 _B 重复行）——auction 最后 slot 的 pct_vs_preclose(百分比)
    gap_med = None
    try:
        fp = os.path.join(base, f"auction_{date_str}.json")
        if os.path.exists(fp):
            j = json.load(open(fp, encoding="utf-8"))
            snaps = j.get("snapshots") or {}
            if snaps:
                rows = (snaps[list(snaps.keys())[-1]].get("rows") or {})
                vals = []
                for _c, _v in rows.items():
                    if str(_c).endswith("_B"):
                        continue
                    _p = (_v or {}).get("pct_vs_preclose")
                    if _p is not None:
                        vals.append(float(_p))
                if vals:
                    gap_med = float(np.median(vals))
    except Exception:
        gap_med = None
    out["gap_med"] = gap_med
    if gap_med is None:
        return out
    # 2) Top20 竞价跌占比
    top20_down_ratio, top20_missing = None, True
    try:
        fp = os.path.join(base, f"preopen_{date_str}.json")
        if os.path.exists(fp):
            s = json.load(open(fp, encoding="utf-8")).get("auction_summary") or {}
            up = float(s.get("top20_up") or 0)
            dn = float(s.get("top20_down") or 0)
            if s.get("top20_status") == "empty" or (up + dn) <= 0:
                top20_missing = True
            else:
                top20_missing = False
                top20_down_ratio = dn / (up + dn)
    except Exception:
        top20_missing = True
    out["top20_down_ratio"] = top20_down_ratio
    out["top20_missing"] = top20_missing
    if top20_missing:
        # 数据缺失降级：缺口单条件
        out["degraded_top20"] = True
        if gap_med <= _c20["l2_gap"]:
            out["level"] = 2
        elif gap_med <= _c20["l1_gap"]:
            out["level"] = 1
        return out
    if top20_down_ratio is None:
        return out
    if gap_med <= _c20["l2_gap"] and top20_down_ratio >= _c20["l2_top20_down_ratio"]:
        out["level"] = 2
    elif gap_med <= _c20["l1_gap"] and top20_down_ratio >= _c20["l1_top20_down_ratio"]:
        out["level"] = 1
    return out


def _maybe_push_index_regime_morning(now: datetime) -> None:
    """09:26-09:31 早盘大盘基调推送（每日一次；须在 scan_once 的 <9:30 早退分支之前调用）

    V2：调 detect_index_regime(mode="morning")，模块自动对齐到今天之前最近一个
    已完成交易日（基于其收盘判定），并输出 detail.recent_days 近3日
    [{date,regime,score}]；卡片注明 9:30-10:00 决策窗口主要参考前两日状态。
    B-2(2026-08-21)：推送前做 C20 竞价现实校验——看多基调被 Level2 推翻时改"震荡观察"+红条，
    Level1 加黄条；非看多基调不动作（只纠乐观错）。
    """
    global _index_regime_morning_pushed_date
    try:
        t = now.time()
        if now.weekday() >= 5 or not (dtime(9, 26) <= t <= dtime(9, 31)):
            return
        today = now.strftime("%Y-%m-%d")
        if _index_regime_morning_pushed_date == today:
            return
        _index_regime_morning_pushed_date = today          # 先占位防重复触发（无论成败）
        if not _index_regime_feishu_enabled():
            return
        # mode="morning"：as_of 默认=今天，模块内部对齐到 <今天 的最近已完成交易日，
        # 保证评分基于昨日收盘（周一自动对齐到上周五），不受当日集合竞价 partial bar 影响
        regime, score, ctx = detect_index_regime(mode="morning")
        recent_days = (ctx.get("detail") or {}).get("recent_days") or []
        # B-2: C20 竞价现实校验
        _banner, _override_name = [], None
        _check = _c20_auction_check(today)
        if _check.get("level") and str(ctx.get("regime")) in _BULL_REGIMES:
            _gm = _check.get("gap_med")
            _gm_txt = f"{_gm:.2f}%" if _gm is not None else "?"
            if _check.get("degraded_top20"):
                _bt = "Top20缺失·单条件"
            else:
                _ratio = _check.get("top20_down_ratio")
                _bt = f"Top20跌 {round((_ratio or 0) * 100)}%"
            _msg = f"竞价现实与基调背离：持仓缺口 {_gm_txt}，{_bt}"
            if _check["level"] >= 2:
                _override_name = "震荡观察"
                _banner.append(f"🔴 {_msg}")
            else:
                _banner.append(f"🟡 {_msg}")
        payload = _build_index_regime_card(
            ctx, "🧭 早盘大盘基调",
            as_of_note="（基于昨日收盘的判定）",
            recent_days=recent_days,
            extra_lines=["**决策提示**：9:30-10:00 决策窗口主要参考前两日状态"],
            regime_name_override=_override_name,
            banner_lines=_banner,
        )
        send_feishu_payload(
            payload=payload,
            success_log=f"✅ 早盘大盘基调已推送: {_override_name or ctx.get('regime_name')} S={ctx.get('score')} (mode=morning)"
                        + (f" C20_level={_check.get('level')}" if _check.get("level") else ""),
            error_prefix="早盘大盘基调推送",
        )
    except Exception as e:
        log.warning(f"⚠️ 早盘大盘基调钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")


def _maybe_push_index_regime_eod(now: datetime) -> None:
    """14:30-14:55 尾盘大盘评分预判推送（每日一次，mode="tail" 含当日 forming bar；
    盘中预判 estimate，最终以收盘落库为准；标注状态切换；推送后更新 INDEX_REGIME_CONTEXT）"""
    global _index_regime_eod_pushed_date
    try:
        t = now.time()
        if now.weekday() >= 5 or not (dtime(14, 30) <= t <= dtime(14, 55)):
            return
        today = now.strftime("%Y-%m-%d")
        if _index_regime_eod_pushed_date == today:
            return
        _index_regime_eod_pushed_date = today              # 先占位防重复触发（无论成败）
        if not _index_regime_feishu_enabled():
            return
        # mode="tail"：14:30 后当日态势已基本定型，腾讯日线含 forming bar；
        # 模块标注 estimate=true 且不写 state.json/trace（保持 EOD 状态机纯净，收盘落库为准）
        regime, score, ctx = detect_index_regime(mode="tail")
        state_detail = (ctx.get("detail") or {}).get("state") or {}
        prev_regime = state_detail.get("prev_regime")
        switched = bool(prev_regime) and str(prev_regime) != str(ctx.get("regime"))
        payload = _build_index_regime_card(
            ctx, "📊 尾盘大盘评分预判", switched=switched, prev_regime=prev_regime,
            as_of_note="（盘中预判 estimate，最终以收盘落库为准）",
        )
        send_feishu_payload(
            payload=payload,
            success_log=f"✅ 尾盘大盘评分预判已推送: {ctx.get('regime_name')} S={ctx.get('score')} 切换={switched} (mode=tail)",
            error_prefix="尾盘大盘评分预判推送",
            trigger_urgent_alarm_after_success=switched,   # 切换日加急
        )
        try:
            push_index_regime_context(ctx)                 # 更新 INDEX_REGIME_CONTEXT / SESSION_CONTEXT
        except Exception as e:
            log.warning(f"⚠️ push_index_regime_context 异常: {str(e)[:80]}")
        # V3.0: 大盘热度×韭研TOP3 合成卡片（后台线程计算+落盘+推送，异常自吞不阻塞主循环）
        try:
            if 'push_daily_sentiment' in globals():
                push_daily_sentiment(now=now)
        except Exception as e:
            log.warning(f"⚠️ push_daily_sentiment 钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")
    except Exception as e:
        log.warning(f"⚠️ 尾盘大盘评分预判钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")


_daily_pnl_push_date = ""  # 模块级：14:59推送防重复
_position_builder_push_date = ""  # 模块级：收盘后建仓扫描防重复
_position_builder_intraday_last = None  # 盘中建仓扫描节流（datetime）
_position_builder_intraday_pushed = set()  # 当日已推送的股票代码（防重复推送）
_position_builder_eod_last_attempt = None  # eod 扫描失败重试节流（datetime）
_position_builder_intraday_thread = None  # 盘中建仓扫描后台线程（fix P1-5）
import threading as _threading
_position_scan_lock = _threading.Lock()  # 盘中/收盘建仓扫描互斥（trace 写盘线程安全）
_ma_break_last = None  # 破5/10日线报警节流（datetime，仿盘中建仓扫描）
_ma_break_thread = None  # 破5/10日线报警后台线程
_TOTAL_EQUITY_CACHE = {"ts": 0.0, "value": 0.0}  # fix P0-9(B1): total_equity 缓存


def _compute_total_equity() -> float:
    """fix P0-9(B1): 从 t_io/state/portfolio_config.json 计算账户总资产
    （账户A现金+持仓市值 + 账户B现金+持仓市值）。
    现金 ≈ total_capital - 持仓成本（忽略已实现盈亏/手续费）；持仓市值用快照最新价。
    """
    now_ts = time.time()
    if _TOTAL_EQUITY_CACHE["value"] > 0 and now_ts - _TOTAL_EQUITY_CACHE["ts"] < 300:
        return _TOTAL_EQUITY_CACHE["value"]
    equity = 0.0
    try:
        fp = os.path.join(BASE_DIR, "t_io", "state", "portfolio_config.json")
        with open(fp, encoding="utf-8") as f:
            cfg = json.load(f)
        capital_by_acct = {k: float((v or {}).get("total_capital") or 0)
                           for k, v in (cfg.get("accounts") or {}).items()}
        cost_by_acct = {k: 0.0 for k in capital_by_acct}
        mv_by_acct = {k: 0.0 for k in capital_by_acct}
        for _c, _h in (HOLDINGS or {}).items():
            acct = _h.get("account") or "账户A"
            if acct not in capital_by_acct:
                continue
            _q = int(_h.get("qty") or 0)
            if _q <= 0:
                continue
            _cost = float(_h.get("cost") or 0)
            _dec = DAILY_DECISION_STATS.get(_c) or {}
            _price = float(_dec.get("last_price") or 0) or float(_h.get("pre_close") or 0) or _cost
            cost_by_acct[acct] += _cost * _q
            mv_by_acct[acct] += _price * _q
        for acct, cap in capital_by_acct.items():
            cash = max(0.0, cap - cost_by_acct.get(acct, 0.0))
            equity += cash + mv_by_acct.get(acct, 0.0)
    except Exception as e:
        log.warning(f"⚠️ total_equity 计算失败（本轮单股上限检查跳过）: {str(e)[:120]}")
        return 0.0
    _TOTAL_EQUITY_CACHE["ts"] = now_ts
    _TOTAL_EQUITY_CACHE["value"] = equity
    return equity


def _maybe_run_position_builder_intraday(now: datetime) -> None:
    """盘中实时建仓信号扫描（每 5 分钟，signal 股即时飞书推送，每只每天只推一次）。

    fix P1-5: 扫描移入独立线程执行，避免阻塞做T主循环；与收盘档扫描互斥保证 trace 写盘线程安全。
    """
    global _position_builder_intraday_last, _position_builder_intraday_pushed, _position_builder_intraday_thread
    if _run_position_scan is None:
        return
    t = now.time()
    # 仅交易时段: 9:30-11:30, 13:00-14:55（09:30 开盘即扫，竞价期间9:15-9:25无分钟线不扫）
    if now.weekday() >= 5:
        return
    in_morning = dtime(9, 30) <= t <= dtime(11, 30)
    in_afternoon = dtime(13, 0) <= t <= dtime(14, 55)
    if not (in_morning or in_afternoon):
        return
    # 节流：每 5 分钟最多跑一次
    if _position_builder_intraday_last is not None:
        if (now - _position_builder_intraday_last).total_seconds() < 300:
            return
    # fix P1-5: 上一轮扫描线程未结束则跳过本轮（不推进节流时间戳，下一轮重试）
    if _position_builder_intraday_thread is not None and _position_builder_intraday_thread.is_alive():
        log.debug("⏳ 上一轮盘中建仓扫描仍在进行，跳过本轮")
        return
    _position_builder_intraday_last = now

    # 日期翻转时清空推送记录
    today = now.strftime("%Y-%m-%d")
    if not hasattr(_maybe_run_position_builder_intraday, "_date") or \
       _maybe_run_position_builder_intraday._date != today:
        _maybe_run_position_builder_intraday._date = today
        _position_builder_intraday_pushed.clear()

    def _scan_worker(day: str) -> None:
        try:
            # 与收盘档扫描互斥，保证 trace 写盘线程安全
            with _position_scan_lock:
                results = _run_position_scan(date_str=day, silent=True, scan_type="intraday")
            for r in results:
                if r.get("verdict") == "signal" and r["code"] not in _position_builder_intraday_pushed:
                    _position_builder_intraday_pushed.add(r["code"])
                    log.info(f"🏗️ 盘中建仓信号触发: {r['code']} {r['name']} 得分={r['composite_score']}")
        except Exception as e:
            log.warning(f"⚠️ 盘中建仓扫描异常（已吞掉）: {str(e)[:200]}")

    _position_builder_intraday_thread = _threading.Thread(
        target=_scan_worker, args=(today,), name="position-scan-intraday", daemon=True)
    _position_builder_intraday_thread.start()


def _ma_break_feishu_enabled() -> bool:
    """读取 config.json 的 feishu.enabled + notify_on_ma_break 开关。"""
    try:
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        if not bool(feishu_cfg.get("enabled", True)):
            return False
        return bool(feishu_cfg.get("notify_on_ma_break", True))
    except Exception:
        return True


def _maybe_run_ma_break_alert(now: datetime) -> None:
    """盘中破5/10日线报警（每5分钟，刚跌破事件即飞书提醒建仓，每只每天只推一次）。

    与盘中建仓扫描同节奏/同互斥锁；run_ma_break_alert 内部按 (code,date) 状态文件去重。
    """
    global _ma_break_last, _ma_break_thread
    try:
        if not _ma_break_feishu_enabled():
            return
    except Exception:
        return
    if _run_position_scan is None:  # position_builder 未加载时不可用
        return
    t = now.time()
    if now.weekday() >= 5:
        return
    in_morning = dtime(9, 30) <= t <= dtime(11, 30)
    in_afternoon = dtime(13, 0) <= t <= dtime(14, 55)
    if not (in_morning or in_afternoon):
        return
    if _ma_break_last is not None:
        if (now - _ma_break_last).total_seconds() < 300:
            return
    if _ma_break_thread is not None and _ma_break_thread.is_alive():
        log.debug("⏳ 上一轮破线报警仍在进行，跳过本轮")
        return
    _ma_break_last = now

    today = now.strftime("%Y-%m-%d")

    def _worker(day: str) -> None:
        try:
            from position_builder import run_ma_break_alert as _run_ma_break_alert
            if not _position_scan_lock.acquire(timeout=120):
                log.warning("⚠️ 破线报警等待建仓扫描释放锁超时，本轮跳过（下轮重试）")
                return
            try:
                pushed = _run_ma_break_alert(date_str=day, silent=True)
            finally:
                _position_scan_lock.release()
            for e in pushed:
                log.info(f"⚠️ 破5/10日线报警: {e['code']} {e['name']} "
                         f"现价{e.get('price')} MA5={e.get('ma5')} MA10={e.get('ma10')}")
        except Exception as ex:
            log.warning(f"⚠️ 破线报警异常（已吞掉）: {str(ex)[:200]}")

    _ma_break_thread = _threading.Thread(
        target=_worker, args=(today,), name="ma-break-alert", daemon=True)
    _ma_break_thread.start()


def _maybe_run_position_builder(now: datetime) -> None:
    """收盘后（15:05 起）每日一次建仓信号扫描 + 盘后汇总飞书推送。

    fix P0-15关联(收盘档断供): 原 15:05-15:15 硬窗口在进程休眠/重启时整日断供（08-07 复盘：
    15:00:02 进入低频保活后进程疑似休眠，错过窗口；且异常时日期占位已写入导致当日不重试）。
    改为 15:05 后任意时刻补扫一次（限交易日）；扫描成功才占位，失败 10 分钟后重试。
    """
    global _position_builder_push_date, _position_builder_eod_last_attempt
    if _run_position_scan is None:
        return
    if now.weekday() >= 5:  # fix: 周末不触发（原逻辑周末 15:05 也会扫）
        return
    today = now.strftime("%Y-%m-%d")
    if _position_builder_push_date == today:
        return
    t = now.time()
    if t < dtime(15, 5):
        return
    # 失败重试节流：10 分钟（防持续异常刷屏）
    if _position_builder_eod_last_attempt is not None and \
       (now - _position_builder_eod_last_attempt).total_seconds() < 600:
        return
    _position_builder_eod_last_attempt = now
    _catchup = t > dtime(15, 15)  # 超过原窗口视为补扫（进程休眠/重启兜底）
    # fix P1-5: 与盘中扫描线程互斥，保证 trace 写盘线程安全；带超时防盘中线程卡死拖住主循环
    if not _position_scan_lock.acquire(timeout=120):
        log.warning("⚠️ 建仓信号扫描等待盘中扫描释放锁超时，本轮跳过（下轮重试）")
        return
    try:
        results = _run_position_scan(date_str=today, silent=True, scan_type="eod")
        _position_builder_push_date = today  # 扫描成功才占位（原逻辑异常也占位 → 当日断供）
        signals = [r for r in results if r.get("verdict") == "signal"]
        _tag = "（补扫）" if _catchup else ""
        if signals:
            log.info(f"🏗️ 建仓信号扫描完成{_tag}: {len(signals)} 只触发 signal, "
                     f"{len(results) - len(signals)} 只未满足")
        else:
            log.info(f"🏗️ 建仓信号扫描完成{_tag}: 0/{len(results)} 只触发 signal")

        # 盘后汇总推送
        if _push_summary_feishu and results:
            _push_summary_feishu(results, date_str=today)
            log.info(f"📋 建仓扫描汇总已推送")

    except Exception as e:
        log.warning(f"⚠️ 建仓信号扫描异常（已吞掉，10分钟后重试）: {str(e)[:200]}")
    finally:
        _position_scan_lock.release()

def _maybe_push_daily_pnl_summary(now: datetime) -> None:
    """V1.29: 14:59-15:01 每日一次 收益汇总推送。
    逐股计算当日浮动盈亏 + 做T实盈 + 总资产，推飞书卡片。
    """
    global _daily_pnl_push_date
    try:
        t = now.time()
        if now.weekday() >= 5 or not (dtime(14, 59) <= t <= dtime(15, 1)):
            return
        today = now.strftime("%Y-%m-%d")
        if _daily_pnl_push_date == today:
            return
        _daily_pnl_push_date = today

        if not HOLDINGS:
            return

        rows = []
        stock_records = []  # for JSONL logging
        total_t0_pnl = 0.0
        total_value = 0.0
        total_cost_value = 0.0
        commission_rate = float(PARAMS.get("commission_rate", 0.00015) or 0.00015)

        for code, holding in sorted(HOLDINGS.items()):
            name = holding.get("name", code)
            qty = int(holding.get("qty", 0) or 0)
            cost = float(holding.get("cost", 0) or 0)
            pre_close = float(holding.get("pre_close", 0) or 0)

            # 当前价：从扫描缓存取
            dec = DAILY_DECISION_STATS.get(code) or {}
            price = float(dec.get("last_price") or 0) or pre_close

            # 当日浮动盈亏
            day_pnl = (price - pre_close) * qty if pre_close > 0 else 0.0
            day_pct = (price / pre_close - 1) * 100 if pre_close > 0 else 0.0

            # 持仓市值
            mkt_val = price * qty
            cost_val = cost * qty
            total_value += mkt_val
            total_cost_value += cost_val

            # 做T实盈（已配对部分，使用实际成交价）
            vt = VIRTUAL_TRADES.get(code) or {}
            sell_qty_tot = sum(tr.get("qty", 0) for tr in vt.get("SELL_HIGH", []))
            buy_qty_tot = sum(tr.get("qty", 0) for tr in vt.get("BUY_LOW", []))
            sold = sum(tr.get("qty", 0) * max(tr.get("price", 0), 0) for tr in vt.get("SELL_HIGH", []))
            bought = sum(tr.get("qty", 0) * max(tr.get("price", 0), 0) for tr in vt.get("BUY_LOW", []))
            matched = min(sell_qty_tot, buy_qty_tot)
            avg_s = sold / max(sell_qty_tot, 1)
            avg_b = bought / max(buy_qty_tot, 1)
            matched = min(
                sum(tr.get("qty", 0) for tr in vt.get("SELL_HIGH", [])),
                sum(tr.get("qty", 0) for tr in vt.get("BUY_LOW", [])),
            )
            avg_s = sold / max(matched, 1)
            avg_b = bought / max(matched, 1)
            t0_pnl = round(matched * (avg_s - avg_b) - (sold + bought) * commission_rate, 2)
            total_t0_pnl += t0_pnl

            arrow = "🔴" if day_pnl < 0 else ("🟢" if day_pnl > 0 else "⚪")
            stock_records.append({
                "code": code, "name": name, "qty": qty, "price": round(price, 2),
                "day_pnl": round(day_pnl, 2), "day_pct": round(day_pct, 2),
                "mkt_val": round(mkt_val, 0), "t0_pnl": t0_pnl,
            })
            rows.append({
                "name": f"{arrow} {name}({code})",
                "qty": qty,
                "price": round(price, 2),
                "day_pnl": round(day_pnl, 2),
                "day_pct": round(day_pct, 2),
                "mkt_val": round(mkt_val, 0),
                "t0_pnl": t0_pnl,
            })

        total_day_float = sum(
            (float(dec.get("last_price") or h.get("pre_close", 0) or 0) - float(h.get("pre_close", 0) or 0))
            * int(h.get("qty", 0) or 0)
            for code, h in HOLDINGS.items()
            for dec in [DAILY_DECISION_STATS.get(code) or {}]
        )

        # 构建飞书卡片 — 区分浮动盈亏与做T实盈
        lines = [
            f"📊 **{today} 当日收益汇总**",
            "",
            f"| 标的 | 持仓 | 现价 | 浮动盈亏 | 涨跌 | T0实盈 |",
            f"|------|------|------|----------|------|--------|",
        ]
        for r in rows:
            lines.append(
                f"| {r['name']} | {r['qty']}股 | {r['price']} | {r['day_pnl']:+,.0f} | {r['day_pct']:+.2f}% | {r['t0_pnl']:+,.0f} |"
            )
        lines += [
            "",
            f"📈 **持仓浮动盈亏**: {total_day_float:+,.0f} 元（现价相对昨收）",
            f"🔄 **今日做T实盈**: {total_t0_pnl:+,.0f} 元（已配对买卖差价，扣费后）",
            f"💰 **持仓总市值**: {total_value:,.0f} 元",
            f"📊 **今日总收益**: {total_day_float + total_t0_pnl:+,.0f} 元（浮动+T0）",
        ]

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"📊 {today} 收益汇总"},
                    "template": "red" if total_day_float < 0 else "green",
                },
                "elements": [
                    {"tag": "markdown", "content": "\n".join(lines)},
                    {"tag": "hr"},
                    {"tag": "note", "elements": [{"tag": "plain_text", "content": "⏰ 14:59 自动推送 · 数据基于最新扫描价"}]},
                ],
            },
        }
        send_feishu_payload(
            card,
            success_log=f"✅ 14:59 收益汇总已推送: 持仓{total_value:,.0f} T0实盈{total_t0_pnl:+.0f}",
            error_prefix="收益汇总推送",
        )

        # V1.29: 同时写 JSONL 日志供复盘
        _pnl_log_dir = _os.path.join(BASE_DIR, "t_io", "logs")
        _os.makedirs(_pnl_log_dir, exist_ok=True)
        _pnl_log_path = _os.path.join(_pnl_log_dir, "daily_pnl.jsonl")
        _pnl_record = {
            "date": today,
            "pushed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "total_value": round(total_value, 2),
            "total_cost": round(total_cost_value, 2),
            "total_pnl": round(total_value - total_cost_value, 2),
            "total_pnl_pct": round((total_value - total_cost_value) / total_cost_value * 100 if total_cost_value > 0 else 0.0, 2),
            "day_pnl_float": round(total_day_float, 2),
            "t0_realized": round(total_t0_pnl, 2),
            "stocks": stock_records,
        }
        with open(_pnl_log_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(_pnl_record, ensure_ascii=False) + "\n")
        log.info(f"📝 收益汇总已写入日志: {_pnl_log_path}")

        # V1.30: 收盘自动更新 holdings.json 的 pre_close 为当日收盘价
        _updated = False
        if HOLDINGS_FILE and _os.path.exists(HOLDINGS_FILE):
            try:
                with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
                    _hdata = _json.load(f)
                for code, h in _hdata.items():
                    dec = DAILY_DECISION_STATS.get(code) or {}
                    _cp = float(dec.get("last_price") or 0)
                    if _cp > 0:
                        h["pre_close"] = _cp
                        _updated = True
                if _updated:
                    with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
                        _json.dump(_hdata, f, ensure_ascii=False, indent=2)
                    log.info(f"📝 pre_close 已更新为当日收盘价")
            except Exception as e:
                log.warning(f"⚠️ pre_close 更新失败: {str(e)[:80]}")

    except Exception as e:
        log.warning(f"⚠️ 收益汇总推送异常（已吞掉，不影响主循环）: {str(e)[:120]}")

def _maybe_audit_closure(now: datetime) -> None:
    """V3.0: 14:50-15:05 每日一次 买卖闭环审计
    逐股核对：VIRTUAL_TRADES 卖出 vs 接回（未接回>0 → 告警行 + 建议尾盘接回价）、
    正T买入未卖出、holdings qty vs base 一致性；有异常推飞书红卡，无异常落日志；
    无论有无异常均写 logs/closure_audit.jsonl。"""
    global _closure_audit_date
    try:
        t = now.time()
        if now.weekday() >= 5 or not (dtime(14, 50) <= t <= dtime(15, 5)):
            return
        today = now.strftime("%Y-%m-%d")
        if _closure_audit_date == today:
            return
        _closure_audit_date = today                          # 先占位防重复触发（无论成败）

        problems = []
        details = []
        commission_rate = float(PARAMS.get("commission_rate", 0.00025) or 0.00025)
        for code, holding in (HOLDINGS or {}).items():
            name = holding.get("name", code)
            vt = VIRTUAL_TRADES.get(code) or {}
            sold = sum(tr.get("qty", 0) for tr in vt.get("SELL_HIGH", []))
            bought = sum(tr.get("qty", 0) for tr in vt.get("BUY_LOW", []))
            unrebuilt = max(0, sold - bought)                # 反T/高抛卖出未接回
            unclosed_buy = max(0, bought - sold)             # 正T买入未卖出
            # V1.30: 价格字段完整性守卫 —— 缺 price/price<=0 的历史记录不进入利润公式
            # （07-24 事故：V1.29 之前记录无 price 字段，avg_buy=0 把卖出成交额全额记成利润 +13018）
            _sells_all = vt.get("SELL_HIGH", [])
            _buys_all = vt.get("BUY_LOW", [])
            valid_sells = [tr for tr in _sells_all if float(tr.get("price", 0) or 0) > 0]
            valid_buys = [tr for tr in _buys_all if float(tr.get("price", 0) or 0) > 0]
            n_price_missing = (len(_sells_all) - len(valid_sells)) + (len(_buys_all) - len(valid_buys))
            vsold = sum(tr.get("qty", 0) for tr in valid_sells)
            vbought = sum(tr.get("qty", 0) for tr in valid_buys)
            sell_amt = sum(tr.get("qty", 0) * tr.get("price", 0) for tr in valid_sells)
            buy_amt = sum(tr.get("qty", 0) * tr.get("price", 0) for tr in valid_buys)
            # V2c 数据源：当日做T估算盈亏（撮合对口径：min(卖,买) 量的价差 - 双边费用）
            matched = min(vsold, vbought)
            avg_sell = sell_amt / vsold if vsold else 0.0
            avg_buy = buy_amt / vbought if vbought else 0.0
            fees = (buy_amt + sell_amt) * commission_rate
            est_pnl = round(matched * (avg_sell - avg_buy) - fees, 2) if (vsold or vbought) else 0.0
            if n_price_missing:
                problems.append(
                    f"• {name}({code}) {n_price_missing} 条虚拟记录缺价格字段，"
                    f"已隔离不计入利润（旧版本数据损坏）")
            qty = int(holding.get("qty", 0) or 0)
            base = int(holding.get("base", 0) or 0)
            qty_diff = qty - base
            # 参考价：优先扫描循环的最新 VWAP/价，兜底昨收
            dec_stats = DAILY_DECISION_STATS.get(code) or {}
            ref = float(dec_stats.get("last_vwap") or 0) or float(dec_stats.get("last_price") or 0) \
                or float(holding.get("pre_close") or 0)
            details.append({"code": code, "name": name, "sold": sold, "bought": bought,
                            "unrebuilt": unrebuilt, "unclosed_buy": unclosed_buy,
                            "est_pnl": est_pnl,
                            "qty": qty, "base": base, "qty_diff": qty_diff,
                            "ref_price": round(ref, 3)})
            if unrebuilt > 0:
                buyback = ref * 0.992 if ref > 0 else 0
                problems.append(
                    f"• {name}({code}) 已卖 {sold} / 未接回 **{unrebuilt}**"
                    + (f" → 建议尾盘接回价 ≈{buyback:.2f}（参考价下方0.8%）" if buyback else ""))
            if unclosed_buy > 0:
                problems.append(
                    f"• {name}({code}) 正T买入 {bought} / 未卖出 **{unclosed_buy}** → 建议尾盘卖出还原仓位")
            if qty_diff != 0:
                problems.append(
                    f"• {name}({code}) 持仓 qty={qty} 与 base={base} 不一致（差 {qty_diff:+d}）→ 请核对 holdings.json")

        # V1.28: 收盘自动同步 holdings.json + 释放冻结仓位
        # V1.30: 同步前校验 —— 虚拟记录价格/数量字段完整性；不合格则跳过同步并告警，
        # 防止幽灵交易被固化为次日底仓（07-24 曾把静默信号的虚拟卖出直接写进 holdings.json）
        _sync_violations = []
        for d in details:
            _vt = VIRTUAL_TRADES.get(d["code"]) or {}
            for tr in (_vt.get("SELL_HIGH", []) + _vt.get("BUY_LOW", [])):
                if int(tr.get("qty", 0) or 0) <= 0 or float(tr.get("price", 0) or 0) <= 0:
                    _sync_violations.append(f"{d['code']}:{tr.get('action','?')} qty={tr.get('qty')} price={tr.get('price')}")
        holdings_updated = False
        if _sync_violations:
            log.warning(f"⚠️ 收盘同步校验失败（{len(_sync_violations)} 条记录缺价格/数量），"
                        f"跳过 holdings.json 同步: {_sync_violations[:5]}")
            problems.append(f"• 收盘同步校验失败：{len(_sync_violations)} 条虚拟记录缺价格/数量，"
                            f"holdings.json 未同步，请人工核对")
            try:
                send_feishu_payload(
                    payload={"msg_type": "interactive", "card": {
                        "config": {"wide_screen_mode": True},
                        "header": _feishu_card_header(f"⚠️ 收盘同步校验失败 - {FEISHU_KEYWORD}", "orange"),
                        "elements": [_feishu_md_div(
                            f"{len(_sync_violations)} 条虚拟成交记录缺价格/数量字段，holdings.json **未同步**。\n"
                            + "\n".join(f"• {v}" for v in _sync_violations[:8]))]},
                        "notify_type": 1},
                    success_log="✅ 收盘同步校验失败告警已推送",
                    error_prefix="收盘同步告警推送",
                )
            except Exception:
                pass
        for d in ([] if _sync_violations else details):
            code = d["code"]
            holding = HOLDINGS.get(code)
            if holding is None:
                continue
            # V1.1.3 (2026-08-06, 修复类): t_qty 只减不增不变量（holdings_sync.apply_eod_sync）——
            # t_qty 增加只能来自晨间截图 reconcile（人工）；纯底仓 t_qty=0 天然持久，sync 不得复活。
            # 事故：旧逻辑 t_qty=qty 无条件"释放冻结"，今日 14:50:25 复活 002639/603667 纯底仓，
            # 致 14:50:45 002639 误推 SELL_HIGH + 幻影卖出持久化。
            from holdings_sync import apply_eod_sync
            old_qty = int(holding.get("qty", 0))
            old_t_qty = int(holding["t_qty"]) if "t_qty" in holding else old_qty
            new_qty, new_t_qty, new_base, delta, _changed = apply_eod_sync(
                holding, d["unclosed_buy"], d["unrebuilt"])
            holding["qty"] = new_qty
            holding["t_qty"] = new_t_qty  # 只减不增（V1.1.3）；增加只能来自晨间 reconcile
            holding["base"] = new_base
            if delta != 0 or old_t_qty != new_t_qty:
                log.info(f"📝 收盘同步 {d['name']}({code}): "
                         f"qty {old_qty}→{new_qty}, t_qty {old_t_qty}→{new_t_qty} (delta={delta:+d}, t_qty只减不增)")
                holdings_updated = True
        if holdings_updated:
            try:
                with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(HOLDINGS, f, ensure_ascii=False, indent=2)
                log.info(f"✅ holdings.json 已更新（共 {len(HOLDINGS)} 只），冻结仓位已释放")
            except Exception as e:
                log.warning(f"⚠️ holdings.json 写入失败: {str(e)[:80]}")
            # 收盘同步后清空 VIRTUAL_TRADES
            VIRTUAL_TRADES.clear()
            save_virtual_trades(VIRTUAL_TRADES)
            shared['VIRTUAL_TRADES'] = VIRTUAL_TRADES
            log.info("🔄 VIRTUAL_TRADES 已清空，准备下一交易日")

        record = {"date": today, "time": now.strftime("%H:%M:%S"),
                  "ok": not problems, "problems": problems, "details": details}
        try:
            _append_jsonl(os.path.join(LOG_DIR, "closure_audit.jsonl"), record)
        except Exception:
            pass

        # 15:00 推送当日做T收益明细飞书卡
        try:
            _push_daily_pnl_feishu(record, today)
        except Exception as e:
            log.debug(f"pnl 推送异常（已吞）: {str(e)[:80]}")

        if problems:
            lines = [f"**日期**：{today}（14:50 闭环审计）", ""] + problems \
                  + ["", "请尾盘手动处理，确保卖出=接回、qty=base 闭环。"]
            card = {"config": {"wide_screen_mode": True},
                    "header": _feishu_card_header(f"🚨 闭环审计异常 - {FEISHU_KEYWORD}", "red"),
                    "elements": [_feishu_md_div(x) for x in lines]}
            send_feishu_payload(
                payload={"msg_type": "interactive", "card": card, "notify_type": 1},
                success_log=f"✅ 闭环审计异常告警已推送: {len(problems)} 条",
                error_prefix="闭环审计告警推送",
            )
        else:
            log.info(f"✅ 闭环审计通过（{len(details)} 只：卖出=接回，qty=base）")
    except Exception as e:
        log.warning(f"⚠️ 闭环审计钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")


def _push_daily_pnl_feishu(record: dict, date_str: str) -> None:
    """15:00 推送当日做T收益明细飞书卡（基于闭环审计数据）"""
    if not FEISHU_WEBHOOK:
        return
    details = record.get("details") or []
    traded = [d for d in details if d.get("sold", 0) > 0 or d.get("bought", 0) > 0]
    if not traded and not record.get("problems"):
        log.info("📊 做T收益：今日无交易，跳过推送")
        return

    total_pnl = sum(d.get("est_pnl", 0) for d in details)
    total_fees = sum((d.get("sold", 0) * d.get("ref_price", 0)
                      + d.get("bought", 0) * d.get("ref_price", 0)) * 0.00025
                     for d in traded)
    total_trades = sum(d.get("sold", 0) for d in details) + sum(d.get("bought", 0) for d in details)

    lines = [f"📅 {date_str} 做T收益明细"]
    if record.get("problems"):
        lines.append("⚠️ **闭环异常**：")
        lines.extend(record["problems"])
        lines.append("")
    lines.append(f"| 标的 | 买卖(股) | 预估盈亏 |")
    lines.append(f"|------|----------|----------|")
    for d in details:
        if d.get("sold", 0) == 0 and d.get("bought", 0) == 0:
            continue
        pnl = d.get("est_pnl", 0)
        pnl_str = f"{pnl:+.0f}" if abs(pnl) >= 1 else f"{pnl:+.2f}"
        lines.append(f"| {d['name']}({d['code']}) | 卖{d['sold']}/买{d['bought']} | {pnl_str} |")
    lines.append(f"| **合计** | **{total_trades}股** | **{total_pnl:+.0f}** |")
    if len(details) > len(traded):
        idle = len(details) - len(traded)
        lines.append(f"\n*无交易的持仓：{idle}只*")

    card = {"config": {"wide_screen_mode": True},
            "header": _feishu_card_header(f"📊 做T收益明细 - {FEISHU_KEYWORD}",
                                          "red" if total_pnl < 0 else "green"),
            "elements": [_feishu_md_div(x) for x in lines]}
    send_feishu_payload(
        payload={"msg_type": "interactive", "card": card, "notify_type": 1},
        success_log=f"✅ 做T收益明细已推送（{len(traded)}只有交易，合计{total_pnl:+.0f}）",
        error_prefix="做T收益明细推送",
    )


def _maybe_check_index_intraday_alert(now: datetime) -> None:
    """盘中 09:35-14:55 大盘分时预警（每轮检查；拉数据 300s 节流；同 tag 60 分钟不重复推）"""
    global _INDEX_INTRADAY_LAST_FETCH_TS
    try:
        t = now.time()
        if now.weekday() >= 5 or not (dtime(9, 35) <= t <= dtime(14, 55)):
            return
        if dtime(11, 30) < t < dtime(13, 0):
            return
        now_ts = time.time()
        if now_ts - _INDEX_INTRADAY_LAST_FETCH_TS < 300:   # 拉取节流：至多 300 秒一次
            return
        _INDEX_INTRADAY_LAST_FETCH_TS = now_ts
        if not _index_regime_feishu_enabled():
            return
        # 宿主调参合并：index_regime_intraday 无 globals 合并机制，调用前把
        # config.py 的 INDEX_INTRADAY_PARAMS 合入模块内 IRI_DEFAULT_PARAMS
        try:
            if isinstance(INDEX_INTRADAY_PARAMS, dict):
                IRI_DEFAULT_PARAMS.update(INDEX_INTRADAY_PARAMS)
        except Exception:
            pass
        minute_bars = fetch_index_minutes_live("sh000001")
        # 2026-08-23: 大盘分时落盘缓存（供当日复盘直接用，绕过 tushare T-1 当日拿不到分钟）
        try:
            from market_review import save_daily_index_minutes as _save_idx_min
            _save_idx_min()
        except Exception:
            pass
        result = detect_intraday_alert(
            minute_bars,
            daily_regime=str(INDEX_REGIME_CONTEXT.get("regime", "range")),
            daily_score=float(INDEX_REGIME_CONTEXT.get("score") or 0.0),
        )
        alerts = result.get("alerts") or []
        # V1.30: 维护活动预警状态（注入 feats 供展示），45 分钟未刷新自动过期
        global _INDEX_INTRADAY_ACTIVE_ALERTS, _INDEX_INTRADAY_ACTIVE_TS
        if alerts:
            _INDEX_INTRADAY_ACTIVE_ALERTS = list(alerts)
            _INDEX_INTRADAY_ACTIVE_TS = now_ts
        elif _INDEX_INTRADAY_ACTIVE_ALERTS and now_ts - _INDEX_INTRADAY_ACTIVE_TS > 2700:
            _INDEX_INTRADAY_ACTIVE_ALERTS = []
        if not alerts:
            return
        # 同 tag 60 分钟内不重复推
        fresh = [a for a in alerts
                 if now_ts - float(_index_intraday_alert_cache.get(a.get("tag", ""), 0)) >= 3600]
        if not fresh:
            return
        for a in fresh:
            _index_intraday_alert_cache[a.get("tag", "")] = now_ts

        snapshot = result.get("snapshot") or {}
        level_rank = {"alert": 2, "warn": 1, "info": 0}
        top_level = max((level_rank.get(a.get("level"), 0) for a in fresh), default=0)
        template = "red" if top_level >= 2 else "blue"
        icon = "🚨" if top_level >= 2 else ("⚠️" if top_level == 1 else "ℹ️")
        title = f"{icon} 大盘分时预警 - {FEISHU_KEYWORD}"
        lines = [f"- 【{a.get('tag')}｜{a.get('level')}】{a.get('msg')}" for a in fresh]
        if snapshot:
            lines.append(_feishu_hr())
            lines.append(
                f"现价 {snapshot.get('last')}（{snapshot.get('chg_pct', 0):+.2f}%）｜"
                f"日线态势 {index_regime_name(snapshot.get('daily_regime', 'range'))}｜"
                f"VWAP {snapshot.get('vwap')}")
        card_elements = [_feishu_md_div(line) if not isinstance(line, dict) else line for line in lines]
        card = {"config": {"wide_screen_mode": True},
                "header": _feishu_card_header(title, template),
                "elements": card_elements}
        send_feishu_payload(
            payload={"msg_type": "interactive", "card": card, "notify_type": 1},
            success_log=f"✅ 大盘分时预警已推送: {[a.get('tag') for a in fresh]}",
            error_prefix="大盘分时预警推送",
        )
    except Exception as e:
        log.warning(f"⚠️ 大盘分时预警钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")


# ==================== 主循环函数（从原始 t_trader_v1.10.py lines 4970-5363 提取） ====================

def scan_once():
    global _last_idle_log, _scan_count, _scan_lock, _BUY_FUSE_NOTIFY_DATE
    global _SWING_DEDUP_DATE
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

        _maybe_collect_auction_snapshot(now)             # 09:20/09:22 竞价快照采集（每日各一次，W32-B2）
        _maybe_push_index_regime_morning(now)          # 09:26-09:31 早盘大盘基调（须在 <9:30 早退之前）

        _maybe_push_pivot_report(now)                  # 09:25-09:30 支撑/压力位推送

        if dtime(14, 55) <= t <= dtime(15, 5):
            pass  # EOD复盘已移除（V2简化）
        _maybe_push_daily_pnl_summary(now)             # 14:59-15:01 每日收益汇总推送（每日一次，V1.29）
        _maybe_audit_closure(now)                      # 14:50-15:05 买卖闭环审计（每日一次，V3.0）
        _maybe_push_index_regime_eod(now)              # 14:30-14:55 尾盘大盘评分预判 mode="tail"（须在 >15:00 早退之前）

        if dtime(14, 50) <= t <= dtime(15, 5):
            pivot_audit(now)                           # 14:50-15:05 pivot 支撑/压力复盘

        _maybe_run_position_builder(now)                # 15:05-15:15 建仓信号扫描（每日一次）

        if now.weekday() >= 5 or t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
            if (_now() - _last_idle_log).total_seconds() >= PARAMS["idle_log_minutes"] * 60:
                log.info("⏸ 非交易时段，进入低频保活")
                _last_idle_log = _now()
            return

        log.info(f"🫀 扫描心跳 第{_scan_count + 1}轮开始")

        _maybe_check_index_intraday_alert(now)         # 09:35-14:55 大盘分时预警（300s 节流）
        _maybe_run_position_builder_intraday(now)      # 09:45-14:55 盘中建仓信号扫描（每5分钟）
        _maybe_run_ma_break_alert(now)                 # 09:30-14:55 盘中破5/10日线报警（每5分钟，提醒建仓）

        if not HOLDINGS:
            return
        preopen_context = _ensure_preopen_context(force=False)
        _scan_count += 1
        panel_rows = []
        minute_issue_stats = {}

        # V1.28: 暴跌模式优先生成跌幅最大标的的信号
        _crash_prioritized = False
        try:
            _idx_score = float(DAILY_CONTEXT_CACHE.get("index_score", 0) or 0)
        except Exception:
            _idx_score = 0.0
        if _idx_score <= -30:
            _codes_with_decline = []
            for _c, _h in HOLDINGS.items():
                _dec = DAILY_DECISION_STATS.get(_c) or {}
                _lp = float(_dec.get("last_price") or 0)
                _pc = float(_h.get("pre_close") or 0)
                if _pc > 0 and _lp > 0:
                    _decline = (_lp - _pc) / _pc
                else:
                    _decline = 0.0
                _codes_with_decline.append((_decline, _c, _h))
            _codes_with_decline.sort(key=lambda x: x[0])  # 跌幅最大(最负)的排最前
            HOLDINGS_SORTED = [(c, h) for _, c, h in _codes_with_decline]
            _crash_prioritized = True
            if _scan_count % 4 == 1:
                log.info(f"📉 暴跌优先扫描模式(指数{_idx_score:.0f}分)：按跌幅降序扫描")
        else:
            HOLDINGS_SORTED = list(HOLDINGS.items())

        for code, holding in HOLDINGS_SORTED:
            _ensure_ai_review_stats(code, holding)
            dec = _ensure_daily_decision_stats(code, holding)
            # C-5(2026-08-21): 信号评估白名单——已清仓 qty=0 跳过信号评估，仅记 trace（C27）
            _wl = _signal_whitelist(code, holding)
            dec["_signal_whitelist"] = _wl
            if not (_wl.get("eval_buy") or _wl.get("eval_sell")):
                dec["last_status"] = "已清仓跳过(白名单)"
                panel_rows.append([label(code, holding), "-", "-", "-", "-", "已清仓跳过"])
                log.info(f"🚫 {label(code, holding)} qty=0 已清仓，跳过信号评估（C-5/C27）")
                try:
                    _append_jsonl(_trace_path("data_quality"), {
                        "fetch_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                        "code": code, "source": "scan_gate", "whitelist_skip": "qty0",
                    })
                except Exception:
                    pass
                continue

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
                # VWAP 精度修正：有实时快照 VWAP 时覆盖分钟线自算的最后一格
                _refresh_spot_vwap_cache()
                api_code = clean_code(code)
                spot_vwap = _SPOT_VWAP_CACHE.get(api_code)
                if spot_vwap and spot_vwap > 0:
                    df.loc[df.index[-1], "vwap"] = spot_vwap

                price = float(df.iloc[-1]["close"]) if "close" in df.columns else 0.0
                vwap = float(df.iloc[-1]["vwap"]) if "vwap" in df.columns else price
                amp = float(df.iloc[-1]["day_amplitude"]) if "day_amplitude" in df.columns else 0.0
                # C-4(2026-08-21): 高开急拉预警（09:30-09:45，每股每日1条）
                try:
                    _high_open_spike_check(code, holding, df, preopen_context, now)
                except Exception:
                    pass

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
                # V1.30: 盘中分时预警注入引擎特征（供展示/回溯）
                try:
                    if _INDEX_INTRADAY_ACTIVE_ALERTS:
                        daily_ctx["intraday_alerts"] = [
                            {"tag": a.get("tag"), "level": a.get("level"), "msg": a.get("msg")}
                            for a in _INDEX_INTRADAY_ACTIVE_ALERTS
                        ]
                except Exception:
                    pass
                # V1.30: 买入熔断飞书明示（每日一次）—— uni_down 日"仅卖不买"不再沉默
                try:
                    if (str(daily_ctx.get("index_regime", "")) == "uni_down"
                            and _BUY_FUSE_NOTIFY_DATE != now.strftime("%Y-%m-%d")):
                        _BUY_FUSE_NOTIFY_DATE = now.strftime("%Y-%m-%d")
                        send_feishu_payload(
                            payload={"msg_type": "interactive", "card": {
                                "config": {"wide_screen_mode": True},
                                "header": _feishu_card_header(f"🧊 买入熔断 - {FEISHU_KEYWORD}", "red"),
                                "elements": [_feishu_md_div(
                                    f"大盘态势：**单边下行**（S={float(daily_ctx.get('index_score', 0) or 0):.1f}）\n"
                                    "今日买入端已熔断（index_uni_down_clearance），仅卖不买。\n"
                                    "盘中反弹由分时预警（I1~I5）监控，解除以收盘后日线状态机为准。")]},
                                "notify_type": 1},
                            success_log="✅ 买入熔断明示已推送（uni_down）",
                            error_prefix="买入熔断推送",
                        )
                except Exception as _e:
                    log.debug(f"买入熔断明示异常（已吞掉）: {_e}")
                dec["daily_status"] = daily_ctx.get("daily_status", "unknown")
                dec["last_daily_gate"] = daily_ctx.get("daily_gate", "neutral")
                dec["last_daily_trend_bg"] = daily_ctx.get("daily_trend_bg", "unknown")
                dec["last_daily_support"] = daily_ctx.get("daily_support_name", "")
                dec["last_daily_support_gap"] = daily_ctx.get("daily_support_gap", 0.0)
                dec["last_daily_overheated"] = daily_ctx.get("daily_overheated", False)
                _attach_dynamic_t_decision(code, holding, daily_ctx, now)
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
                # V1.30: 熔断显性化 —— 买入被风控硬否决时面板直接显示原因，不再呈现"分够却HOLD"的假矛盾
                _last_dec = engine.last_decision.get(code, {}) if hasattr(engine, "last_decision") else {}
                _dec_reason = str(_last_dec.get("reason", ""))
                if dec.get("last_stand_down_reason"):
                    stat = f"停手:{dec.get('last_stand_down_reason')}"
                elif _dec_reason.startswith("HOLD_BUY_BLOCKED:index_uni_down_clearance"):
                    stat = "停手:买入熔断(单边下行)"
                elif _dec_reason.startswith("HOLD_BUY_BLOCKED:"):
                    stat = f"停手:买入熔断({_dec_reason.split(':', 1)[1][:20]})"
                elif _dec_reason == "HOLD_SELL_PRIORITY":
                    stat = "停手:卖压压制(卖分>买分)"
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

                if sig:
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
                    _advice = None  # W33 G1: sizing_advice 结构化落盘（买卖双侧）
                    try:
                        from position_sizer import calc_sell_qty, calc_buy_qty, set_all_holdings
                        set_all_holdings(HOLDINGS)  # fix P0-9(B4): 注入全量持仓供单股上限 A/B 合并判定
                        threshold = float(sig.factors.get("threshold", 35))
                        cur_price = float(sig.price or 0)
                        total_equity = _compute_total_equity()  # fix P0-9(B1): 单股上限所需真实总资产
                        if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                            dynamic_qty = calc_sell_qty(
                                code, holding, regime,
                                float(sig.score), threshold,
                                used_sells=engine.sell_count_per_stock.get(code, 0),
                                params={**PARAMS, **STOCK_PARAMS.get(code, {})},
                                virtual_trades=VIRTUAL_TRADES,
                                index_ctx=daily_ctx,
                                current_price=cur_price,
                                total_equity=total_equity,
                            )
                        else:
                            dynamic_qty = calc_buy_qty(
                                code, holding, regime,
                                float(sig.score), threshold,
                                params={**PARAMS, **STOCK_PARAMS.get(code, {})},
                                virtual_trades=VIRTUAL_TRADES,
                                index_ctx=daily_ctx,
                                current_price=cur_price,
                                total_equity=total_equity,
                            )
                        if dynamic_qty > 0:
                            sig.hold_qty = dynamic_qty
                            total_t = int(holding.get("t_qty", 0))  # 纯底仓(t_qty=0)不应用qty回退
                            pct = dynamic_qty / total_t * 100 if total_t > 0 else 0
                            log.info(f"📊 动态份数 {code}: 状态={regime.value if regime else 'normal'} 信号强度{sig.score:.0f}/阈值{threshold:.0f}, 建议交易{dynamic_qty}股/份 ({pct:.0f}%)")
                        else:
                            sig.hold_qty = 0
                            log.info(f"🛑 大盘熔断/仓控阻断 {code}: {daily_ctx.get('index_circuit_state', 'normal')} / {daily_ctx.get('index_gate_advice', 'normal_t')}")
                        # W33 G1: 捕获 sizing 画像（pushed 待推送决策后回填）
                        _vt = VIRTUAL_TRADES.get(code, {})
                        _buy_sum = sum(t.get("qty", 0) for t in _vt.get("BUY_LOW", []))
                        _sell_sum = sum(t.get("qty", 0) for t in _vt.get("SELL_HIGH", []))
                        _net = max(0, int(holding.get("t_qty", 0)) + _buy_sum - _sell_sum)
                        _unrebuilt = max(0, _sell_sum - _buy_sum)
                        _advice = {
                            "ts": _now().strftime("%Y-%m-%d %H:%M:%S"),
                            "code": code, "name": holding.get("name", code),
                            "action": sig.action,
                            "price": cur_price,
                            "vwap": float(sig.indicators.get("vwap", sig.price) or sig.price),
                            "suggested_qty": dynamic_qty,
                            "score": float(sig.score), "threshold": threshold,
                            "t_qty": int(holding.get("t_qty", 0)),
                            "net_qty": _net, "unrebuilt": _unrebuilt,
                            "max_buyable": max(0, int(holding.get("t_qty", 0)) - _net),
                        }
                    except Exception as e:
                        log.warning(f"⚠️  动态份数计算失败 {code}: {e}")
                    
                    # 3. 信号分数门槛 + 通知
                    # V1.29: 推送阈值从硬编码改为 PARAMS + 个股STOCK_PARAMS 双层管理
                    # 个股专属值 > 全局PARAMS > 硬编码默认值
                    _sp = STOCK_PARAMS.get(code, {})
                    today_ret_snap = daily_ctx.get("daily_day_ret", 0.0)
                    if sig.action in ["BUY_LOW", "ADD_POS"]:
                        notify_threshold = _sp.get("notify_buy_threshold") or PARAMS.get("notify_buy_threshold", 68)
                    else:
                        if t >= dtime(10, 0):
                            notify_threshold = _sp.get("notify_sell_threshold") or PARAMS.get("notify_sell_threshold", 65)
                        elif today_ret_snap < -0.04 and sig.action in ("PANIC_SELL", "SELL_HIGH"):
                            notify_threshold = PARAMS.get("notify_sell_panic_threshold", 60)
                        else:
                            notify_threshold = _sp.get("notify_sell_threshold") or PARAMS.get("notify_sell_early_threshold", 75)
                    
                    # 高抛低吸纯两点 (2026-08-13): 两点满足即推送；仓控0股也推(仅供参考不记账)
                    pushed = sig.score >= notify_threshold
                    _block_reason = None  # C13修复(2026-08-18): 记录真实拦截原因，供日志/shadow落盘
                    # 纯两点规则 (2026-08-13): 移除轮次上限/单股日限；score=100 恒过阈值 → 一定推送
                    # 仅保留最小防重：同 (code, action, 5分钟桶) 每日只推一次
                    if pushed:
                        _today = now.strftime("%Y-%m-%d")
                        if _SWING_DEDUP_DATE != _today:
                            _SWING_DEDUP_DATE = _today
                            _SWING_PUSH_DEDUP.clear()
                        _dkey = (code, sig.action, t.hour * 12 + t.minute // 5)
                        if _dkey in _SWING_PUSH_DEDUP:
                            pushed = False
                            _block_reason = "防重桶拦截（同股同向5分钟桶内已推）"
                        else:
                            _SWING_PUSH_DEDUP.add(_dkey)
                    # 指数5分钟共振门控（2026-08-14）：指数与个股同向才推送，否则整条信号作废
                    # C-1: SELL_HIGH 已在 _resonance_gate 内部按 RESONANCE_GATE.bypass_sell_high 跳过
                    _res_blocked = False
                    if pushed:
                        _gate_pass, _res = _resonance_gate(code, sig, now)
                        if _res is not None:
                            if _res.get("bypass"):
                                _res_status = "SELL_HIGH跳过共振(C-1)"
                            elif _res.get("missing"):
                                _res_status = "指数数据缺失" if not _gate_pass else "指数数据缺失放行"
                            elif _gate_pass:
                                _res_status = "共振通过"
                            else:
                                _res_status = f"共振拦截({_res.get('gate', '')})"
                        else:
                            _res_status = "共振放行(模块不可用)"
                        dec["last_resonance_status"] = _res_status
                        dec["last_resonance_gate"] = (_res or {}).get("gate", "")
                        dec["last_resonance_index"] = (_res or {}).get("index_code", "")
                        if not _gate_pass:
                            _res_blocked = True
                            pushed = False
                            log.info(f"🚫 {code} {sig.action} 指数共振拦截（{_res_status}）: {(_res or {}).get('gate_reason', '')}")
                    # C-2(2026-08-21): 个股MA5闸——破五日线只卖不买（用户规则系统化）；BUY_LOW 最新收盘<MA5 不推送
                    _ma5_suppressed = False
                    if pushed and sig.action == "BUY_LOW":
                        try:
                            if _below_ma5(code):
                                _ma5_suppressed = True
                                pushed = False
                                _block_reason = "个股收盘<MA5(破线只卖不买)"
                                log.info(f"🚫 {code} BUY_LOW 个股收盘<MA5(破线只卖不买)，不推送")
                        except Exception:
                            pass
                    # C-3(2026-08-21): 拦截可见性——被拦截且原score>=推送阈值，推低优飞书（每股每向每日1条）
                    if (_res_blocked or _ma5_suppressed) and sig.score >= notify_threshold:
                        try:
                            _push_intercept_notice(code, sig, now,
                                                   _res_status if _res_blocked else "个股收盘<MA5")
                        except Exception:
                            pass
                    # C-5(2026-08-21): 纯底仓股(t_qty=0)保留评估但跳过买/卖推送（C25）
                    _wl = dec.get("_signal_whitelist") or {}
                    if pushed and not _wl.get("push", True):
                        pushed = False
                        _block_reason = "纯底仓股(t_qty=0)跳过推送(白名单)"
                        log.info(f"🚫 {code} {sig.action} 纯底仓股(t_qty=0)，跳过推送（C-5/C25）")
                    if pushed:
                        notify(sig, holding)
                        if sig.action in ["SELL_HIGH", "PANIC_SELL"]:
                            engine.incr_cycle(code)
                        engine.record_signal(code, sig.action, sig.price, sig.score)
                        if sig.hold_qty > 0:
                            engine.record_trade_action(code, sig.action, sig.hold_qty, price=sig.price)
                        else:
                            log.info(f"📡 {code} {sig.action}两点触发(score={sig.score:.0f})但仓控可交易量为0，"
                                     f"已推送仅供参考(不记账)")
                    else:
                        action_type = "买入" if sig.action in ["BUY_LOW", "ADD_POS"] else "卖出"
                        time_window = "10:00前" if t < dtime(10, 0) else "10:00后"
                        # C13修复(2026-08-18): 日志与shadow miss_reason 写真实拦截原因（阈值/防重/共振三态）
                        if _res_blocked:
                            _miss = f"指数共振拦截({(_res or {}).get('gate', '')})"
                            log.info(f"🚫 {code} {action_type}信号被指数共振拦截（非阈值不足），不推送")
                        elif _ma5_suppressed:
                            _miss = "个股收盘<MA5(破线只卖不买)"
                            log.info(f"🚫 {code} BUY_LOW 个股收盘<MA5(破线只卖不买)，不推送")
                        elif _block_reason:
                            _miss = _block_reason
                            log.info(f"🔁 {code} {action_type}信号得分{sig.score:.0f}分，{_block_reason}，不重复推送")
                        else:
                            _miss = f"低于推送阈值静默（{time_window}阈值{notify_threshold}分）"
                            log.info(f"📉 {code} {action_type}信号得分{sig.score:.0f}分，低于{time_window}阈值{notify_threshold}分，静默处理（不推送飞书）")
                        try:
                            _sp2 = STOCK_PARAMS.get(code, {})
                            _nb = _sp2.get("notify_buy_threshold") or PARAMS.get("notify_buy_threshold", 68)
                            _ns = _sp2.get("notify_sell_threshold") or PARAMS.get("notify_sell_threshold", 65)
                            write_shadow_signal(
                                code, holding.get("name", code), sig.price,
                                float(sig.indicators.get("vwap", sig.price) or sig.price),
                                buy_score, sell_score, _nb, _ns,
                                _miss,
                                extra={"action": sig.action,
                                       "decision_reason": engine.last_decision.get(code, {}).get("reason", "")},
                            )
                        except Exception:
                            pass
                    # W33 G1: sizing_advice 结构化落盘（每次 sizing 调用一行，喂每日 Review §3 加仓逐笔）
                    if _advice is not None:
                        try:
                            _advice["pushed"] = bool(pushed)
                            _advice["buy_kind"] = "rebuild" if _advice["unrebuilt"] > 0 else "first_add"
                            if sig.hold_qty <= 0:
                                _advice["note"] = "仓控可交易量为0(满仓/熔断)，仅供参考不记账"
                            _append_jsonl(_trace_path("sizing_advice"), _advice)
                        except Exception as _e:
                            log.warning(f"⚠️  sizing_advice 落盘失败 {code}: {_e}")

                # V1.14: 尾盘强制平仓已删除（用户反馈不需要）

            except Exception as e:
                log.warning(f"⚠️  {label(code, holding)} 扫描异常: {str(e)[:120]}")
                continue

        if _scan_count % 4 == 1:
            lines = [f"\n📊 护城河防御面板 第{_scan_count}轮\n" + "─"*70]
            lines.append(f"{'标的':<16}{'现价':>8}{'均价(VWAP)':>10}{'振幅':>8} {'多买/空卖评分':>13}  {'状态'}")
            for r in panel_rows:
                lines.append(f"{r[0]:<16}{r[1]:>8}{r[2]:>10}{r[3]:>8} {r[4]:>13}  {r[5]:<8}")
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
                    _attach_dynamic_t_decision(code, state, daily_ctx, SIM_NOW)
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


def tushare_replay(date_str=None):
    """
    V1.19: 使用 Tushare 分钟数据复测今日表现（可指定历史日期）
    仅输出会触发飞书通知的信号（score >= notify_threshold）
    """
    import tushare as ts
    token = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
    ts.set_token(token)
    pro = ts.pro_api()

    global SIM_NOW, HOLDINGS, MINUTE_FETCH_STATUS, MINUTE_FETCH_DETAIL
    global DAILY_DECISION_STATS, AI_REVIEW_STATS, SIGNAL_OUTCOME_TRACKER, T_MODE

    today = date_str or get_today_str()
    HOLDINGS = load_holdings()
    shared['HOLDINGS'] = HOLDINGS  # V1.19: 更新共享命名空间中的HOLDINGS
    T_MODE = load_t_mode()
    shared['T_MODE'] = T_MODE
    holdings = HOLDINGS

    results = []
    print(f"[replay] 日期={today} 持仓={len(holdings)} 只")

    for code, holding in holdings.items():
        # 转换代码为 tushare 格式（去除 _A/_B 等账户后缀）
        api_code = code.split("_")[0] if "_" in code else code
        if api_code.startswith(("6", "9", "5")):
            ts_code = f"{api_code}.SH"
        else:
            ts_code = f"{api_code}.SZ"

        print(f"[replay] {code} {holding.get('name', '')} 拉取 {ts_code} 分钟线...")
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
        print(f"[replay] {code} 分钟线 {len(df)} 根，开始逐分钟模拟...")
        
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
            # 回测不调 _attach_dynamic_t_decision：它内部会拉日线特征做网络调用，本机可能挂起
            # 改为静态 T_MODE 注入（回测目的在信号引擎，不依赖当日大盘态）
            _replay_t_mode = "long"
            if isinstance(T_MODE, dict):
                _replay_t_mode = T_MODE.get(code) or T_MODE.get(code.split("_")[0]) or "long"
            if _replay_t_mode not in {"long", "short"}:
                _replay_t_mode = "long"
            daily_ctx["t_mode"] = _replay_t_mode
            daily_ctx["effective_t_mode"] = _replay_t_mode
            daily_ctx["t_mode_source"] = "replay_static"
            daily_ctx["t_pos_factor"] = 1.0
            daily_ctx["t_trade_gate"] = "normal"
            daily_ctx["t_reason"] = "replay静态T模式"

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
    # V1.2.1 (2026-08-11 01:11 用户拍板): 取消 sizing 冻结——手动跟单场景信号达标即推送
    # （卖出保底100股 / 放开满仓买建议 / 底仓地板默认关 sell_floor_enabled=False；纯底仓 t_qty=0 与大盘熔断/防守风控保留）
    # 补充（同日拍板覆盖引擎层）：signal_engine sell_floor_protect 闸接入同一 sell_floor_enabled 开关，
    # 默认放开（原地板压制 tick 自此可出卖信号；轮次上限 max_sell_times 保留不动）
    report_lines.append(f"## 版本: V1.2.1")
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


def _auto_t_mode_suggestion() -> dict:
    """V3.0: 决策矩阵自动建议（供 _prompt_t_mode_selection）。
    regime 取 detect_index_regime(mode="morning")（基于昨日收盘）；
    z_top3 / z_S / K-day / 系统性风险 取 sentiment_daily.jsonl 最后一条（=昨日 14:30 热度记录）；
    morning 语境下昨日即"K-day 次日"判定日，故 k_day_type 传 None、prev_k_down 传昨日记录，
    避免 K-down 被双重计入。逐股决策经 daily_sentiment.per_stock_decisions 应用
    V2 个股级覆盖（背离否决 / 高开不追 / 连亏熔断）。"""
    out = {"regime": "range", "regime_name": "横盘震荡", "z_top3": 0.0, "z_S": None,
           "basis_date": None, "prev_k_down": False, "overheat_streak": 0,
           "uni_down_days": 0, "systemic_risk": False,
           "decision": {"mode": "long", "pos_factor": 1.0,
                        "reason": "默认正T标准仓（无热度历史）"},
           "per_stock": {}}
    try:
        # 1) 昨日热度记录（z_top3 / K-day / 连续过热 / 系统性风险）
        hist = []
        try:
            if 'load_sentiment_history' in globals():
                hist = load_sentiment_history() or []
        except Exception:
            hist = []
        if hist:
            last = hist[-1]
            out["basis_date"] = last.get("date")
            out["z_top3"] = float(last.get("z_top3") or 0.0)
            out["z_S"] = last.get("z_S")
            out["prev_k_down"] = str(last.get("k_day_type") or "") == "k_down"
            out["systemic_risk"] = bool(last.get("systemic_risk"))
            oh_z = 1.5
            try:
                oh_z = float(SENTIMENT_PARAMS.get("overheat_z", 1.5))
            except Exception:
                pass
            streak = 0
            for rec in reversed(hist):
                try:
                    if float(rec.get("z_top3")) >= oh_z:
                        streak += 1
                    else:
                        break
                except Exception:
                    break
            out["overheat_streak"] = streak
        # 2) 大盘态（morning：昨日收盘判定）
        try:
            _r, _s, _ctx = detect_index_regime(mode="morning")
            out["regime"] = str(_ctx.get("regime", "range"))
            out["regime_name"] = str(_ctx.get("regime_name", out["regime"]))
            if out["regime"] == "uni_down":
                out["uni_down_days"] = int(_ctx.get("days_in_regime") or 0)
        except Exception as e:
            log.warning(f"⚠️ 自动建议大盘态获取失败（按 range 兜底）: {str(e)[:100]}")
        # 3) 决策矩阵 + V2 个股级覆盖
        if 'per_stock_decisions' in globals():
            try:
                ds = per_stock_decisions(
                    regime=out["regime"], z_S=out["z_S"], z_top3=out["z_top3"],
                    overheat_streak=out["overheat_streak"], k_day_type=None,
                    prev_k_down=out["prev_k_down"], uni_down_days=out["uni_down_days"],
                    systemic_risk=out["systemic_risk"], holdings=HOLDINGS or None,
                    date_str=out["basis_date"])
                out["decision"] = ds.get("market") or out["decision"]
                out["per_stock"] = ds.get("per_stock") or {}
            except Exception as e:
                log.warning(f"⚠️ 个股级决策异常（回退市场级）: {str(e)[:100]}")
        if not out["per_stock"] and 't_decision' in globals():
            out["decision"] = t_decision(
                regime=out["regime"], z_S=out["z_S"], z_top3=out["z_top3"],
                overheat_streak=out["overheat_streak"], k_day_type=None,
                prev_k_down=out["prev_k_down"], uni_down_days=out["uni_down_days"])
        # 昨日触发系统性风险 → 根据今日实际大盘态分级处理
        if out.get("systemic_risk") and isinstance(out.get("decision"), dict):
            today_regime = out.get("regime", "range")
            if today_regime == "uni_down":
                # 大盘仍处单边下行 → 保持清仓门控（昨日风险未解除）
                out["decision"]["mode"] = "short"
                out["decision"]["mode_cn"] = "反T"
                out["decision"]["pos_factor"] = 0.0
                out["decision"]["trade_gate"] = "clear"
                out["decision"]["t_enabled"] = False
                out["decision"]["reason"] = str(out["decision"].get("reason", "")) + "；昨日系统性风险+今日仍uni_down→清仓门控"
            elif today_regime == "range":
                # 大盘已恢复震荡 → 风险缓和，仍反T但允许轻仓操作（不零封）
                out["decision"]["mode"] = "short"
                out["decision"]["mode_cn"] = "反T"
                out["decision"]["pos_factor"] = min(out["decision"].get("pos_factor", 0.3), 0.3)
                out["decision"]["trade_gate"] = "normal"
                out["decision"]["t_enabled"] = True
                out["decision"]["reason"] = str(out["decision"].get("reason", "")) + "；昨日系统性风险但今日已转range→反T轻仓"
            else:
                # 大盘已转好 → 不触发风控，尊重矩阵结论
                out["systemic_risk"] = False
    except Exception as e:
        log.warning(f"⚠️ 自动建议计算异常（按默认正T兜底）: {str(e)[:120]}")
    return out


def _attach_dynamic_t_decision(code: str, holding: dict, daily_ctx: dict, now_dt=None) -> dict:
    """把当前有效T决策写回daily_ctx和全局T_MODE，供执行层和回测复用。"""
    decision = {}
    per_stock = {}
    try:
        auto = _auto_t_mode_suggestion()
        decision = dict(auto.get("decision") or {})
        per_stock = auto.get("per_stock") or {}
        stock_decision = per_stock.get(code) or {}
        if isinstance(stock_decision, dict) and stock_decision:
            decision.update({
                "mode": stock_decision.get("mode", decision.get("mode", "long")),
                "mode_cn": stock_decision.get("mode_cn", decision.get("mode_cn", "正T")),
                "pos_factor": stock_decision.get("pos_factor", decision.get("pos_factor", 1.0)),
                "reason": stock_decision.get("reason", decision.get("reason", "")),
                "trade_gate": stock_decision.get("trade_gate", decision.get("trade_gate", "normal")),
                "t_enabled": stock_decision.get("t_enabled", decision.get("t_enabled", True)),
            })
        mode = decision.get("mode", "long")
        if mode not in {"long", "short"}:
            mode = "long"
        daily_ctx["t_mode"] = mode
        daily_ctx["effective_t_mode"] = mode
        daily_ctx["t_mode_source"] = "dynamic_sentiment"
        daily_ctx["t_pos_factor"] = float(decision.get("pos_factor", 1.0) or 0.0)
        daily_ctx["t_trade_gate"] = str(decision.get("trade_gate", "normal") or "normal")
        daily_ctx["t_reason"] = str(decision.get("reason", "") or "")
        daily_ctx["t_basis_date"] = auto.get("basis_date")
        daily_ctx["t_heat"] = auto.get("z_top3")
        daily_ctx["t_sysrisk"] = bool(auto.get("systemic_risk"))
        global T_MODE
        if isinstance(T_MODE, dict):
            T_MODE[code] = mode
        shared["T_MODE"] = T_MODE
        return {"auto": auto, "decision": decision, "per_stock": per_stock}
    except Exception as e:
        log.warning(f"⚠️ 动态T决策注入失败: {str(e)[:120]}")
        daily_ctx.setdefault("t_mode", "long")
        daily_ctx.setdefault("effective_t_mode", "long")
        daily_ctx.setdefault("t_mode_source", "fallback")
        daily_ctx.setdefault("t_pos_factor", 1.0)
        daily_ctx.setdefault("t_trade_gate", "normal")
        daily_ctx.setdefault("t_reason", "fallback")
        return {"auto": {}, "decision": {}, "per_stock": {}}


def _push_morning_t_strategy(t_mode: dict, auto: dict) -> None:
    """V3.0: 早盘推送当日T策略卡（仅自动决策日首日推送一次，手动覆盖后重推）"""
    try:
        if not FEISHU_WEBHOOK:
            return
        dec = auto.get("decision") or {}
        mode_names = {"long": "正T(先买后卖)", "short": "反T(先卖后买)"}
        lines = [
            f"**依据**：大盘{auto.get('regime_name')}｜z_top3={float(auto.get('z_top3') or 0):+.2f}"
            f"（{auto.get('basis_date') or '无热度记录'}）",
            f"**矩阵结论**：{dec.get('mode_cn', '正T')} ×{dec.get('pos_factor', 1.0)} — {dec.get('reason', '')}",
            "**逐股**：",
        ]
        per_stock_auto = auto.get("per_stock") or {}
        for code, mode in t_mode.items():
            if str(code).startswith("_"):
                continue
            name = (HOLDINGS.get(code) or {}).get("name", code)
            line = f"• {name}({code})：{mode_names.get(mode, mode)}"
            notes = (per_stock_auto.get(code) or {}).get("notes") or []
            if notes:
                line += f"｜{'；'.join(notes)}"
            lines.append(line)
        if auto.get("systemic_risk"):
            lines.append("🚨 昨日触发系统性风险：今日以清仓门控为主，禁止正T")
        card = {"config": {"wide_screen_mode": True},
                "header": _feishu_card_header(f"🎯 当日T策略（矩阵） - {FEISHU_KEYWORD}", "blue"),
                "elements": [_feishu_md_div(x) for x in lines]}
        send_feishu_payload(
            payload={"msg_type": "interactive", "card": card, "notify_type": 1},
            success_log=f"✅ 当日T策略卡已推送: {dec.get('mode_cn')}",
            error_prefix="当日T策略卡推送",
        )
    except Exception as e:
        log.warning(f"⚠️ 当日T策略卡推送异常（已吞掉）: {str(e)[:120]}")


def _auto_apply_t_mode(holdings, t_mode):
    """V3.1: 基于昨日热度+sentiment自动决定今日正T/反T，无需人工选择。
    sentiment_daily.jsonl 由 daily_sentiment.py 在 14:30 写入昨日大盘热度+z_top3，
    启动时 _auto_t_mode_suggestion() 读取该记录并通过决策矩阵判定 T-mode，
    结果自动写入 t_mode.json，推送飞书策略卡。"""
    auto = _auto_t_mode_suggestion()
    dec = auto.get("decision") or {}
    auto_mode = dec.get("mode", "long")
    auto_factor = dec.get("pos_factor", 1.0)
    auto_reason = dec.get("reason", "")
    today = _now().strftime("%Y-%m-%d")
    prev_meta = {}
    try:
        if os.path.exists(T_MODE_FILE):
            with open(T_MODE_FILE, "r", encoding="utf-8") as f:
                prev_meta = (json.load(f).get("_auto_decision") or {})
    except Exception:
        prev_meta = {}

    mode_names = {"long": "正T(先买后卖)", "short": "反T(先卖后买)"}
    print("\n" + "=" * 60)
    print("【V3.1 T模式自动决策】基于昨日热度 + 大盘态势")
    print(f"  依据: 大盘{auto.get('regime_name')}｜z_top3={float(auto.get('z_top3') or 0):+.2f}"
          f"（{auto.get('basis_date') or '无热度记录'}）")
    print(f"  矩阵结论: {dec.get('mode_cn', '正T')} ×{auto_factor} — {auto_reason}")
    print("=" * 60)

    per_stock_auto = auto.get("per_stock") or {}
    for code, holding in holdings.items():
        name = holding.get("name", code)
        s_dec = per_stock_auto.get(code) or {}
        s_mode = s_dec.get("mode", auto_mode)
        s_reason = s_dec.get("reason", auto_reason)
        t_mode[code] = s_mode
        print(f"  {name}({code}) → {mode_names.get(s_mode, s_mode)} — {s_reason}")

    t_mode["_auto_decision"] = {
        "date": today,
        "regime": auto.get("regime"),
        "regime_name": auto.get("regime_name"),
        "z_top3": auto.get("z_top3"),
        "basis_date": auto.get("basis_date"),
        "matrix_mode": auto_mode,
        "pos_factor": auto_factor,
        "reason": auto_reason,
        "per_stock": {c: (d.get("mode") if isinstance(d, dict) else str(d))
                      for c, d in per_stock_auto.items()},
        "source": "auto_v3.1",
    }
    if 'save_t_mode' in globals():
        save_t_mode(t_mode)
        print(f"✅ T模式已自动保存到 t_mode.json（含 _auto_decision 元信息）")
    # 早盘策略卡：今日首次推送一次
    if str(prev_meta.get("date")) != today:
        _push_morning_t_strategy(t_mode, auto)
    print("=" * 60 + "\n")


def _maybe_backfill_sentiment():
    """启动时检查昨日 sentiment 数据是否存在，缺失则补算"""
    try:
        if 'load_sentiment_history' in globals():
            hist = load_sentiment_history() or []
        else:
            hist = []
        yesterday = (_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        has_yesterday = any(str(r.get("date")) == yesterday for r in hist if isinstance(r, dict))
        if has_yesterday:
            return
        log.info(f"📡 缺失{sentiment_log_dir()}: {yesterday}热度记录，启动后台补算...")
        if 'compute_daily_sentiment' in globals() and 'save_sentiment_record' in globals():
            import threading
            def _worker():
                try:
                    result = compute_daily_sentiment(mode="eod", as_of=yesterday)
                    save_sentiment_record(result)
                    log.info(f"✅ 热度补算完成: {yesterday} {result.get('regime_name')} z_S={result.get('z_S')} z_top3={result.get('z_top3')}")
                except Exception as e:
                    log.warning(f"⚠️ 热度补算失败: {str(e)[:150]}")
            th = threading.Thread(target=_worker, name="backfill_sentiment", daemon=True)
            th.start()
    except Exception as e:
        log.warning(f"⚠️ backfill_sentiment 异常: {str(e)[:100]}")


def run_watch():
    global HOLDINGS, engine, T_MODE
    HOLDINGS = load_holdings()
    shared['HOLDINGS'] = HOLDINGS  # V1.12: 更新共享命名空间中的HOLDINGS，供signal_engine使用

    # V1.28: 启动时加载持久化的 VIRTUAL_TRADES，防止重启后盘中交易记录丢失
    loaded_vt = load_virtual_trades()
    if loaded_vt:
        VIRTUAL_TRADES.clear()
        VIRTUAL_TRADES.update(loaded_vt)
        shared['VIRTUAL_TRADES'] = VIRTUAL_TRADES
        total_sells = sum(len(v.get("SELL_HIGH", [])) for v in loaded_vt.values())
        total_buys = sum(len(v.get("BUY_LOW", [])) for v in loaded_vt.values())
        log.info(f"🔄 已恢复 VIRTUAL_TRADES: {len(loaded_vt)} 只股票, {total_sells} 笔卖出, {total_buys} 笔买入")
    else:
        log.info("🔄 VIRTUAL_TRADES: 无历史记录，全新开始")

    # V3.1fix: 启动时补算昨日热度（如果缺失）
    _maybe_backfill_sentiment()

    # V3.1: 基于昨日大盘热度+数决矩阵自动决定今日正T/反T，无需人工选择
    T_MODE = load_t_mode()
    shared['T_MODE'] = T_MODE
    _auto_apply_t_mode(HOLDINGS, T_MODE)

    _ensure_preopen_context(force=True)
    engine = SignalEngine()
    log.info("========= 做T终极护城河防御版 (V1.26 正T/反T模式切换版) 启动 =========")
    if PREOPEN_CONTEXT is not None:
        log.info(_format_preopen_brief(PREOPEN_CONTEXT))
    log.info(f"飞书推送: {'✓ 已启用' if FEISHU_WEBHOOK else '✗ 未配置'}")
    log.info(f"飞书关键词: {FEISHU_KEYWORD}")
    if FEISHU_WEBHOOK:
        log.info(f"飞书Webhook: {FEISHU_WEBHOOK[:55]}...")

    cleanup_expired_minute_cache()

    # 报警音已由 GUI 看板统一接管（Web Audio，方向区分），此处仅信息提示
    if not SYS_ALERT_AVAILABLE:
        log.info("🖥 报警音由 GUI 看板接管（Web Audio），无需 system_alert 模块")
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


# ---------- GUI 看板集成 ----------
# 运行盯盘时默认以子进程拉起桌面 GUI 看板（t_gui.py）。
# 原因：pywebview 必须在主线程运行，而盯盘主循环已占用主线程，只能子进程隔离。
# GUI 只读盘上 JSON/JSONL 数据，与盯盘进程天然解耦，失败不影响盯盘。
_GUI_PROC = None


def _launch_gui():
    global _GUI_PROC
    if "--no-gui" in sys.argv:
        log.info("🖥 GUI 看板: 已通过 --no-gui 禁用")
        return
    try:
        import subprocess
        gui_path = os.path.join(BASE_DIR, "t_gui.py")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _GUI_PROC = subprocess.Popen(
            [sys.executable, gui_path],
            cwd=BASE_DIR,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info(f"🖥 GUI 看板: 已启动 (pid={_GUI_PROC.pid})，盯盘结束将自动关闭；如需禁用加 --no-gui")
    except Exception as e:
        log.warning(f"🖥 GUI 看板启动失败（不影响盯盘）: {e}")


def _close_gui():
    if _GUI_PROC and _GUI_PROC.poll() is None:
        try:
            _GUI_PROC.terminate()
            log.info("🖥 GUI 看板: 已随盯盘结束关闭")
        except Exception:
            pass


if __name__ == "__main__":
    import atexit
    atexit.register(_close_gui)
    if len(sys.argv) > 1 and sys.argv[1] == "--replay-today":
        replay_today()
    elif len(sys.argv) > 1 and sys.argv[1] == "--tushare-replay":
        tushare_replay(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        _launch_gui()
        run_watch()
