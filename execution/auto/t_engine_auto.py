# -*- coding: utf-8 -*-
"""t_engine_auto.py — auto 侧做T引擎适配器（期B：做T引擎同源，双侧单一真源）

把 gm_main 消费的 SignalEngine 从旧 sigmoid 评分引擎（_gm/signals/engine.py，已删）切换为
core/t_decision.py 的 Renko 触发式决策核（手动侧 signal_engine.py 同源）。

保留（执行侧，不进决策核）：
  · RiskManager —— 纯函数一票否决（buy_block/sell_block，决策后挂否决）
  · FeatureExtractor —— auto 侧特征提取（_last_feats 契约：gm_main 原地写 price/profit_pct 等）
  · 执行状态全量（冷却/计数/回补记忆/轮次/诊断……）
  · 回补价格记忆门控：delayed/not_target/downgrade（WP-B07/B18）——但买回触发交给 Renko 向下砖，
    不再有分数激励（用户拍板「纯 Renko 触发」）

删除：ScoringEngine / FACTOR_WEIGHTS / calc_buy_score / calc_sell_score（sigmoid 连续打分）。

三段式加载决策核（与 build_decision_auto 同款跨仓消费）：sys.modules 短路 → 常规 import →
importlib 绝对路径（SUPERTRADER_ROOT，.gszq 部署环境回退）。
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


def _load_t_decision():
    if "core.t_decision" in sys.modules:
        return sys.modules["core.t_decision"]
    try:
        from core import t_decision as m
        return m
    except ImportError:
        pass
    _root = os.environ.get("SUPERTRADER_ROOT") or os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _path = os.path.join(_root, "core", "t_decision.py")
    if not os.path.exists(_path):
        raise RuntimeError(f"做T决策核缺失（期B 依赖）: {_path}")
    _spec = importlib.util.spec_from_file_location("core.t_decision", _path)
    _m = importlib.util.module_from_spec(_spec)
    sys.modules["core.t_decision"] = _m
    _spec.loader.exec_module(_m)
    return _m


td = _load_t_decision()
Signal = td.Signal
TDecisionEngine = td.TDecisionEngine


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
        g = rsi_delta.clip(lower=0).rolling(6, min_periods=1).mean()
        l = (-rsi_delta).clip(upper=0).rolling(6, min_periods=1).mean()
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
        self.awaiting_buyback: Dict[str, Dict[str, Any]] = {}
        self.diagnostics: Dict[str, Dict[str, Any]] = {}
        self.last_decision: Dict[str, Dict[str, Any]] = {}
        self.signals: List[Signal] = []
        self._last_feats: Dict[str, Dict[str, Any]] = {}
        # 期B: 做T决策核单一真源（与手动侧 core/signal_engine.py 同源）
        self._core = TDecisionEngine()
        # P0-5(2026-09-01): 做T买入价 t_entry_price 持久化/回灌（防策略重启丢内存态 → 600176 闭环漏记）
        self._t_entry_path = os.path.join(
            os.environ.get("SUPERTRADER_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
            "t_io", "state", "auto_t_entry.json")
        self._load_t_entry()

    def _persist_t_entry(self):
        try:
            with open(self._t_entry_path, "w", encoding="utf-8") as f:
                json.dump({"date": _engine_now().strftime("%Y-%m-%d"),
                           "entries": dict(getattr(self._core, "t_entry_price", {}) or {})},
                          f, ensure_ascii=False, default=str)
        except Exception:
            pass

    def _load_t_entry(self):
        try:
            if os.path.exists(self._t_entry_path):
                with open(self._t_entry_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if str(d.get("date")) == _engine_now().strftime("%Y-%m-%d"):
                    for k, v in (d.get("entries") or {}).items():
                        try:
                            self._core.t_entry_price[k] = dict(v)
                        except Exception:
                            pass
        except Exception:
            pass

    def _get_params(self, code: str) -> dict:
        p = dict(PARAMS)
        sp = STOCK_PARAMS.get(code, {})
        p.update(sp)
        return p

    def _check_date_reset(self):
        now = _engine_now().date()
        if now != datetime.strptime(self.state_reset_date, "%Y-%m-%d").date():
            for k in ["buy_cooldown", "sell_cooldown", "buy_count_per_stock",
                       "sell_count_per_stock", "awaiting_buyback"]:
                getattr(self, k).clear()
            self.diagnostics.clear()
            self.last_decision.clear()
            self.last_signal_state.clear()
            self.last_trade_state.clear()
            self.state_reset_date = now.strftime("%Y-%m-%d")
            # 期B: 决策核 Renko 砖状态 + 做T买入价随日界清空（决策核单一真源）
            self._core.reset_day(self.state_reset_date)

    def evaluate(self, code, name, df, holding, daily_ctx=None) -> tuple:
        """主入口：返回 (buy_score, sell_score, Signal|None)。

        决策委托 core/t_decision.py（Renko 触发式，双侧同源）；执行侧一票否决/冷却/计数/
        回补记忆门控保留在本适配器。"""
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

        # 决策核（唯一真源）：Renko 向下砖买入 / 目标止盈·时间止损·尾盘强平卖出
        # 幻影entry修复（2026-08-31）：决策核在发信号时会即时改写 t_entry_price
        # （BUY_LOW 写入 / SELL_HIGH 弹出），而执行侧门控在其后才判定。快照 entry，
        # 凡信号被门控丢弃时恢复原状，保证「信号未采用 ⇒ entry 状态不变」不变式——
        # 否则一个被 daily_breakdown_risk 拦截的 BUY_LOW 会毒化全天买入机会。
        _entry_before = self._core.t_entry_price.get(code)
        try:
            core_sig, buy_score, sell_score, _reason, _swing_meta = self._core.evaluate(
                code, name, df,
                price=float(feats.get("price", 0)),
                t_val=int(feats.get("t_val", 0)),
                vwap=float(feats.get("vwap", 0) or feats.get("price", 0)),
                today_ret=float(feats.get("today_ret", 0)),
                daily_status=feats.get("daily_status", "unknown"),
                today_str=_engine_now().strftime("%Y-%m-%d"),
                params=p,
            )
        except Exception:
            core_sig, buy_score, sell_score = None, 0.0, 0.0

        def _restore_entry():
            if _entry_before is None:
                self._core.t_entry_price.pop(code, None)
            else:
                self._core.t_entry_price[code] = _entry_before

        # 卖出判定（决策核 SELL_HIGH + 执行侧闸）
        if core_sig is not None and core_sig.action == "SELL_HIGH":
            sell_allowed = not sell_blocks
            sell_cooldown_ok = code not in self.sell_cooldown or now >= self.sell_cooldown.get(code, now)
            max_sells = int(p.get("max_sell_times_per_stock", 3))
            sell_count_ok = self.sell_count_per_stock.get(code, 0) < max_sells
            if sell_allowed and sell_cooldown_ok and sell_count_ok:
                sig = core_sig
            else:
                _restore_entry()  # 卖出被门控丢弃 → 恢复被弹出的 entry（做T仓仍持有）

        # 买入判定（卖出优先）
        if sig is None and core_sig is not None and core_sig.action == "BUY_LOW":
            # ── WP-B07/B18: 回补价格记忆 — TTL清理 + 高接门控（纯 Renko 触发，不再分数激励）──
            buyback_gate = None  # None / "delayed" / "downgrade" / "not_target"
            buyback_info = None
            ab = self.awaiting_buyback.get(code)
            if ab and float(ab.get("sell_price", 0) or 0) > 0 and feats.get("price", 0) > 0:
                _ttl = int(p.get("awaiting_buyback_ttl_minutes", 240))
                _expired = False
                _elapsed = 0
                if ab.get("persisted"):
                    # WP-B18: 跨日恢复记忆——日内 TTL 不再适用，按 expire_date 判过期
                    _exp = str(ab.get("expire_date", "") or "")
                    if _exp and str(now.date()) > _exp:
                        _expired = True
                else:
                    _elapsed = (now - ab["sell_time"]).total_seconds() / 60
                    if _elapsed > _ttl:
                        _expired = True
                if _expired:
                    self.awaiting_buyback.pop(code, None)  # 过期清除
                    self.diagnostics[code] = {
                        "buyback_ttl_expired": True,
                        "sell_price": ab.get("sell_price"),
                        "elapsed_min": round(_elapsed, 1) if not ab.get("persisted") else None,
                        "expire_date": ab.get("expire_date"),
                    }
                else:
                    _sp = float(ab["sell_price"])
                    _cp = float(feats.get("price", 0))
                    _premium = (_cp - _sp) / _sp  # >0=回补价高于前卖价(高接)
                    _delay_pct = float(p.get("buyback_above_sell_delay_pct", 0.01))
                    _dg_pct = float(p.get("buyback_above_sell_downgrade_pct", 0.0))
                    _target = float(ab.get("target_price", _sp) or _sp)
                    buyback_info = {
                        "sell_price": _sp, "price": _cp,
                        "target_price": _target,
                        "premium": round(_premium, 6),
                        "sell_time": str(ab.get("sell_time")),
                        "sell_action": ab.get("sell_action", ""),
                    }
                    if _premium > _delay_pct:
                        buyback_gate = "delayed"      # 硬延迟线之上：不接
                    elif _premium > _dg_pct:
                        buyback_gate = "downgrade"    # 软降档带：信号保留、数量减半
                    elif _cp > _target:
                        # WP-B18 3.3: 触发价语义——price <= target_price 才达标（平触算达标）
                        buyback_gate = "not_target"
                    # else: 正常低吸接回（price <= target），买回触发交给 Renko 向下砖

            buy_allowed = not buy_blocks
            buy_cooldown_ok = code not in self.buy_cooldown or now >= self.buy_cooldown.get(code, now)
            max_buys = int(p.get("max_buy_times_per_stock", 3))
            buy_count_ok = self.buy_count_per_stock.get(code, 0) < max_buys

            if buy_allowed and buy_cooldown_ok and buy_count_ok:
                if buyback_gate == "delayed":
                    # WP-B07 高接延迟：不产生 BUY_LOW，留痕后按 HOLD 返回
                    _restore_entry()  # 幻影entry回滚：信号未采用
                    self.diagnostics[code] = {"buyback_delayed": buyback_info}
                    self.last_decision[code] = {
                        "action": "HOLD",
                        "reason": "buyback_above_sell_delayed",
                        "buy_score": buy_score,
                        "sell_score": sell_score,
                        "buy_blocks": buy_blocks,
                        "sell_blocks": sell_blocks,
                        **buyback_info,
                    }
                    return buy_score, sell_score, None
                if buyback_gate == "not_target":
                    # WP-B18 3.3: 未回踩到 target → 不作为回补触发，留痕后按 HOLD 返回
                    _restore_entry()  # 幻影entry回滚：信号未采用
                    self.diagnostics[code] = {"buyback_not_target": buyback_info}
                    self.last_decision[code] = {
                        "action": "HOLD",
                        "reason": "buyback_not_target",
                        "buy_score": buy_score,
                        "sell_score": sell_score,
                        "buy_blocks": buy_blocks,
                        "sell_blocks": sell_blocks,
                        **buyback_info,
                    }
                    return buy_score, sell_score, None
                sig = core_sig
                if buyback_gate == "downgrade":
                    # WP-B07 高接降档：信号保留，main.py 在 sizer 处数量减半
                    sig.details.append({
                        "buyback_downgrade": True,
                        "sell_price": buyback_info["sell_price"],
                        "price": buyback_info["price"],
                        "premium": buyback_info["premium"],
                    })
                    self.diagnostics[code] = {"buyback_downgrade": buyback_info}
            else:
                # 幻影entry回滚：买入被门控/冷却/次数拦截，信号未采用（2026-08-31）
                _restore_entry()

        if sig:
            sig.channel = "auto"  # 期B: 自动链路标记（决策核默认 manual，auto 侧改写）
            self.signals.append(sig)

        self.last_decision[code] = {
            "action": sig.action if sig else "HOLD",
            "reason": "信号触发" if sig else "无信号",
            "buy_score": buy_score,
            "sell_score": sell_score,
            "buy_blocks": buy_blocks,
            "sell_blocks": sell_blocks,
        }
        return buy_score, sell_score, sig

    # WP-B07: 卖出类成交动作（回补价格记忆对全部卖出通道生效，
    # 以 main.py on_order_status 成交回调为实际写入点）
    BUYBACK_SELL_ACTIONS = ("SELL_HIGH", "PANIC_SELL", "TRAIL_SELL",
                            "TREND_EXIT", "TARGET_SELL", "TAIL")
    BUYBACK_BUY_ACTIONS = ("BUY_LOW", "ADD_POS")

    def arm_awaiting_buyback(self, code: str, price: float, qty: int,
                             action: str = "SELL_HIGH") -> Optional[Dict[str, Any]]:
        """WP-B07: 卖出成交后建立回补价格记忆。返回记忆记录（price<=0 时返回 None）。"""
        price = float(price or 0)
        if price <= 0:
            return None
        now = _engine_now()
        p = self._get_params(code)
        # awaiting_buyback_vwap_gap: 乘数（如0.975=低于卖价2.5%接回），兼容百分比（0.003→0.997）
        _gap = float(p.get("awaiting_buyback_vwap_gap", 0.998))
        if _gap < 0.1:
            _gap = 1.0 - _gap
        # WP-B18: 有效期 = 卖出日 + N 交易日（跨日持久化用；日内仍以 TTL 为主判断）
        _n = int(p.get("buyback_persist_days", 3))
        rec = {
            "sell_price": price,
            "sell_time": now,
            "sell_qty": int(qty or 0),
            "sell_action": action,
            "target_price": round(price * _gap, 2),
            "expire_date": _business_day_add(now.date(), _n).strftime("%Y-%m-%d"),
        }
        self.awaiting_buyback[code] = rec
        return rec

    def record_trade_action(self, code, action, qty=0, price=0.0):
        """成交回报登记。返回值（WP-B07 新增，旧调用方忽略不影响）：
        {"armed": 新建回补记忆|None, "buyback_filled": 被清除的回补记忆|None}"""
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
        # WP-B07: 回补价格记忆生命周期（仅真实成交 qty>0 才建立/清除；
        # 地板保护等 qty=0 的账面登记不动记忆）
        if int(qty or 0) > 0:
            if action in self.BUYBACK_SELL_ACTIONS:
                ret["armed"] = self.arm_awaiting_buyback(code, price, qty, action)
            elif action in self.BUYBACK_BUY_ACTIONS:
                ret["buyback_filled"] = self.awaiting_buyback.pop(code, None)
        self._persist_t_entry()  # P0-5(2026-09-01): 成交后持久化做T买入价，防重启丢内存态
        return ret
