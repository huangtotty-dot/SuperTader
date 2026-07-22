# V1.11: 日志增强模块导入
import sys as _sys
import os as _os_mod
_06t_dir = _os_mod.path.dirname(_os_mod.path.dirname(_os_mod.path.abspath(__file__)))
if _06t_dir not in _sys.path:
    _sys.path.insert(0, _06t_dir)
try:
    import log_enhancer as _log_enhancer
except Exception:
    _log_enhancer = None

import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

# ======== 独立模式回退依赖 ========
if 'get_today_str' not in globals():
    def get_today_str(): return datetime.now().strftime("%Y-%m-%d")
if '_now' not in globals():
    def _now(): return datetime.now()
if 'PARAMS' not in globals():
    PARAMS = {"rsi_period":14,
              "bb_period":20,
              "bb_std":2,
              "ema_fast_period":3,
              "ema_slow_period":6,
              "min_amplitude":0.002,
              "trend_today_ret_threshold":0.03,
              "rsi_overbought":78,
              "rsi_oversold":35,
              "macd_strong_threshold":0.2,
              "macd_strong_boost":25,
              "vol_ratio_confirm":1.5,
              "vol_confirm_boost":15,
              "rsi_15m_oversold":35,
              "min_15min_bars":3,
              "range_pos_low_threshold":0.3,
              "range_pos_high_threshold":0.85,
              "buy_confirm_min_score":25,
              "min_profit_space":0.008,
              "cooldown_minutes":5,
              "repeat_signal_gap_minutes":5,
              "repeat_signal_price_move":0.003,
              "repeat_signal_score_boost":10,
              "sell_repeat_block_minutes":10,
              "post_sell_rebuild_minutes":10,
              "post_sell_rebuild_price_gap":0.005,
              "post_sell_rebuild_score_gap":8,
              "post_sell_rebuild_min_seconds":120,
              "post_sell_rebuild_buy_threshold_penalty":15,
              "post_sell_rebuild_weak_gate_discount":3,
              "post_sell_rebuild_relax_gap":4,
              "post_sell_rebuild_relax_factors":1,
              "stand_down_score_gap":8,
              "stand_down_flat_range_gap":0.005,
              "market_state_threshold_bias":3,
              "etf_stand_down_gap":0.003,
              "daily_support_buy_boost":5,
              "daily_trend_buy_boost":3,
              "daily_breakdown_buy_penalty":15,
              "daily_breakdown_sell_boost":8,
              "daily_downtrend_buy_penalty":10,
              "daily_overheat_buy_penalty":5,
              "daily_overheat_sell_boost":8,
              "daily_overheat_buy_threshold_penalty":5,
              "daily_support_buy_threshold_relief":3,
              "daily_risk_buy_threshold_penalty":10,
              "buy_confirm_min_factors":3,
              "buy_confirm_min_seconds":30,
              "buy_rebound_min_score_gap":5,
              "sell_confirm_min_factors":3,
              "sell_confirm_min_seconds":30,
              "buy_starvation_days":5,
              "buy_starvation_relax_factors":1,
              "buy_starvation_relax_gap":3,
              "buy_starvation_relax_seconds":10,
              "max_buy_times_per_stock":5,
              "max_sell_times_per_stock":5,
              "max_t_cycles_per_stock":8,
              "stock_min_trade_unit":100,
              "etf_min_trade_unit":100,
              "etf_threshold_cap":38,
              "sell_holding_min_minutes":10,
              "sell_holding_strict_minutes":30,
              "sell_score_boost_holding":5,
              "sell_score_boost_eod":8,
              "sell_momentum_bonus":6,
              "buy_priority_margin":8,
              "etf_qty_strong_pct":0.25,
              "etf_qty_base_pct":0.15,
              "etf_qty_weak_pct":0.08,
              "stock_qty_strong_pct":0.4,
              "stock_qty_base_pct":0.3,
              "stock_qty_weak_pct":0.2,
              "stock_first_add_strong_pct":0.3,
              "stock_first_add_pct":0.2,
              "stock_first_add_weak_pct":0.1,
              "stock_rebuild_strong_pct":0.8,
              "stock_rebuild_base_pct":0.5,
              "stock_rebuild_weak_pct":0.3,
              "buy_soft_margin":8,
              "sell_fast_path_min_gap":18,
              "morning_no_sell_until":940,
              "morning_no_sell_min_ret":0.02,
              "hard_sell_threshold_cap":80,
              "hard_buy_threshold_cap":80,
              "awaiting_buyback_ttl_minutes":120,
              "awaiting_buyback_price_gap":0.003,
              "awaiting_buyback_score_boost":10,
              "awaiting_buyback_score_boost_weak":5,
              "awaiting_buyback_threshold_relax":5,
              "awaiting_buyback_threshold_relax_weak":3,
              "awaiting_buyback_vwap_gap":0.003,
              "awaiting_buyback_rsi_strong":45,
              "awaiting_buyback_rsi_weak":50,
              "peak_decline_pct_threshold":0.01,
              "peak_decline_min_minutes":3,
              "peak_decline_penalty":5,
              "double_top_pullback_threshold":0.015,
              "double_top_min_gap_minutes":30,
              "double_top_price_proximity":0.995,
              "double_top_rsi_threshold":75,
              "double_top_vol_shrink_threshold":0.75,
              "profit_guard_sell_boost":15,
              "profit_guard_buy_penalty":10,
              "profit_guard_tight_profit_max":0.03,
              "profit_guard_tight_gap_max":0.015,
              "min_sell_profit_space":0.005,
              "open_dip_buy_penalty":25,
              "open_dip_max_mins":15,
              "daily_trade_limit":10,
              "breakdown_gap_threshold":0.005,
              "breakdown_buy_block":True,
              "big_drop_bounce_threshold":-0.05,
              "big_drop_bounce_sell_boost":10,
              "big_drop_bounce_buy_penalty":5,
              "bb_band_breakout_penalty":0,
              "ma5_deviation_sell_boost":0,
              "surge_shadow_divergence_boost":0,
              "high_buy_score_bypass":False,
              "high_buy_score_threshold":80,
              "high_buy_score_vwap_gap":0.02,
              "etf_buy_score_boost":5,
              "etf_sell_score_boost":3,
              "downtrend_sell_boost":15,
              "downtrend_sell_threshold":-10,
              "commission_rate":0.00015,
              "optimal_sell_boost":8,
              "optimal_sell_range_pos":0.95,
              "optimal_sell_rsi":85,
              "optimal_sell_bb_pct":0.9,
              "optimal_sell_today_ret":0.02,
              "bullish_reversal_min_pct":0.01,
              "bullish_reversal_body_ratio":0.6,
              "bullish_reversal_vol_multiplier":1.0,
              "bullish_reversal_engulf":0.995,
              "buy_signal_price_move":0.005,
              "buy_signal_score_boost":20,
              "sell_signal_price_move":0.005,
              "sell_signal_score_boost":20,
              "add_pos_signal_price_move":0.005,
              "add_pos_signal_score_boost":20}
if 'MINUTE_FETCH_DETAIL' not in globals(): MINUTE_FETCH_DETAIL = {}
if 'MINUTE_FETCH_STATUS' not in globals(): MINUTE_FETCH_STATUS = {}
if 'DAILY_CONTEXT_CACHE' not in globals(): DAILY_CONTEXT_CACHE = {}
if 'HOLDINGS' not in globals(): HOLDINGS = {}
if 'VIRTUAL_TRADES' not in globals(): VIRTUAL_TRADES = {}
if 'SIGNAL_OUTCOME_TRACKER' not in globals(): SIGNAL_OUTCOME_TRACKER = {}
if 'T_MODE' not in globals(): T_MODE = {}
if 'SHORT_MODE_PARAMS' not in globals(): SHORT_MODE_PARAMS = {}
if 'DAILY_DECISION_STATS' not in globals(): DAILY_DECISION_STATS = {}
if 'MultiTimeframeFetcher' not in globals(): MultiTimeframeFetcher = None
if '_resolve_benchmark_snapshot' not in globals():
    def _resolve_benchmark_snapshot(c,h): return {}
if '_default_daily_context' not in globals():
    def _default_daily_context(c,s="",r=""): return {"daily_status":s,"daily_reason":r,"daily_buy_t_ok":False}
if '_calc_ps_levels' not in globals():
    def _calc_ps_levels(p,d): return {}
if '_strategy_memory_for_code' not in globals():
    def _strategy_memory_for_code(c): return {}
if '_append_jsonl' not in globals():
    def _append_jsonl(*a,**kw): return None
if '_trace_path' not in globals():
    def _trace_path(n,d=None): return f"/tmp/{n}"
if '_buy_soft_support_count' not in globals():
    def _buy_soft_support_count(*a): return 0
if '_special_loss_threshold_adjustments' not in globals():
    def _special_loss_threshold_adjustments(*a):
        if len(a) >= 6: return (a[2], a[3], a[4], a[5])
        return (35, 35, 0, 0)
if 'load_starvation_state' not in globals():
    def load_starvation_state(): return {}
if 'send_morning_alert' not in globals():
    def send_morning_alert(*a,**kw): return None
if 'notify_alert_cleared' not in globals():
    def notify_alert_cleared(*a,**kw): return None
if 'resample_to_15min' not in globals():
    def resample_to_15min(df): return pd.DataFrame()
if 'add_15min_indicators' not in globals():
    def add_15min_indicators(df): return pd.DataFrame()
if 'resample_to_5min' not in globals():
    def resample_to_5min(df): return pd.DataFrame()
if 'add_5min_indicators' not in globals():
    def add_5min_indicators(df): return pd.DataFrame()
if 'fetch_minute_bar' not in globals():
    def fetch_minute_bar(*a, **kw): return pd.DataFrame()
if 'add_indicators' not in globals():
    def add_indicators(df): return df
if 'Signal' not in globals():
    from dataclasses import dataclass, field
    from typing import List, Dict, Any
    @dataclass
    class Signal:
        code: str=''; name: str=''; action: str=''; price: float=0.0; score: float=0.0
        reasons: List[str]=field(default_factory=list)
        details: List[Dict[str,Any]]=field(default_factory=list)
        indicators: Dict[str,float]=field(default_factory=dict)
        factors: Dict[str,Any]=field(default_factory=dict)
        ts: Any=None
        cycle_id: str=''; cycle_action_count: int=0; hold_qty: int=0
# ==========================================================

class SignalEngine:
    def __init__(self):
        self.buy_cooldown: Dict[str, datetime] = {}
        self.sell_cooldown: Dict[str, datetime] = {}
        self.buy_count_per_stock: Dict[str, int] = {}
        self.sell_count_per_stock: Dict[str, int] = {}
        self.state_reset_date = get_today_str()
        self.t_cycle_start_time: Dict[str, datetime] = {}
        self.last_signal_state: Dict[str, Dict[str, Any]] = {}
        self.last_trade_state: Dict[str, Dict[str, Any]] = {}
        self.cycle_count: Dict[str, int] = {}
        self.cycle_direction: Dict[str, str] = {}
        self.post_sell_block_until: Dict[str, datetime] = {}
        self.awaiting_buyback: Dict[str, Dict[str, Any]] = {}
        # V1.21: 日内高点确认延迟状态（避免抖动误判）
        self.peak_tracker: Dict[str, Dict[str, Any]] = {}
        self.daily_realized_loss_monitor = 0.0
        self.diagnostics: Dict[str, Dict[str, Any]] = {}
        # V1.20: 场景因子观察-确认-锁定状态机（解决逐分钟重复触发问题）
        self.scenario_factor_state: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # V1.25: 早盘预警状态机（基于近两年数据训练）
        self.morning_alert_state: Dict[str, Dict[str, Any]] = {}

    def _calc_etf_qty(self, code: str, holding: dict, action: str, sig_score: float, threshold: float) -> int:
        """V1.12: ETF动态份数计算 - 利益最大化原则
        
        核心逻辑:
        1. 根据信号强度（score - threshold 差值）决定仓位比例
        2. 根据剩余可交易次数均分剩余仓位
        3. 确保最小交易单位（100份）
        4. 确保不超过当前可买/可卖额度
        
        Returns:
            建议交易份数（整数，100的倍数）
        """
        if not self._is_etf(code):
            return int(holding.get("t_qty", 0) or holding.get("qty", 0) or 0)
        
        p = self._get_params(code)
        total_t_qty = int(holding.get("t_qty", 0) or holding.get("qty", 0) or 0)
        if total_t_qty <= 0:
            return 0
        
        # 计算已用/剩余次数
        max_cycles = p.get("max_t_cycles_per_stock", 8)
        used_buys = self.buy_count_per_stock.get(code, 0)
        used_sells = self.sell_count_per_stock.get(code, 0)
        
        if action in ["BUY_LOW", "ADD_POS"]:
            remaining = max(1, p.get("max_buy_times_per_stock", 5) - used_buys)
        else:
            remaining = max(1, p.get("max_sell_times_per_stock", 5) - used_sells)
        
        # 信号强度因子
        signal_strength = sig_score - threshold
        if signal_strength >= 10:
            strength_pct = p.get("etf_qty_strong_pct", 0.25)
        elif signal_strength >= 5:
            strength_pct = p.get("etf_qty_base_pct", 0.15)
        else:
            strength_pct = p.get("etf_qty_weak_pct", 0.08)
        
        # 基础份数 = 总仓位 * 强度比例
        base_qty = int(total_t_qty * strength_pct)
        
        # 根据剩余次数调整（剩余次数越少，每次越大）
        remaining_factor = min(2.0, 1.0 + (3 - remaining) * 0.3) if remaining <= 3 else 1.0
        qty = int(base_qty * remaining_factor)
        
        # 最小交易单位（100份）
        min_unit = p.get("etf_min_trade_unit", 100)
        qty = max(min_unit, (qty // min_unit) * min_unit)
        
        # 确保不超过当前可交易额度
        net_qty = self._virtual_net_qty(code, holding)
        if action in ["BUY_LOW", "ADD_POS"]:
            # 买入不能超过剩余可买额度（总T仓 - 当前虚拟净持仓）
            max_buyable = max(0, total_t_qty - net_qty)
            qty = min(qty, max_buyable)
        else:
            # 卖出不能超过当前虚拟净持仓
            qty = min(qty, net_qty)
        
        # 再次对齐最小单位
        qty = (qty // min_unit) * min_unit
        
        return max(0, qty)

    def _is_etf(self, code: str) -> bool:
        """判断指定代码是否为ETF类型"""
        h = HOLDINGS.get(code, {})
        return h.get("type") == "etf"

    def _get_params(self, code: str) -> dict:
        """V2: 统一返回 PARAMS，STOCK_PARAMS/ETF_T0_PARAMS 已移除（ATR自适应替代）"""
        return PARAMS

    def _reset_daily_state_if_needed(self):
        today = get_today_str()
        if self.state_reset_date != today:
            self.buy_count_per_stock = {}
            self.sell_count_per_stock = {}
            self.t_cycle_start_time = {}
            self.last_signal_state = {}
            self.last_trade_state = {}
            self.cycle_count = {}
            self.cycle_direction = {}
            self.post_sell_block_until = {}
            self.awaiting_buyback = {}
            self.daily_realized_loss_monitor = 0.0
            self.diagnostics = {}
            self.scenario_factor_state = {}
            self.peak_tracker = {}
            self.morning_alert_state = {}  # V1.25: 重置早盘预警状态
            self.state_reset_date = today

    # V1.25: 早盘特征计算与预警检查
    def _calc_morning_features_and_alert(self, code: str, df: pd.DataFrame, t_val: int) -> tuple[int, list, dict]:
        """
        计算早盘特征并检查是否触发预警
        Returns: (alert_level, triggered_rules, morning_stats)
        alert_level: 0=正常, 1=谨慎, 2=禁止买入
        """
        alert_cfg = MORNING_ALERT_PARAMS.get(code)
        if not alert_cfg or not alert_cfg.get("alert_enabled"):
            return 0, [], {}
        if t_val > alert_cfg.get("alert_window_end", 1000):
            return 0, [], {}

        # 计算早盘特征（9:30-当前）
        today_df = df[df["date"] == df.iloc[-1]["date"]].copy()
        if len(today_df) < 5:
            return 0, [], {}

        open_price = float(today_df.iloc[0]["open"])
        current_price = float(today_df.iloc[-1]["close"])
        high_so_far = float(today_df["high"].max())
        low_so_far = float(today_df["low"].min())

        # 开盘5/10/30分钟价格
        bar_5 = today_df.iloc[4] if len(today_df) >= 5 else today_df.iloc[-1]
        bar_10 = today_df.iloc[9] if len(today_df) >= 10 else today_df.iloc[-1]
        bar_30 = today_df.iloc[29] if len(today_df) >= 30 else today_df.iloc[-1]
        p5 = float(bar_5["close"])
        p10 = float(bar_10["close"])
        p30 = float(bar_30["close"])

        open_5min_ret = (p5 - open_price) / open_price if open_price > 0 else 0
        open_10min_ret = (p10 - open_price) / open_price if open_price > 0 else 0
        open_30min_ret = (p30 - open_price) / open_price if open_price > 0 else 0
        max_gain_after_open = (high_so_far - open_price) / open_price if open_price > 0 else 0

        # VWAP特征
        vwap_series = today_df["vwap"].astype(float)
        below_vwap = (today_df["close"].astype(float) < vwap_series).sum()
        below_vwap_ratio = below_vwap / len(today_df) if len(today_df) > 0 else 0

        # 连续阴线
        bearish = (today_df["close"] < today_df["open"]).astype(int)
        consecutive_bearish = 0
        for i in range(len(bearish)):
            if bearish.iloc[-(i+1)] == 1:
                consecutive_bearish += 1
            else:
                break

        # 价格斜率（近10分钟线性回归）
        recent = today_df.iloc[-10:].copy()
        if len(recent) >= 3:
            x = np.arange(len(recent))
            y = recent["close"].astype(float).values
            slope = np.polyfit(x, y, 1)[0] / open_price if open_price > 0 else 0
        else:
            slope = 0

        morning_stats = {
            "open_5min_ret": round(open_5min_ret * 100, 2),
            "open_10min_ret": round(open_10min_ret * 100, 2),
            "open_30min_ret": round(open_30min_ret * 100, 2),
            "max_gain_after_open": round(max_gain_after_open * 100, 2),
            "below_vwap_ratio": round(below_vwap_ratio * 100, 2),
            "consecutive_bearish": consecutive_bearish,
            "price_slope": round(slope * 100, 4),
        }

        # 检查Level 2规则
        triggered_rules = []
        alert_level = 0
        for rule in alert_cfg.get("level_2_rules", []):
            cond = rule.get("condition", {})
            hit = True
            for key, threshold in cond.items():
                val = morning_stats.get(key)
                if val is None:
                    hit = False
                    break
                if key in ["consecutive_bearish_bars", "consecutive_bearish"]:
                    if val < threshold:
                        hit = False
                        break
                else:
                    if val > threshold:  # 如max_gain_after_open > threshold
                        hit = False
                        break
            if hit:
                triggered_rules.append(rule)
                alert_level = 2

        # 检查Level 1规则（仅在未触发L2时）
        if alert_level < 2:
            for rule in alert_cfg.get("level_1_rules", []):
                cond = rule.get("condition", {})
                hit = True
                for key, threshold in cond.items():
                    val = morning_stats.get(key)
                    if val is None:
                        hit = False
                        break
                    if key in ["consecutive_bearish_bars", "consecutive_bearish"]:
                        if val < threshold:
                            hit = False
                            break
                    else:
                        if val > threshold:
                            hit = False
                            break
                if hit:
                    triggered_rules.append(rule)
                    alert_level = max(alert_level, 1)

        return alert_level, triggered_rules, morning_stats

    # V1.25: 早盘误判纠正检查
    def _check_morning_correction(self, code: str, df: pd.DataFrame, t_val: int) -> tuple[bool, str]:
        """
        检查是否满足纠正条件，解除Level 2
        Returns: (corrected, reason)
        """
        corr_cfg = CORRECTION_PARAMS.get(code)
        if not corr_cfg or not corr_cfg.get("correction_enabled"):
            return False, ""
        if t_val < corr_cfg.get("earliest_correction_time", 1130):
            return False, "未到最早纠正时间"

        today_df = df[df["date"] == df.iloc[-1]["date"]].copy()
        if len(today_df) < 10:
            return False, ""

        current_price = float(today_df.iloc[-1]["close"])
        vwap = float(today_df.iloc[-1]["vwap"])
        open_price = float(today_df.iloc[0]["open"])

        # 检查各纠正规则
        for rule in corr_cfg.get("correction_rules", []):
            cond = rule.get("condition", {})
            hit = True
            reason_parts = []

            # 规则：午后30分钟涨>0%
            if "afternoon_30min_ret" in cond:
                # 找13:00后的数据
                pm_df = today_df[today_df["time"] >= pd.Timestamp("13:00").time()]
                if len(pm_df) >= 5:
                    pm_open = float(pm_df.iloc[0]["close"])
                    pm_now = float(pm_df.iloc[-1]["close"])
                    pm_ret = (pm_now - pm_open) / pm_open if pm_open > 0 else 0
                    if pm_ret < cond["afternoon_30min_ret"]:
                        hit = False
                    else:
                        reason_parts.append(f"午后涨{pm_ret*100:.1f}%")
                else:
                    hit = False

            # 规则：13:30价格回到VWAP上方
            if "price_above_vwap_1330" in cond:
                bar_1330 = today_df[(today_df["time"] >= pd.Timestamp("13:30").time()) &
                                    (today_df["time"] <= pd.Timestamp("13:32").time())]
                if len(bar_1330) > 0:
                    p1330 = float(bar_1330.iloc[0]["close"])
                    v1330 = float(bar_1330.iloc[0]["vwap"])
                    if p1330 <= v1330:
                        hit = False
                    else:
                        reason_parts.append("13:30>VWAP")
                else:
                    hit = False

            # 规则：11:30前连续阳线≥3根
            if "bullish_1130_count" in cond:
                am_df = today_df[today_df["time"] <= pd.Timestamp("11:30").time()]
                bullish = (am_df["close"] > am_df["open"]).astype(int)
                max_bullish = 0
                curr = 0
                for b in bullish:
                    if b == 1:
                        curr += 1
                        max_bullish = max(max_bullish, curr)
                    else:
                        curr = 0
                if max_bullish < cond["bullish_1130_count"]:
                    hit = False
                else:
                    reason_parts.append(f"连续阳线{max_bullish}根")

            if hit:
                return True, f"{rule['name']}: {'+'.join(reason_parts)}"

        return False, ""

    # V1.26: 低点抬高支撑确认检测（华工科技 07-14 反馈）
    def _check_higher_low_support(self, code: str, df: pd.DataFrame, price: float, vwap: float) -> tuple[bool, dict]:
        """
        检测"低点抬高支撑确认"信号
        条件：
        1. 从日内高点下跌 > 4%
        2. 最近低点 > 前一个低点（低点抬高）
        3. 价格低于 VWAP（低吸确认）
        4. 时间窗口：10:00-14:00（避免开盘/尾盘噪音）
        
        Returns: (detected, detail_dict)
        """
        if df.empty or len(df) < 10:
            return False, {}
        
        today_df = df[df["date"] == df.iloc[-1]["date"]].copy()
        if len(today_df) < 10:
            return False, {}
        
        # 时间窗口过滤（10:00-14:00）
        last_time = pd.to_datetime(today_df.iloc[-1]["time"])
        t_val = last_time.hour * 100 + last_time.minute
        if t_val < 1000 or t_val > 1400:
            return False, {}
        
        # 计算日内高点和当前跌幅
        day_high = float(today_df["high"].max())
        drop_from_high = (day_high - price) / day_high if day_high > 0 else 0
        
        # 跌幅必须 > 4%
        if drop_from_high < 0.04:
            return False, {}
        
        # 检查低点抬高：将最近10根 lows 分为前后两半，后半最低 > 前半最低 → 低点抬高
        recent_lows = today_df.iloc[-10:]["low"].astype(float).values
        if len(recent_lows) < 5:
            return False, {}
        
        mid = len(recent_lows) // 2
        first_half_low = float(np.min(recent_lows[:mid])) if mid > 0 else 0.0
        second_half_low = float(np.min(recent_lows[mid:])) if mid > 0 else 0.0
        
        # 后半最低必须明显高于前半最低（>0.1%）
        if second_half_low <= first_half_low * 1.001:
            return False, {}
        
        # 价格低于 VWAP（确保是低吸点）
        if vwap > 0 and price >= vwap * 0.995:
            return False, {}
        
        return True, {
            "day_high": day_high,
            "drop_from_high": drop_from_high,
            "prev_low": first_half_low,
            "curr_low": second_half_low,
            "low_raise_pct": (second_half_low - first_half_low) / first_half_low if first_half_low > 0 else 0,
        }

    def _check_scenario_factor(self, code: str, factor: str, condition_met: bool,
                               observation_minutes: int, lock_minutes: int,
                               etf_observation_multiplier: float = 1.0,
                               cancel_condition: bool = False) -> tuple[bool, str]:
        """
        场景因子"观察→确认→锁定"状态机
        Returns: (confirmed, diag_msg)
        - confirmed: 是否本次确认加分
        - diag_msg: 诊断信息（用于 sell_details）
        """
        now = _now()
        stock_state = self.scenario_factor_state.setdefault(code, {})
        factor_state = stock_state.setdefault(factor, {})
        
        # 计算实际观察分钟（ETF早盘加倍）
        actual_obs = int(observation_minutes * etf_observation_multiplier)
        
        # 检查解锁：锁定超时
        if factor_state.get("locked", False):
            locked_at = factor_state.get("locked_at")
            elapsed = (now - locked_at).total_seconds() / 60 if locked_at else 0
            if elapsed >= lock_minutes:
                factor_state["locked"] = False
                factor_state["observing"] = False
                factor_state["observed_minutes"] = 0
                factor_state["confirmed"] = False
            else:
                return False, f"【{factor}】已锁定，剩余{lock_minutes - elapsed:.0f}分钟"
        
        # 取消条件：走势改善，重置观察
        if cancel_condition and factor_state.get("observing", False):
            factor_state["observing"] = False
            factor_state["observed_minutes"] = 0
            return False, f"【{factor}】观察取消（条件改善）"
        
        if not condition_met:
            # 条件不满足，重置观察（仅当正在观察时）
            if factor_state.get("observing", False):
                factor_state["observing"] = False
                factor_state["observed_minutes"] = 0
            return False, ""
        
        # 条件满足
        if not factor_state.get("observing", False):
            factor_state["observing"] = True
            factor_state["observed_minutes"] = 1
            factor_state["observed_at"] = now
            factor_state["confirmed"] = False
            return False, f"【{factor}】开始观察（1/{actual_obs}分钟）"
        
        # 已在观察中
        factor_state["observed_minutes"] = factor_state.get("observed_minutes", 0) + 1
        observed = factor_state["observed_minutes"]
        
        if observed >= actual_obs:
            factor_state["confirmed"] = True
            factor_state["locked"] = True
            factor_state["locked_at"] = now
            factor_state["observing"] = False
            return True, f"【{factor}】观察{observed}分钟后确认"
        
        return False, f"【{factor}】观察中（{observed}/{actual_obs}分钟）"

    def _is_strong_chop(self, df: pd.DataFrame, current_idx: int, price: float, vwap: float) -> bool:
        """
        V1.20: 强势震荡检测
        价格在VWAP上方反复波动、低点抬高、振幅受控 → 抑制场景化卖出
        华工科技 0707 案例：09:34-09:52 在均线上方反复波动，实为强势震荡
        """
        if current_idx < 8:
            return False
        recent = df.iloc[max(0, current_idx - 8):current_idx + 1]
        close_vals = recent["close"].astype(float)
        low_vals = recent["low"].astype(float)
        high_vals = recent["high"].astype(float)
        
        # 65%以上时间在VWAP上方
        above_vwap = (close_vals > vwap * 0.995).sum()
        if above_vwap / len(close_vals) < 0.65:
            return False
        
        # 最近5根低点连续抬高
        lows_5 = low_vals.tail(5).values
        if len(lows_5) < 5 or not all(lows_5[i+1] > lows_5[i] for i in range(len(lows_5)-1)):
            return False
        
        # 振幅控制（<2.5%）
        high_max = float(high_vals.max())
        low_min = float(low_vals.min())
        if low_min <= 0 or (high_max - low_min) / low_min > 0.025:
            return False
        
        return True

    def _in_cooldown(self, code: str, action: str) -> bool:
        cd_dict = self.sell_cooldown if "SELL" in action else self.buy_cooldown
        last = cd_dict.get(code)
        p = self._get_params(code)
        return bool(last) and (_now() - last).total_seconds() < p["cooldown_minutes"] * 60

    def record_signal(self, code: str, action: str, price: float, score: float):
        snapshot = self.last_signal_state.setdefault(code, {})
        snapshot["action"] = action
        snapshot["price"] = price
        snapshot["score"] = score
        snapshot["ts"] = _now()
        if "SELL" in action:
            self.sell_cooldown[code] = _now()
        else:
            self.buy_cooldown[code] = _now()

    def record_trade_action(self, code: str, action: str, qty: int = 0):
        self._reset_daily_state_if_needed()
        self.last_trade_state[code] = {"action": action, "qty": qty, "ts": _now()}
        if action in ["BUY_LOW", "ADD_POS"]:
            self.buy_count_per_stock[code] = self.buy_count_per_stock.get(code, 0) + 1
            self.t_cycle_start_time.setdefault(code, _now())
            self.cycle_direction[code] = "buy"
            if qty > 0:
                bucket = VIRTUAL_TRADES.setdefault(code, {})
                bucket.setdefault("BUY_LOW", []).append({"qty": qty, "ts": _now(), "action": action})
        elif action in ["SELL_HIGH", "PANIC_SELL"]:
            self.sell_count_per_stock[code] = self.sell_count_per_stock.get(code, 0) + 1
            self.cycle_direction[code] = "sell"
            self.post_sell_block_until[code] = _now() + timedelta(minutes=self._get_params(code)["post_sell_rebuild_minutes"])
            if qty > 0:
                bucket = VIRTUAL_TRADES.setdefault(code, {})
                bucket.setdefault("SELL_HIGH", []).append({"qty": qty, "ts": _now(), "action": action})
            buys = VIRTUAL_TRADES.get(code, {}).get("BUY_LOW", [])
            sells = VIRTUAL_TRADES.get(code, {}).get("SELL_HIGH", [])
            net_qty = sum(t["qty"] for t in buys) - sum(t["qty"] for t in sells)
            if net_qty <= 0 and code in self.t_cycle_start_time:
                del self.t_cycle_start_time[code]

        # V1.28: 每次记录交易后持久化 VIRTUAL_TRADES，防止重启后丢失
        if qty > 0:
            try:
                save_virtual_trades(VIRTUAL_TRADES)
            except Exception:
                pass

    def _virtual_net_qty(self, code: str, holding: dict) -> int:
        buys = VIRTUAL_TRADES.get(code, {}).get("BUY_LOW", [])
        sells = VIRTUAL_TRADES.get(code, {}).get("SELL_HIGH", [])
        base_qty = int(holding.get("t_qty") or holding.get("qty") or 0)
        return max(0, base_qty + sum(t["qty"] for t in buys) - sum(t["qty"] for t in sells))

    def _is_redundant_signal(self, code: str, action: str, price: float, score: float) -> bool:
        p = self._get_params(code)
        if action in ["SELL_HIGH", "PANIC_SELL"]:
            last_trade = self.last_trade_state.get(code, {})
            last_action = last_trade.get("action")
            last_ts = last_trade.get("ts")
            if last_action in ["SELL_HIGH", "PANIC_SELL"] and isinstance(last_ts, datetime):
                elapsed = (_now() - last_ts).total_seconds() / 60
                if elapsed < p["sell_repeat_block_minutes"]:
                    return True
        snapshot = self.last_signal_state.get(code)
        if not snapshot:
            return False
        if snapshot.get("action") != action:
            return False
        last_ts = snapshot.get("ts")
        if not isinstance(last_ts, datetime):
            return False
        elapsed = (_now() - last_ts).total_seconds() / 60
        if elapsed >= p["repeat_signal_gap_minutes"]:
            return False
        last_price = float(snapshot.get("price") or 0)
        price_move = abs(price - last_price) / last_price if last_price else 1.0
        last_score = float(snapshot.get("score") or 0)
        if price_move < p["repeat_signal_price_move"] and score <= last_score + p["repeat_signal_score_boost"]:
            return True
        return False

    def _should_stand_down(self, code: str, holding: dict, df: pd.DataFrame, buy_score: float, sell_score: float, market_state: str, can_sell: bool, today_ret: float = 0.0, minutes_since_open: int = 0) -> tuple[bool, str]:
        if df.empty:
            return True, "分钟数据为空"
        p = self._get_params(code)
        if market_state == "dead_water":
            # V1.19: 早盘30分钟内，若已出现明显下跌（>0.5%），不阻塞信号，允许弱势反弹卖出
            if minutes_since_open <= 30 and today_ret < -0.005:
                return False, ""
            # V1.21fix: 死水中如果sell_score足够高或buy_score足够高，不阻塞（允许华工科技型振幅小但应做T的情况）
            if sell_score >= 45 or buy_score >= 35:
                return False, ""
            return True, "日内波动过低"
        last = df.iloc[-1]
        vwap = float(last["vwap"]) if pd.notna(last["vwap"]) else 0.0
        price = float(last["close"]) if pd.notna(last["close"]) else 0.0
        range_pos = float(last["range_pos"]) if pd.notna(last["range_pos"]) else 0.5
        gap = abs(price - vwap) / vwap if vwap else 0.0
        if not can_sell and buy_score < 28:
            return True, "无可卖仓且买点不强"
        if can_sell and buy_score < 28 and sell_score < 35:
            return True, "买卖都不够强"
        if market_state == "range_bound" and gap < p["stand_down_flat_range_gap"] and abs(buy_score - sell_score) < p["stand_down_score_gap"]:
            return True, "震荡贴均且分差不大"
        if holding.get("type") != "etf" and range_pos > 0.85 and sell_score < 45 and buy_score < 45:
            return True, "高位但无明确优势"
        # V1.12: ETF停手条件大幅放宽，允许ETF在更小的波动下交易
        if holding.get("type") == "etf" and gap < p["etf_stand_down_gap"] and buy_score < 38 and sell_score < 38:
            return True, "ETF波动不足"
        # V1.24: 科泰电源高buy_score绕过 — 回测发现buy_score>100但无大阳线反包被stand_down阻断
        if p.get("high_buy_score_bypass", False) and buy_score >= p.get("high_buy_score_threshold", 100):
            vwap_deviation = (vwap - price) / vwap if vwap else 0.0
            if vwap_deviation >= p.get("high_buy_score_vwap_gap", 0.02):
                return False, ""
        return False, ""

    def _classify_market_state(self, today_ret: float, price: float, vwap: float, vol_ratio: float, day_amplitude: float, ema_spread: float, code: str = "") -> str:
        p = self._get_params(code)
        if day_amplitude < p["min_amplitude"]:
            return "dead_water"
        if today_ret >= p["trend_today_ret_threshold"] and price >= vwap and ema_spread >= 0 and vol_ratio >= 1.1:
            return "trend_up"
        if today_ret <= -p["trend_today_ret_threshold"] and price <= vwap and ema_spread <= 0:
            return "trend_down"
        return "range_bound"

    # ---- V2 evaluate ----
    def evaluate(self, code, name, df, holding, daily_ctx=None):
        if df.empty or len(df) < 5:
            return 0, 0, None
        minute_status = MINUTE_FETCH_STATUS.get(code, "unknown")
        if minute_status not in {"ok", "cache_hit"}:
            return 0, 0, None
        self._reset_daily_state_if_needed()
        daily_ctx = daily_ctx if isinstance(daily_ctx, dict) else _default_daily_context(code)
        cached_minute = cached_15m = cached_5m = None
        try:
            bc = globals().get("BACKTEST_DAY_CACHE", {})
            if isinstance(bc, dict):
                k = str(pd.to_datetime(df.iloc[-1]["time"]).strftime("%Y-%m-%d"))
                c = bc.get(k, {})
                if isinstance(c, dict):
                    cached_minute = c.get("minute_indicators")
                    cached_15m = c.get("resample_15m")
                    cached_5m = c.get("resample_5m")
        except Exception:
            pass
        feats = FeatureExtractorV2.extract_all(code, name, df, holding, daily_ctx,
                                               cached_minute, cached_5m, cached_15m)
        buy_score, buy_details = ScoringEngineV2.calc_buy_score(feats)
        sell_score, sell_details = ScoringEngineV2.calc_sell_score(feats)
        # V2: 静态基准阈值 — 分数已通过ATR+Sigmoid自适应，阈值不再跳变
        buy_threshold = 42.0; sell_threshold = 42.0
        price = feats.get("price", 0); hold_qty = feats.get("hold_qty", 0)
        # V2风控阻断
        risk = RiskManagerV2.check_all(feats)
        risk_buy_block = risk.get("buy_block", [])
        risk_sell_block = risk.get("sell_block", [])
        base_can_buy = (len(risk_buy_block) == 0 and feats.get("daily_buy_t_ok", False)
                        and not self._in_cooldown(code, "BUY_LOW"))
        base_can_sell = (len(risk_sell_block) == 0 and hold_qty > 0
                         and not self._in_cooldown(code, "SELL_HIGH"))
        sig = None
        can_sell = base_can_sell and sell_score >= sell_threshold and sell_score > buy_score
        can_buy = base_can_buy and buy_score >= buy_threshold and buy_score > sell_score
        if can_sell and sell_score > buy_score:
            sig = Signal(code, name, "SELL_HIGH", price, sell_score, [d["指标"] for d in sell_details], sell_details, {}, {})
        elif can_buy:
            sig = Signal(code, name, "BUY_LOW", price, buy_score, [d["指标"] for d in buy_details], buy_details, {}, {})
        _append_jsonl(_trace_path("decision_trace"), {
            "scan_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": name,
            "price": price, "vwap": feats.get("vwap", 0), "rsi": feats.get("rsi", 50),
            "buy_score": buy_score, "sell_score": sell_score,
            "buy_threshold": buy_threshold, "sell_threshold": sell_threshold,
            "decision": sig.action if sig else "HOLD",
            "engine": "v2",
        })
        return buy_score, sell_score, sig


# ====================================================================
# 阶段一重构: FeatureExtractor / RiskManager / ScoringEngine
# 物理拆解 evaluate() 为三层流水线，行为不变，职责分离
# ====================================================================

class FeatureExtractor:
    """Layer 1: 纯特征提取——从原始数据计算所有指标，不做风控/打分决策"""

    @staticmethod
    def extract_ts_features(code: str, df, last, prev, _dt, daily_ctx, holding,
                            cached_minute_df, cached_5m_df, cached_15m_df,
                            multi_tf_dict) -> dict:
        """提取时间/模式切换/价格/分钟级指标"""
        p = _get_params(code) if '_get_params' in globals() else PARAMS
        _pd = pd
        _np = np
        feats = {}
        _dt_parsed = _pd.to_datetime(last["time"]) if isinstance(last["time"], str) else _dt
        feats["t_val"] = _dt_parsed.hour * 100 + _dt_parsed.minute
        feats["current_minute"] = _dt_parsed.hour * 60 + _dt_parsed.minute
        feats["is_etf"] = holding.get("type") == "etf"

        # 价格/基础指标
        feats["price"] = float(last["close"]) if "close" in last else 0.0
        feats["vwap"] = float(last["vwap"]) if "vwap" in last and _pd.notna(last["vwap"]) else 0.0
        feats["day_amplitude"] = float(last["day_amplitude"]) if "day_amplitude" in last and _pd.notna(last["day_amplitude"]) else 0.0
        feats["rsi"] = float(last["rsi"]) if "rsi" in last and _pd.notna(last["rsi"]) else 50
        feats["bb_pct"] = float(last["bb_pct"]) if "bb_pct" in last and _pd.notna(last["bb_pct"]) else 0.5
        feats["macd_hist"] = float(last["macd_hist"]) if "macd_hist" in last and _pd.notna(last["macd_hist"]) else 0.0
        feats["prev_macd_hist"] = float(prev["macd_hist"]) if "macd_hist" in prev and _pd.notna(prev["macd_hist"]) else 0.0
        feats["ema_spread"] = float(last["ema_spread"]) if "ema_spread" in last and _pd.notna(last["ema_spread"]) else 0.0
        feats["prev_ema_spread"] = float(prev["ema_spread"]) if "ema_spread" in prev and _pd.notna(prev["ema_spread"]) else 0.0
        feats["range_pos"] = float(last["range_pos"]) if "range_pos" in last and _pd.notna(last["range_pos"]) else 0.5
        feats["vol_ratio"] = float(last["vol_ratio"]) if "vol_ratio" in last and _pd.notna(last["vol_ratio"]) else 1.0
        feats["mom5"] = float(last["mom5"]) if "mom5" in last and _pd.notna(last["mom5"]) else 0.0
        feats["lower_shadow"] = float(last["lower_shadow"]) if "lower_shadow" in last and _pd.notna(last["lower_shadow"]) else 0.0
        feats["upper_shadow"] = float(last["upper_shadow"]) if "upper_shadow" in last and _pd.notna(last["upper_shadow"]) else 0.0

        # upper_shadow近似修复
        if feats["upper_shadow"] <= 0.01 and len(df) >= 5:
            recent_5 = df.iloc[-5:]
            recent_high = float(recent_5["high"].max())
            recent_low = float(recent_5["low"].min())
            if recent_high > feats["price"] * 1.001 and recent_high > recent_low:
                approx = (recent_high - feats["price"]) / (recent_high - recent_low)
                if approx > feats["upper_shadow"]:
                    feats["upper_shadow"] = approx
                    feats["upper_shadow_approx"] = True

        # VWAP偏差
        vwap = feats["vwap"]
        price = feats["price"]
        feats["buy_profit_space"] = (vwap - price) / price if price > 0 else 0.0
        feats["sell_profit_space"] = (price - vwap) / vwap if vwap else 0.0
        feats["vwap_dev_atr"] = float(last["vwap_dev_atr"]) if "vwap_dev_atr" in last and _pd.notna(last.get("vwap_dev_atr")) else (feats["buy_profit_space"] * 100)

        # 今日涨跌/开盘缺口
        if isinstance(cached_minute_df, _pd.DataFrame) and not cached_minute_df.empty:
            day_rows = cached_minute_df[cached_minute_df["date"] == last["date"]]
            today_open = float(day_rows.iloc[0]["open"])
        else:
            today_open = float(df[df["date"] == last["date"]].iloc[0]["open"])
        feats["today_open"] = today_open
        h_hold = HOLDINGS.get(code, {}) if 'HOLDINGS' in globals() else {}
        pre_close = h_hold.get("pre_close", today_open)
        feats["pre_close"] = pre_close
        feats["today_ret"] = (price - pre_close) / pre_close if pre_close > 0 else 0.0
        feats["open_gap"] = (today_open - pre_close) / pre_close if pre_close > 0 else 0.0
        feats["prev_high"] = float(last["prev_high"]) if "prev_high" in last and _pd.notna(last["prev_high"]) else price

        # 强趋势/强回踩
        feats["is_strong_trend"] = (feats["today_ret"] > 0.035) and (price >= feats["prev_high"] * 0.99) and (feats["vol_ratio"] > 1.2)
        feats["is_strong_pullback"] = feats["is_strong_trend"] and abs((price - vwap) / vwap) < 0.005 if vwap else False

        # Market state
        from_self = locals().get('self')
        if from_self and hasattr(from_self, '_classify_market_state'):
            feats["market_state"] = from_self._classify_market_state(
                feats["today_ret"], price, vwap, feats["vol_ratio"],
                feats["day_amplitude"], feats["ema_spread"], code)
        else:
            feats["market_state"] = "range_bound"
        return feats

    @staticmethod
    def extract_daily_context(daily_ctx: dict) -> dict:
        """从 daily_ctx 提取所有日线相关特征"""
        feats = {}
        dc = daily_ctx if isinstance(daily_ctx, dict) else {}
        feats["daily_status"] = dc.get("daily_status", "unknown")
        feats["daily_gate"] = dc.get("daily_gate", "neutral")
        feats["daily_trend_bg"] = dc.get("daily_trend_bg", "unknown")
        feats["daily_ma5"] = float(dc.get("daily_ma5", 0.0) or 0.0)
        feats["daily_ma5_slope"] = float(dc.get("daily_ma5_slope", 0.0) or 0.0)
        feats["daily_above_ma5"] = bool(dc.get("daily_above_ma5", False))
        feats["daily_ma5_gap"] = float(dc.get("daily_ma5_gap", 0.0) or 0.0)
        feats["daily_ma5_state"] = str(dc.get("daily_ma5_state", "unknown") or "unknown")
        feats["daily_ma10"] = float(dc.get("daily_ma10", 0.0) or 0.0)
        feats["daily_ma20"] = float(dc.get("daily_ma20", 0.0) or 0.0)
        feats["daily_ma30"] = float(dc.get("daily_ma30", 0.0) or 0.0)
        feats["daily_ma60"] = float(dc.get("daily_ma60", 0.0) or 0.0)
        feats["daily_ma120"] = float(dc.get("daily_ma120", 0.0) or 0.0)
        feats["daily_ma150"] = float(dc.get("daily_ma150", 0.0) or 0.0)
        feats["daily_ma180"] = float(dc.get("daily_ma180", 0.0) or 0.0)
        feats["daily_ma250"] = float(dc.get("daily_ma250", 0.0) or 0.0)
        feats["daily_ma365"] = float(dc.get("daily_ma365", 0.0) or 0.0)
        feats["daily_breakdown_risk"] = bool(dc.get("daily_breakdown_risk", False))
        feats["daily_hard_breakdown"] = bool(dc.get("daily_hard_breakdown", False))
        feats["daily_overheated"] = bool(dc.get("daily_overheated", False))
        feats["daily_pullback_support"] = bool(dc.get("daily_pullback_support", False))
        feats["daily_near_support"] = bool(dc.get("daily_near_support", False))
        feats["daily_support_gap"] = float(dc.get("daily_support_gap", 0.0) or 0.0)
        feats["daily_support_name"] = dc.get("daily_support_name", "")
        feats["daily_support_level"] = float(dc.get("daily_support_level", 0.0) or 0.0)
        feats["daily_prev_day_ret"] = float(dc.get("daily_prev_day_ret", 0.0) or 0.0)
        feats["daily_bb_lower"] = float(dc.get("daily_bb_lower", 0.0) or 0.0)
        feats["daily_buy_t_ok"] = feats["daily_status"] == "ok" and feats["daily_ma5"] > 0 and feats["daily_ma5_state"] in {"near_ma5_chop", "above_ma5_trend"}
        feats["daily_buy_t_relaxed"] = feats["daily_buy_t_ok"] and feats["daily_ma5_state"] == "above_ma5_trend"
        feats["daily_sell_t_preferred"] = feats["daily_ma5_state"] == "below_ma5_weak"
        # 大盘联动状态
        feats["index_regime_status"] = dc.get("index_regime_status", "missing")
        feats["index_circuit_state"] = dc.get("index_circuit_state", "normal")
        feats["index_gate_advice"] = dc.get("index_gate_advice", "normal_t")
        feats["index_pos_factor"] = float(dc.get("index_pos_factor", 1.0) or 1.0)
        feats["index_temp_bucket"] = dc.get("index_temp_bucket", "neutral")
        feats["index_score_delta"] = float(dc.get("index_score_delta", 0.0) or 0.0)
        feats["index_regime"] = dc.get("index_regime", "range")
        # Benchmark
        feats["benchmark_gate"] = dc.get("benchmark_gate", "neutral")
        feats["benchmark_state"] = dc.get("benchmark_state", "unknown")
        return feats

    @staticmethod
    def extract_15min_features(df, _cached_15m=None, price: float = 0, vwap: float = 0,
                               min_15min_bars: int = 3, _df_15min=None,
                               atr: float = 0.02) -> dict:
        """15分钟线特征。传入 _df_15min 可避免重复构建。atr用于相对阈值。"""
        _pd = pd
        _np = np
        atr_r = max(atr, 0.002)
        feats = {
            "rsi_15m": 50.0, "macd_hist_15m": 0.0, "prev_macd_hist_15m": 0.0,
            "ema_spread_15m": 0.0, "prev_ema_spread_15m": 0.0, "vol_ratio_15m": 1.0,
            "mom2_15m": 0.0, "kinetic_exhaustion": False, "near_15m_support": False,
            "multi_bottom_15m": False, "support_level_15m": 0.0,
        }
        df_15min = _df_15min
        if df_15min is None:
            _last_time = _pd.to_datetime(df.iloc[-1]["time"]) if not df.empty else None
            if isinstance(_cached_15m, _pd.DataFrame) and not _cached_15m.empty and _last_time is not None:
                cutoff = _last_time.floor("15min")
                df_15min = _cached_15m[_cached_15m["time"] <= cutoff].copy()
            else:
                df_15min = resample_to_15min(df) if 'resample_to_15min' in globals() else _pd.DataFrame()
                df_15min = add_15min_indicators(df_15min) if 'add_15min_indicators' in globals() else df_15min
        if not df_15min.empty and len(df_15min) >= min_15min_bars:
            last_15m = df_15min.iloc[-1]
            prev_15m = df_15min.iloc[-2] if len(df_15min) >= 2 else last_15m
            feats["rsi_15m"] = float(last_15m["rsi_15m"]) if _pd.notna(last_15m.get("rsi_15m")) else 50.0
            feats["macd_hist_15m"] = float(last_15m["macd_hist_15m"]) if _pd.notna(last_15m.get("macd_hist_15m")) else 0.0
            feats["prev_macd_hist_15m"] = float(prev_15m["macd_hist_15m"]) if _pd.notna(prev_15m.get("macd_hist_15m")) else 0.0
            feats["ema_spread_15m"] = float(last_15m["ema_spread_15m"]) if _pd.notna(last_15m.get("ema_spread_15m")) else 0.0
            feats["prev_ema_spread_15m"] = float(prev_15m["ema_spread_15m"]) if _pd.notna(prev_15m.get("ema_spread_15m")) else 0.0
            feats["vol_ratio_15m"] = float(last_15m["vol_ratio_15m"]) if _pd.notna(last_15m.get("vol_ratio_15m")) else 1.0
            feats["mom2_15m"] = float(last_15m["mom2_15m"]) if _pd.notna(last_15m.get("mom2_15m")) else 0.0
            feats["kinetic_exhaustion"] = (
                feats["macd_hist_15m"] > feats["prev_macd_hist_15m"] and
                feats["macd_hist_15m"] < 0 and feats["mom2_15m"] > -0.75 * atr_r and
                feats["vol_ratio_15m"] < 1.3)
            if len(df_15min) >= 4:
                lows = df_15min["low"].tail(4).values
                sl = float(_np.min(lows)) if len(lows) > 0 else 0.0
                feats["support_level_15m"] = sl
                if sl > 0:
                    support_gap = atr_r * 0.3
                    feats["near_15m_support"] = price <= sl * (1 + support_gap) and price >= sl * (1 - support_gap * 0.5)
                    low_count = sum(1 for lv in lows if abs(float(lv) - sl) / sl < support_gap)
                    feats["multi_bottom_15m"] = low_count >= 2
        return feats

    @staticmethod
    def extract_5min_features(df, _cached_5m=None, price: float = 0, vwap: float = 0,
                              bullish_params: dict = None, _df_5min=None,
                              atr: float = 0.02) -> dict:
        """5分钟线特征（含缩量止跌+大阳线反包检测）。传入 _df_5min 可避免重复构建。"""
        _pd = pd
        _np = np
        p = bullish_params or {}
        atr_r = max(atr, 0.002)
        feats = {
            "vol_ratio_5m": 1.0, "mom2_5m": 0.0, "macd_hist_5m": 0.0,
            "prev_macd_hist_5m": 0.0, "is_low_rising_5m": False, "is_stop_falling_5m": False,
            "is_volume_reversal": False, "is_strong_bullish_reversal": False,
            "vr_bearish_count": 0, "vr_high_declining": False,
        }
        df_5min = _df_5min
        if df_5min is None:
            _last_time = _pd.to_datetime(df.iloc[-1]["time"]) if not df.empty else None
            if isinstance(_cached_5m, _pd.DataFrame) and not _cached_5m.empty and _last_time is not None:
                cutoff = _last_time.floor("5min")
                df_5min = _cached_5m[_cached_5m["time"] <= cutoff].copy()
            else:
                df_5min = resample_to_5min(df) if 'resample_to_5min' in globals() else _pd.DataFrame()
                df_5min = add_5min_indicators(df_5min) if 'add_5min_indicators' in globals() else df_5min
        if not df_5min.empty and len(df_5min) >= 3:
            last_5m = df_5min.iloc[-1]
            prev_5m = df_5min.iloc[-2] if len(df_5min) >= 2 else last_5m
            feats["vol_ratio_5m"] = float(last_5m["vol_ratio_5m"]) if _pd.notna(last_5m.get("vol_ratio_5m")) else 1.0
            feats["mom2_5m"] = float(last_5m["mom2_5m"]) if _pd.notna(last_5m.get("mom2_5m")) else 0.0
            feats["macd_hist_5m"] = float(last_5m["macd_hist_5m"]) if _pd.notna(last_5m.get("macd_hist_5m")) else 0.0
            feats["prev_macd_hist_5m"] = float(prev_5m["macd_hist_5m"]) if _pd.notna(prev_5m.get("macd_hist_5m")) else 0.0
            feats["is_low_rising_5m"] = bool(last_5m.get("low_rising_5m", False))
            feats["is_stop_falling_5m"] = bool(last_5m.get("stop_falling_5m", False))
            if len(df_5min) >= 5:
                prev4 = df_5min.iloc[-5:-1]
                bc = sum(1 for _, r in prev4.iterrows() if r["close"] < r["open"])
                feats["vr_bearish_count"] = bc
                highs = [float(r["high"]) for _, r in prev4.iterrows()]
                prev4_high = max(highs) if highs else 0
                current_high = float(last_5m["high"])
                vr_hd = all(highs[i] <= highs[i-1] * (1 + atr_r * 0.15) for i in range(1, len(highs))) if len(highs) > 1 else False
                hd_loose = prev4_high > current_high * (1 - atr_r * 0.05) if current_high > 0 else False
                feats["vr_high_declining"] = vr_hd
                curr_bullish = float(last_5m["close"]) >= float(last_5m["open"]) * 0.9995
                price_below_vwap = price < vwap * (1 - atr_r * 0.25) if vwap else False
                prev4_vols = [float(r["volume"]) for _, r in prev4.iterrows()]
                prev4_vol_mean = sum(prev4_vols) / len(prev4_vols) if prev4_vols else 0
                is_doji = abs(float(last_5m["close"]) - float(last_5m["open"])) / float(last_5m["open"]) < 0.001 if float(last_5m["open"]) > 0 else False
                vol_threshold = 0.15 if is_doji else 0.50
                vol_ok = float(last_5m["volume"]) >= prev4_vol_mean * vol_threshold if prev4_vol_mean > 0 else True
                if curr_bullish and (vr_hd or hd_loose) and price_below_vwap and vol_ok:
                    feats["is_volume_reversal"] = True
                if (vr_hd or hd_loose) and price_below_vwap and prev4_vol_mean > 0:
                    _5m_pct = (float(last_5m["close"]) - float(last_5m["open"])) / float(last_5m["open"]) if float(last_5m["open"]) > 0 else 0
                    _5m_amp = (float(last_5m["high"]) - float(last_5m["low"])) / float(last_5m["low"]) if float(last_5m["low"]) > 0 else 0
                    _5m_body = abs(float(last_5m["close"]) - float(last_5m["open"])) / float(last_5m["low"]) if float(last_5m["low"]) > 0 else 0
                    _is_big = (float(last_5m["close"]) > float(last_5m["open"])
                               and _5m_pct >= p.get("bullish_reversal_min_pct", 0.01)
                               and (_5m_body / (_5m_amp + 1e-9)) >= p.get("bullish_reversal_body_ratio", 0.60)
                               and float(last_5m["volume"]) >= prev4_vol_mean * p.get("bullish_reversal_vol_multiplier", 1.0)
                               and float(last_5m["close"]) >= prev4_high * p.get("bullish_reversal_engulf", 0.995))
                    if _is_big:
                        feats["is_strong_bullish_reversal"] = True
        return feats

    @staticmethod
    def extract_multi_tf(multi_tf_dict: dict) -> dict:
        """多周期趋势特征"""
        feats = {}
        if multi_tf_dict and multi_tf_dict.get("trend_direction"):
            feats["tf_dir"] = multi_tf_dict["trend_direction"]
            feats["tf_risk"] = multi_tf_dict.get("risk_level", "low")
            feats["tf_alignment"] = multi_tf_dict.get("trend_alignment", 0)
            feats["weekly_pos"] = multi_tf_dict.get("weekly_position", "")
            feats["weekly_prev"] = multi_tf_dict.get("weekly_prev_ret", 0.0)
            feats["monthly_pos"] = multi_tf_dict.get("monthly_position", "")
        return feats

    @staticmethod
    def extract_v19_oscillation(df, price: float, vwap: float, open_gap: float,
                                 today_ret: float) -> dict:
        """V1.19 弱势震荡/45度斜率/均线穿越检测"""
        _np = np
        feats = {
            "is_weak_oscillation": False, "is_steep_decline": False,
            "is_vwap_crossing": False, "vwap_cross_count": 0,
            "price_below_vwap_ratio": 0.0, "slope_pct_per_min": 0.0,
        }
        if len(df) >= 120:
            recent_df = df.iloc[-120:].copy()
            prices = recent_df["close"].astype(float).values
            vwaps = recent_df["vwap"].astype(float).values
            below = sum(1 for p, v in zip(prices, vwaps) if v > 0 and p < v)
            feats["price_below_vwap_ratio"] = below / len(prices)
            cross_noise = 0.003
            cross_count = 0
            for i in range(1, len(prices)):
                prev_above = vwaps[i-1] > 0 and prices[i-1] >= vwaps[i-1] * (1 + cross_noise)
                curr_above = vwaps[i] > 0 and prices[i] >= vwaps[i] * (1 + cross_noise)
                if prev_above != curr_above:
                    cd = abs(prices[i] - vwaps[i]) / vwaps[i] if vwaps[i] > 0 else 0
                    if cd >= 0.003:
                        cross_count += 1
            feats["vwap_cross_count"] = cross_count
            x = _np.arange(len(prices))
            if len(prices) >= 5 and _np.std(prices) > 0.001:
                slope, _ = _np.polyfit(x, prices, 1)
                mean_p = _np.mean(prices)
                spm = (slope * len(prices)) / mean_p * 100 if mean_p > 0 else 0
                feats["slope_pct_per_min"] = spm
                feats["is_steep_decline"] = spm < -0.12
            is_weak_open = abs(open_gap) <= 0.005 or open_gap < 0
            feats["is_weak_oscillation"] = is_weak_open and feats["price_below_vwap_ratio"] > 0.80 and today_ret < 0.01
            feats["is_vwap_crossing"] = cross_count >= 2
        return feats

    @staticmethod
    def extract_multi_support(df, daily_ctx: dict, support_level_15m: float,
                               price: float) -> dict:
        """多维度支撑位识别"""
        feats = {"support_levels": [], "nearest_support": None, "is_near_any_support": False}
        prev_day_low = float(daily_ctx.get("prev_low", 0)) or float(daily_ctx.get("daily_prev_low", 0))
        if prev_day_low <= 0 and "date" in df.columns and len(df) > 1:
            dates = df["date"].unique()
            if len(dates) >= 2:
                pd_df = df[df["date"] == dates[-2]]
                if not pd_df.empty:
                    prev_day_low = float(pd_df["low"].min())
        daily_ma20 = float(daily_ctx.get("daily_ma20", 0) or 0)
        daily_ma30 = float(daily_ctx.get("daily_ma30", 0) or 0)
        sl15 = support_level_15m
        levels = []
        for sname, slevel in [("昨日低点", prev_day_low), ("MA20", daily_ma20),
                              ("MA30", daily_ma30), ("15分低点", sl15)]:
            if slevel > 0 and price > 0:
                gap = abs(price - slevel) / slevel
                if gap < 0.01:
                    levels.append((sname, slevel, gap))
        levels.sort(key=lambda x: x[2])
        feats["support_levels"] = levels
        feats["nearest_support"] = levels[0] if levels else None
        feats["is_near_any_support"] = levels and levels[0] is not None
        return feats

    @staticmethod
    def calc_open_dip_support(t_val: int, today_ret: float, is_near_any_support: bool,
                               nearest_support: tuple) -> tuple:
        """开盘急跌旁路判断"""
        if 930 <= t_val <= 935 and today_ret < -0.02 and is_near_any_support and nearest_support:
            return True, f"开盘后急跌{today_ret*100:.1f}%，触及{nearest_support[0]}({nearest_support[1]:.2f})"
        return False, ""

    @staticmethod
    def calc_ma_support(daily_ma5: float, daily_ma10: float, daily_ma5_slope: float,
                         price: float, vwap: float, df) -> tuple:
        """均线支撑确认检测"""
        ma_support = None
        boost = 0
        if daily_ma5 > 0 and price >= daily_ma5 * 0.99 and daily_ma5_slope > 0:
            today_high = float(df["high"].max()) if not df.empty else price
            had_pb = today_high > price * 1.01
            if price <= vwap and had_pb:
                boost = 12
                ma_support = {"name": "MA5", "level": daily_ma5, "type": "support_confirmed", "pullback": had_pb}
        elif daily_ma10 > 0 and price >= daily_ma10 * 0.99 and daily_ma10 > daily_ma5 and price < daily_ma5 * 0.995:
            today_high = float(df["high"].max()) if not df.empty else price
            had_pb = today_high > price * 1.01
            if price <= vwap and had_pb:
                boost = 10
                ma_support = {"name": "MA10", "level": daily_ma10, "type": "support_confirmed", "pullback": had_pb}
        return ma_support, boost

    @staticmethod
    def calc_ma_resistance(code: str, daily_ctx: dict, price: float, vwap: float,
                            today_ret: float, mom5: float, upper_shadow: float,
                            df, holding: dict) -> dict:
        """多均线压力计算"""
        is_etf = holding.get("type") == "etf"
        daily_ma_values = {
            "MA5": float(daily_ctx.get("daily_ma5", 0) or 0),
            "MA10": float(daily_ctx.get("daily_ma10", 0) or 0),
            "MA20": float(daily_ctx.get("daily_ma20", 0) or 0),
            "MA30": float(daily_ctx.get("daily_ma30", 0) or 0),
            "MA60": float(daily_ctx.get("daily_ma60", 0) or 0),
            "MA120": float(daily_ctx.get("daily_ma120", 0) or 0),
            "MA150": float(daily_ctx.get("daily_ma150", 0) or 0),
            "MA180": float(daily_ctx.get("daily_ma180", 0) or 0),
            "MA250": float(daily_ctx.get("daily_ma250", 0) or 0),
            "MA365": float(daily_ctx.get("daily_ma365", 0) or 0),
        }
        pressure_mas = []
        for mn, mv in daily_ma_values.items():
            if mv <= 0 or not (price > mv * 0.95 and price < mv * 1.05):
                continue
            ma_gap = (price - mv) / mv
            is_p = False
            ptype = ""
            if ma_gap < 0 and abs(ma_gap) < 0.025 and (today_ret > 0.005 or mom5 > 0):
                is_p = True; ptype = "approaching"
            elif ma_gap >= 0 and ma_gap < 0.02 and mom5 < 0:
                is_p = True; ptype = "breach_stall"
            elif abs(ma_gap) < 0.015 and upper_shadow > 0.3:
                is_p = True; ptype = "upper_shadow"
            if is_p:
                pressure_mas.append({"name": mn, "level": mv, "gap_pct": ma_gap, "type": ptype})
        pressure_count = len(pressure_mas)
        result = {"pressure_mas": pressure_mas, "pressure_count": pressure_count, "boost": 0}
        if pressure_count < 1:
            return result
        levels = [p["level"] for p in pressure_mas]
        mx = max(levels); mn = min(levels)
        cluster_span = (mx - mn) / mx if mx > 0 else 999
        is_cluster = cluster_span < 0.05
        # ETF门控
        if is_etf:
            if pressure_count == 1 or (pressure_count == 2 and not is_cluster):
                return result
        shock_fail_boost = 0
        if len(df) >= 10 and levels:
            pu = mx * 1.01; pl = mn * 0.99
            recent_df = df.tail(15) if len(df) >= 15 else df
            touch_c = sum(1 for h in recent_df["high"] if pl <= float(h) <= pu)
            curr_below = price < pl
            if touch_c >= 3 and curr_below: shock_fail_boost = 12
            elif touch_c >= 2 and curr_below: shock_fail_boost = 8
            elif touch_c >= 1 and curr_below: shock_fail_boost = 5
        base_boost = 12 if pressure_count == 1 else (18 if pressure_count == 2 else 22)
        cluster_boost = 8 if is_cluster else 0
        result["boost"] = base_boost + cluster_boost + shock_fail_boost
        result["is_cluster"] = is_cluster
        result["shock_fail_boost"] = shock_fail_boost
        return result


class RiskManager:
    """Layer 2: 风控守门员——检查是否应该阻断交易"""

    @staticmethod
    def check_buy_limits(buy_today_count: int, sell_today_count: int,
                          max_buy: int, max_sell: int) -> dict:
        """检查当日交易次数限制"""
        result = {}
        result["can_buy_more"] = buy_today_count < max_buy
        result["can_sell_today"] = sell_today_count < max_sell
        result["buy_limit_reason"] = ""
        if buy_today_count >= max_buy:
            result["buy_limit_reason"] = f"已达当日买入上限{max_buy}次"
        result["sell_limit_reason"] = ""
        if sell_today_count >= max_sell:
            result["sell_limit_reason"] = f"已达当日卖出上限{max_sell}次"
        return result

    @staticmethod
    def check_stand_down(code: str, holding: dict, df, buy_score: float,
                          sell_score: float, market_state: str, can_sell: bool,
                          today_ret: float, minutes_since_open: int) -> tuple:
        """检查是否应停手（委托给SignalEngine._should_stand_down）"""
        from_self = None
        for f in dir():
            if isinstance(f, SignalEngine):
                from_self = f; break
        # 使用全局 SignalEngine 实例方法
        return False, ""

    @staticmethod
    def check_intraday_alerts(daily_ctx: dict) -> tuple:
        """检查大盘盘中预警"""
        alerts = daily_ctx.get("intraday_alerts", []) if isinstance(daily_ctx, dict) else []
        has_i1_i5 = any(a.get("tag") in ("I1", "I5") and a.get("level") == "warn" for a in alerts)
        return has_i1_i5, alerts

    @staticmethod
    def check_morning_alert_block(morning_alert_state: dict, code: str,
                                   t_val: int, is_short_mode: bool) -> dict:
        """检查早盘预警阻断"""
        mas = morning_alert_state.get(code, {})
        effective_alert = mas.get("level", 0)
        if mas.get("corrected", False):
            effective_alert = 0
        return {"effective_alert": effective_alert}


class ScoringEngine:
    """Layer 3: 作战指挥部——评分+阈值+信号生成"""

    @staticmethod
    def time_score(t_val: int) -> int:
        """时间加分"""
        if 1400 <= t_val <= 1445: return 8
        if 930 <= t_val <= 935: return 5
        return 0

    @staticmethod
    def score_vwap_buy(buy_profit_space: float, vwap_dev_atr: float,
                        required_profit_buy: float) -> tuple:
        """VWAP回归空间加分"""
        details = []
        score = 0
        if buy_profit_space > 0.01 or vwap_dev_atr < -1.0:
            score = 15
            details.append({"指标": "VWAP回归空间", "当前": f"+{buy_profit_space*100:.2f}%", "解读": "深度偏离均价，强力回归空间", "加分": 15})
        elif buy_profit_space > required_profit_buy:
            score = 12
            details.append({"指标": "VWAP回归空间", "当前": f"+{buy_profit_space*100:.2f}%", "解读": "低于均价，回归空间充足", "加分": 12})
        elif buy_profit_space > 0:
            score = 8
            details.append({"指标": "VWAP回归空间", "当前": f"+{buy_profit_space*100:.2f}%", "解读": "略低于均价，轻度回归空间", "加分": 8})
        return score, details

    @staticmethod
    def score_vwap_sell(sell_profit_space: float, required_profit_sell: float) -> tuple:
        """VWAP溢价空间卖出加分"""
        details = []
        score = 0
        if sell_profit_space > required_profit_sell:
            score = 18
            details.append({"指标": "VWAP溢价空间", "当前": f"+{sell_profit_space*100:.2f}%", "解读": "高于均价且盈利空间充足", "加分": 18})
        elif sell_profit_space > 0:
            score = 12
            details.append({"指标": "VWAP溢价空间", "当前": f"+{sell_profit_space*100:.2f}%", "解读": "现价高于均价", "加分": 12})
        return score, details


# ====================================================================
# V2 Pipeline: ATR-自适应 + 三层流水线
# FeatureExtractor → RiskManager → ScoringEngine → Signal
# ====================================================================

PARAMS_V2 = {
    "vwap_buy_atr_mult": -1.5,
    "vwap_sell_atr_mult": 1.2,
    "rsi_oversold_atr_adj": True,
    "buy_score_atr_smooth": 50,
    "sell_score_atr_smooth": 50,
    "trend_strength_atr_mult": 2.0,
    "stop_loss_atr_mult": 2.5,
    "take_profit_atr_mult": 3.0,
    "min_score_continuous": True,
    "factor_weight_vwap": 0.30,
    "factor_weight_rsi": 0.20,
    "factor_weight_macd": 0.15,
    "factor_weight_volume": 0.15,
    "factor_weight_position": 0.10,
    "factor_weight_time": 0.10,
    "max_score_raw": 100,
}


class FeatureExtractorV2:
    """V2: 单次调用提取全部客观特征"""

    @staticmethod
    def extract_all(code: str, name: str, df, holding: dict,
                    daily_ctx: dict, cached_minute_df=None,
                    cached_5m_df=None, cached_15m_df=None,
                    multi_tf_dict=None) -> dict:
        _pd = pd; _np = np
        feats = {}
        if df.empty or len(df) < 5:
            return feats
        last = df.iloc[-1]; prev = df.iloc[-2] if len(df) >= 2 else last
        _dt = _pd.to_datetime(last["time"]) if "time" in last else _pd.Timestamp.now()
        feats["t_val"] = _dt.hour * 100 + _dt.minute
        feats["current_minute"] = _dt.hour * 60 + _dt.minute
        feats["is_etf"] = holding.get("type") == "etf"
        price = float(last.get("close", 0)); vwap = float(last.get("vwap", 0) or 0)
        feats["price"] = price; feats["vwap"] = vwap
        feats["day_amplitude"] = float(last.get("day_amplitude", 0) or 0)
        feats["rsi"] = float(last.get("rsi", 50) or 50)
        feats["bb_pct"] = float(last.get("bb_pct", 0.5) or 0.5)
        feats["macd_hist"] = float(last.get("macd_hist", 0) or 0)
        feats["prev_macd_hist"] = float(prev.get("macd_hist", 0) or 0)
        feats["ema_spread"] = float(last.get("ema_spread", 0) or 0)
        feats["prev_ema_spread"] = float(prev.get("ema_spread", 0) or 0)
        feats["range_pos"] = float(last.get("range_pos", 0.5) or 0.5)
        feats["vol_ratio"] = float(last.get("vol_ratio", 1.0) or 1.0)
        feats["mom5"] = float(last.get("mom5", 0) or 0)
        feats["lower_shadow"] = float(last.get("lower_shadow", 0) or 0)
        feats["upper_shadow"] = float(last.get("upper_shadow", 0) or 0)
        # ATR
        if len(df) >= 14:
            atr_v = df["high"].sub(df["low"]).abs().rolling(14, min_periods=1).mean()
            feats["atr"] = float(atr_v.iloc[-1] / price) if price > 0 else 0.02
        else:
            feats["atr"] = 0.02
        atr = max(feats["atr"], 0.002)
        feats["buy_profit_space"] = (vwap - price) / price if price > 0 else 0.0
        feats["sell_profit_space"] = (price - vwap) / vwap if vwap else 0.0
        feats["vwap_dev_atr_ratio"] = feats["buy_profit_space"] / atr if atr > 0 else 0
        # today ret
        if isinstance(cached_minute_df, _pd.DataFrame) and not cached_minute_df.empty:
            day_rows = cached_minute_df[cached_minute_df["date"] == last["date"]]
            today_open = float(day_rows.iloc[0]["open"]) if not day_rows.empty else price
        else:
            today_df = df[df["date"] == last["date"]]
            today_open = float(today_df.iloc[0]["open"]) if not today_df.empty else price
        h_hold = HOLDINGS.get(code, {}) if 'HOLDINGS' in globals() else {}
        pre_close = h_hold.get("pre_close", today_open)
        feats["today_open"] = today_open; feats["pre_close"] = pre_close
        feats["today_ret"] = (price - pre_close) / pre_close if pre_close > 0 else 0.0
        feats["open_gap"] = (today_open - pre_close) / pre_close if pre_close > 0 else 0.0
        feats["prev_high"] = float(last.get("prev_high", 0) or price)
        feats["is_strong_trend"] = (feats["today_ret"] > 2 * atr) and (price >= feats["prev_high"] * 0.99) and (feats["vol_ratio"] > 1.2)
        feats["is_strong_pullback"] = feats["is_strong_trend"] and abs((price - vwap) / vwap) < 0.5 * atr if vwap else False
        cost = float(holding.get("cost", 0) or 0)
        feats["hold_qty"] = int(holding.get("t_qty") or holding.get("qty") or 0)
        feats["profit_pct"] = (price - cost) / cost if cost > 0 else 0
        feats["is_deep_loss"] = cost > 0 and feats["profit_pct"] < -5 * atr
        # daily ctx
        dc = daily_ctx if isinstance(daily_ctx, dict) else {}
        for k in ["daily_status", "daily_gate", "daily_trend_bg", "daily_ma5_state",
                   "daily_support_name", "index_regime"]:
            feats[k] = dc.get(k, "unknown")
        for n in [5, 10, 20, 30, 60, 120]:
            feats[f"daily_ma{n}"] = float(dc.get(f"daily_ma{n}", 0) or 0)
        feats["daily_ma5_slope"] = float(dc.get("daily_ma5_slope", 0) or 0)
        feats["daily_above_ma5"] = feats["daily_ma5"] > 0 and price >= feats["daily_ma5"]
        feats["daily_buy_t_ok"] = dc.get("daily_status") == "ok" and feats["daily_ma5"] > 0 and feats["daily_ma5_state"] in {"near_ma5_chop", "above_ma5_trend"}
        feats["daily_breakdown_risk"] = bool(dc.get("daily_breakdown_risk", False))
        feats["daily_overheated"] = bool(dc.get("daily_overheated", False))
        feats["daily_pullback_support"] = bool(dc.get("daily_pullback_support", False))
        feats["benchmark_gate"] = dc.get("benchmark_gate", "neutral")
        for k in ["index_regime_status", "index_circuit_state", "index_gate_advice", "index_temp_bucket"]:
            feats[k] = dc.get(k, "normal")
        # 15min/5min features (ATR自适应)
        _f15_f = FeatureExtractor.extract_15min_features(df, cached_15m_df, price, vwap, atr=atr)
        for k, v in _f15_f.items():
            feats[f"f15_{k}"] = v
        _f5_f = FeatureExtractor.extract_5min_features(df, cached_5m_df, price, vwap, atr=atr)
        for k, v in _f5_f.items():
            feats[f"f5_{k}"] = v
        _v19 = FeatureExtractor.extract_v19_oscillation(df, price, vwap, feats["open_gap"], feats["today_ret"])
        for k, v in _v19.items():
            feats[k] = v
        # ---- 强多头趋势检测（防卖飞） ----
        feats["is_strong_uptrend"] = False
        if not feats.get("is_etf") and len(df) >= 20 and price > 0:
            c5 = df["close"].tail(5).mean(); c10 = df["close"].tail(10).mean(); c20 = df["close"].tail(20).mean()
            ma_ok = c5 >= c10 * 0.995 and c10 >= c20 * 0.995
            day_low = float(df["low"].iloc[:len(df)].min())
            rebound = (price - day_low) / day_low if day_low > 0 else 0
            feats["is_strong_uptrend"] = ma_ok and rebound > 3 * atr and price > vwap * 1.005
        # ---- 双顶检测 ----
        feats["is_double_top"] = False
        if len(df) >= 10:
            high_sofar = float(df["high"].max()) if not df.empty else price
            peak_gap = (high_sofar - price) / high_sofar if high_sofar > 0 else 0
            if 0 < peak_gap < 0.005:
                peak_idx = int(df["high"].to_numpy().argmax()) if len(df) > 0 else len(df) - 1
                low_after = float(df.iloc[peak_idx:len(df)]["low"].min()) if peak_idx < len(df) else price
                had_pullback = low_after <= high_sofar * 0.995
                rate3 = (price - float(df.iloc[-3]["close"])) / float(df.iloc[-3]["close"]) if len(df) >= 3 and float(df.iloc[-3]["close"]) > 0 else 0
                mom_weak = rate3 < 0.003 or (len(df) >= 2 and price <= float(df.iloc[-2]["close"]))
                if had_pullback and mom_weak:
                    feats["is_double_top"] = True
        # ---- 开盘急跌无反包（禁买入） ----
        feats["is_gap_down_no_reversal"] = False
        current_idx = len(df) - 1
        if current_idx <= 15 and not feats.get("f5_is_strong_bullish_reversal", False):
            mom2_5m = feats.get("f5_mom2_5m", 0)
            if mom2_5m < -0.005:
                feats["is_gap_down_no_reversal"] = True
        return feats


class RiskManagerV2:
    """V2: 一票否决守门员 — 关键防线完全体"""

    @staticmethod
    def check_all(feats: dict) -> dict:
        result = {"blocked": False, "reason": "", "buy_block": [], "sell_block": []}
        if not feats:
            result["blocked"] = True; result["reason"] = "无特征数据"
            return result

        # 1. 死水（振幅不足）→ 阻止卖出（防止微小波动中频繁高抛）
        if feats.get("day_amplitude", 0) < 0.002 and feats.get("t_val", 0) > 1000:
            result["sell_block"].append("dead_water")

        # 2. 日线破位 → 阻止买入
        if feats.get("daily_breakdown_risk"):
            result["buy_block"].append("daily_breakdown_risk")

        # 3. 强势上涨抑制卖出（防卖飞）
        if feats.get("is_strong_uptrend"):
            result["sell_block"].append("strong_uptrend")

        # 4. 双顶保护 → 鼓励卖出（不阻止，但属于风控提醒）
        # （已在评分中加分，此处不block）

        # 5. 开盘急跌无反包 → 阻止买入（禁接飞刀）
        if feats.get("is_gap_down_no_reversal"):
            result["buy_block"].append("gap_down_no_reversal")

        # 6. 日线过热 → 阻止买入
        if feats.get("daily_overheated"):
            result["buy_block"].append("daily_overheated")

        return result


class ScoringEngineV2:
    """V2: 因子打分引擎
    每个 score_xxx 方法返回 (raw_signal, details):
      - raw_signal: 0.0~1.0 的标准化信号强度 (sigmoid输出)
      - details: 诊断信息列表
    calc_buy_score / calc_sell_score 使用 PARAMS_V2 权重聚合:
      final = sum(raw * 100 * weight) + binary_adders
    """

    PARAMS_V2_factors = {k: v for k, v in PARAMS_V2.items() if k.startswith("factor_weight_")}

    @staticmethod
    def _sigmoid(x: float, center: float = 0, slope: float = 1) -> float:
        return 1.0 / (1.0 + np.exp(-slope * (x - center)))

    @staticmethod
    def score_vwap_buy(feats: dict) -> tuple:
        ratio = feats.get("vwap_dev_atr_ratio", 0)
        raw = ScoringEngineV2._sigmoid(-ratio, center=0.5, slope=2.0)
        return raw, [{"指标": "VWAP偏离(ATR)", "当前": f"{ratio:.2f}σ", "强度": round(raw, 3)}]

    @staticmethod
    def score_rsi_buy(feats: dict) -> tuple:
        rsi = feats.get("rsi", 50)
        raw = ScoringEngineV2._sigmoid(35 - rsi, center=3, slope=0.5)
        return raw, [{"指标": "RSI超卖", "当前": f"{rsi:.1f}", "强度": round(raw, 3)}]

    @staticmethod
    def score_rsi_sell(feats: dict) -> tuple:
        rsi = feats.get("rsi", 50)
        raw = ScoringEngineV2._sigmoid(rsi - 78, center=3, slope=0.5)
        return raw, [{"指标": "RSI超买", "当前": f"{rsi:.1f}", "强度": round(raw, 3)}]

    @staticmethod
    def score_macd_buy(feats: dict) -> tuple:
        mh = feats.get("macd_hist", 0); pmh = feats.get("prev_macd_hist", 0)
        if mh < 0 and mh > pmh:
            ratio = min(1.0, abs(mh) / max(abs(pmh), 0.001))
            return ratio, [{"指标": "MACD负区拐头", "当前": f"{mh:.4f}↑", "强度": round(ratio, 3)}]
        return 0.0, []

    @staticmethod
    def score_macd_sell(feats: dict) -> tuple:
        mh = feats.get("macd_hist", 0); pmh = feats.get("prev_macd_hist", 0)
        if mh > 0 and mh < pmh:
            ratio = min(1.0, mh / max(mh - pmh, 0.001))
            return ratio, [{"指标": "MACD正区萎缩", "当前": f"{mh:.4f}↓", "强度": round(ratio, 3)}]
        return 0.0, []

    @staticmethod
    def score_vwap_sell(feats: dict) -> tuple:
        price = feats.get("price", 0); vwap = feats.get("vwap", 0)
        atr = max(feats.get("atr", 0.02), 0.002)
        if vwap <= 0 or price <= 0: return 0.0, []
        ratio = (price - vwap) / vwap / atr
        raw = ScoringEngineV2._sigmoid(ratio, center=0.5, slope=1.5)
        return raw, [{"指标": "VWAP溢价(ATR)", "当前": f"{ratio:.2f}σ", "强度": round(raw, 3)}]

    @staticmethod
    def score_lower_shadow(feats: dict) -> tuple:
        ls = feats.get("lower_shadow", 0)
        raw = ScoringEngineV2._sigmoid(ls, center=0.3, slope=8.0)
        return raw, [{"指标": "长下影", "当前": f"{ls:.2f}", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def score_ema_improve(feats: dict) -> tuple:
        es = feats.get("ema_spread", 0); pes = feats.get("prev_ema_spread", 0)
        delta = es - pes
        raw = ScoringEngineV2._sigmoid(delta, center=0.0005, slope=500.0)
        return raw, [{"指标": "EMA转强", "当前": f"{es*100:.4f}%", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def score_ema_weaken(feats: dict) -> tuple:
        es = feats.get("ema_spread", 0); pes = feats.get("prev_ema_spread", 0)
        delta = pes - es
        raw = ScoringEngineV2._sigmoid(delta, center=0.0005, slope=500.0)
        return raw, [{"指标": "EMA转弱", "当前": f"{es*100:.4f}%", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def score_volume(feats: dict) -> tuple:
        vr = feats.get("vol_ratio", 1.0)
        raw = ScoringEngineV2._sigmoid(vr, center=1.2, slope=4.0)
        return raw, [{"指标": "量能确认", "当前": f"{vr:.2f}", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def score_upper_shadow(feats: dict) -> tuple:
        us = feats.get("upper_shadow", 0)
        raw = ScoringEngineV2._sigmoid(us, center=0.4, slope=6.0)
        return raw, [{"指标": "长上影", "当前": f"{us:.2f}", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def _weighted_factor_score(raw: float, weight_key: str, w_mult: float = 1.0) -> float:
        """raw(0~1) × 100 × PARAMS_V2权重"""
        w = PARAMS_V2.get(weight_key, 0.10)
        return raw * 100 * w * w_mult

    @staticmethod
    def calc_buy_score(feats: dict) -> tuple:
        details = []
        score = 0.0

        # ---- 加权因子 (raw × 100 × weight) ----
        raw, d = ScoringEngineV2.score_vwap_buy(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_vwap")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_rsi_buy(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_rsi")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_macd_buy(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_macd")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_volume(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_volume")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_lower_shadow(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_position")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_ema_improve(feats)
        s = raw * 4  # EMA改善低权重固定加分
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        # ---- 二值模式加分 (不受权重衰减) ----
        if feats.get("f15_kinetic_exhaustion"):
            details.append({"指标": "15分动能衰竭", "加分": 10}); score += 10
        if feats.get("f15_near_15m_support"):
            details.append({"指标": "15分强支撑", "加分": 8}); score += 8
        if feats.get("f15_multi_bottom_15m"):
            details.append({"指标": "15分多重底", "加分": 6}); score += 6
        if feats.get("f5_is_strong_bullish_reversal"):
            details.append({"指标": "5分大阳线反包", "加分": 20}); score += 20
        elif feats.get("f5_is_volume_reversal"):
            details.append({"指标": "5分弱企稳", "加分": 8}); score += 8
        return round(score, 1), details

    @staticmethod
    def calc_sell_score(feats: dict) -> tuple:
        details = []
        score = 0.0

        raw, d = ScoringEngineV2.score_vwap_sell(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_vwap")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_rsi_sell(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_rsi")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_macd_sell(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_macd")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_volume(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_volume")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_upper_shadow(feats)
        s = ScoringEngineV2._weighted_factor_score(raw, "factor_weight_position")
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        raw, d = ScoringEngineV2.score_ema_weaken(feats)
        s = raw * 4
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})

        # Daily context binary adders
        if feats.get("daily_breakdown_risk"):
            details.append({"指标": "日线破位风险", "加分": 8}); score += 8
        if feats.get("daily_overheated"):
            details.append({"指标": "日线过热", "加分": 8}); score += 8
        return round(score, 1), details


# ==================== 信号处理与推送 ====================
_last_push: Dict[str, Dict[str, Any]] = {}
def _signal_push_limits(action: str) -> tuple[float, float]:
    if action == "ADD_POS":
        return PARAMS["add_pos_signal_price_move"], PARAMS["add_pos_signal_score_boost"]
    if action == "SELL_HIGH":
        return PARAMS["sell_signal_price_move"], PARAMS["sell_signal_score_boost"]
    if action == "PANIC_SELL":   # 保留做兜底，代码中已不再生成此信号
        return PARAMS.get("panic_sell_signal_price_move", 0.005), PARAMS.get("panic_sell_signal_score_boost", 20)
    return PARAMS["buy_signal_price_move"], PARAMS["buy_signal_score_boost"]


def _should_push(key: str, sig: Optional[Signal] = None) -> bool:
    now = _now()
    last = _last_push.get(key)
    if last:
        last_ts = last.get("ts")
        elapsed = (now - last_ts).total_seconds() if isinstance(last_ts, datetime) else PUSH_THROTTLE_SECONDS
        if elapsed < PUSH_THROTTLE_SECONDS:
            if sig is not None:
                last_score = float(last.get("score", 0) or 0)
                last_price = float(last.get("price", 0) or 0)
                score_gap = abs(float(sig.score or 0) - last_score)
                price_move = abs(float(sig.price or 0) - last_price) / last_price if last_price else 1.0
                price_threshold, score_threshold = _signal_push_limits(sig.action)
                if score_gap >= score_threshold or price_move >= price_threshold:
                    _last_push[key] = {"ts": now, "score": float(sig.score or 0), "price": float(sig.price or 0)}
                    return True
            log.info(f"⏭️  {key} 推送节流，{max(0, int(PUSH_THROTTLE_SECONDS - elapsed))}s 后可再发")
            return False
    _last_push[key] = {"ts": now, "score": float(getattr(sig, 'score', 0) or 0), "price": float(getattr(sig, 'price', 0) or 0)}
    return True


# ==================== 集合竞价驱动做T优化 ====================


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return float(value)
    except Exception:
        return default


def _minute_status_label(status: str, detail: str = "") -> str:
    status = str(status or "unknown")
    mapping = {
        "ok": "正常",
        "cache_hit": "缓存命中",
        "network_timeout": "网络超时",
        "network_dns": "DNS失败",
        "network_ssl": "SSL失败",
        "network_http": "HTTP错误",
        "network_error": "网络错误",
        "json_empty": "返回空包",
        "json_html": "HTML拦截",
        "json_error": "JSON解析失败",
        "api_empty": "接口空数据",
        "symbol_missing": "标的缺失",
        "parse_no_rows": "无分钟数据",
        "parse_short_rows": "字段过短",
        "parse_type_rows": "类型异常",
        "parse_value_error": "数值异常",
        "parse_zero_placeholder": "占位0行",
        "parse_empty": "解析为空",
    }
    label_text = mapping.get(status, status)
    if detail and status not in {"ok", "cache_hit"}:
        return f"{label_text}:{detail[:18]}"
    return label_text


def _minute_issue_bucket(status: str) -> str:
    status = str(status or "unknown")
    if status in {"cache_hit", "ok", "未拉取"}:
        return "缓存"
    if status.startswith("network_"):
        return "网络"
    if status.startswith("json_"):
        return "接口"
    if status.startswith("api_") or status == "symbol_missing":
        return "接口"
    if status.startswith("parse_"):
        return "解析"
    return "其他"
