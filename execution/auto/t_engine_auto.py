# -*- coding: utf-8 -*-
"""t_engine_auto.py — auto 侧执行态适配器

⚠️ 2026-09-22（owner 指示）：**Renko 做T决策核（`core/t_decision.py`）已删除**。
本文件不再加载决策核、`SignalEngine.evaluate` **恒返回 `(0.0, 0.0, None)`**（不产生 T 信号）。
保护类卖出（HARD_STOP_EXIT / PANIC_SELL / TRAIL_SELL / TREND_EXIT / TARGET_SELL）
由 `sell_channels._sell_channel_gate` 独立生成，**不受本次删除影响**。

本文件保留的是 auto 侧**执行态**：
  · RiskManager —— 纯函数一票否决（buy_block/sell_block）
  · FeatureExtractor —— auto 侧特征提取（`_last_feats` 契约：gm_main 原地写 price/profit_pct 等）
  · 冷却 / 计数 / 状态复位（`_check_date_reset`）/ `last_decision`（GUI 与留痕）
  · ⚠️ `awaiting_buyback` 仅作**空字典占位**保留（旧反T回补义务机制已整删），
    以免尚未清理的读取点 AttributeError。
"""
import importlib.util
import json
import os
import sys

import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

from config.params import PARAMS, STOCK_PARAMS


# 2026-09-22（owner 指示）：Renko/做T 决策核已删除，本适配器不再加载 core/t_decision.py。
# 保留的是**执行侧**：RiskManager / FeatureExtractor / 冷却计数 / 回补链持久化 / 状态复位。


# ===== 时间注入（回测/回放时由 gm_main 设为当前 K 线时间） =====
SIM_NOW: Optional[datetime] = None


def _engine_now() -> datetime:
    return SIM_NOW if SIM_NOW is not None else datetime.now()


def _business_day_add(d, n):
    """WP-B18: 日期加 n 个交易日（跳过周末；节假日不剔除，交易日历留 Phase D）。"""
    cur = d
    cnt = 0
    while cnt < n:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            cnt += 1
    return cur


# ===== RiskManager =====

class RiskManager:
    """一票否决守门员（纯函数，保留在 auto 侧执行态，不进决策核）"""

    @staticmethod
    def check_all(feats: dict, stock_params: dict = None) -> dict:
        result = {"blocked": False, "reason": "", "buy_block": [], "sell_block": []}
        if not feats:
            result["blocked"] = True
            result["reason"] = "无特征数据"
            return result
        sp = stock_params or {}
        if feats.get("day_amplitude", 0) < 0.002 and feats.get("t_val", 0) > 1000:
            result["sell_block"].append("dead_water")
        # 2026-08-31: 破位/过热拦截支持个股放行开关（对齐根 config.py 既有设计，
        # 此前 auto 侧硬拦截、allow_* 键形同虚设，回测实证 588170 做T瘫痪）
        if feats.get("daily_breakdown_risk") and not sp.get("allow_breakdown_buy"):
            result["buy_block"].append("daily_breakdown_risk")
        # N1 fix: strong_uptrend 不再禁卖（做T策略的利润来源就是卖强），
        # 改为在评分中降分处理。仅当 指数uni_up + 个股强趋势 双确认时才降分不禁卖
        # (已通过 factor_weight_index_regime 在评分中体现)
        if feats.get("is_gap_down_no_reversal"):
            result["buy_block"].append("gap_down_no_reversal")
        if feats.get("daily_overheated") and not sp.get("allow_overheated_buy"):
            result["buy_block"].append("daily_overheated")
        index_regime = feats.get("index_regime", "range")
        if index_regime == "uni_down":
            result["buy_block"].append("index_uni_down_clearance")
            # C-3(2026-09-07): 熔断可归因到板块（reason 带指数码；无板块码=市场级上证）
            if feats.get("index_board_code"):
                result["board_index_code"] = feats["index_board_code"]
        for alert in (feats.get("intraday_alerts") or []):
            if alert.get("tag") in ("I1", "I4"):
                result["buy_block"].append(f"intraday_panic_{alert.get('tag')}")
        return result


# ===== FeatureExtractor =====

class FeatureExtractor:
    """单次调用提取全部客观特征（auto 侧口径，供 _last_feats 与 RiskManager）"""

    @staticmethod
    def extract_all(code: str, name: str, df, holding: dict,
                    daily_ctx: dict, cached_5m_df=None,
                    cached_15m_df=None) -> dict:
        feats = {}
        if df.empty or len(df) < 5:
            return feats
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        _dt = df.index if hasattr(last, "time") else pd.Timestamp.now()
        if "time" in last and hasattr(last["time"], "hour"):
            _dt = pd.to_datetime(last["time"])
        feats["t_val"] = _dt.hour * 100 + _dt.minute if hasattr(_dt, "hour") else 0
        feats["current_minute"] = _dt.hour * 60 + _dt.minute if hasattr(_dt, "hour") else 0
        feats["is_etf"] = holding.get("type") == "etf"
        price = float(last.get("close", 0))
        vwap = float(last.get("vwap", 0) or 0)
        feats["price"] = price
        feats["vwap"] = vwap
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
        if len(df) >= 14:
            atr_v = df["high"].sub(df["low"]).abs().rolling(14, min_periods=1).mean()
            feats["atr"] = float(atr_v.iloc[-1] / price) if price > 0 else 0.02
        else:
            feats["atr"] = 0.02
        atr = max(feats["atr"], 0.002)
        feats["buy_profit_space"] = (vwap - price) / price if price > 0 else 0.0
        feats["sell_profit_space"] = (price - vwap) / vwap if vwap else 0.0
        feats["vwap_dev_atr_ratio"] = feats["buy_profit_space"] / atr if atr > 0 else 0
        today_df = df[df["date"] == last["date"]]
        today_open = float(today_df.iloc[0]["open"]) if not today_df.empty else price
        pre_close = float(holding.get("pre_close", today_open) or today_open)
        feats["today_open"] = today_open
        feats["pre_close"] = pre_close
        feats["today_ret"] = (price - pre_close) / pre_close if pre_close > 0 else 0.0
        feats["open_gap"] = (today_open - pre_close) / pre_close if pre_close > 0 else 0.0
        feats["prev_high"] = float(last.get("prev_high", 0) or price)
        feats["is_strong_trend"] = (feats["today_ret"] > 2 * atr) and (price >= feats["prev_high"] * 0.99) and (feats["vol_ratio"] > 1.2)
        feats["is_strong_pullback"] = feats["is_strong_trend"] and abs((price - vwap) / vwap) < 0.5 * atr if vwap else False
        cost = float(holding.get("cost", 0) or 0)
        feats["hold_qty"] = int(holding.get("t_qty") or holding.get("qty") or 0)
        feats["profit_pct"] = (price - cost) / cost if cost > 0 else 0
        # N4 fix: 用日线级 ATR（≈日振幅的 1/14）代替 1分钟 K 线 ATR
        daily_atr = float(daily_ctx.get("daily_atr", 0) or 0)
        if daily_atr <= 0:
            daily_atr = atr * 14  # 1分钟ATR×14 ≈ 日ATR 近似
        # N9 fix: PANIC触发线带固定下限 -12%，防止暴跌中ATR自解除
        _panic_floor = -0.12  # -12% 固定下限
        _panic_atr_line = -5 * daily_atr
        _panic_trigger = max(_panic_atr_line, _panic_floor)
        feats["is_deep_loss"] = cost > 0 and feats["profit_pct"] < _panic_trigger
        feats["panic_trigger"] = _panic_trigger
        feats["panic_atr_line"] = _panic_atr_line
        dc = daily_ctx if isinstance(daily_ctx, dict) else {}
        for k in ["daily_status", "daily_gate", "daily_trend_bg", "daily_ma5_state",
                   "daily_support_name", "index_regime"]:
            feats[k] = dc.get(k, "unknown")
        feats["index_board_code"] = dc.get("index_board_code", "")     # C-3: 该股所属板块指数码（熔断归因）
        for n in [5, 10, 20, 30, 60, 120]:
            feats[f"daily_ma{n}"] = float(dc.get(f"daily_ma{n}", 0) or 0)
        feats["daily_ma5_slope"] = float(dc.get("daily_ma5_slope", 0) or 0)
        feats["daily_above_ma5"] = feats["daily_ma5"] > 0 and price >= feats["daily_ma5"]
        feats["daily_buy_t_ok"] = dc.get("daily_status") == "ok" and feats["daily_ma5"] > 0 and feats["daily_ma5_state"] in {"near_ma5_chop", "above_ma5_trend"}
        feats["daily_breakdown_risk"] = bool(dc.get("daily_breakdown_risk", False))
        feats["daily_overheated"] = bool(dc.get("daily_overheated", False))
        feats["daily_pullback_support"] = bool(dc.get("daily_pullback_support", False))
        feats["benchmark_gate"] = dc.get("benchmark_gate", "neutral")
        feats["intraday_alerts"] = dc.get("intraday_alerts", [])
        for k in ["index_regime_status", "index_circuit_state", "index_gate_advice", "index_temp_bucket"]:
            feats[k] = dc.get(k, "normal")
        if cached_15m_df is not None and not cached_15m_df.empty:
            _f15 = FeatureExtractor.extract_15min_features(cached_15m_df, price, vwap, atr=atr)
            for k, v in _f15.items():
                feats[f"f15_{k}"] = v
        if cached_5m_df is not None and not cached_5m_df.empty:
            _f5 = FeatureExtractor.extract_5min_features(cached_5m_df, price, vwap, atr=atr)
            for k, v in _f5.items():
                feats[f"f5_{k}"] = v
        feats["is_strong_uptrend"] = False
        if not feats.get("is_etf") and len(df) >= 20 and price > 0:
            c5 = df["close"].tail(5).mean()
            c10 = df["close"].tail(10).mean()
            c20 = df["close"].tail(20).mean()
            ma_ok = c5 >= c10 * 0.995 and c10 >= c20 * 0.995
            # N1 fix: 用当日最低点而非全缓存最低点（原 bug: 跨2日缓存使 low 偏太多）
            today_df = df[df["date"] == last["date"]]
            day_low = float(today_df["low"].min()) if not today_df.empty else 0
            rebound = (price - day_low) / day_low if day_low > 0 else 0
            feats["is_strong_uptrend"] = ma_ok and rebound > 3 * atr and price > vwap * 1.005
        feats["is_double_top"] = False
        if len(df) >= 10:
            high_sofar = float(df["high"].max()) if not df.empty else price
            peak_gap = (high_sofar - price) / high_sofar if high_sofar > 0 else 0
        return feats

    @staticmethod
    def extract_15min_features(df, price, vwap, atr=0.02):
        """15分钟线特征"""
        feats = {}
        if df is None or df.empty or len(df) < 3:
            return feats
        c15 = df["close"]
        rsi_delta = c15.diff()
        # Wilder 平滑（2026-09-21 统一口径；真相源 analysis/indicators.py::wilder_rsi）
        g = rsi_delta.clip(lower=0).ewm(alpha=1.0 / 6, adjust=False).mean()
        # fix 2026-09-21 符号 bug：原写 `(-rsi_delta).clip(upper=0)` —— 先取负再截断，
        # 使下跌计 0、上涨计为负 ⇒ rs 为负 ⇒ RSI 算出负值或除零。正解是先截断再取负。
        l = (-rsi_delta.clip(upper=0)).ewm(alpha=1.0 / 6, adjust=False).mean()
        rs = g / l.replace(0, np.nan)
        feats["rsi"] = float(100 - 100 / (1 + rs).iloc[-1]) if rs.notna().any() else 50
        return feats

    @staticmethod
    def extract_5min_features(df, price, vwap, atr=0.02):
        """5分钟线特征"""
        feats = {}
        if df is None or df.empty or len(df) < 3:
            return feats
        return feats


# ===== SignalEngine =====

class SignalEngine:
    def __init__(self):
        self.buy_cooldown: Dict[str, datetime] = {}
        self.sell_cooldown: Dict[str, datetime] = {}
        self.buy_count_per_stock: Dict[str, int] = {}
        self.sell_count_per_stock: Dict[str, int] = {}
        self.state_reset_date = _engine_now().strftime("%Y-%m-%d")
        self.last_signal_state: Dict[str, Dict[str, Any]] = {}
        self.last_trade_state: Dict[str, Dict[str, Any]] = {}
        # 2026-09-22：做T引擎（含反T回补义务 awaiting_buyback）已删除。
        # 该属性仅作为**空字典占位**保留，避免任何尚未清理的读取点 AttributeError；
        # 无任何代码再写入它（旧义务链的落盘/恢复/过期机制已整删）。
        self.awaiting_buyback: Dict[str, Dict[str, Any]] = {}
        self.diagnostics: Dict[str, Dict[str, Any]] = {}
        self.last_decision: Dict[str, Dict[str, Any]] = {}
        self.signals: List[Any] = []
        self._last_feats: Dict[str, Dict[str, Any]] = {}

    def _get_params(self, code: str) -> dict:
        p = dict(PARAMS)
        sp = STOCK_PARAMS.get(code, {})
        p.update(sp)
        return p

    def _check_date_reset(self):
        now = _engine_now().date()
        if now != datetime.strptime(self.state_reset_date, "%Y-%m-%d").date():
            for k in ["buy_cooldown", "sell_cooldown", "buy_count_per_stock",
                       "sell_count_per_stock"]:
                getattr(self, k).clear()
            self.diagnostics.clear()
            self.last_decision.clear()
            self.last_signal_state.clear()
            self.last_trade_state.clear()
            self.state_reset_date = now.strftime("%Y-%m-%d")

    def evaluate(self, code, name, df, holding, daily_ctx=None) -> tuple:
        """主入口：返回 (buy_score, sell_score, Signal|None)。

        ⚠️ 2026-09-22：Renko 做T决策核已删除 ⇒ **恒不产生 T 信号**（返回 None）。
        保留执行侧契约：`_last_feats` / RiskManager 否决 / `last_decision`。"""
        self._check_date_reset()
        now = _engine_now()
        p = self._get_params(code)
        daily_ctx = daily_ctx or {}
        feats = FeatureExtractor.extract_all(code, name, df, holding, daily_ctx)
        if not feats:
            return 0.0, 0.0, None

        self._last_feats[code] = feats
        risk = RiskManager.check_all(feats, STOCK_PARAMS.get(code, {}))
        sig = None

        if risk.get("blocked"):
            self.last_decision[code] = {"action": "HOLD", "reason": risk.get("reason", "blocked")}
            return 0.0, 0.0, None

        sell_blocks = risk.get("sell_block", [])
        buy_blocks = risk.get("buy_block", [])

        # 2026-09-22（owner 指示）：Renko 做T决策核（TDecisionEngine）已删除
        # ⇒ 本方法**恒不产生 T 信号**（BUY_LOW / SELL_HIGH 一律不再生成）。
        # 保护类卖出（HARD_STOP_EXIT / PANIC_SELL / TRAIL_SELL / TREND_EXIT / TARGET_SELL）
        # **不在此处**：由 `sell_channels._sell_channel_gate` 独立生成，且其显式支持
        # `sig is None`（sell_channels.py:383），故底仓保护链不受本次删除影响。
        # 本方法保留执行侧契约：`_last_feats`（gm_main 与 sell_channels 都读）、
        # RiskManager 否决、`last_decision`（GUI/留痕）。
        self.last_decision[code] = {
            "action": "HOLD",
            "reason": "T引擎已删除（无 T 信号）",
            "buy_score": 0.0,
            "sell_score": 0.0,
            "buy_blocks": buy_blocks,
            "sell_blocks": sell_blocks,
        }
        return 0.0, 0.0, None

    def record_trade_action(self, code, action, qty=0, price=0.0):
        """成交回报登记。

        2026-09-22：回补价格记忆（BUYBACK_SELL_ACTIONS / arm_awaiting_buyback /
        _apply_buyback_fill）已随做T引擎删除 ⇒ 返回值 `armed`/`buyback_filled` 恒为 None，
        仅为兼容旧调用方保留键。冷却/计数照旧。"""
        now = _engine_now()
        p = self._get_params(code)
        self.last_trade_state[code] = {
            "action": action, "qty": qty, "price": price, "time": now,
        }
        ret = {"armed": None, "buyback_filled": None}
        if action in ("SELL_HIGH", "PANIC_SELL"):
            cd = int(p.get("cooldown_minutes", 30))
            self.sell_cooldown[code] = now + timedelta(minutes=cd)
            self.sell_count_per_stock[code] = self.sell_count_per_stock.get(code, 0) + 1
        elif action in ("BUY_LOW", "ADD_POS"):
            cd = int(p.get("cooldown_minutes", 30))
            self.buy_cooldown[code] = now + timedelta(minutes=cd)
            self.buy_count_per_stock[code] = self.buy_count_per_stock.get(code, 0) + 1
        return ret
