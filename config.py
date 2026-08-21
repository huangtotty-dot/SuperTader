# -*- coding: utf-8 -*-
"""
A股持仓实时做T盯盘脚本（V1.8 确认型收敛版）
基于 v1.4 稳定运行骨架，整合 v1.5 风控与评分优化。
新增：轻量动态市场状态调节、EMA 趋势辅助、区间位置辅助、当日买入次数限制、T-cycle 持仓计时与确认型收敛门控。
"""
import os
import sys
import json
import time
import logging
import importlib.util
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime
from typing import Dict, List, Optional, Any
import urllib.request

# ==================== 彻底禁用所有代理 (无论环境变量还是 Windows 系统代理) ====================
for _k in ['http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','all_proxy',
            'no_proxy','NO_PROXY']:
    os.environ.pop(_k, None)
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
# 强制 urllib 全局无代理
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
# 创建全局 requests Session（trust_env=False 彻底禁用代理检测，覆盖 Windows IE 系统代理）
import requests as _req
_REQ_SESSION = _req.Session()
_REQ_SESSION.trust_env = False
_req.post = lambda url, **kw: _REQ_SESSION.request('POST', url, **kw)
_req.get = lambda url, **kw: _REQ_SESSION.request('GET', url, **kw)
# 同步给后续 import requests 的引用
import sys as _sys
_sys.modules['requests'].post = _req.post
_sys.modules['requests'].get = _req.get

import akshare as ak
import numpy as np
import pandas as pd
import requests
import urllib.request
import urllib.error

# ==================== 路径与常量 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
T_IO_DIR = os.path.join(BASE_DIR, "t_io")
HOLDINGS_FILE = os.path.join(BASE_DIR, "holdings.json")
T_MODE_FILE = os.path.join(BASE_DIR, "t_mode.json")
LEARNING_FILE = os.path.join(T_IO_DIR, "t_trader_learning.json")
LOG_DIR = os.path.join(T_IO_DIR, "logs")
CACHE_DIR = os.path.join(T_IO_DIR, "cache")
SNAPSHOT_DIR = os.path.join(T_IO_DIR, "minute_snapshots")
PREOPEN_DIR = os.path.join(T_IO_DIR, "preopen")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
TRACE_DIR = os.path.join(T_IO_DIR, "traces")
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")
VIRTUAL_TRADES_FILE = os.path.join(T_IO_DIR, "virtual_trades.json")

for d in [T_IO_DIR, LOG_DIR, CACHE_DIR, SNAPSHOT_DIR, TRACE_DIR, PREOPEN_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)


def load_runtime_config() -> Dict[str, Any]:
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"⚠️  运行配置读取失败: {str(e)[:80]}")
    return {}


def resolve_feishu_webhook() -> str:
    env_webhook = (os.getenv("FEISHU_WEBHOOK", "") or "").strip()
    if env_webhook:
        return env_webhook
    config = load_runtime_config()
    return (config.get("feishu", {}).get("webhook_url", "") or "").strip()


def resolve_feishu_keyword() -> str:
    config = load_runtime_config()
    return (config.get("feishu", {}).get("keyword", "") or "做T猎手预警").strip() or "做T猎手预警"


def resolve_feishu_system_keyword() -> str:
    config = load_runtime_config()
    return (config.get("feishu", {}).get("system_keyword", "") or "系统消息").strip() or "系统消息"


FEISHU_WEBHOOK = resolve_feishu_webhook()
FEISHU_KEYWORD = resolve_feishu_keyword()
FEISHU_SYSTEM_KEYWORD = resolve_feishu_system_keyword()
PUSH_THROTTLE_SECONDS = 300

def should_run_startup_self_test() -> bool:
    config = load_runtime_config()
    return bool(config.get("feishu", {}).get("startup_self_test", True))


def send_feishu_payload(payload: dict, success_log: str, error_prefix: str, trigger_urgent_alarm_after_success: bool = False) -> bool:
    if not FEISHU_WEBHOOK:
        log.warning(f"⚠️  {error_prefix}：飞书 Webhook 未配置")
        return False

    last_error = None
    for attempt in range(3):
        try:
            response = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("code", 0) != 0:
                log.warning(f"⚠️  {error_prefix}失败: {result}")
                return False
            log.info(success_log)
            if trigger_urgent_alarm_after_success and SYS_ALERT_AVAILABLE:
                try:
                    trigger_alert("urgent")
                    log.info("🔔 已触发急促报警")
                except Exception as e:
                    log.warning(f"⚠️  急促报警触发失败: {str(e)[:80]}")
            return True
        except Exception as e:
            last_error = e
            wait = (attempt + 1) * 2
            if attempt < 2:
                log.warning(f"⚠️  {error_prefix}第{attempt+1}次发送失败，{wait}秒后重试: {str(e)[:80]}")
                time.sleep(wait)
            else:
                log.error(f"❌ {error_prefix}3次重试均失败: {str(e)[:120]}")
    return False


def send_startup_self_test():
    if not FEISHU_WEBHOOK:
        log.warning("⚠️  启动自检跳过：飞书 Webhook 未配置")
        return

    preopen = _ensure_preopen_context(force=False)

    runtime_config = load_runtime_config()
    feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
    at_all = feishu_cfg.get("at_all_on_signal", True)
    use_strong = feishu_cfg.get("use_strong_notification", True)
    relay_urgent_alarm = feishu_cfg.get("relay_urgent_alarm_on_feishu", True)
    at_text = "<at user_id=\"all\">所有人</at>" if at_all else ""
    title = f"🚨🚨🚨 【加急】{FEISHU_KEYWORD} - 启动自检 🚨🚨🚨" if use_strong else f"📢 【提醒】{FEISHU_KEYWORD} - 启动自检"

    card_elements = []
    if at_all:
        card_elements.append({
            "tag": "div",
            "text": {"content": at_text, "tag": "lark_md"}
        })
    card_elements.append({
        "tag": "div",
        "text": {"content": title, "tag": "lark_md"}
    })
    preopen_text = "盘前解读：未生成"
    if preopen is not None:
        adv = _preopen_adv_counts(preopen)
        hot_theme = preopen.breadth.get("hot_theme_text", "") if isinstance(preopen.breadth, dict) else ""
        preopen_text = (
            f"盘前解读：{preopen.market_bias} | 评分 {preopen.market_score:.1f} | {preopen.session_note}\n"
            f"涨跌家数：{adv['up']} / {adv['down']} / {adv['flat']} | 热主题：{hot_theme or '暂无'}"
        )
    card_elements.append({
        "tag": "div",
        "text": {
            "content": (
                f"【{FEISHU_SYSTEM_KEYWORD}】\n"
                f"t_trader_v1.8 已启动。\n"
                f"{preopen_text}\n"
                f"如果你收到此消息并听到急促报警音，说明飞书推送与本地报警链路均正常。"
            ),
            "tag": "lark_md"
        }
    })

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "elements": card_elements
        },
        "notify_type": 1
    }
    send_feishu_payload(
        payload=payload,
        success_log="✅ 启动自检飞书消息已成功送达",
        error_prefix="启动自检飞书推送",
        trigger_urgent_alarm_after_success=use_strong and relay_urgent_alarm,
    )


SIM_NOW: Optional[datetime] = None


def _now() -> datetime:
    return SIM_NOW or datetime.now()


def get_today_str():
    """动态获取今日日期字符串，防止跨日运行Bug"""
    return _now().strftime("%Y-%m-%d")


def chunk_list(items: List[Any], size: int):
    size = max(1, int(size or 1))
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ==================== VIRTUAL_TRADES 持久化 ====================

def load_virtual_trades() -> Dict[str, Dict[str, list]]:
    """从文件加载虚拟交易记录。若非当日数据，自动重置（每日清零）。"""
    try:
        if not os.path.exists(VIRTUAL_TRADES_FILE):
            return {}
        with open(VIRTUAL_TRADES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        saved_date = data.get("_date", "")
        today = get_today_str()
        if saved_date != today:
            log.info(f"🔄 VIRTUAL_TRADES 日期 {saved_date} != 今日 {today}，自动重置")
            return {}
        raw = data.get("trades", {})
        if not isinstance(raw, dict):
            return {}
        return raw
    except Exception as e:
        log.warning(f"⚠️  VIRTUAL_TRADES 加载失败: {str(e)[:80]}")
        return {}


def save_virtual_trades(data: dict) -> None:
    """将虚拟交易记录持久化到文件，附带日期标记。"""
    try:
        os.makedirs(os.path.dirname(VIRTUAL_TRADES_FILE), exist_ok=True)
        payload = {
            "_date": get_today_str(),
            "trades": data,
        }
        with open(VIRTUAL_TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        log.warning(f"⚠️  VIRTUAL_TRADES 保存失败: {str(e)[:80]}")


# ==================== 【高级声音报警引擎动态挂载】 ====================
SYS_ALERT_AVAILABLE = False
try:
    for alert_filename in ["system_alert_v17_3.py", "system_alert_v17.3.py"]:
        alert_file = os.path.join(BASE_DIR, alert_filename)
        if not os.path.exists(alert_file):
            continue
        spec = importlib.util.spec_from_file_location("sys_alert", alert_file)
        sys_alert = importlib.util.module_from_spec(spec)
        sys.modules["sys_alert"] = sys_alert
        spec.loader.exec_module(sys_alert)
        init_alert = sys_alert.init_alert
        trigger_alert = sys_alert.trigger_alert
        SYS_ALERT_AVAILABLE = True
        break
except Exception:
    pass # 挂载失败不影响主程序运行

# ==================== 【做T核心风控参数 V1.8】 ====================
# V3.0 P0-D: PARAMS 精简 — 从 214 键删至 ~55 键（V1.x 死机制残留全清）
# 保留键均经全库 grep 验证有活跃消费端；详见 doc/v1.0.2_config参数审计报告.md
PARAMS = {
    # —— 指标周期 ——
    "poll_interval": 15,
    "rsi_period": 6,
    "bb_period": 20,
    "bb_std": 2.0,
    "ema_fast_period": 8,
    "ema_slow_period": 21,
    # —— V3.0 5分钟趋势层 ——
    "rsi_period_5m": 14,
    "rsi_oversold_5m": 32,
    "rsi_overbought_5m": 68,
    "trend_bb_slope_flat": 0.0005,
    "trend_bb_width_expand": 1.05,
    "trend_debounce_bars": 2,
    # —— 风控 ——
    "min_amplitude": 0.015,
    # 2026-08-15 成本修正: 0.001(0.1%)→0.00025(万2.5)，A股散户实际佣金。
    # 回测实证: 做T在真实成本下净EV≈-0.01%(接近盈亏平衡)，0.1%假设会高估亏损。
    "commission_rate": 0.00025,
    "sell_floor_ratio": 0.5,
    # V1.2.1 (2026-08-11 01:11 用户拍板): "手动跟单场景，取消冻结，做T不用考虑底仓问题"——
    # 底仓地板默认不生效；True 时恢复 V1.30 钳制（position_sizer.py calc_sell_qty 软消费；harness T_SELL_FLOOR_ENABLED 可注入对照）
    "sell_floor_enabled": False,
    # V1.2.2 (2026-08-11 用户拍板): "满仓保底买先不用保留"——08-11 实盘 600481 满仓仍推 3 笔买入建议（与卖互抵）。
    # False（默认）: max_buyable<=0（满仓或大盘目标仓位钳到 0）时 calc_buy_qty 返回 0，不产生买入建议（恢复 V1.2.0 早退语义）；
    # True: 恢复 V1.2.1 满仓保底一手行为。仅影响"算不出可买量"场景；非满仓不足一手保底 100（V1.2.1）不受影响。
    "allow_full_position_buy": False,
    "index_regime_intraday_lock": True,
    "max_single_position_pct": 0.30,
    "max_sell_times_per_stock": 3,
    # —— 早盘 ——
    "morning_no_sell_until": 940,
    # —— 高抛低吸纯两点 (2026-08-13 用户拍板: 布林线触轨 + 确认点) ——
    "rsi_period_5m_swing": 6,   # 5分钟RSI周期(专用列 rsi_5m_p6, 不动 rsi_5m(14))
    # 2026-08-15 实验后回退: 15分MACD确认(macd15_bb5)修正未来函数后无效(44.4% vs 原45.9%)，
    # 故默认 False 保持原 5分RSI 确认；True 仅供实验，不作为生产口径
    "swing_macd15_dir": False,              # False=5分RSI确认(生产)；True=15分MACD方向(实验)
    "swing_macd15_bb_upper": 0.85,          # 实验: 高抛 5分收盘 bb_pct_5m ≥ 0.85
    "swing_macd15_bb_lower": 0.15,          # 实验: 低吸 5分收盘 bb_pct_5m ≤ 0.15
    "swing_sell_rsi": 75.0,     # 高抛: 5分RSI(6) > 75
    "swing_buy_rsi": 35.0,      # 低吸: 5分RSI(6) < 35
    "swing_bb_upper": 1.0,      # 高抛: 5分收盘 ≥ 上轨 (bb_pct_5m>=1.0)
    "swing_bb_lower": 0.0,      # 低吸: 5分收盘 ≤ 下轨 (bb_pct_5m<=0.0)
    "swing_min_5m_bars": 13,    # 预热: 至少13根5分K才开始判断
    # 2026-08-15 因子实验实施: 高抛放量确认（样本内+6.0pp/样本外+6.4pp，稳健）
    # 放量冲高(近5分钟量≥全天均量×该倍数)时高抛更可靠；0=关闭
    "swing_sell_vol_ratio": 1.5,
    # V1.2.0 (2026-08-08 用户拍板上线): C1' 口径B — 全部买信号单股日限 7 内置状态机
    # record_signal 层计数，第 8 条起当日不再产生买入信号（卖信号不受限；0/None=关闭；
    # harness T_BUY_DAILY_CAP 可显式覆盖做 A/B）
    "buy_daily_cap": 7,
    # —— 通知阈值 ——
    # v1.1.0 X9 阈值阶梯实测采纳 t55 档（两组胜率+2.1~2.8pp、密度双升、无单股恶化，
    # 依据 t_io/validation/v109_threshold/阈值阶梯报告.md）；买侧 68 未实验不动
    # E1 采纳（2026-08-02, t_io/validation/e1_final + doc/E1决赛档AB报告_20260802.md）:
    # 引擎买阈基线 42→36（signal_engine.py 软消费本键）；T36b 全管线五闸门全过——
    # 买 144/wr 50.38%、闭环 47 对/+252.98、卖侧 −0.29pp、阴跌日买 wr 0.368、买密度 0.320/股日
    "engine_buy_threshold_base": 36.0,      # E1 采纳: 42→36（T36b 档）
    "notify_buy_threshold": 68,
    "notify_sell_threshold": 55,          # v1.1.0: 65→55
    "notify_sell_early_threshold": 65,    # v1.1.0: 75→65（保持早盘+10梯度）
    "notify_sell_panic_threshold": 50,    # v1.1.0: 60→50（防倒挂：panic档须低于正常档55）
    # v1.1.0 补定义: V1.30 轮次上限被 main.py:1223/1341 以 PARAMS["max_t_cycles_per_stock"] 消费
    # 但从未在 config 定义(首个达标卖出信号即 KeyError 的潜伏崩溃); 默认值与 position_sizer.py:289 一致
    "max_t_cycles_per_stock": 8,
    # v1.1.0 补定义: 与上同批 P0-D 误删 — signal_engine.py:291 消费(卖出后重建封锁分钟数);
    # 恢复 P0-D 清理前全局值 3(个股原 10/12 已随 STOCK_PARAMS 清理退役)
    "post_sell_rebuild_minutes": 3,
    # —— 仓位（position_sizer 消费） ——
    "stock_qty_base_pct": 0.30,
    "stock_qty_strong_pct": 0.40,
    "stock_qty_weak_pct": 0.20,
    # W33 B2 (2026-08-13): strength 三档失效(V2 纯两点后恒"强"档) → 单档固定比例
    "stock_rebuild_pct": 0.60,   # 接回单档
    "stock_first_add_pct": 0.20, # 首加单档
    "etf_buy_qty_pct": 0.25,     # ETF 接回/首加单档
    "stock_min_trade_unit": 100,
    "etf_qty_strong_pct": 0.25,  # 卖出侧 _calc_etf_sell_qty 在用
    "etf_qty_base_pct": 0.15,
    "etf_qty_weak_pct": 0.08,
    # —— 其他 ——
    "idle_log_minutes": 10,
    "cache_ttl_seconds": 180,
    "daily_ma_support_loose_gap": 0.04,
    # —— v1.1.1: P0 键修复 —— 以下 8 键被 data_fetcher.py 以 PARAMS["..."] 硬消费但从未定义，
    #    导致 get_daily_context 必抛 KeyError 被吞(status="error")、daily_gate 全场景锁死。
    #    语义从未在生产运行过，取值为按消费逻辑与邻近参数量级确定的设计意图默认值（待 E2 后续校准）。
    "daily_context_min_rows": 66,        # data_fetcher.py:64/72 — 日线上下文最少行数(MA60+6日斜率需≥66; 生产拉180日约120行)
    "daily_cache_ttl_seconds": 1800,     # data_fetcher.py:381 — 日上下文缓存TTL(盘中30分钟刷新, ref_price随tick更新)
    "daily_ma_breakdown_gap": 0.03,      # data_fetcher.py:157/160 — 破位风险: 现价跌破MA20/MA30达3% → buy_block(daily_breakdown_risk)
    "daily_ma_hard_breakdown_gap": 0.05, # data_fetcher.py:142/161 — 硬破位: 现价跌破MA60达5%且MA60下行 → trend_bg=weak_breakdown
    "daily_overheat_ma10_gap": 0.05,     # data_fetcher.py:163 — 过热: 现价超MA10达5% → buy_block(daily_overheated)
    "daily_overheat_ma20_gap": 0.08,     # data_fetcher.py:165 — 过热: 现价超MA20达8%
    "daily_overheat_day_ret": 0.05,      # data_fetcher.py:167 — 过热: 单日涨幅>5%且超MA10达4%
    "trend_today_ret_threshold": 0.008,  # data_fetcher.py:497/499 — 基准盘中趋势判定阈值(trend_up/down, 参照min_amplitude=0.015同量级)
}

# 个股专属参数覆盖（基于近90日分钟数据统计回测定制）
# 科泰电源 300153：反转最强、流动性最差、尾盘低点概率最高
# V3.0 P0-D: STOCK_PARAMS 精简 — 仅保留有 _sp_param 消费端的键（含 N3/N4 修复后生效的）
STOCK_PARAMS = {
    "600481": {  # 双良节能
        "stock_qty_base_pct": 0.39, "stock_qty_strong_pct": 0.27,
        "bullish_reversal_min_pct": 0.008,     # N4
        "notify_sell_threshold": 55, "notify_buy_threshold": 36.0,  # v1.1.0: sell 62→55 对齐t55档; E1采纳: buy 43→36 对齐引擎T36b档
    },
    "000988": {  # 华工科技
        "stock_qty_base_pct": 0.30, "stock_qty_strong_pct": 0.29,
        "max_sell_times_per_stock": 2,
        "bullish_reversal_min_pct": 0.006,     # N4
        "bullish_reversal_body_ratio": 0.50,
        "bullish_reversal_vol_multiplier": 0.7,
        "notify_sell_threshold": 55, "notify_buy_threshold": 36.0,  # v1.1.0: sell 63→55 对齐t55档; E1采纳: buy 43→36 对齐引擎T36b档
    },
    "588170": {  # 科创芯片ETF
        "stock_qty_base_pct": 0.15, "stock_qty_strong_pct": 0.25,
        "max_sell_times_per_stock": 2,
        "notify_sell_threshold": 55, "notify_buy_threshold": 36.0,  # v1.1.0: sell 67→55 对齐t55档; E1采纳: buy 40→36 对齐引擎T36b档
    },
    "600176": {  # 中国巨石
        "stock_qty_base_pct": 0.34, "stock_qty_strong_pct": 0.59,
        "max_sell_times_per_stock": 3,
        "notify_sell_threshold": 51, "notify_buy_threshold": 36.0,  # E1采纳: buy 40→36 对齐引擎T36b档
    },
    "603667": {  # 五洲新春
        "stock_qty_base_pct": 0.28, "stock_qty_strong_pct": 0.37,
        "notify_sell_threshold": 55, "notify_buy_threshold": 36.0,  # v1.1.0: sell 64→55 对齐t55档; E1采纳: buy 40→36 对齐引擎T36b档
    },
}

# ==================== 早盘预警门控参数（2026-07-14 基于近两年数据训练） ====================
# 训练标的：科泰电源/中文在线/双良节能/华工科技/科创芯片ETF 共5只
# 模型表现：平均AUC>0.85，核心特征一致性高
# 核心发现：
#   - 双良节能单边下行风险最高（19.8%），ETF最低（11.1%）
#   - 最强预警指标（5只一致）：开盘后最高涨幅(max_gain_after_open) > 开盘30分钟涨跌幅
#   - 所有标的的早抛晚接/倒T策略均为负收益，只做正T(VWAP深V低吸)
#
# 使用方式：在 signal_engine.evaluate() 中，开盘至10:00期间实时计算特征
# 触发Level 2 → 🚨 全天禁止买入（已有仓位可卖出止损）
# 触发Level 1 → ⚠️ 只做减仓不做加仓
# Level 0 → ✅ 正常执行VWAP深V低吸策略
# 飞书推送格式见 notify() 函数中的 alert_card 模板
MORNING_ALERT_PARAMS = {
    # V3.0fix: 300153 (科泰电源) + 300364 (中文在线) 已不在持仓，MORNING_ALERT 整段删除
    # ===== 双良节能 600481 =====
    # 单边下行日：74天/374天(19.8%)，平均跌幅-3.55%，最大-9.05%
    "600481": {
        "alert_enabled": True,
        "alert_window_end": 1000,
        # V3.0fix: 删除含 deviation_vwap_1000 的规则（无测量逻辑）
        "level_2_rules": [],
        # V3.0fix: 删除含 deviation_vwap_1000 的规则（无测量逻辑）
        "level_1_rules": [
            {
                "name": "【主】开盘30分钟大跌",
                "desc": "开盘30分钟涨幅≤-1.00%（核心阈值）",
                "precision": 0.55, "recall": 0.72,
                "condition": {"open_30min_ret": -0.010},
            },
        ],
    },
    # ===== 华工科技 000988 =====
    # 单边下行日：62天/465天(13.3%)，平均跌幅-3.74%，最大-11.21%
    "000988": {
        "alert_enabled": True,
        "alert_window_end": 1000,
        "level_2_rules": [
            {
                "name": "无反弹+开盘30分钟暴跌",
                "desc": "开盘后最高涨幅≤0.18% 且 开盘30分钟涨幅≤-1.79%",
                "precision": 1.00, "recall": 0.194,
                "condition": {"max_gain_after_open": 0.0018, "open_30min_ret": -0.0179},
            },
        ],
        "level_1_rules": [
            {
                "name": "【主】开盘30分钟大跌",
                "desc": "开盘30分钟涨幅≤-1.00%（核心阈值）",
                "precision": 0.55, "recall": 0.72,
                "condition": {"open_30min_ret": -0.010},
            },
            {
                "name": "开盘30分钟大跌(严格)",
                "desc": "开盘30分钟涨幅≤-1.41%",
                "precision": 0.582, "recall": 0.629,
                "condition": {"open_30min_ret": -0.0141},
            },
            {
                "name": "开盘5分+10分连续跌",
                "desc": "开盘5分钟涨幅≤-1.20% 且 开盘10分钟涨幅≤-1.31%",
                "precision": 0.576, "recall": 0.306,
                "condition": {"open_5min_ret": -0.0120, "open_10min_ret": -0.0131},
            },
        ],
    },
    # ===== 科创芯片ETF 588170 =====
    # 单边下行日：27天/243天(11.1%)，平均跌幅-3.46%，最大-7.97%
    "588170": {
        "alert_enabled": True,
        "alert_window_end": 1000,
        # V3.0fix: 删除含 price_slope_30min 的规则（无测量逻辑）
        "level_2_rules": [],
        "level_1_rules": [
            {
                "name": "【主】开盘30分钟大跌",
                "desc": "开盘30分钟涨幅<-1.00%（核心阈值）",
                "precision": 0.60, "recall": 0.70,
                "condition": {"open_30min_ret": -0.010},
            },
            {
                "name": "开盘30分钟大跌(严格)",
                "desc": "开盘30分钟涨幅<-1.30%",
                "precision": 0.708, "recall": 0.654,
                "condition": {"open_30min_ret": -0.0130},
            },
            {
                "name": "开盘后最低跌幅大",
                "desc": "开盘后最低跌幅<-2.04%",
                "precision": 0.583, "recall": 0.538,
                "condition": {"max_loss_after_open": -0.0204},
            },
        ],
    },
}

# V3.0fix: CORRECTION_PARAMS + ETF_T0_PARAMS 删除（零消费，整段死字典）

# ==================== 大盘态势判定参数（index_regime.py 日线核心 V2.2.4） ====================
# index_regime.py 的 _ir_params() 通过 globals().get("INDEX_REGIME_PARAMS") 合并本 dict
# 覆盖模块内 IR_DEFAULT_PARAMS 默认值（2026-07-18 全面对齐 V2.2.4 含 K-day 跃迁、SHARP C1~C6、价格结构强化与分数转折，键表见 index_regime.py IR_DEFAULT_PARAMS）。
# 红线：键名必须与模块 IR_DEFAULT_PARAMS 完全一致；V1 残留键（chop/均线排列等）一律删除，
# 否则成为死键或错误覆盖 V2 默认值。
# 评估时点说明：eod/morning/tail 是 detect_index_regime() 的调用参数（非配置键），
# 由 main.py 钩子按决策时点传入；内存缓存键为 f"{mode}:{date}"，TTL 由 score_cache_ttl 控制。
# state_dir / e5_ths_cache 为 None 哨兵的运维级键，不入本 dict（默认 None → t_io 落库）。
INDEX_REGIME_PARAMS = {
    # —— 合成与状态机 ——
    "trend_weight": 0.60,          # 趋势维度权重
    "env_weight": 0.40,            # 环境维度权重
    "enter_threshold": 25.0,       # 单边入场阈值 |S|
    "exit_threshold": 15.0,        # 单边退出阈值 |S|（立即生效）
    "enter_confirm_days": 2,       # 入场需连续 N 个交易日越过阈值（含当日）
    "smooth_ema_days": 3,          # 综合分 S 的 EMA 平滑窗口
    "hurst_window": 120,           # Hurst R/S 窗口
    "hurst_smooth": 20,            # H 的平滑日均窗口
    "exhaust_slope_pct": 0.80,     # 衰竭：|120日斜率| > 近一年 80% 分位
    "exhaust_bias_pct": 0.90,      # 衰竭：BIAS20 > 近一年 90% 分位
    "exhaust_factor": 0.7,         # 衰竭修正系数
    "score_cache_ttl": 1800,       # 评分内存缓存 TTL（秒）；缓存键=评估时点mode+日期
    # —— 趋势维度内权重（V2.2.4；结构分显式纳入）——
    "w_ma_streak": 0.35,           # T1 MA5>MA10 多头 streak 累积分
    "w_structure": 0.15,           # T1.5 价格结构强化（MA5/MA20/MA60）
    "w_adx": 0.18,                 # T2 ADX
    "w_reg_r2": 0.17,              # T3 回归斜率×R²
    "w_er": 0.08,                  # T4 Kaufman ER
    "w_aroon": 0.07,               # T5 Aroon
    # —— 环境维度内权重（合计 1.00）——
    "w_breadth": 0.35,             # E1 涨跌家数强度+ADL
    "w_nhnl": 0.25,                # E2 NH-NL（数据层降级中）
    "w_volume": 0.25,              # E3 量能确认
    "w_qvix": 0.15,                # E4 QVIX
    # —— T1 MA streak 累积分曲线（分段线性锚点：第k天 → 分值，±40 封顶）——
    "streak_curve": ((1, 8.0), (3, 16.0), (5, 24.0), (8, 32.0), (10, 36.0), (13, 40.0)),
    "streak_cap": 40.0,            # 累积分封顶（第13天起满级）
    "streak_late_day": 20,         # |streak|>=20 → 晚期警示（不再加分，R1 惩罚加倍）
    "late_penalty_mult": 2.0,      # 晚期警示下 R1 扣减比例倍数（0.5×2=1.0 → 清零）
    "r1_half_factor": 0.5,         # R1：streak 内收破 MA5 → 当日累积分扣减比例（收复 MA5 自动恢复）
    # R2【破位快速退出】为状态机结构规则（uni_up 收破 MA10 / uni_down 收复 MA10 → 当日退出
    # range，跳过连续确认；入场 streak 方向门控），无数值参数，见 _ir_step_regime。
    # —— R0 震荡三元组压缩（研究中震荡段 100% 命中 / 趋势段 0% 误判）——
    "r0_cross_min": 3,             # 近20日 MA5/MA10 交叉次数下限
    "r0_vol_max": 1.0,             # 量比 vol_MA5/vol_MA20 上限（缩量）
    "r0_pos_lo": 15.0,             # 20日价格区间位置带下沿
    "r0_pos_hi": 65.0,             # 20日价格区间位置带上沿
    "r0_factor": 0.6,              # 命中 → 总分 ×0.6
    # —— V2.1 K-day 关键日跃迁（校准自 sh000001_daily_features.csv，2026-07-18；
    #    K-up：04-08 实测 pct=+2.70%/margin=1.74%/cross15=3；K-down：05-14 实测 pct=-1.52%/prev_ma5_up=8；
    #    k_boost=9 校准：+4 在 E=-27.76 环境拖累+EMA 稀释下 04-08 仅 +2.7 跳升，不满足验收 +10）——
    "k_up_pct": 1.0,               # K-up 大阳线当日涨幅下限%（04-08=+2.70%）
    "k_down_pct": 1.0,             # K-down 大阴线当日跌幅下限%（05-14=-1.52%；04-24=-0.33% 排除）
    "k_margin": 0.3,               # K-up 收盘站上两线的最小余量%（06-12=0.08% 排除）
    "k_boost": 9,                  # K日 streak 等效天数跃迁（第1天按第10天档 36/40 计，存续期 real+boost 并入曲线）
    "k_cross_bg": 2,               # K-up 缠绕背景：近 k_cross_bg_days 日交叉次数下限（06-16 段 cross15<=1 排除）
    "k_cross_bg_days": 15,         # 缠绕背景回看窗
    "k_ma5_up_days": 3,            # K-down 前置：此前连续站上 MA5 天数下限
    "k_bull_streak_bg": 8,         # K-down 背景：多头 streak 下限（02-13 streak=3 排除）
    "k_anchor_recover_days": 2,    # 空头锚点解除：收复 MA5 连续天数
    # —— V2.2.4 指标锐化分 SHARP（转折日锐化 + 触发-衰减携带；2026-07-18 对齐 index_regime.py IR_DEFAULT_PARAMS）——
    # 机制：波动突破(0~9)+量能确认(0~5,默认补全)+均线状态(0~8,默认补全) → sharp_net/22×40 映射为
    # sharp_s（±40），规则层加法项（E5 同层，R0 之后 EMA 之前）；|sharp_s|>=32 当日触发全额计入，
    # 其后每交易日 ×0.5 衰减，新触发重置、状态切换清零、反向 K-day 清零。
    # V2.2.1 结构性修正：C1 同向触发抑制（uni_up 只监测空头/uni_down 只监测多头/range 双向，
    # 被抑制侧计入 detail 但不入 S、不触发、不重置衰减）；C2 补位缠绕门控（ku_fill/anchor_fill
    # 需近 k_cross_bg_days 日交叉>=k_cross_bg）；C3 触发线 20→28；C4 S 量程封顶（EMA 后 clip ±s_clip_max，记 s_pre_clip）。
    # V2.2.2 补丁：C5 档内子项聚合修复（收破确认/竞价跳空此前未计入 up/dn 聚合，波动侧由实际
    # 封顶5恢复规格满分9，04-08 sharp_up 13→17）；C6 触发线 28→32（= sharp_net 17.6/22，整数分
    # >=18：聚合修复后满档突破9+均线8=17→30.9 被拦，触发必须带量能确认 9+3+8=20→36.4）。
    # V2.2.4 追加：价格结构强化与分数突变 turn 规则，MA5/MA20/MA60 与 score_delta 同时参与转折判定。
    "sharp_full": 22,              # 锐化满分（波动9 + 量能5 + 均线8）
    "sharp_map_max": 40.0,         # sharp_s = sharp_net/sharp_full × 40（±40 封顶）
    "sharp_trigger": 32.0,         # 【V2.2.2 C6】转折触发线 |sharp_s|（= sharp_net 17.6/22，整数分>=18，必须带量能确认；V2.2.1 为 28）
    "sharp_decay": 0.5,            # 触发后每交易日衰减系数（age+1 → ×0.5）
    "sharp_suppress_same_dir": True,  # 【V2.2.1 C1】同向触发抑制：uni_up 只监测空头锐化、uni_down 只监测多头锐化
    "sharp_fill_cross_bg": True,   # 【V2.2.1 C2】补位缠绕门控：ku_fill/anchor_fill 需交叉背景>=k_cross_bg
    "s_clip_max": 100.0,           # 【V2.2.1 C4】S 量程封顶：EMA 之后 clip 到 ±100（detail.pipeline 记 s_pre_clip）
    "sharp_bo5_high": 5,           # 5日档：high > max(前5日 high)
    "sharp_bo5_close": 2,          # 5日档内：close > max(前5日 close)
    "sharp_bo3_high": 3,           # 3日档：high > max(前3日 high)（未达5日档时）
    "sharp_bo3_close": 1,          # 3日档内：close > max(前3日 close)
    "sharp_gap": 2,                # 档内竞价高开/低开：open vs prev_close
    "sharp_vol_15": 1.5,           # 【默认补全】vol_ma5/vol_ma20 高档（用户表格第2项截断，参数化待校正）
    "sharp_vol_12": 1.2,           # 【默认补全】vol_ma5/vol_ma20 低档
    "sharp_vol_hi_score": 5,       # 【默认补全】量比高档分值
    "sharp_vol_lo_score": 3,       # 【默认补全】量比低档分值
    "sharp_ma5": 4,                # 【默认补全】close 站上/跌破 MA5（用户表格第3项截断）
    "sharp_ma10": 4,               # 【默认补全】close 站上/跌破 MA10（与 MA5 叠加）
    "sharp_fast_enter": True,      # 多头锐化触发：RANGE 中 S>=enter 允许单日确认进 uni_up
    "sharp_fast_enter_down": False,  # 空头锐化单日确认进 uni_down（默认关，对齐 K-down 沿用退出规则）
    "ma5_slope_eps_pct": 0.0,       # MA5 斜率阈值（绝对值小于视为平）
    "full_above_ma5_confirm_days": 2,  # 全 K 站上 MA5 的确认天数
    "full_above_ma5_bonus": 8,      # 全 K 站上 MA5 的结构加分
    "ma60_break_ma5_slope_down_hard": True,  # MA60 破位 + MA5 下行硬切下行
    "full_above_ma5_hard_up": True, # 全 K 站上 MA5 硬切上行
    "struct_hard_before_r2": True,  # 结构硬转向优先于 R2 退出
    "score_drop_turn_threshold": 15.0,  # 分数单日下坠转折阈值
    "score_rise_turn_threshold": 15.0,   # 分数单日上冲转折阈值
    "score_turn_hard_enabled": True,     # 分数突变硬转向开关
    # —— 指标窗口 ——
    "atr_len": 14,                 # ATR（Wilder）窗口
    "adx_len": 14,                 # ADX 窗口
    "reg_len": 40,                 # T3 回归窗口
    "er_len": 10,                  # T4 Kaufman ER 窗口
    "er_smooth": 5,                # T4 ER 平滑
    "aroon_len": 25,               # T5 Aroon 窗口
    "aroon_smooth": 3,             # T5 Aroon 平滑
    "exhaust_reg_len": 120,        # 衰竭判定回归窗口
    "bias_len": 20,                # BIAS 窗口
    "pct_lookback": 250,           # 各类"近一年分位"回看窗
    "qvix_pct_lookback": 750,      # QVIX/HV20 近三年分位回看窗
    # —— E1 广度 ——
    "breadth_ema_days": 10,        # 涨跌家数强度 EMA 窗口
    "adl_lookback": 60,            # ADL 斜率回看窗
    # —— E3 量能打分档位 ——
    "vol_ratio_high": 1.2,         # 放量阈值（MA5/MA20 成交额比）
    "vol_ratio_low": 0.8,          # 缩量阈值
    "vol_fade_factor": 0.4,        # 缩量时方向分衰减系数
    # —— E4 QVIX 阈值 ——
    "qvix_panic_pct": 0.85,        # QVIX 近三年分位恐慌线
    "qvix_low_pct": 0.20,          # QVIX 近三年分位低波线
    "qvix_panic_ret20": -0.05,     # 恐慌修正所需的 20 日跌幅
    # —— E5 涨跌停情绪规则 ——
    "e5_dt_count": 30,             # 跌停家数触发线
    "e5_dt_delta": -10.0,          # 跌停触发时 S 修正值
    "e5_zt_count": 80,             # 涨停家数触发线
    "e5_zt_delta": 10.0,           # 涨停触发时 S 修正值
    "e5_zb_ratio": 0.45,           # 炸板率触发线
    "e5_zb_factor": 0.9,           # 炸板触发时 S 乘数
    "e5_s_threshold": 15.0,        # E5 触发所需的 |S| 门槛
    # —— E5 数据源（V2：同花顺主源 + 东财近3周兜底）——
    "e5_source": "ths",            # "ths" 同花顺涨停聚焦（>=8个月历史）；"em" 强制东财
    "e5_em_fallback": True,        # THS 失败/全0 时东财三池兜底（仅近 ~3 周有效）
    # —— 数据与 IO ——
    "index_symbol_sh": "sh000001", # 上证指数代码（腾讯日线主源）
    "index_symbol_sz": "sz399001", # 深证成指代码（成交额腿）
    "kline_count_sh": 900,         # 上证日线拉取根数
    "kline_count_sz": 450,         # 深证日线拉取根数
    "min_bars": 150,               # 趋势指标所需最少日线数（不足则逐项降级）
    "http_timeout": 15,            # 外部数据单次硬限时（秒）
    "http_retry": 2,               # 外部数据重试次数
    "http_retry_sleep": 1.0,       # 重试间隔（秒）
}

# V3.0fix: INDEX_INTRADAY_PARAMS 删除（9键全部与模块默认值相同，main.py 本就 merge，效果不变）

# ==================== 日志双写配置 ====================
log = logging.getLogger("做T助手")
log.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

if not log.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)

    sys_log_file = os.path.join(LOG_DIR, f"t_trader_sys_{get_today_str()}.log")
    file_handler = logging.FileHandler(sys_log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)

logging.getLogger("apscheduler").setLevel(logging.WARNING)

# ==================== 全局状态与统计 ====================

HOLDINGS: Dict[str, dict] = {}
STRATEGY_MEMORY: Dict[str, dict] = {}
VIRTUAL_TRADES: Dict[str, Dict[str, list]] = {}
AI_REVIEW_STATS: Dict[str, dict] = {}
MINUTE_FETCH_STATUS: Dict[str, str] = {}
MINUTE_FETCH_DETAIL: Dict[str, str] = {}
DAILY_DECISION_STATS: Dict[str, dict] = {}
SIGNAL_OUTCOME_TRACKER: Dict[str, list] = {}
DAILY_CONTEXT_CACHE: Dict[str, Dict[str, Any]] = {}
SESSION_CONTEXT: Dict[str, Any] = {}
T_MODE: Dict[str, str] = {}  # V1.26: T模式配置 {code: 'long'|'short'}
PREOPEN_CONTEXT: Optional[Any] = None
_preopen_logged_date: Optional[str] = None
_preopen_pushed_date: Optional[str] = None
_preopen_monitor_date: Optional[str] = None
_preopen_monitor_last_push_at: Optional[datetime] = None
_preopen_monitor_last_signature: Optional[str] = None
_preopen_monitor_push_count: int = 0
_preopen_overview_last_push_at: Optional[datetime] = None
_eod_logged_date: Optional[str] = None
_scan_lock = False

# ---- 大盘态势判定（index_regime / index_regime_intraday）全局状态 ----
INDEX_REGIME_CONTEXT: Dict[str, Any] = {}              # 最新大盘态势上下文（仿 PREOPEN_CONTEXT 范式，由 push_index_regime_context 更新）
_index_regime_morning_pushed_date: Optional[str] = None  # 早盘基调推送每日去重
_index_regime_eod_pushed_date: Optional[str] = None      # 收盘评分推送每日去重
_index_intraday_alert_cache: Dict[str, float] = {}       # {预警tag: 上次推送时间戳}，同 tag 60 分钟内不重复推

# ==================== 指数5分钟共振过滤（做T信号，2026-08-14 新增） ====================
# 逻辑见 index_resonance.py；门控接在 main.py 信号推送前。
INDEX_RESONANCE_PARAMS = {
    "enabled": True,                # 总开关；False 时不拦截、仅落盘
    # 门控口径: "index_ma5_dir"(指数5分钟MA5方向, 默认) / "contrarian"(反向) / "same_direction"(同向极值) / "non_contrary"(不逆势)
    # 2026-08-15 修正未来函数后的诚实结论(35候选/1年, +0.5%/-0.4%/30tick, 无lookahead)：
    #   index_ma5_dir(买需指数站上其5分钟MA5/卖需指数跌破其MA5) 放行49.6% vs 全池45.9% → 真实增益 +4.4pp
    #   （此前报的 +20pp 系回测取了未收盘指数根的未来函数假象，已修正；数值以本注释为准）
    #   contrarian 47.4%(+2.5pp)；15分钟MACD信号(macd15_bb5)修正后无效，已回退
    "gate": "index_ma5_dir",
    "fail_closed": True,            # 指数数据缺失/不足时拦截信号
    # 同向极值（指数5分钟与个股同处极值区）
    "buy_bb_max": 0.25,             # 低吸: 指数 bb_pct_5m <= buy_bb_max
    "buy_rsi_max": 40.0,            # 低吸: 指数 rsi_6_5m <= buy_rsi_max
    "sell_bb_min": 0.75,            # 高抛: 指数 bb_pct_5m >= sell_bb_min
    "sell_rsi_min": 60.0,           # 高抛: 指数 rsi_6_5m >= sell_rsi_min
    # 不逆势（指数未逆向于交易方向）
    "buy_floor": -0.30,             # 低吸: 指数 bb_pct_5m >= buy_floor（未深破下轨）
    "sell_floor": -0.20,            # 高抛: 指数 bb_pct_5m >= sell_floor（不在恐慌底）
    "min_index_5m_bars": 5,         # 指数5分钟K线最少根数，不足视为数据不足
}

# ==================== C-1: 做T/接回意图分流（2026-08-21 评审通过） ====================
# SELL_HIGH(日内了结) 跳过指数共振门控直接放行——08-19 破线日全天 0 条卖出信号是真正缺口，
# 卖侧不受指数 MA5 尺约束；BUY_LOW 维持共振门控 + C-2 个股 MA5 闸；接回/加仓维持门控不变。
RESONANCE_GATE = {
    "enabled": True,            # 总开关；False 时恢复旧行为（全部信号走共振门控）
    "bypass_sell_high": True,   # SELL_HIGH 跳过共振门控
}
# 个股/ETF → 板块指数覆盖（分板默认之外显式指定）；值 = (index_code, index_name)
INDEX_RESONANCE_MAP = {
    "588170": ("sh000688", "科创50"),   # 科创半导体ETF → 科创50
}

# ==================== 建仓/加仓时机判定（timing_gate.py，2026-08-15 新增） ====================
# 基于 W34 时机实验（17863行两时段）：多头趋势追强、空头趋势抄底、震荡降频。
ENTRY_TIMING_PARAMS = {
    "enabled": True,          # 总开关；False 时不参与建仓/加仓判定
    "regime_ma60": True,      # 用指数 vs MA60 定市场状态
    "regime_up_buffer": 1.005,  # B-3(2026-08-21): 多头缓冲带 close>MA60*1.005 才 trend_up；中间带归 range，防 razor 横跳
    "trend_up_drawdown_min": -0.03,   # 多头趋势：浅回撤阈值（追强）
    "trend_dn_drawdown_max": -0.10,   # 空头趋势：深回撤阈值（抄底）
    "trend_dn_rsi_max": 20.0,         # 空头趋势：RSI(14) 深度超卖阈值（抄底超卖极值，2026-08-16 实验两时段稳健）
    "apply_to_add": True,     # 加仓侧也应用时机门控（NO-GO 时阻断加仓买入，降频）
    "add_block_rebuild": True,  # NO-GO 时是否也阻断"接回"(rebuild)；False=仅阻断首加
}

# ==================== B-2: C20 竞价现实校验（2026-08-21 评审通过，双条件与门） ====================
# 09:26 基调推送前校验：持仓缺口(gap_med) 与 Top20 竞价跌占比 双条件，只纠"乐观错"不纠"悲观错"
# Level1(降级标注黄条): gap_med<=l1_gap 且 top20跌占比>=l1_top20_down_ratio
# Level2(推翻基调红条): gap_med<=l2_gap 且 top20跌占比>=l2_top20_down_ratio
# Top20 缺失(top20_status=empty)时退化为缺口单条件，卡片标注"Top20缺失·单条件"
C20_AUCTION_CHECK = {
    "enabled": True,          # 总开关；False 时跳过竞价校验（回滚用）
    "l1_gap": -1.0,           # Level1 持仓缺口中位数阈值(%)
    "l2_gap": -2.5,           # Level2 持仓缺口中位数阈值(%)
    "l1_top20_down_ratio": 0.60,  # Level1 Top20 跌家占比阈值
    "l2_top20_down_ratio": 0.75,  # Level2 Top20 跌家占比阈值
}

# ==================== V1.26: T模式配置（正T/反T切换） ====================
# long = 正T（先买后卖，默认）
# short = 反T（先卖后买，下跌趋势用）
_T_MODE_VALID = {"long", "short"}


def _normalize_t_mode_value(value: Any) -> str:
    if value in _T_MODE_VALID:
        return str(value)
    return ""


def load_t_mode() -> Dict[str, str]:
    """加载T模式配置，返回 {code: 'long'|'short'}"""
    if not os.path.exists(T_MODE_FILE):
        return {}
    try:
        with open(T_MODE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        runtime: Dict[str, str] = {}
        for k, v in data.items():
            if str(k).startswith("_"):
                continue
            mode = _normalize_t_mode_value(v)
            if mode:
                runtime[str(k)] = mode
        return runtime
    except Exception as e:
        log.warning(f"⚠️  T模式配置读取失败: {str(e)[:80]}")
    return {}


def save_t_mode(t_mode: Dict[str, str]):
    """保存T模式配置到文件"""
    try:
        existing = {}
        if os.path.exists(T_MODE_FILE):
            with open(T_MODE_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        merged = {k: v for k, v in existing.items() if str(k).startswith("_")}
        for k, v in (t_mode or {}).items():
            if str(k).startswith("_"):
                merged[k] = v
                continue
            mode = _normalize_t_mode_value(v)
            if mode:
                merged[k] = mode
        with open(T_MODE_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"⚠️  T模式配置保存失败: {str(e)[:80]}")


# 反T模式（short）专属参数覆盖
# 核心逻辑反转：正T是"低买高卖"，反T是"高卖低买"
# 参数调整原则：
#   1. 降低卖出门槛（鼓励早盘/冲高卖出）
#   2. 提高买入门槛（只接回深跌）
#   3. 取消"价格低于VWAP禁买"限制（反T需要在低位接回）
#   4. 早盘允许卖出（反T的核心是早盘先卖）
# V1.26fix: SHORT_MODE_PARAMS removed in V3.0 (no consumers — dead config block)

# ==================== W33 A3: 仓位管理器共享计算（t_gui 与 position_builder 同源） ====================
# 从 t_gui.load_position_manager 内联逻辑抽取，避免 GUI/建仓扫描两处实现漂移。

def build_position_gap(total_capital: float, raw_list: list, default_pct: float = 0.30) -> dict:
    """由各持仓的基础市值/目标比例计算归一化目标市值与欠配缺口。

    raw_list: [{code, name, raw_pct, mkt_val, total_qty}]（A/B 双账户已按基础代码合并；
               mkt_val=当前市值，total_qty=当前总股数，raw_pct=个股目标比例）
    default_pct: raw_pct 缺失时回落的全局默认比例（PARAMS stock_qty_base_pct）。

    返回 {ratio_sum, rows:[{code,name,target_pct,target_val,mkt_val,price,gap_pct,gap_qty,
          add_batches,over,under}]} — 口径与 t_gui.load_position_manager 一致。
    """
    ratio_sum = sum(r.get("raw_pct", default_pct) for r in raw_list) or 1.0
    rows = []
    for r in raw_list:
        raw_pct = r.get("raw_pct", default_pct)
        target_pct = raw_pct / ratio_sum  # 归一化：总和=100%
        target_val = total_capital * target_pct
        mkt_val = float(r.get("mkt_val") or 0)
        total_qty = int(r.get("total_qty") or 0)
        pct = mkt_val / total_capital if total_capital else 0
        gap_pct = (mkt_val / target_val - 1) if target_val else 0
        px = (mkt_val / total_qty) if total_qty else 0
        gap_val = target_val - mkt_val
        if px > 0:
            gap_qty = int(gap_val / px // 100) * 100  # 欠配=+可加，超配=-应减
        else:
            gap_qty = 0
        # 欠配股数三等分：每批整手(100股)，末批含余数；不足3手不强分3批
        add_batches = []
        if gap_qty > 0:
            if gap_qty >= 300:
                batch = (gap_qty // 3 // 100) * 100
                add_batches = [batch, batch, gap_qty - 2 * batch]
                if add_batches[2] < 100:
                    add_batches[1] += add_batches[2]
                    add_batches = add_batches[:2]
            elif gap_qty >= 200:
                half = gap_qty // 2 // 100 * 100
                add_batches = [half, gap_qty - half]
            else:
                add_batches = [gap_qty]
        rows.append({
            "code": r.get("code"), "name": r.get("name"),
            "target_pct": round(target_pct, 4),
            "target_val": round(target_val, 0),
            "mkt_val": round(mkt_val, 0),
            "total_qty": total_qty,
            "price": round(px, 3) if total_qty else 0,
            "pct": round(pct * 100, 1),
            "gap_pct": round(gap_pct * 100, 1),
            "gap_qty": int(gap_qty),
            "add_batches": add_batches,
            "over": gap_pct > 0.05,    # 超配 >5%
            "under": gap_pct < -0.05,  # 欠配 >5%
        })
    return {"ratio_sum": ratio_sum, "rows": rows}


# ==================== V3.0: 大盘热度×韭研TOP3 联动（daily_sentiment.py） ====================
# daily_sentiment.py 的 sentiment_params() 通过 globals().get("SENTIMENT_PARAMS") 合并本 dict
# 覆盖模块内 DEFAULT_SENTIMENT_PARAMS 默认值。归一化常量口径：108 日窗（2026-02~2026-07）
# z_S=(S+4.41)/31.82；z_top3=(top3_avg-5.05)/2.34；生产 60 日滚动、样本不足回退常量。
SENTIMENT_PARAMS = {
    # —— z 归一化常量（108 日窗校准，回退用）——
    "z_S_mean": -4.41, "z_S_std": 31.82,
    "z_top3_mean": 5.05, "z_top3_std": 2.34,
    "rolling_window": 60,              # 生产滚动归一化窗口（交易日）
    "rolling_min_samples": 20,         # 历史样本不足则回退 108 日常量
    # —— 热度分档（z_top3）——
    "overheat_z": 1.5,                 # >= +1.5 过热
    "ice_z": -1.0,                     # <= -1.0 冰点
    "overheat_streak_days": 2,         # uni_up 连续过热 N 日 → 反T止盈
    "uni_down_ban_long_days": 3,       # uni_down 连续 >=N 日 → 禁止正T
    # —— 系统性风险（V2.1：z_S≤阈值 → 当日全标的 hold + systemic_risk；E5跌停潮/指数暴跌为清仓升级确认）——
    "sysrisk_z_S": -1.5,               # z_S 阈值
    "sysrisk_index_drop_pct": -2.0,    # 指数当日跌幅阈值 %（清仓流程升级确认条件之一）
    "sysrisk_e5_dt": 30,               # E5 跌停潮阈值（家，对齐 e5_dt_count；清仓流程升级确认条件之一）
    "sysrisk_intraday_enforce": True,  # V2.1: 14:30 tail z_S≤阈值 → 当日全标的 hold（盘中生效，不等次日）
    # —— V2/V2.1 个股级覆盖规则（优先级 P1>P2>P3>P4>P5>P6>P7，见 daily_sentiment.per_stock_decisions）——
    "stock_diverge_drop_5d": -8.0,     # P2 个股前5日累计跌幅% ≤ 此值 → 背离否决禁 long
    "stock_diverge_below_ma5_days": 3,  # P2 收盘连续 N 日 <MA5 → 背离否决禁 long
    "enable_yesterday_crash_veto": True,  # P3 昨日大跌否决开关
    "yesterday_crash_pct": -4.0,       # P3 昨日跌幅% ≤ 此值 → 次日禁 long 降 hold
    "yesterday_limit_pct": -9.8,       # P4 昨日跌幅% ≤ 此值（近似跌停/一字板）→ 次日 hold
    "loss_streak_days": 2,             # P6 同一标的连续 N 日做T亏损 → 次日 hold
    "gap_up_no_chase_pct": 1.0,        # P7 正T日竞价高开 >此值% → 标注等回踩VWAP确认才买
    "gap_vwap_retrace_pct": 0.3,       # P7 标注文案中的 VWAP 回踩幅度%
    "closure_audit_file": None,        # P6 数据源；None → <BASE_DIR>/t_io/logs/closure_audit.jsonl
    # —— 执行层参数（供 signal_engine/下游读取）——
    "stop_loss_pct": 0.008,            # 正T买后浮亏-0.8%立即止损 / 反T卖后反向+0.8%接回止损
    "profit_target_pct": 0.008,        # 做T单笔止盈目标 0.8%
    "force_flat_time": "14:50",        # 尾盘强制平仓/接回时点
    # —— 决策矩阵（plan.md V3.0 原表，可配置；键=regime|heat，值=[mode, pos_factor, 理由]）——
    "t_matrix": {
        "uni_up|overheat": ["long", 0.5, "单边上涨×过热→正T半仓，禁追买"],
        "uni_up|hot": ["long", 1.0, "单边上涨×偏热→正T标准仓"],
        "uni_up|cold": ["long", 1.0, "单边上涨×偏冷→正T标准仓(B2区低吸)"],
        "uni_up|ice": ["long", 1.2, "单边上涨×冰点→正T加仓"],
        "range|overheat": ["short", 1.0, "震荡×过热→反T标准仓(S4禁追)"],
        "range|hot": ["long", 1.0, "震荡×偏热→正T标准仓"],
        "range|cold": ["long", 1.0, "震荡×偏冷→正T标准仓"],
        "range|ice": ["long", 1.2, "震荡×冰点→正T加仓(B1区低吸)"],
        "uni_down|overheat": ["short", 0.5, "单边下行×过热→反T轻仓"],
        "uni_down|hot": ["short", 1.0, "单边下行×偏热→反T标准仓"],
        "uni_down|cold": ["short", 0.5, "单边下行×偏冷→反T轻仓"],
        "uni_down|ice": ["short", 1.0, "单边下行×冰点→反T标准仓，先卖后买"],
    },
    # —— V1.28: 量化分数动态调仓（z_S 维度，叠加在矩阵输出之上）——
    "z_score_pos_factor_boost": True,      # 是否启用 z_S 分数动态调仓
    "z_score_sell_bias_threshold": -1.0,   # z_S ≤ 此值 → uni_down反T仓位×1.3
    "z_score_buy_cap_threshold": 1.5,      # z_S ≥ 此值 → 非uni_down仓位限制≤0.7
    # —— 数据源与落盘 ——
    "report_gen_dir": r"E:\04_实战资料\report_gen",
    "log_dir": None,                   # None → env SENTIMENT_LOG_DIR > BASE_DIR/logs
    "push_enabled": True,
}

# V3.0: T_AUTO_MODE=1 时启动跳过人工确认，直接采用决策矩阵逐股建议（无人值守）
AUTO_T_MODE = os.getenv("T_AUTO_MODE", "0").strip() == "1"

# V3.0: 闭环审计（_maybe_audit_closure）每日去重
_closure_audit_date: Optional[str] = None

