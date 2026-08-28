# -*- coding: utf-8 -*-
"""
core/t_decision.py — 做T决策核（期B：做T引擎同源，双侧单一真源）

把 Renko 触发式做T决策（向下砖 + 15分MACD金叉 买入 / 目标止盈·时间止损·尾盘强平 卖出）
抽成**纯函数式决策核**：无 IO、无 superTrader/goldminer 环境依赖，仅用标准库 + numpy/pandas。

消费方：
  · superTrader（手动侧）：core/signal_engine.py 的 SignalEngine 委托本模块决策
  · goldminer（自动侧）：execution/auto/t_engine_auto.py 经 SUPERTRADER_ROOT 以 importlib
    绝对路径加载本文件（与 core/build_decision.py 同一跨仓消费模式）

纪律：
  · 本模块的 Renko 触发规则是双侧做T决策一致性的唯一来源——改动必须双侧同步验证
    （scripts/t_engine_parity.py 双跑对照）。
  · 禁止在本模块加 IO/网络/文件读写；trace 通过注入的 callable 产出事件，由两侧各自落盘。
  · 只依赖 numpy/pandas + stdlib，不 import 顶层 config.py（goldminer 环境有自己的 config 包）。

依据（39支×1年1min复验）：
  Renko买入择时 +30min 60.6%(39/39支>50%)；target+0.5%止盈 完整做T闭环胜率 78.5%。
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# 与 config.PARAMS 的 swing_* 键同值（goldminer 侧无法 import superTrader config.py）。
# 以此为本模块内嵌兜底；自动侧 _get_params 返回的 dict 与它合并（swing_* 不抄进 _gm/params.py）。
DEFAULT_T_PARAMS = {
    "swing_renko_brick_pct": 0.003,
    "swing_take_profit_pct": 0.005,
    "swing_t_max_hold_min": 0,
    "swing_force_exit_tval": 1455,
}


@dataclass
class Signal:
    """做T决策输出载体（唯一真源，带 channel）。manual=人工链路；auto=自动化链路。"""
    code: str = ''; name: str = ''; action: str = ''; price: float = 0.0; score: float = 0.0
    reasons: List[str] = field(default_factory=list)
    details: List[Dict[str, Any]] = field(default_factory=list)
    indicators: Dict[str, float] = field(default_factory=dict)
    factors: Dict[str, Any] = field(default_factory=dict)
    ts: Any = None
    cycle_id: str = ''; cycle_action_count: int = 0; hold_qty: int = 0
    channel: str = "manual"


# ═══════════════════════════════════════════
# vendored 指标（复刻 build_decision._resample_15min 做法，只 vendor 决策所需，保口径一致）
# ═══════════════════════════════════════════

def _resample_to_15min(df: pd.DataFrame) -> pd.DataFrame:
    """1分钟 → 15分钟聚合（与 analysis/indicators.resample_to_15min 同口径）。
    相比原版：amount 列做存在性防御（缺失则跳过该聚合，goldminer 某路径可能无 amount）。"""
    if df.empty or len(df) < 15:
        return pd.DataFrame()
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["time_15m"] = df["time"].dt.floor("15min")
    agg_dict = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "amount" in df.columns:
        agg_dict["amount"] = "sum"
    agg = df.groupby("time_15m").agg(agg_dict).reset_index()
    return agg.rename(columns={"time_15m": "time"})


def _macd_hist_15m(df_15min: pd.DataFrame) -> float:
    """15分钟 MACD(12,26,9) 柱状体最新值（与 analysis/indicators.add_15min_indicators 同口径）。"""
    if df_15min.empty or len(df_15min) < 3:
        return 0.0
    c = df_15min["close"]
    exp1 = c.ewm(span=12, adjust=False).mean()
    exp2 = c.ewm(span=26, adjust=False).mean()
    macd = exp1 - exp2
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = (macd - signal) * 2
    last = hist.iloc[-1]
    return float(last) if pd.notna(last) else 0.0


class RenkoBuilder:
    """Renko K线构建器（vendor 自 analysis/renko_builder.py，纯 numpy/pandas 无 IO）。
    砖高参数：保守0.2% / 中等0.3%(推荐) / 激进0.5%。"""

    def __init__(self, brick_size_pct: float = 0.003, brick_size_absolute: Optional[float] = None):
        self.brick_size_pct = brick_size_pct
        self.brick_size_absolute = brick_size_absolute
        self.bricks: List[Dict] = []
        self.last_brick_top = None
        self.last_brick_bottom = None
        self.brick_direction = None  # "up" or "down"

    def _get_brick_size(self, price: float) -> float:
        if self.brick_size_absolute is not None:
            return self.brick_size_absolute
        return price * self.brick_size_pct

    def update(self, timestamp, close_price: float, high_price: float, low_price: float,
               volume: float = 0) -> bool:
        """更新 Renko 砖（返回是否产生新砖）。"""
        if not self.bricks:
            brick_size = self._get_brick_size(close_price)
            self.last_brick_top = close_price + brick_size
            self.last_brick_bottom = close_price - brick_size
            self.bricks.append({
                "timestamp": timestamp, "price": close_price, "direction": None,
                "volume": volume, "brick_top": self.last_brick_top,
                "brick_bottom": self.last_brick_bottom,
            })
            return False

        brick_size = self._get_brick_size(close_price)
        new_brick_created = False

        if close_price > self.last_brick_top:
            self.brick_direction = "up"
            self.last_brick_bottom = self.last_brick_top
            self.last_brick_top = self.last_brick_bottom + brick_size
            new_brick_created = True
        elif close_price < self.last_brick_bottom:
            self.brick_direction = "down"
            self.last_brick_top = self.last_brick_bottom
            self.last_brick_bottom = self.last_brick_top - brick_size
            new_brick_created = True

        if new_brick_created:
            self.bricks.append({
                "timestamp": timestamp, "price": close_price,
                "direction": self.brick_direction, "volume": volume,
                "brick_top": self.last_brick_top, "brick_bottom": self.last_brick_bottom,
            })
        return new_brick_created


# ═══════════════════════════════════════════
# 做T决策引擎（有状态、无 IO）
# ═══════════════════════════════════════════

class TDecisionEngine:
    """Renko 触发式做T决策。有状态（增量砖 + 当日买入价），但规则纯：
    只读显式参数 + 自身砖/entry 状态，不读任何全局/文件/时钟。两侧各持一个实例，状态语义即同源。"""

    def __init__(self):
        self._renko_states: Dict[str, Dict[str, Any]] = {}  # code -> {date, builder, last_ts}
        self.t_entry_price: Dict[str, Dict[str, Any]] = {}   # code -> {date, price, ts}

    def reset_day(self, today_str: str) -> None:
        """日切清空（两侧各自在日界调用，替代原 _reset_daily_state_if_needed 的 renko 段）。"""
        self._renko_states = {}
        self.t_entry_price = {}

    def evaluate(self, code: str, name: str, df, price: float, t_val: int, vwap: float,
                 today_ret: float, daily_status: str, today_str: str,
                 params: dict = None, trace=None
                 ) -> tuple:
        """做T决策主入口。返回 (sig, buy_score, sell_score, decision_reason, swing_meta)。

        - df: 1min DataFrame，需 time/open/high/low/close/volume 列（amount 可选）
        - price/t_val/vwap/today_ret/daily_status: 由调用方从 feats 提取后显式传入
        - today_str: 日期字符串，用于 Renko 砖/entry 的日界判断
        - params: 与 DEFAULT_T_PARAMS 合并（swing_renko_brick_pct / swing_take_profit_pct /
          swing_t_max_hold_min / swing_force_exit_tval）
        - trace: 可注入 callable(event_dict)；None=不记（无 IO）。event 无 ts 字段，由调用方补。
        """
        p = {**DEFAULT_T_PARAMS, **(params or {})}
        # 1) Renko 增量状态机（实盘/回放共用，避免每次全量重建）
        rs = self._renko_states.get(code)
        if rs is None or rs.get("date") != today_str:
            rs = {"date": today_str,
                  "builder": RenkoBuilder(brick_size_pct=float(p.get("swing_renko_brick_pct", 0.003))),
                  "last_ts": None}
        builder = rs["builder"]
        last_ts = rs.get("last_ts")
        try:
            new_rows = df[df["time"] > last_ts] if last_ts is not None else df
        except Exception:
            new_rows = df
        last_down = False
        for row in new_rows.itertuples():
            try:
                created = builder.update(row.time, float(row.close), float(row.high),
                                         float(row.low), float(getattr(row, "volume", 0) or 0))
            except Exception:
                created = False
            if created:
                last_down = (builder.brick_direction == "down")
        if len(new_rows) > 0:
            rs["last_ts"] = df.iloc[-1]["time"]
        self._renko_states[code] = rs

        entry = self.t_entry_price.get(code)
        has_entry = bool(entry and entry.get("date") == today_str)
        m15 = _macd_hist_15m(_resample_to_15min(df))
        tp = float(p.get("swing_take_profit_pct", 0.005))
        max_hold = int(p.get("swing_t_max_hold_min", 0) or 0)
        force_tval = int(p.get("swing_force_exit_tval", 1455))

        # GUI 实时流展示用：当前 Renko 状态（证明引擎持续评估，0 分≠异常而是等待触发）
        _brick_dir = getattr(builder, "brick_direction", None)
        swing_meta = {
            "brick_dir": _brick_dir,
            "brick_count": len(builder.bricks) if hasattr(builder, "bricks") else None,
            "m15": round(m15, 3),
            "has_entry": has_entry,
            "tp_gap_pct": round((price / entry["price"] - 1) * 100, 2)
                          if has_entry and entry.get("price") else None,
        }
        _ind = {"vwap": vwap, "today_ret": today_ret,
                "market_state": daily_status,
                "entry_kind": "swing_renko", "macd_hist_15m": m15}
        _fac = {"threshold": 0.0, "entry_kind": "swing_renko"}

        # 2) 卖出优先：目标止盈 / 时间止损 / 尾盘强平
        if has_entry and price > 0:
            exit_reason = None
            if price >= entry["price"] * (1 + tp):
                exit_reason = f"目标止盈+{tp*100:.1f}%(卖{price:.2f}≥买{entry['price']:.2f}×{1+tp:.3f})"
            elif max_hold > 0 and entry.get("ts") is not None:
                try:
                    mins = (df.iloc[-1]["time"] - pd.to_datetime(entry["ts"])).total_seconds() / 60
                except Exception:
                    mins = 0
                if mins >= max_hold:
                    exit_reason = f"时间止损{max_hold}min"
            elif t_val >= force_tval:
                exit_reason = "尾盘强平(当日闭环)"
            if exit_reason:
                sell_score = 100.0
                _det = f"Renko做T卖出({exit_reason})"
                sig = Signal(code, name, "SELL_HIGH", price, sell_score,
                             [_det], [{"指标": "高抛", "当前": _det, "加分": 100.0}], _ind, dict(_fac))
                self.t_entry_price.pop(code, None)
                if trace is not None:
                    try:
                        trace({
                            "code": code, "name": name, "action": "SELL_HIGH",
                            "price": round(float(price), 3),
                            "entry_price": round(float(entry["price"]), 3),
                            "tp_target": round(float(entry["price"]) * (1 + tp), 3),
                            "exit_reason": exit_reason,
                            "macd15": round(m15, 3),
                        })
                    except Exception:
                        pass
                return sig, 0.0, sell_score, "SELL_HIGH", swing_meta

        # 3) 买入：最新向下砖 + 15分MACD金叉（当日未持有做T仓）
        if not has_entry and last_down and m15 > 0 and price > 0:
            buy_score = 100.0
            _det = f"Renko向下砖+15分MACD金叉({m15:.2f})"
            sig = Signal(code, name, "BUY_LOW", price, buy_score,
                         [_det], [{"指标": "低吸", "当前": _det, "加分": 100.0}], _ind, dict(_fac))
            self.t_entry_price[code] = {"date": today_str, "price": price, "ts": df.iloc[-1]["time"]}
            if trace is not None:
                try:
                    trace({
                        "code": code, "name": name, "action": "BUY_LOW",
                        "price": round(float(price), 3),
                        "macd15": round(m15, 3),
                        "brick_direction": "down",
                    })
                except Exception:
                    pass
            return sig, buy_score, 0.0, "BUY_LOW", swing_meta

        if has_entry:
            _gap = swing_meta["tp_gap_pct"]
            swing_meta["wait"] = (f"持仓中·距目标止盈+{tp*100:.1f}%: {_gap:+.2f}%"
                                  if _gap is not None else f"持仓中·等+{tp*100:.1f}%")
        else:
            _df15 = _resample_to_15min(df)
            _n15 = len(_df15)
            if _n15 < 3:
                swing_meta["wait"] = f"MACD15预热中(需3根15分K线, 当前{_n15}根)"
            elif last_down:
                swing_meta["wait"] = f"等MACD15转正(当前{m15:.2f})"
            else:
                swing_meta["wait"] = f"等Renko向下砖(当前{_brick_dir or '首砖'})"
        return None, 0.0, 0.0, "HOLD_NO_SWING", swing_meta
