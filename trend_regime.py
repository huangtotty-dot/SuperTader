# -*- coding: utf-8 -*-
"""
trend_regime.py — V3.0 5分钟趋势状态机 + RSI 择时触发器

三层信号架构的核心模块（第二层：趋势层）。
消费 indicators.py 的 5分钟 MACD/BOLL/RSI 输出，产生：
  1. trend_state ∈ {STRONG_BULL, BULL, NEUTRAL, BEAR, STRONG_BEAR}
  2. trend_confidence ∈ [0, 1]
  3. rsi_buy_trigger / rsi_sell_trigger — 择时事件

设计原则：
  - 防抖：状态切换需连续 2 根 5分钟 K 线确认
  - 从严：STRONG 档需 BOLL 方向确认（防止 MACD 假突破）
  - NEUTRAL 双向放行（不做方向限制）
"""
import numpy as np
import pandas as pd
from enum import Enum
from typing import Tuple, Optional, Dict, Any
from datetime import datetime


class TrendState(Enum):
    STRONG_BULL = "STRONG_BULL"   # DIF>0, DEA>0, 中轨向上, 带宽扩张
    BULL = "BULL"                 # DIF>0, DEA>0
    NEUTRAL = "NEUTRAL"           # 零轴附近或中轨走平+收窄
    BEAR = "BEAR"                 # DIF<0, DEA<0
    STRONG_BEAR = "STRONG_BEAR"   # DIF<0, DEA<0, 中轨向下, 带宽扩张


# ── 判定阈值（可通过 PARAMS 覆盖）──
DEFAULT_PARAMS = {
    "trend_bb_slope_lookback": 5,      # BOLL 中轨斜率回看 K 线数
    "trend_bb_slope_flat": 0.0005,      # 中轨斜率 < 此值视为"走平"（相对变化率）
    "trend_bb_width_expand": 1.05,      # 带宽 / 前 N 根均值 > 此值视为"扩张"
    "trend_bb_width_contract": 0.95,    # 带宽 / 前 N 根均值 < 此值视为"收窄"
    "trend_dif_near_zero": 0.001,       # DIF 绝对值 < 此值视为"零轴附近"（相对价格）
    "trend_debounce_bars": 2,           # 状态切换确认所需连续 K 线数
    "rsi_oversold_5m": 32,              # 5分钟 RSI 超卖阈值
    "rsi_overbought_5m": 68,            # 5分钟 RSI 超买阈值
    "rsi_reversal_min_delta": 2.0,      # RSI 极值反转最小变化量
}


class TrendRegime:
    """5分钟趋势状态机

    用法：
        regime = TrendRegime()
        state, confidence = regime.update(df_5min)  # df_5min 须已含 indicators.py 计算的所有列

    状态持久化（重启恢复）：
        regime.to_dict() / TrendRegime.from_dict(data)
    """

    def __init__(self, params: dict = None):
        self.params = {**DEFAULT_PARAMS, **(params or {})}
        self._current_state: TrendState = TrendState.NEUTRAL
        self._confidence: float = 0.0
        self._pending_state: Optional[TrendState] = None
        self._pending_bars: int = 0
        self._last_dif: float = 0.0
        self._last_dea: float = 0.0
        self._last_rsi: float = 50.0
        self._prev_rsi: float = 50.0
        self._bb_mid_slope: float = 0.0
        self._bb_width_ratio: float = 1.0
        # 触发器
        self.rsi_buy_trigger: bool = False
        self.rsi_sell_trigger: bool = False
        # 历史
        self._state_history: list = []  # [(timestamp, state, confidence), ...]

    # ── 属性 ──

    @property
    def state(self) -> TrendState:
        return self._current_state

    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def is_bullish(self) -> bool:
        return self._current_state in (TrendState.BULL, TrendState.STRONG_BULL)

    @property
    def is_bearish(self) -> bool:
        return self._current_state in (TrendState.BEAR, TrendState.STRONG_BEAR)

    @property
    def is_neutral(self) -> bool:
        return self._current_state == TrendState.NEUTRAL

    # ── 核心判定逻辑 ──

    def update(self, df_5min: pd.DataFrame, now: datetime = None) -> Tuple[TrendState, float]:
        """根据最新的 5分钟 K 线 DataFrame 更新趋势状态。

        Args:
            df_5min: 须含 dif_5m, dea_5m, bb_mid_5m, bb_width_5m, rsi_5m 列
            now: 当前时间戳（用于历史记录）

        Returns:
            (TrendState, confidence)
        """
        if df_5min.empty or len(df_5min) < 5:
            return self._current_state, self._confidence

        last = df_5min.iloc[-1]
        price = float(last["close"]) if "close" in last else 0

        # 1. 提取当前值
        dif = float(last.get("dif_5m", 0) or 0)
        dea = float(last.get("dea_5m", 0) or 0)
        bb_mid = float(last.get("bb_mid_5m", 0) or 0)
        bb_width = float(last.get("bb_width_5m", 0) or 0)
        rsi = float(last.get("rsi_5m", 50) or 50)

        # 2. BOLL 中轨斜率（N 根 K 线线性回归）
        p = self.params
        lookback = min(p["trend_bb_slope_lookback"], len(df_5min))
        mids = df_5min["bb_mid_5m"].tail(lookback).values
        if len(mids) >= 3 and np.std(mids) > 1e-8 and mids[-1] > 0:
            x = np.arange(len(mids))
            slope, _ = np.polyfit(x, mids, 1)
            self._bb_mid_slope = slope / mids[-1]  # 相对斜率
        else:
            self._bb_mid_slope = 0.0

        # 3. BOLL 带宽变化率（当前 / 前 N 根均值）
        if len(df_5min) >= lookback + 1:
            prev_widths = df_5min["bb_width_5m"].iloc[-(lookback+1):-1].values
            mean_width = np.mean(prev_widths) if len(prev_widths) > 0 else bb_width
            self._bb_width_ratio = bb_width / mean_width if mean_width > 0 else 1.0
        else:
            self._bb_width_ratio = 1.0

        # 4. 存储上期 RSI
        self._prev_rsi = self._last_rsi
        self._last_rsi = rsi
        self._last_dif = dif
        self._last_dea = dea

        # 5. 判定基础方向（MACD 零轴）
        if price > 0:
            dif_norm = abs(dif) / price  # 归一化到价格水平
        else:
            dif_norm = abs(dif)

        dif_near_zero = dif_norm < p["trend_dif_near_zero"]

        if dif > 0 and dea > 0:
            base_state = TrendState.BULL
        elif dif < 0 and dea < 0:
            base_state = TrendState.BEAR
        elif dif_near_zero:
            base_state = TrendState.NEUTRAL
        elif dif > 0:  # DIF 上穿但 DEA 未跟上
            base_state = TrendState.BULL  # 偏多
        else:
            base_state = TrendState.BEAR  # 偏空

        # 6. BOLL 辅助确认（升级/降级 STRONG 档 / NEUTRAL）
        mid_rising = self._bb_mid_slope > p["trend_bb_slope_flat"]
        mid_falling = self._bb_mid_slope < -p["trend_bb_slope_flat"]
        width_expanding = self._bb_width_ratio > p["trend_bb_width_expand"]
        width_contracting = self._bb_width_ratio < p["trend_bb_width_contract"]

        if base_state == TrendState.BULL and mid_rising and width_expanding:
            candidate_state = TrendState.STRONG_BULL
            candidate_conf = min(1.0, 0.6 + 0.2 * (1 if mid_rising else 0) + 0.2 * (1 if width_expanding else 0))
        elif base_state == TrendState.BEAR and mid_falling and width_expanding:
            candidate_state = TrendState.STRONG_BEAR
            candidate_conf = min(1.0, 0.6 + 0.2 * (1 if mid_falling else 0) + 0.2 * (1 if width_expanding else 0))
        elif base_state == TrendState.BULL:
            candidate_state = TrendState.BULL
            candidate_conf = 0.55 + 0.15 * (1 if mid_rising else 0)
        elif base_state == TrendState.BEAR:
            candidate_state = TrendState.BEAR
            candidate_conf = 0.55 + 0.15 * (1 if mid_falling else 0)
        else:
            # NEUTRAL：中轨走平 + 带宽收窄 → 高置信度 NEUTRAL
            candidate_state = TrendState.NEUTRAL
            mid_flat = not mid_rising and not mid_falling
            candidate_conf = 0.5 + 0.3 * (1 if (mid_flat and width_contracting) else 0)

        # 7. 防抖：状态切换需连续 N 根 K 线确认
        debounce = p["trend_debounce_bars"]
        if candidate_state != self._current_state:
            if candidate_state == self._pending_state:
                self._pending_bars += 1
            else:
                self._pending_state = candidate_state
                self._pending_bars = 1

            if self._pending_bars >= debounce:
                # 确认切换
                self._current_state = self._pending_state
                self._confidence = candidate_conf
                self._pending_state = None
                self._pending_bars = 0
            # 否则保持当前状态，置信度不变
        else:
            # 同状态，清除待定
            self._pending_state = None
            self._pending_bars = 0
            # 更新置信度（平滑）
            self._confidence = 0.7 * self._confidence + 0.3 * candidate_conf

        # 8. RSI 择时触发器
        self._update_rsi_triggers(rsi)

        # 9. 记录历史
        ts = now or datetime.now()
        if len(self._state_history) > 500:
            self._state_history = self._state_history[-200:]
        self._state_history.append((ts, self._current_state.value, round(self._confidence, 3)))

        return self._current_state, self._confidence

    def _update_rsi_triggers(self, rsi: float):
        """更新 RSI 极值反转触发器"""
        p = self.params
        prev = self._prev_rsi

        # 买入触发：从超卖区回升
        self.rsi_buy_trigger = (
            rsi > p["rsi_oversold_5m"]
            and prev <= p["rsi_oversold_5m"]
            and (rsi - prev) >= p["rsi_reversal_min_delta"]
        )

        # 卖出触发：从超买区回落
        self.rsi_sell_trigger = (
            rsi < p["rsi_overbought_5m"]
            and prev >= p["rsi_overbought_5m"]
            and (prev - rsi) >= p["rsi_reversal_min_delta"]
        )

    # ── 方向门控 ──

    def buy_gate_multiplier(self) -> float:
        """返回买入分数乘数（趋势方向门控）

        STRONG_BEAR: 0.3 — 大幅抑制逆势买入
        BEAR:        0.6 — 适度抑制
        NEUTRAL:     1.0 — 双向放行
        BULL:        1.0 — 顺势买入
        STRONG_BULL: 1.0 — 顺势买入（无抑制）
        """
        if self._current_state == TrendState.STRONG_BEAR:
            return 0.3
        elif self._current_state == TrendState.BEAR:
            return 0.6
        return 1.0

    def buy_threshold_penalty(self) -> float:
        """返回买入阈值惩罚分（提高买入门槛）"""
        if self._current_state == TrendState.STRONG_BEAR:
            return 12.0
        elif self._current_state == TrendState.BEAR:
            return 6.0
        return 0.0

    def sell_gate_multiplier(self) -> float:
        """返回卖出分数乘数"""
        if self._current_state == TrendState.STRONG_BULL:
            return 0.3
        elif self._current_state == TrendState.BULL:
            return 0.6
        return 1.0

    def sell_threshold_penalty(self) -> float:
        """返回卖出阈值惩罚分（提高卖出门槛）"""
        if self._current_state == TrendState.STRONG_BULL:
            return 12.0
        elif self._current_state == TrendState.BULL:
            return 6.0
        return 0.0

    # ── T_MODE 方向适配 ──

    def apply_t_mode(self, t_mode: str, buy_score: float, sell_score: float) -> Tuple[float, float]:
        """根据 T 模式调整买卖分数。

        正T (long)：优先找买点 → 卖点从严
        反T (short)：优先找卖点 → 买点从严
        """
        if t_mode == "short":
            # 反T：抑制买入，鼓励卖出
            if self.is_bullish:
                # 牛市+反T：卖点优先（降低卖出门槛）
                sell_score *= 1.15
                buy_score *= 0.7
            elif self.is_bearish:
                # 熊市+反T：顺势做空（正常反T）
                pass  # 默认行为即可
        elif t_mode == "long":
            # 正T：抑制卖出，鼓励买入
            if self.is_bearish:
                # 熊市+正T：买点优先（降低买入门槛）
                buy_score *= 1.15
                sell_score *= 0.7
            elif self.is_bullish:
                # 牛市+正T：顺势做多（正常正T）
                pass
        return buy_score, sell_score

    # ── 持久化 ──

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self._current_state.value,
            "confidence": self._confidence,
            "pending_state": self._pending_state.value if self._pending_state else None,
            "pending_bars": self._pending_bars,
            "last_dif": self._last_dif,
            "last_dea": self._last_dea,
            "last_rsi": self._last_rsi,
            "prev_rsi": self._prev_rsi,
            "bb_mid_slope": self._bb_mid_slope,
            "bb_width_ratio": self._bb_width_ratio,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], params: dict = None) -> "TrendRegime":
        obj = cls(params=params)
        if data:
            try:
                obj._current_state = TrendState(data.get("state", "NEUTRAL"))
            except ValueError:
                obj._current_state = TrendState.NEUTRAL
            obj._confidence = float(data.get("confidence", 0.0))
            ps = data.get("pending_state")
            obj._pending_state = TrendState(ps) if ps else None
            obj._pending_bars = int(data.get("pending_bars", 0))
            obj._last_dif = float(data.get("last_dif", 0))
            obj._last_dea = float(data.get("last_dea", 0))
            obj._last_rsi = float(data.get("last_rsi", 50))
            obj._prev_rsi = float(data.get("prev_rsi", 50))
            obj._bb_mid_slope = float(data.get("bb_mid_slope", 0))
            obj._bb_width_ratio = float(data.get("bb_width_ratio", 1.0))
        return obj

    def __repr__(self) -> str:
        arrows = {"STRONG_BULL": "▲▲", "BULL": "▲", "NEUTRAL": "─", "BEAR": "▼", "STRONG_BEAR": "▼▼"}
        arrow = arrows.get(self._current_state.value, "?")
        return f"TrendRegime({arrow} {self._current_state.value} conf={self._confidence:.2f} dif={self._last_dif:.4f} dea={self._last_dea:.4f} rsi={self._last_rsi:.1f})"
