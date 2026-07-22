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
    def __init__(self, factor_weights: dict = None):
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
        # 可传入自定义权重参数，默认 FACTOR_WEIGHTS（支持 HPO 多进程调参）
        self.factor_weights = factor_weights or FACTOR_WEIGHTS

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
            self.peak_tracker = {}
            self.morning_alert_state = {}
            self.state_reset_date = today

    def _in_cooldown(self, code: str, action: str) -> bool:
        cd_dict = self.sell_cooldown if "SELL" in action else self.buy_cooldown
        last = cd_dict.get(code)
        return bool(last) and (_now() - last).total_seconds() < PARAMS["cooldown_minutes"] * 60

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
            self.post_sell_block_until[code] = _now() + timedelta(minutes=PARAMS["post_sell_rebuild_minutes"])
            if qty > 0:
                bucket = VIRTUAL_TRADES.setdefault(code, {})
                bucket.setdefault("SELL_HIGH", []).append({"qty": qty, "ts": _now(), "action": action})
            buys = VIRTUAL_TRADES.get(code, {}).get("BUY_LOW", [])
            sells = VIRTUAL_TRADES.get(code, {}).get("SELL_HIGH", [])
            net_qty = sum(t["qty"] for t in buys) - sum(t["qty"] for t in sells)
            if net_qty <= 0 and code in self.t_cycle_start_time:
                del self.t_cycle_start_time[code]
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
        feats = FeatureExtractor.extract_all(code, name, df, holding, daily_ctx,
                                               cached_minute, cached_5m, cached_15m)
        buy_score, buy_details = ScoringEngine.calc_buy_score(feats, self.factor_weights)
        sell_score, sell_details = ScoringEngine.calc_sell_score(feats, self.factor_weights)
        # 静态基准阈值 — 分数已通过ATR+Sigmoid自适应，阈值不再跳变
        buy_threshold = 42.0; sell_threshold = 42.0
        price = feats.get("price", 0); hold_qty = feats.get("hold_qty", 0)
        # 风控阻断 + 左侧抄底豁免（5分钟强反转可绕过日线封锁）
        risk = RiskManager.check_all(feats)
        risk_buy_block = risk.get("buy_block", [])
        risk_sell_block = risk.get("sell_block", [])
        can_bypass_daily = feats.get("f5_is_strong_bullish_reversal", False) or feats.get("f5_is_volume_reversal", False)
        is_daily_ok = feats.get("daily_buy_t_ok", False) or can_bypass_daily
        base_can_buy = (len(risk_buy_block) == 0 and is_daily_ok
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
            "buy_factors": {d["指标"]: d.get("加分", 0) for d in buy_details},
            "sell_factors": {d["指标"]: d.get("加分", 0) for d in sell_details},
            "engine": "v2_final",
        })
        return buy_score, sell_score, sig


# ====================================================================
# V2 Engine: FeatureExtractor → RiskManager → ScoringEngine → Signal
# ====================================================================

class FeatureExtractor:
    """单次调用提取全部客观特征（含ATR自适应子级别特征）"""

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


class RiskManager:
    """一票否决守门员 — 死水/破位/过热/急跌/强多头防卖飞"""

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


FACTOR_WEIGHTS = {
    "vwap_buy_atr_mult": -1.5,
    "vwap_sell_atr_mult": 1.2,
    "rsi_oversold_atr_adj": True,
    "buy_score_atr_smooth": 50,
    "sell_score_atr_smooth": 50,
    "trend_strength_atr_mult": 2.0,
    "stop_loss_atr_mult": 2.5,
    "take_profit_atr_mult": 3.0,
    "min_score_continuous": True,
    "factor_weight_vwap": 0.25,
    "factor_weight_rsi": 0.15,
    "factor_weight_macd": 0.10,
    "factor_weight_volume": 0.10,
    "factor_weight_position": 0.10,
    "factor_weight_ema": 0.05,
    "factor_weight_pattern": 0.25,
    "factor_weight_time": 0.00,
    "max_score_raw": 100,
}


class ScoringEngine:
    """因子打分引擎
    每个 score_xxx 方法返回 (raw_signal, details):
      - raw_signal: 0.0~1.0 的标准化信号强度 (sigmoid输出)
      - details: 诊断信息列表
    calc_buy_score / calc_sell_score 使用 FACTOR_WEIGHTS 权重聚合:
      final = sum(raw * 100 * weight) + binary_adders
    """

    @staticmethod
    def _sigmoid(x: float, center: float = 0, slope: float = 1) -> float:
        return 1.0 / (1.0 + np.exp(-slope * (x - center)))

    @staticmethod
    def score_vwap_buy(feats: dict) -> tuple:
        ratio = feats.get("vwap_dev_atr_ratio", 0)
        raw = ScoringEngine._sigmoid(-ratio, center=0.5, slope=2.0)
        return raw, [{"指标": "VWAP偏离(ATR)", "当前": f"{ratio:.2f}σ", "强度": round(raw, 3)}]

    @staticmethod
    def score_rsi_buy(feats: dict) -> tuple:
        rsi = feats.get("rsi", 50)
        raw = ScoringEngine._sigmoid(35 - rsi, center=3, slope=0.5)
        return raw, [{"指标": "RSI超卖", "当前": f"{rsi:.1f}", "强度": round(raw, 3)}]

    @staticmethod
    def score_rsi_sell(feats: dict) -> tuple:
        rsi = feats.get("rsi", 50)
        raw = ScoringEngine._sigmoid(rsi - 78, center=3, slope=0.5)
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
        raw = ScoringEngine._sigmoid(ratio, center=0.5, slope=1.5)
        return raw, [{"指标": "VWAP溢价(ATR)", "当前": f"{ratio:.2f}σ", "强度": round(raw, 3)}]

    @staticmethod
    def score_lower_shadow(feats: dict) -> tuple:
        ls = feats.get("lower_shadow", 0)
        raw = ScoringEngine._sigmoid(ls, center=0.3, slope=8.0)
        return raw, [{"指标": "长下影", "当前": f"{ls:.2f}", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def score_ema_improve(feats: dict) -> tuple:
        es = feats.get("ema_spread", 0); pes = feats.get("prev_ema_spread", 0)
        delta = es - pes
        raw = ScoringEngine._sigmoid(delta, center=0.0005, slope=500.0)
        return raw, [{"指标": "EMA转强", "当前": f"{es*100:.4f}%", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def score_ema_weaken(feats: dict) -> tuple:
        es = feats.get("ema_spread", 0); pes = feats.get("prev_ema_spread", 0)
        delta = pes - es
        raw = ScoringEngine._sigmoid(delta, center=0.0005, slope=500.0)
        return raw, [{"指标": "EMA转弱", "当前": f"{es*100:.4f}%", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def score_volume(feats: dict) -> tuple:
        vr = feats.get("vol_ratio", 1.0)
        raw = ScoringEngine._sigmoid(vr, center=1.2, slope=4.0)
        return raw, [{"指标": "量能确认", "当前": f"{vr:.2f}", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def score_upper_shadow(feats: dict) -> tuple:
        us = feats.get("upper_shadow", 0)
        raw = ScoringEngine._sigmoid(us, center=0.4, slope=6.0)
        return raw, [{"指标": "长上影", "当前": f"{us:.2f}", "强度": round(raw, 3)}] if raw > 0.05 else (0.0, [])

    @staticmethod
    def _weighted_factor_score(raw: float, weight_key: str, w_mult: float = 1.0,
                                 p: dict = None) -> float:
        """raw(0~1) × 100 × 权重。p 来自实例的 factor_weights，默认 FACTOR_WEIGHTS。"""
        w = (p or FACTOR_WEIGHTS).get(weight_key, 0.10)
        return raw * 100 * w * w_mult

    @staticmethod
    def calc_buy_score(feats: dict, p: dict = None) -> tuple:
        """p: 可选权重参数，来自 SignalEngine.factor_weights。默认 FACTOR_WEIGHTS。"""
        details = []; score = 0.0
        raw, d = ScoringEngine.score_vwap_buy(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_vwap", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_rsi_buy(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_rsi", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_macd_buy(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_macd", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_volume(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_volume", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_lower_shadow(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_position", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_ema_improve(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_ema", p=p); score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        # ---- 形态因子 (Pattern Factor, 通过 factor_weight_pattern 加权) ----
        _pattern_raw = 0.0
        _pnames = []
        if feats.get("f5_is_strong_bullish_reversal"):
            _pattern_raw = max(_pattern_raw, 1.0); _pnames.append("5分大阳线反包")
        if feats.get("f5_is_volume_reversal") and _pattern_raw < 0.7:
            _pattern_raw = max(_pattern_raw, 0.7); _pnames.append("5分弱企稳")
        if feats.get("f15_kinetic_exhaustion"):
            _pattern_raw = max(_pattern_raw, 0.6); _pnames.append("15分动能衰竭")
        if feats.get("f15_near_15m_support"):
            _pattern_raw = max(_pattern_raw, 0.5); _pnames.append("15分强支撑")
        if feats.get("f15_multi_bottom_15m"):
            _pattern_raw = max(_pattern_raw, 0.4); _pnames.append("15分多重底")
        _s_pattern = ScoringEngine._weighted_factor_score(_pattern_raw, "factor_weight_pattern", p=p)
        score += _s_pattern
        if _pnames:
            details.append({"指标": "形态组合(" + "/".join(_pnames) + ")", "强度": round(_pattern_raw, 2), "加分": round(_s_pattern, 1)})
        return round(score, 1), details

    @staticmethod
    def calc_sell_score(feats: dict, p: dict = None) -> tuple:
        details = []; score = 0.0
        raw, d = ScoringEngine.score_vwap_sell(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_vwap", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_rsi_sell(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_rsi", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_macd_sell(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_macd", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_volume(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_volume", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_upper_shadow(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_position", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_ema_weaken(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_ema", p=p); score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        # ---- 卖出端形态因子 (Pattern Factor) ----
        _pattern_raw = 0.0
        _pnames = []
        if feats.get("daily_breakdown_risk"):
            _pattern_raw = max(_pattern_raw, 1.0); _pnames.append("日线破位风险")
        if feats.get("daily_overheated"):
            _pattern_raw = max(_pattern_raw, 0.8); _pnames.append("日线过热")
        _s_pattern = ScoringEngine._weighted_factor_score(_pattern_raw, "factor_weight_pattern", p=p)
        score += _s_pattern
        if _pnames:
            details.append({"指标": "卖出形态(" + "/".join(_pnames) + ")", "强度": round(_pattern_raw, 2), "加分": round(_s_pattern, 1)})
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
