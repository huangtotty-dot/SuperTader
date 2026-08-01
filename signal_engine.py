# V1.11: 日志增强模块导入
import sys as _sys
import os as _os_mod
# V3.0fix: __file__ = E:\06_T\signal_engine.py → dirname = E:\06_T
_06t_dir = _os_mod.path.dirname(_os_mod.path.abspath(__file__))
if _06t_dir not in _sys.path:
    _sys.path.insert(0, _06t_dir)

import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

# === V3.0: 显式导入，替代 exec() 共享命名空间 ===
try:
    from config import PARAMS, STOCK_PARAMS, PUSH_THROTTLE_SECONDS
except ImportError:
    PARAMS = {}; STOCK_PARAMS = {}; PUSH_THROTTLE_SECONDS = 300

try:
    from trend_regime import TrendRegime, TrendState
except ImportError:
    TrendRegime = None; TrendState = None

# ======== 独立模式回退依赖 ========
if 'get_today_str' not in globals():
    def get_today_str(): return datetime.now().strftime("%Y-%m-%d")
if '_now' not in globals():
    def _now(): return datetime.now()
# PARAMS now exclusively from config.py (stub removed — dual-copy drift fixed in V3.0)
if 'MINUTE_FETCH_DETAIL' not in globals(): MINUTE_FETCH_DETAIL = {}
if 'MINUTE_FETCH_STATUS' not in globals(): MINUTE_FETCH_STATUS = {}
if 'DAILY_CONTEXT_CACHE' not in globals(): DAILY_CONTEXT_CACHE = {}
if 'HOLDINGS' not in globals(): HOLDINGS = {}
if 'VIRTUAL_TRADES' not in globals(): VIRTUAL_TRADES = {}
if 'SIGNAL_OUTCOME_TRACKER' not in globals(): SIGNAL_OUTCOME_TRACKER = {}
if 'T_MODE' not in globals(): T_MODE = {}
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
    from indicators import resample_to_15min, add_15min_indicators
if 'resample_to_5min' not in globals():
    from indicators import resample_to_5min, add_5min_indicators
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
        self.pending_sells: Dict[str, Dict[str, Any]] = {}  # V1.29: 买入→高抛追踪
        self.daily_realized_loss_monitor = 0.0
        # V1.20/V1.21 dead states removed in V3.0 (peak_tracker, diagnostics, scenario_factor_state)
        # V1.25: 早盘预警状态机（基于近两年数据训练）
        self.morning_alert_state: Dict[str, Dict[str, Any]] = {}
        # V3.0: 5分钟趋势状态机（每个持仓独立实例，重启从盘中状态恢复）
        self.trend_regimes: Dict[str, "TrendRegime"] = {} if TrendRegime else {}
        # V3.0: 5分钟 DataFrame 缓存（仅在5分钟边界重算，避免15s轮询×N只持仓的浪费）
        self._5min_cache: Dict[str, tuple] = {}  # code → (last_boundary_ts, df_5min)
        # V1.30: 决策原因码缓存（供面板/日报展示熔断等原因）
        self.last_decision: Dict[str, Dict[str, Any]] = {}
        # 可传入自定义权重参数，默认 FACTOR_WEIGHTS（支持 HPO 多进程调参）
        self.factor_weights = factor_weights or FACTOR_WEIGHTS
        # V1.29: 从 VIRTUAL_TRADES 恢复闭环追踪状态（重启后不丢）
        self._recover_tracking_from_trades()
        # V1.30: 恢复轮次/次数/冷却等盘中状态（重启后不清零）
        self._load_intraday_state()

    def _recover_tracking_from_trades(self):
        """V1.29: 从持久化的 VIRTUAL_TRADES 恢复闭环追踪状态。
        程序重启后，根据未配对的买卖还原 awaiting_buyback / pending_sells。
        """
        vt = VIRTUAL_TRADES if 'VIRTUAL_TRADES' in globals() else {}
        for code, actions in vt.items():
            sells = actions.get("SELL_HIGH", [])
            buys = actions.get("BUY_LOW", [])
            total_sold = sum(t.get("qty", 0) for t in sells)
            total_bought = sum(t.get("qty", 0) for t in buys)
            if total_sold > total_bought:
                # 有未接回的卖出 → 建立接回追踪
                last_sell = max(sells, key=lambda t: str(t.get("ts", ""))) if sells else None
                if last_sell and float(last_sell.get("price", 0) or 0) > 0:
                    _gap = PARAMS.get("awaiting_buyback_vwap_gap", 0.975)
                    if _gap < 0.1:
                        _gap = 1.0 - _gap
                    _sp = float(last_sell.get("price", 0) or 0)
                    self.awaiting_buyback[code] = {
                        "sell_price": _sp if _sp > 0 else 0,
                        "sell_time": _now(),
                        "qty": total_sold - total_bought,
                        "target_price": round(_sp * _gap, 2) if _sp > 0 else 0,
                        "ttl": PARAMS.get("awaiting_buyback_ttl_minutes", 120),
                        "recovered": True,
                    }
            elif total_bought > total_sold:
                # 有未卖出的买入 → 建立高抛追踪
                last_buy = max(buys, key=lambda t: str(t.get("ts", ""))) if buys else None
                if last_buy and float(last_buy.get("price", 0) or 0) > 0:
                    tp = _sp_param(code, "take_profit_pct", 0.010)  # V3.0fix N2
                    _bp = float(last_buy.get("price", 0) or 0)
                    self.pending_sells[code] = {
                        "buy_price": _bp if _bp > 0 else 0,
                        "buy_time": _now(),
                        "qty": total_bought - total_sold,
                        "target_price": round(_bp * (1 + tp), 2) if _bp > 0 else 0,
                        "recovered": True,
                    }

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
            self.pending_sells = {}
            self.daily_realized_loss_monitor = 0.0
            self.morning_alert_state = {}
            self._5min_cache = {}       # V3.0: 5分钟缓存每日重置
            self.trend_regimes = {}     # V3.0: 趋势状态机每日重置（开盘从头累积）
            self.state_reset_date = today

    def _in_cooldown(self, code: str, action: str) -> bool:
        cd_dict = self.sell_cooldown if "SELL" in action else self.buy_cooldown
        last = cd_dict.get(code)
        return bool(last) and (_engine_now() - last).total_seconds() < PARAMS["cooldown_minutes"] * 60

    # ===== V1.30: 盘中状态持久化（轮次/次数/冷却，重启后不清零）=====
    def _intraday_state_path(self) -> str:
        try:
            from config import T_IO_DIR
            return _os_mod.path.join(T_IO_DIR, "intraday_state.json")
        except Exception:
            return "intraday_state.json"

    def _persist_intraday_state(self):
        if not PERSIST_INTRADAY_STATE:
            return
        try:
            import json as _j
            data = {
                "date": get_today_str(),
                "cycle_count": dict(self.cycle_count),
                "buy_count": dict(self.buy_count_per_stock),
                "sell_count": dict(self.sell_count_per_stock),
                "buy_cooldown": {k: v.isoformat() for k, v in self.buy_cooldown.items() if v},
                "sell_cooldown": {k: v.isoformat() for k, v in self.sell_cooldown.items() if v},
                # V3.0: 5分钟趋势状态持久化
                "trend_regimes": {k: v.to_dict() for k, v in self.trend_regimes.items()} if TrendRegime else {},
            }
            with open(self._intraday_state_path(), "w", encoding="utf-8") as f:
                _j.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_intraday_state(self):
        if not PERSIST_INTRADAY_STATE:
            return
        try:
            import json as _j
            p = self._intraday_state_path()
            if not _os_mod.path.exists(p):
                return
            with open(p, "r", encoding="utf-8") as f:
                data = _j.load(f)
            if data.get("date") != get_today_str():
                return
            self.cycle_count.update({k: int(v) for k, v in (data.get("cycle_count") or {}).items()})
            self.buy_count_per_stock.update({k: int(v) for k, v in (data.get("buy_count") or {}).items()})
            self.sell_count_per_stock.update({k: int(v) for k, v in (data.get("sell_count") or {}).items()})
            for k, v in (data.get("buy_cooldown") or {}).items():
                try: self.buy_cooldown[k] = datetime.fromisoformat(v)
                except Exception: pass
            for k, v in (data.get("sell_cooldown") or {}).items():
                try: self.sell_cooldown[k] = datetime.fromisoformat(v)
                except Exception: pass
            # V3.0: 恢复5分钟趋势状态
            if TrendRegime and data.get("trend_regimes"):
                for code, tr_data in data["trend_regimes"].items():
                    try:
                        self.trend_regimes[code] = TrendRegime.from_dict(tr_data)
                    except Exception:
                        pass
        except Exception:
            pass

    def incr_cycle(self, code: str):
        """V1.30: 轮次计数 + 持久化（替代 main.py 直接自增，防重启清零）"""
        self._reset_daily_state_if_needed()
        self.cycle_count[code] = self.cycle_count.get(code, 0) + 1
        self._persist_intraday_state()

    def record_signal(self, code: str, action: str, price: float, score: float):
        snapshot = self.last_signal_state.setdefault(code, {})
        snapshot["action"] = action
        snapshot["price"] = price
        snapshot["score"] = score
        snapshot["ts"] = _now()
        self._last_sig_price = price  # V1.29: 供 record_trade_action 建立接回追踪
        if "SELL" in action:
            self.sell_cooldown[code] = _engine_now()
        else:
            self.buy_cooldown[code] = _engine_now()
        self._persist_intraday_state()  # V1.30

    def record_trade_action(self, code: str, action: str, qty: int = 0, price: float = 0.0):
        self._reset_daily_state_if_needed()
        # V1.30: 价格守卫 —— 优先用调用方传入的信号价，杜绝 price 缺失/为0的账面记录
        if price and float(price) > 0:
            self._last_sig_price = float(price)
        self.last_trade_state[code] = {"action": action, "qty": qty, "ts": _now()}
        if action in ["BUY_LOW", "ADD_POS"]:
            self.buy_count_per_stock[code] = self.buy_count_per_stock.get(code, 0) + 1
            self.t_cycle_start_time.setdefault(code, _now())
            self.cycle_direction[code] = "buy"
            if qty > 0:
                bucket = VIRTUAL_TRADES.setdefault(code, {})
                _px = float(getattr(self, '_last_sig_price', 0) or 0)
                bucket.setdefault("BUY_LOW", []).append({"qty": qty, "ts": _now(), "action": action, "price": _px})
                # V1.29: 买入后检查是否完成接回闭环
                ab = self.awaiting_buyback.get(code)
                if ab:
                    total_bought = sum(t.get("qty", 0) for t in bucket.get("BUY_LOW", []))
                    if total_bought >= ab.get("qty", 0):
                        self.awaiting_buyback.pop(code, None)  # 闭环完成
                # V1.29: 买入后建立高抛追踪
                price = float(getattr(self, '_last_sig_price', 0) or 0)
                if price > 0:
                    tp = _sp_param(code, "take_profit_pct", 0.010)  # V3.0fix N2
                    self.pending_sells[code] = {
                        "buy_price": price, "buy_time": _now(), "qty": qty,
                        "target_price": price * (1 + tp),
                    }
        elif action in ["SELL_HIGH", "PANIC_SELL"]:
            self.sell_count_per_stock[code] = self.sell_count_per_stock.get(code, 0) + 1
            self.cycle_direction[code] = "sell"
            self.post_sell_block_until[code] = _now() + timedelta(minutes=PARAMS["post_sell_rebuild_minutes"])
            if qty > 0:
                bucket = VIRTUAL_TRADES.setdefault(code, {})
                _px = float(getattr(self, '_last_sig_price', 0) or 0)
                bucket.setdefault("SELL_HIGH", []).append({"qty": qty, "ts": _now(), "action": action, "price": _px})
                # V1.29: 建立接回追踪 — 卖出后主动寻找低吸机会
                price = float(getattr(self, '_last_sig_price', 0) or 0)
                if price > 0:
                    # awaiting_buyback_vwap_gap: 乘数（如0.975=低于卖价2.5%接回），兼容百分比（0.003→0.997）
                    _gap = PARAMS.get("awaiting_buyback_vwap_gap", 0.975)
                    if _gap < 0.1:  # 百分比格式，转乘数
                        _gap = 1.0 - _gap
                    self.awaiting_buyback[code] = {
                        "sell_price": price, "sell_time": _now(), "qty": qty,
                        "target_price": round(price * _gap, 2),
                        "ttl": PARAMS.get("awaiting_buyback_ttl_minutes", 120),
                    }
                # V1.29: 卖出后检查是否完成高抛闭环
                ps = self.pending_sells.get(code)
                if ps:
                    total_sold = sum(t.get("qty", 0) for t in bucket.get("SELL_HIGH", []))
                    if total_sold >= ps.get("qty", 0):
                        self.pending_sells.pop(code, None)  # 闭环完成
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
        self._persist_intraday_state()  # V1.30

    def _virtual_net_qty(self, code: str, holding: dict) -> int:
        buys = VIRTUAL_TRADES.get(code, {}).get("BUY_LOW", [])
        sells = VIRTUAL_TRADES.get(code, {}).get("SELL_HIGH", [])
        base_qty = int(holding.get("t_qty") or holding.get("qty") or 0)
        return max(0, base_qty + sum(t["qty"] for t in buys) - sum(t["qty"] for t in sells))

    def _check_morning_alert(self, code, name, df, feats):
        """V1.28: 早盘单边下行预警检测 (10:00触发, 每天一次)
        Level 2 → 全天禁止买入
        Level 1 → 提高买入门槛, 仅允许深V
        """
        today = get_today_str()
        alert_state = self.morning_alert_state.get(code, {})
        if alert_state.get("date") == today and alert_state.get("checked", False):
            return
        t_val = feats.get("t_val", 0)
        if t_val < PARAMS.get("morning_no_sell_until", 1000):
            return
        if alert_state.get("date") != today:
            self.morning_alert_state[code] = {"date": today}
        # 计算早盘特征 (开盘~10:00)
        try:
            _pd_loc = pd
            _morning = df[df["time"] < _pd_loc.Timestamp(today + " 10:00:00")]
            if _morning.empty or len(_morning) < 5:
                self.morning_alert_state[code].update({"checked": True, "level": 0})
                return
            first = _morning.iloc[0]
            last_m = _morning.iloc[-1]
            open_p = float(first.get("open", feats.get("today_open", 0)))
            if open_p <= 0:
                self.morning_alert_state[code].update({"checked": True, "level": 0})
                return
            open_30min_ret = (float(last_m["close"]) - open_p) / open_p
            max_gain_after_open = (float(_morning["high"].max()) - open_p) / open_p
            max_loss_after_open = (float(_morning["low"].min()) - open_p) / open_p
            # 5分钟/10分钟开盘涨幅
            _p5 = _morning[_morning["time"] < _pd_loc.Timestamp(today + " 09:35:00")]
            _p10 = _morning[_morning["time"] < _pd_loc.Timestamp(today + " 09:40:00")]
            open_5min_ret = (float(_p5.iloc[-1]["close"]) - open_p) / open_p if len(_p5) >= 3 else 0
            open_10min_ret = (float(_p10.iloc[-1]["close"]) - open_p) / open_p if len(_p10) >= 5 else 0
        except Exception:
            self.morning_alert_state[code].update({"checked": True, "level": 0})
            return
        # 读取MORNING_ALERT_PARAMS
        try:
            from config import MORNING_ALERT_PARAMS
        except ImportError:
            self.morning_alert_state[code].update({"checked": True, "level": 0})
            return
        cfg = MORNING_ALERT_PARAMS.get(code, {})
        if not cfg or not cfg.get("alert_enabled", False):
            self.morning_alert_state[code].update({"checked": True, "level": 0})
            return
        # 检查Level 2 (最严格, 全天禁止买入)
        for rule in cfg.get("level_2_rules", []):
            cond = rule.get("condition", {})
            match = True
            for k, v in cond.items():
                val = locals().get(k, None)
                if val is None:
                    match = False
                    break
                if k in ("open_30min_ret", "open_5min_ret", "open_10min_ret", "max_gain_after_open", "max_loss_after_open"):
                    if val > v:
                        match = False
                        break
            if match:
                self.morning_alert_state[code].update({
                    "checked": True, "level": 2,
                    "rules": [rule.get("name", "L2")]
                })
                # V3.0fix P0-A5: 推送红色预警卡片
                if 'send_morning_alert' in globals():
                    try: send_morning_alert(code, name, 2, [rule.get("name", "L2")],
                        {"open_30min_ret": open_30min_ret, "max_gain": max_gain_after_open})
                    except Exception: pass
                return
        # 检查Level 1 (提高买入门槛)
        for rule in cfg.get("level_1_rules", []):
            cond = rule.get("condition", {})
            match = True
            for k, v in cond.items():
                val = locals().get(k, None)
                if val is None:
                    match = False
                    break
                if k in ("open_30min_ret", "open_5min_ret", "open_10min_ret", "max_gain_after_open", "max_loss_after_open"):
                    if val > v:
                        match = False
                        break
            if match:
                self.morning_alert_state[code].update({
                    "checked": True, "level": 1,
                    "rules": [rule.get("name", "L1")]
                })
                # V3.0fix P0-A5: 推送黄色预警卡片
                if 'send_morning_alert' in globals():
                    try: send_morning_alert(code, name, 1, [rule.get("name", "L1")],
                        {"open_30min_ret": open_30min_ret, "max_gain": max_gain_after_open})
                    except Exception: pass
                return
        _was_alerted = self.morning_alert_state.get(code, {}).get("level", 0) > 0
        self.morning_alert_state[code].update({"checked": True, "level": 0})
        # V3.0fix P0-A5: 预警清除推送
        if _was_alerted and 'notify_alert_cleared' in globals():
            try: notify_alert_cleared(code, name, "预警解除", {"reason": "条件不再满足"})
            except Exception: pass

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
        # ===== V3.0: 5分钟趋势状态机更新（在评分前注入趋势层信息）=====
        if TrendRegime is not None and len(df) >= 5:
            try:
                _df_5m = None
                _trend_feats = None  # 缓存：同一边界内复用趋势层输出
                # 回测模式：使用预计算的 cached_5m
                if isinstance(cached_5m, pd.DataFrame) and not cached_5m.empty:
                    _last_t = pd.to_datetime(df.iloc[-1]["time"])
                    _cutoff = _last_t.floor("5min")
                    _df_5m = cached_5m[cached_5m["time"] <= _cutoff].copy()
                else:
                    # 实盘模式：仅在5分钟边界重算 + 趋势状态机仅边界更新
                    _now_ts = pd.to_datetime(df.iloc[-1]["time"])
                    _boundary = _now_ts.floor("5min")
                    _cache_entry = self._5min_cache.get(code)
                    if _cache_entry and _cache_entry[0] == _boundary:
                        _df_5m = _cache_entry[1]  # 缓存命中
                        _trend_feats = _cache_entry[2] if len(_cache_entry) > 2 else None
                    else:
                        _df_5m = resample_to_5min(df) if 'resample_to_5min' in globals() else pd.DataFrame()
                        _df_5m = add_5min_indicators(_df_5m) if 'add_5min_indicators' in globals() else _df_5m
                        if not _df_5m.empty:
                            # 新边界：更新趋势状态机（一次，不会重复）
                            if code not in self.trend_regimes:
                                # V3.0fix N1: 从 config PARAMS + STOCK_PARAMS 合并趋势层参数
                                _trend_params = {
                                    "rsi_oversold_5m": _sp_param(code, "rsi_oversold_5m", 32),
                                    "rsi_overbought_5m": _sp_param(code, "rsi_overbought_5m", 68),
                                    "rsi_reversal_min_delta": _sp_param(code, "rsi_reversal_min_delta", 2.0),
                                    "trend_bb_slope_flat": _sp_param(code, "trend_bb_slope_flat", 0.0005),
                                    "trend_bb_width_expand": _sp_param(code, "trend_bb_width_expand", 1.05),
                                    "trend_debounce_bars": _sp_param(code, "trend_debounce_bars", 2),
                                } if TrendRegime else {}
                                self.trend_regimes[code] = TrendRegime(params=_trend_params) if TrendRegime else None
                            tr = self.trend_regimes[code]
                            state, conf = tr.update(_df_5m)
                            _trend_feats = {
                                "trend_state": state.value,
                                "trend_confidence": conf,
                                "rsi5_buy_trigger": tr.rsi_buy_trigger,
                                "rsi5_sell_trigger": tr.rsi_sell_trigger,
                                "rsi_5m": tr._last_rsi,
                                "dif_5m": tr._last_dif,
                                "dea_5m": tr._last_dea,
                            }
                            self._5min_cache[code] = (_boundary, _df_5m, _trend_feats)
                # 应用趋势层输出到 feats
                if _trend_feats is None and code in self.trend_regimes:
                    # 缓存命中但有 trend_regime 实例：回退读最后已知状态
                    tr = self.trend_regimes[code]
                    _trend_feats = {
                        "trend_state": tr.state.value,
                        "trend_confidence": tr.confidence,
                        "rsi5_buy_trigger": tr.rsi_buy_trigger,
                        "rsi5_sell_trigger": tr.rsi_sell_trigger,
                        "rsi_5m": tr._last_rsi,
                        "dif_5m": tr._last_dif,
                        "dea_5m": tr._last_dea,
                    }
                if _trend_feats:
                    for k, v in _trend_feats.items():
                        feats[k] = v
            except Exception:
                pass  # 趋势层失败不阻断主流程
        buy_score, buy_details = ScoringEngine.calc_buy_score(feats, self.factor_weights)
        sell_score, sell_details = ScoringEngine.calc_sell_score(feats, self.factor_weights)
        # 静态基准阈值 — 分数已通过ATR+Sigmoid自适应，阈值不再跳变
        buy_threshold = 42.0; sell_threshold = 42.0
        price = feats.get("price", 0); hold_qty = feats.get("hold_qty", 0)
        # 风控阻断 + 左侧抄底豁免（5分钟强反转可绕过日线封锁）
        risk = RiskManager.check_all(feats)
        risk_buy_block = risk.get("buy_block", [])[:]  # 副本防止污染
        risk_sell_block = risk.get("sell_block", [])[:]
        t_val = feats.get("t_val", 0)
        # ===== V1.28: 早盘保护 — morning_no_sell_until =====
        _msu = PARAMS.get("morning_no_sell_until", 1000)
        _msr = PARAMS.get("morning_no_sell_min_ret", 0.03)
        if hold_qty > 0 and t_val < _msu and feats.get("today_ret", 0) < _msr:
            risk_sell_block.append("morning_no_sell")
        # ===== V1.28: VWAP偏离买入门槛 — 无底仓时禁止在非深V位置买入 =====
        _vbd = _sp_param(code, "vwap_buy_deviation", -0.020)  # V3.0fix N3
        _vwap = feats.get("vwap", 0)
        if price > 0 and _vwap > 0 and t_val >= 930 and hold_qty <= 0:
            _vdev = (price - _vwap) / _vwap
            if _vdev > _vbd:
                risk_buy_block.append("vwap_not_dip_enough")
        # ===== V1.28: 早盘单边下行预警 =====
        self._check_morning_alert(code, name, df, feats)
        _malert = self.morning_alert_state.get(code, {})
        if _malert.get("level") == 2:
            risk_buy_block.append("morning_alert_L2")
        elif _malert.get("level") == 1:
            # Level 1: 降低买入阈值 + 仅允许深V买入
            buy_threshold = buy_threshold + 8.0  # 提高买入门槛
            if price > 0 and _vwap > 0:
                _vdev_l1 = (price - _vwap) / _vwap
                if _vdev_l1 > -0.015:
                    risk_buy_block.append("morning_alert_L1_not_dip")
        # ===== V1.29: 卖出→接回闭环 — 主动寻找低吸买回机会 =====
        ab = self.awaiting_buyback.get(code)
        if ab and ab.get("sell_price", 0) > 0 and price > 0:
            # 检查 TTL 是否过期
            elapsed = (_engine_now() - ab["sell_time"]).total_seconds() / 60
            if elapsed > ab["ttl"]:
                self.awaiting_buyback.pop(code, None)  # 过期清理
            else:
                # 价格低于卖出价时，激进入买入
                discount = (ab["sell_price"] - price) / ab["sell_price"]
                if discount > 0.005:
                    boost = PARAMS.get("awaiting_buyback_score_boost", 10)
                    buy_score += boost
                    buy_details.append({"指标": "接回追踪(已卖待接)", "当前": f"卖{ab['sell_price']:.2f}现{price:.2f}折{discount:.1%}", "加分": round(boost, 1)})
                elif discount > 0.001:
                    boost = PARAMS.get("awaiting_buyback_score_boost_weak", 5)
                    buy_score += boost
                    buy_details.append({"指标": "接回追踪(微利)", "当前": f"折{discount:.1%}", "加分": round(boost, 1)})
                buy_threshold -= PARAMS.get("awaiting_buyback_threshold_relax", 5)

        # ===== V1.30: 卖出端保护 —— 底仓地板 + 卖出次数上限（防卖穿底仓）=====
        _net_qty = self._virtual_net_qty(code, holding)
        _base_qty = int(holding.get("base") or holding.get("t_qty") or holding.get("qty") or 0)
        _floor_qty = int(_base_qty * float(_sp_param(code, "sell_floor_ratio", 0.5)))
        if hold_qty > 0 and _floor_qty > 0 and _net_qty <= _floor_qty:
            risk_sell_block.append(f"sell_floor_protect(余{_net_qty}≤地板{_floor_qty})")
        _max_sells = int(_sp_param(code, "max_sell_times_per_stock", 3))
        if self.sell_count_per_stock.get(code, 0) >= _max_sells:
            risk_sell_block.append(f"max_sell_times({self.sell_count_per_stock.get(code, 0)}>={_max_sells})")

        can_bypass_daily = feats.get("f5_is_strong_bullish_reversal", False) or feats.get("f5_is_volume_reversal", False)
        is_daily_ok = feats.get("daily_buy_t_ok", False) or can_bypass_daily
        base_can_buy = (len(risk_buy_block) == 0 and is_daily_ok
                        and not self._in_cooldown(code, "BUY_LOW"))
        base_can_sell = (len(risk_sell_block) == 0 and hold_qty > 0
                         and not self._in_cooldown(code, "SELL_HIGH"))
        # ===== V3.0: 5分钟趋势方向门控 + T_MODE 适配 =====
        _trend_state = feats.get("trend_state", "NEUTRAL")
        # V3.0fix: 读取真正的 t_mode（daily_ctx["t_mode"] > 全局 T_MODE > "long"）
        _t_mode_from_ctx = daily_ctx.get("t_mode", "") if isinstance(daily_ctx, dict) else ""
        _t_mode = feats.get("t_mode", _t_mode_from_ctx or T_MODE.get(code, "long") if 'T_MODE' in globals() else "long")
        if TrendRegime is not None and code in self.trend_regimes:
            tr = self.trend_regimes[code]
            # 方向门控：抑制逆势信号
            _buy_mult = tr.buy_gate_multiplier()
            _sell_mult = tr.sell_gate_multiplier()
            if _buy_mult < 1.0:
                buy_score *= _buy_mult
                buy_details.append({"指标": f"趋势门控({_trend_state})", "当前": f"买入×{_buy_mult}", "加分": 0})
                buy_threshold += tr.buy_threshold_penalty()
            if _sell_mult < 1.0:
                sell_score *= _sell_mult
                sell_details.append({"指标": f"趋势门控({_trend_state})", "当前": f"卖出×{_sell_mult}", "加分": 0})
                sell_threshold += tr.sell_threshold_penalty()
            # T_MODE 方向适配
            _t_mode_str = str(_t_mode or "long")
            if _t_mode_str in ("short", "long"):
                buy_score, sell_score = tr.apply_t_mode(_t_mode_str, buy_score, sell_score)
        # ===== V1.28: 止盈监控 (take_profit_pct) =====
        _tp = _sp_param(code, "take_profit_pct", 0.010)   # V3.0fix N2
        _tpa = _sp_param(code, "take_profit_time_after", 1000)  # V3.0fix N2
        if hold_qty > 0 and t_val >= _tpa and feats.get("profit_pct", 0) >= _tp:
            sell_score += 30.0  # 大幅boost确保触发止盈
        # ===== V1.29: 买入→高抛闭环 — 主动寻找止盈卖点 =====
        ps = self.pending_sells.get(code)
        if ps and ps.get("buy_price", 0) > 0 and price > 0 and hold_qty > 0:
            profit = (price - ps["buy_price"]) / ps["buy_price"]
            if profit >= _tp:
                boost = 25.0  # 止盈：强推卖出
                sell_score += boost
                sell_details.append({"指标": "高抛追踪(止盈)", "当前": f"买{ps['buy_price']:.2f}现{price:.2f}盈{profit:.1%}", "加分": round(boost, 1)})
                self.pending_sells.pop(code, None)  # 闭环完成
            elif profit <= -0.03:  # V1.29: 止损 — 跌超3%强制卖出
                boost = 20.0
                sell_score += boost
                sell_details.append({"指标": "止损追踪(已买待割)", "当前": f"买{ps['buy_price']:.2f}现{price:.2f}亏{profit:.1%}", "加分": round(boost, 1)})
                self.pending_sells.pop(code, None)  # 止损完成，清除追踪
            elif profit <= -0.015:  # 轻度亏损预警
                boost = 8.0
                sell_score += boost
                sell_details.append({"指标": "止损预警(浅亏)", "当前": f"买{ps['buy_price']:.2f}现{price:.2f}亏{profit:.1%}", "加分": round(boost, 1)})
        sig = None
        can_sell = base_can_sell and sell_score >= sell_threshold and sell_score > buy_score
        can_buy = base_can_buy and buy_score >= buy_threshold and buy_score > sell_score
        # ===== V1.30: 决策原因码 —— HOLD 细分可区分，消除"买分超阈值却 HOLD"的假矛盾 =====
        if can_sell and sell_score > buy_score:
            sig = Signal(code, name, "SELL_HIGH", price, sell_score, [d["指标"] for d in sell_details], sell_details, {}, {})
            decision_reason = "SELL_HIGH"
        elif can_buy:
            sig = Signal(code, name, "BUY_LOW", price, buy_score, [d["指标"] for d in buy_details], buy_details, {}, {})
            decision_reason = "BUY_LOW"
        else:
            _buy_worthy = buy_score >= buy_threshold and buy_score > sell_score
            _sell_worthy = sell_score >= sell_threshold and sell_score > buy_score
            if _buy_worthy and risk_buy_block:
                decision_reason = "HOLD_BUY_BLOCKED:" + "|".join(risk_buy_block)
            elif _buy_worthy and not is_daily_ok:
                decision_reason = "HOLD_BUY_BLOCKED:daily_gate"
            elif _buy_worthy:
                decision_reason = "HOLD_BUY_COOLDOWN"
            elif _sell_worthy and risk_sell_block:
                decision_reason = "HOLD_SELL_BLOCKED:" + "|".join(risk_sell_block)
            elif _sell_worthy:
                decision_reason = "HOLD_SELL_COOLDOWN"
            elif buy_score >= buy_threshold and sell_score > buy_score:
                decision_reason = "HOLD_SELL_PRIORITY"   # 买达阈但卖分更高，被卖出优先仲裁压制
            else:
                decision_reason = "HOLD_BELOW_THRESHOLD"
        self.last_decision[code] = {
            "reason": decision_reason, "ts": _now(),
            "buy_block": list(risk_buy_block), "sell_block": list(risk_sell_block),
        }
        _append_jsonl(_trace_path("decision_trace"), {
            "scan_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": name,
            "price": price, "vwap": feats.get("vwap", 0), "rsi": feats.get("rsi", 50),
            "buy_score": buy_score, "sell_score": sell_score,
            "buy_threshold": buy_threshold, "sell_threshold": sell_threshold,
            "decision": sig.action if sig else "HOLD",
            "decision_reason": decision_reason,
            "buy_block": list(risk_buy_block), "sell_block": list(risk_sell_block),
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
        feats["intraday_alerts"] = dc.get("intraday_alerts", [])
        for k in ["index_regime_status", "index_circuit_state", "index_gate_advice", "index_temp_bucket"]:
            feats[k] = dc.get(k, "normal")
        # 15min/5min features (ATR自适应)
        _f15_f = FeatureExtractor.extract_15min_features(df, cached_15m_df, price, vwap, atr=atr)
        for k, v in _f15_f.items():
            feats[f"f15_{k}"] = v
        # V3.0fix N4: 从 STOCK_PARAMS 提取个股反包参数传入
        _bullish_params = {}
        if 'STOCK_PARAMS' in globals():
            _sp = STOCK_PARAMS.get(code, {})
            for _k in ["bullish_reversal_min_pct", "bullish_reversal_body_ratio",
                        "bullish_reversal_vol_multiplier", "bullish_reversal_engulf"]:
                if _k in _sp:
                    _bullish_params[_k] = _sp[_k]
        _f5_f = FeatureExtractor.extract_5min_features(df, cached_5m_df, price, vwap,
                                                         bullish_params=_bullish_params if _bullish_params else None,
                                                         atr=atr)
        for k, v in _f5_f.items():
            feats[f"f5_{k}"] = v
        # V1.19 oscillation features removed in V3.0 (6 dead features, replaced by 5-min MACD trend)
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
        """5分钟线特征（含缩量止跌+大阳线反包检测 + V3.0 MACD/BOLL/RSI）。
        传入 _df_5min 可避免重复构建。"""
        _pd = pd
        _np = np
        p = bullish_params or {}
        atr_r = max(atr, 0.002)
        feats = {
            # 旧字段（兼容 V1.17/V1.22 — 读取 fast MACD(6,13,5) 保持历史行为）
            "vol_ratio_5m": 1.0, "mom2_5m": 0.0, "macd_hist_5m": 0.0,
            "prev_macd_hist_5m": 0.0, "is_low_rising_5m": False, "is_stop_falling_5m": False,
            "is_volume_reversal": False, "is_strong_bullish_reversal": False,
            "vr_bearish_count": 0, "vr_high_declining": False,
            # V3.0 新字段：标准 MACD(12,26,9) + BOLL(20,2) + RSI(14)
            "dif_5m": 0.0, "dea_5m": 0.0,          # MACD 趋势方向
            "bb_mid_5m": 0.0, "bb_width_5m": 0.0, "bb_pct_5m": 0.5,  # BOLL
            "rsi_5m": 50.0,                           # 5分钟 RSI
            "trend_state": "NEUTRAL", "trend_confidence": 0.0,
            "rsi5_buy_trigger": False, "rsi5_sell_trigger": False,
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
            # V1.17兼容：旧MACD(6,13,5)柱状体 → macd_hist_5m_fast
            feats["macd_hist_5m"] = float(last_5m["macd_hist_5m_fast"]) if _pd.notna(last_5m.get("macd_hist_5m_fast")) else 0.0
            feats["prev_macd_hist_5m"] = float(prev_5m["macd_hist_5m_fast"]) if _pd.notna(prev_5m.get("macd_hist_5m_fast")) else 0.0
            feats["is_low_rising_5m"] = bool(last_5m.get("low_rising_5m", False))
            feats["is_stop_falling_5m"] = bool(last_5m.get("stop_falling_5m", False))
            # V3.0: 提取标准 5分钟 MACD/BOLL/RSI
            feats["dif_5m"] = float(last_5m["dif_5m"]) if _pd.notna(last_5m.get("dif_5m")) else 0.0
            feats["dea_5m"] = float(last_5m["dea_5m"]) if _pd.notna(last_5m.get("dea_5m")) else 0.0
            feats["bb_mid_5m"] = float(last_5m["bb_mid_5m"]) if _pd.notna(last_5m.get("bb_mid_5m")) else 0.0
            feats["bb_width_5m"] = float(last_5m["bb_width_5m"]) if _pd.notna(last_5m.get("bb_width_5m")) else 0.0
            feats["bb_pct_5m"] = float(last_5m["bb_pct_5m"]) if _pd.notna(last_5m.get("bb_pct_5m")) else 0.5
            feats["rsi_5m"] = float(last_5m["rsi_5m"]) if _pd.notna(last_5m.get("rsi_5m")) else 50.0
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

# extract_v19_oscillation removed in V3.0 — 6 dead features replaced by 5-min MACD trend_regime


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

        # 7. 大盘单边下行 → 绝对禁止任何买入（接飞刀熔断）
        index_regime = feats.get("index_regime", "range")
        if index_regime == "uni_down":
            result["buy_block"].append("index_uni_down_clearance")

        # 8. 盘中分时崩盘预警 → 紧急冻结买入
        for alert in (feats.get("intraday_alerts") or []):
            if alert.get("tag") in ("I1", "I4"):
                result["buy_block"].append(f"intraday_panic_{alert.get('tag')}")

        return result


FACTOR_WEIGHTS = {
    # —— V3.0: 权重重平衡（新增 5m_trend + 5m_rsi）——
    "factor_weight_vwap": 0.15,          # 曾 0.20
    "factor_weight_rsi": 0.04,           # 曾 0.12（1分钟RSI降权，让位给5分钟RSI）
    "factor_weight_macd": 0.08,
    "factor_weight_volume": 0.08,
    "factor_weight_position": 0.08,
    "factor_weight_ema": 0.04,
    "factor_weight_pattern": 0.13,       # 曾 0.20
    "factor_weight_index_regime": 0.15,  # 曾 0.20
    "factor_weight_5m_trend": 0.15,      # V3.0 新增：5分钟MACD趋势方向
    "factor_weight_5m_rsi": 0.10,        # V3.0 新增：5分钟RSI择时触发
    # —— 配置常量（非权重，保留兼容）——
    "max_score_raw": 100,
}


def _sp_param(code: str, key: str, default=None):
    """V1.30: 个股专属参数 > 全局 PARAMS > default（与 main.py 推送阈值双层管理同构）"""
    try:
        from config import STOCK_PARAMS
        v = STOCK_PARAMS.get(code, {}).get(key)
        if v is not None:
            return v
    except Exception:
        pass
    try:
        v = PARAMS.get(key)
        if v is not None:
            return v
    except Exception:
        pass
    return default


# ===== V1.30: 回测时间注入 =====
# 实盘用真实时钟；回测/回放把 SIM_NOW 设为当前 K 线时间，
# 使冷却/TTL 等时间逻辑在模拟时间轴上正确流逝（否则第一笔交易的真实时钟冷却会封死整个回测期）。
SIM_NOW = None
PERSIST_INTRADAY_STATE = True   # 回测/回放置 False，避免污染实盘盘中状态文件

def _engine_now():
    return SIM_NOW if SIM_NOW is not None else _now()


def write_shadow_signal(code: str, name: str, price: float, vwap: float,
                        buy_score: float, sell_score: float,
                        buy_threshold: float, sell_threshold: float,
                        miss_reason: str, extra: dict = None):
    """V1.30: 恢复 shadow_signals —— 记录"引擎已产生信号但低于推送阈值被静默"的信号。
    格式与 daily_review.py 读取方兼容（scan_time/code/name/*_score/*_threshold/
    distance_to_*_threshold/best_signal_type/best_signal_score/miss_reason）。"""
    try:
        rec = {
            "scan_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": name,
            "buy_score": round(float(buy_score), 1),
            "sell_score": round(float(sell_score), 1),
            "buy_threshold": buy_threshold, "sell_threshold": sell_threshold,
            "current_price": price, "vwap": vwap,
            "distance_to_buy_threshold": round(float(buy_threshold) - float(buy_score), 1),
            "distance_to_sell_threshold": round(float(sell_threshold) - float(sell_score), 1),
            "best_signal_type": "buy" if buy_score >= sell_score else "sell",
            "best_signal_score": round(max(float(buy_score), float(sell_score)), 1),
            "miss_reason": miss_reason,
        }
        if extra:
            rec.update(extra)
        _append_jsonl(_trace_path("shadow_signals"), rec)
    except Exception:
        pass


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
        z = -slope * (x - center)
        if z > 100: return 0.0  # np.exp(>100) → inf
        if z < -100: return 1.0
        return 1.0 / (1.0 + np.exp(z))

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
        return raw, [{"指标": "长下影", "当前": f"{ls:.2f}", "强度": round(raw, 3)}] if raw > 0.05 else []

    @staticmethod
    def score_ema_improve(feats: dict) -> tuple:
        es = feats.get("ema_spread", 0); pes = feats.get("prev_ema_spread", 0)
        delta = es - pes
        raw = ScoringEngine._sigmoid(delta, center=0.0005, slope=500.0)
        return raw, [{"指标": "EMA转强", "当前": f"{es*100:.4f}%", "强度": round(raw, 3)}] if raw > 0.05 else []

    @staticmethod
    def score_ema_weaken(feats: dict) -> tuple:
        es = feats.get("ema_spread", 0); pes = feats.get("prev_ema_spread", 0)
        delta = pes - es
        raw = ScoringEngine._sigmoid(delta, center=0.0005, slope=500.0)
        return raw, [{"指标": "EMA转弱", "当前": f"{es*100:.4f}%", "强度": round(raw, 3)}] if raw > 0.05 else []

    @staticmethod
    def score_volume(feats: dict) -> tuple:
        vr = feats.get("vol_ratio", 1.0)
        raw = ScoringEngine._sigmoid(vr, center=1.2, slope=4.0)
        return raw, [{"指标": "量能确认", "当前": f"{vr:.2f}", "强度": round(raw, 3)}] if raw > 0.05 else []

    @staticmethod
    def score_upper_shadow(feats: dict) -> tuple:
        us = feats.get("upper_shadow", 0)
        raw = ScoringEngine._sigmoid(us, center=0.4, slope=6.0)
        return raw, [{"指标": "长上影", "当前": f"{us:.2f}", "强度": round(raw, 3)}] if raw > 0.05 else []

    # ── V3.0: 5分钟趋势层评分 ──

    @staticmethod
    def score_5m_trend_buy(feats: dict) -> tuple:
        """5分钟 MACD 趋势方向 — 买入端：多头区顺势买入加分"""
        trend = feats.get("trend_state", "NEUTRAL")
        conf = feats.get("trend_confidence", 0.0)
        dif = feats.get("dif_5m", 0.0)
        dea = feats.get("dea_5m", 0.0)
        # BULL/STRONG_BULL → 顺势买入加分；NEUTRAL → 中性；BEAR → 扣分
        if trend == "STRONG_BULL":
            raw = 0.9 + 0.1 * conf
            detail = f"DIF{dif:.4f}/DEA{dea:.4f} 强势多头"
        elif trend == "BULL":
            raw = 0.6 + 0.2 * conf
            detail = f"DIF{dif:.4f}/DEA{dea:.4f} 多头"
        elif trend == "NEUTRAL":
            raw = 0.5
            detail = "趋势中性"
        elif trend == "BEAR":
            raw = 0.2
            detail = "逆势(空头区买入)"
        else:  # STRONG_BEAR
            raw = 0.05
            detail = "强逆势(强空头区买入)"
        return raw, [{"指标": "5分趋势(买)", "当前": detail, "强度": round(raw, 3)}]

    @staticmethod
    def score_5m_trend_sell(feats: dict) -> tuple:
        """5分钟 MACD 趋势方向 — 卖出端：空头区顺势卖出加分"""
        trend = feats.get("trend_state", "NEUTRAL")
        conf = feats.get("trend_confidence", 0.0)
        dif = feats.get("dif_5m", 0.0)
        dea = feats.get("dea_5m", 0.0)
        if trend == "STRONG_BEAR":
            raw = 0.9 + 0.1 * conf
            detail = f"DIF{dif:.4f}/DEA{dea:.4f} 强势空头"
        elif trend == "BEAR":
            raw = 0.6 + 0.2 * conf
            detail = f"DIF{dif:.4f}/DEA{dea:.4f} 空头"
        elif trend == "NEUTRAL":
            raw = 0.5
            detail = "趋势中性"
        elif trend == "BULL":
            raw = 0.2
            detail = "逆势(多头区卖出)"
        else:  # STRONG_BULL
            raw = 0.05
            detail = "强逆势(强多头区卖出)"
        return raw, [{"指标": "5分趋势(卖)", "当前": detail, "强度": round(raw, 3)}]

    @staticmethod
    def score_5m_rsi_buy(feats: dict) -> tuple:
        """5分钟 RSI 择时 — 买入端：超卖回升触发"""
        rsi5 = feats.get("rsi_5m", 50.0)
        triggered = feats.get("rsi5_buy_trigger", False)
        if triggered:
            # 超卖回升 = 高置信度买入信号
            raw = ScoringEngine._sigmoid(35 - rsi5, center=5, slope=0.3)
            return raw, [{"指标": "5分RSI超卖回升", "当前": f"{rsi5:.1f}", "强度": round(raw, 3)}]
        elif rsi5 < 40:
            # 接近超卖但未触发
            raw = ScoringEngine._sigmoid(40 - rsi5, center=8, slope=0.3)
            return raw, [{"指标": "5分RSI偏低", "当前": f"{rsi5:.1f}", "强度": round(raw, 3)}] if raw > 0.05 else []
        return 0.0, []

    @staticmethod
    def score_5m_rsi_sell(feats: dict) -> tuple:
        """5分钟 RSI 择时 — 卖出端：超买回落触发"""
        rsi5 = feats.get("rsi_5m", 50.0)
        triggered = feats.get("rsi5_sell_trigger", False)
        if triggered:
            raw = ScoringEngine._sigmoid(rsi5 - 65, center=5, slope=0.3)
            return raw, [{"指标": "5分RSI超买回落", "当前": f"{rsi5:.1f}", "强度": round(raw, 3)}]
        elif rsi5 > 60:
            raw = ScoringEngine._sigmoid(rsi5 - 60, center=8, slope=0.3)
            return raw, [{"指标": "5分RSI偏高", "当前": f"{rsi5:.1f}", "强度": round(raw, 3)}] if raw > 0.05 else []
        return 0.0, []

    @staticmethod
    def _weighted_factor_score(raw: float, weight_key: str, w_mult: float = 1.0,
                                 p: dict = None) -> float:
        """raw(0~1) × 100 × 权重。p 来自实例的 factor_weights，默认 FACTOR_WEIGHTS。"""
        w = (p or FACTOR_WEIGHTS).get(weight_key, 0.10)
        return raw * 100 * w * w_mult

    @staticmethod
    def score_index_regime(feats: dict, side: str = "buy") -> float:
        """大盘态势因子：输出 0~1 标准化信号强度
        uni_down: sell=1.0(清仓), buy=0.0(停止买入)
        uni_up:   sell=0.2(防卖飞), buy=1.0(顺势)
        range:    均为 0.5(标准作战)"""
        regime = feats.get("index_regime", "range")
        if regime == "uni_down":
            return 1.0 if side == "sell" else 0.0
        if regime == "uni_up":
            return 0.2 if side == "sell" else 1.0
        return 0.5  # range / 其他

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
        # ---- V3.0: 5分钟趋势层因子 (买入端) ----
        raw, d = ScoringEngine.score_5m_trend_buy(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_5m_trend", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_5m_rsi_buy(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_5m_rsi", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        # ---- 大盘态势因子 ----
        raw = ScoringEngine.score_index_regime(feats, "buy")
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_index_regime", p=p)
        score += s
        if raw != 0.5:
            regime = feats.get("index_regime", "range")
            details.append({"指标": f"大盘态势({regime})", "强度": round(raw, 2), "加分": round(s, 1)})
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
        # ---- V3.0: 5分钟趋势层因子 (卖出端) ----
        raw, d = ScoringEngine.score_5m_trend_sell(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_5m_trend", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        raw, d = ScoringEngine.score_5m_rsi_sell(feats)
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_5m_rsi", p=p)
        score += s; d and details.append(d[0] | {"加分": round(s, 1)})
        # ---- 大盘态势因子 (卖出端) ----
        raw = ScoringEngine.score_index_regime(feats, "sell")
        s = ScoringEngine._weighted_factor_score(raw, "factor_weight_index_regime", p=p)
        score += s
        if raw != 0.5:
            regime = feats.get("index_regime", "range")
            details.append({"指标": f"大盘态势({regime})", "强度": round(raw, 2), "加分": round(s, 1)})
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


# _should_push removed in V3.0 (dead function — push throttling handled in main.py notify())
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
