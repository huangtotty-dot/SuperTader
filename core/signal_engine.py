# V1.11: 日志增强模块导入
# V1.1.2 (2026-08-04, 修复类非调优): RSI NaN 兜底 C 语义上线 — indicators.py/data_fetcher.py
#   共 5 处 RSI 计算点修复 0/0 钉平窗除零 NaN（填 50 中性；纯上涨窗保持 NaN 与现网一致；
#   预热 leading NaN 不变）。本文件零代码变更，仅登记版本；参数/阈值/权重零调整。
#   回归证据: t_io/validation/rsi_nan_guard/regression_report_2026-08-03.md
# V1.1.3 (2026-08-06, 修复类非调优): 收盘同步 t_qty 只减不增不变量 — holdings_sync.py/main.py
#   旧 eod_sync `t_qty=qty` 无条件释放冻结，今日 14:50:25 复活 002639/603667 纯底仓(t_qty=0)，
#   致 14:50:45 002639 误推 SELL_HIGH + 幻影卖出持久化。修复后 t_qty 增加只能来自晨间截图 reconcile。
#   本文件零代码变更，仅登记版本；参数/阈值/权重零调整。
#   回归证据: t_io/validation/test_holdings_sync_invariant.py（当日数据重放全绿）
# V1.2.0 (2026-08-08, 用户拍板上线): C1' 口径B 采纳 — W33 全管线决赛六闸+2附加全过
#   （t_io/validation/w32_c1p/C1P_FINAL.md；GATE_BREACH 的 C1 经"全部买信号单股日限7内置状态机"修复后采纳）
#   ① config.py PARAMS["buyback_bypass_gates"]=True 生产默认开（接回激活 tick 绕过
#      daily_overheated/index_uni_down_clearance，本文件 :615 软消费）
#   ② config.py PARAMS["buy_daily_cap"]=7（record_signal 层计数，buy_daily_cap_reached 谓词；
#      生产 main.py scan_once 与 harness 记录层双挂载点拦截）
#   回归证据: t_io/validation/w32_c1p/（冒烟/复用/决赛产物）+ t_io/validation/test_v120_production_cap.py
# 2026-08-13 纯两点改造 + 僵尸清理（V2 swing2pt）:
#   ① 引擎降级为纯两点规则（bb_pct_5m 触轨 + rsi_5m_p6）；删除 ScoringEngine/FACTOR_WEIGHTS/RiskManager
#      评分链；main.py 移除单股日限/轮次上限拦截（两点恒推送，仓控0股仅供参考不记账）。
#   ② 删除闭环追踪（awaiting_buyback/pending_sells/_recover_tracking_from_trades）、30min 冷却
#      （buy_cooldown/sell_cooldown/_engine_now）、接回解耦开关 buyback_bypass_gates（config/harness 同步清）。
#   ③ 本文件死 fallback 桩与 utils/data_fetcher/market_regime/trend_regime/position_* 等死函数一并清除。
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
    from analysis.trend_regime import TrendRegime, TrendState
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
if '_default_daily_context' not in globals():
    def _default_daily_context(c,s="",r=""): return {"daily_status":s,"daily_reason":r,"daily_buy_t_ok":False}
if '_append_jsonl' not in globals():
    def _append_jsonl(*a,**kw): return None
if '_trace_path' not in globals():
    def _trace_path(n,d=None): return f"/tmp/{n}"
if 'send_morning_alert' not in globals():
    def send_morning_alert(*a,**kw): return None
if 'notify_alert_cleared' not in globals():
    def notify_alert_cleared(*a,**kw): return None
if 'resample_to_15min' not in globals():
    from analysis.indicators import resample_to_15min, add_15min_indicators
if 'resample_to_5min' not in globals():
    from analysis.indicators import resample_to_5min, add_5min_indicators
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
        self.buy_count_per_stock: Dict[str, int] = {}
        self.sell_count_per_stock: Dict[str, int] = {}
        # C1' 口径B（W33 验证开关软消费，默认关）：record_signal 层当日已记录买信号计数
        # （仅 PARAMS["buy_daily_cap"] 开启时递增；计数口径与 signals.jsonl 逐条对应）
        self.buy_recorded_today: Dict[str, int] = {}
        self.state_reset_date = get_today_str()
        self.t_cycle_start_time: Dict[str, datetime] = {}
        self.last_signal_state: Dict[str, Dict[str, Any]] = {}
        self.last_trade_state: Dict[str, Dict[str, Any]] = {}
        self.cycle_count: Dict[str, int] = {}
        self.cycle_direction: Dict[str, str] = {}
        self.post_sell_block_until: Dict[str, datetime] = {}
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
        # V3.1: Renko 增量砖状态（swing_use_renko 启用）— code → {date, builder, last_ts}
        self._renko_states: Dict[str, Dict[str, Any]] = {}
        # V3.1: 当日做T买入价 — code → {date, price, ts}（目标止盈需要买入价上下文）
        self.t_entry_price: Dict[str, Dict[str, Any]] = {}
        # V1.30: 恢复轮次/次数/冷却等盘中状态（重启后不清零）
        self._load_intraday_state()

    def _reset_daily_state_if_needed(self):
        today = get_today_str()
        if self.state_reset_date != today:
            self.buy_count_per_stock = {}
            self.sell_count_per_stock = {}
            self.buy_recorded_today = {}   # C1' 口径B：日限计数随日界重置
            self.t_cycle_start_time = {}
            self.last_signal_state = {}
            self.last_trade_state = {}
            self.cycle_count = {}
            self.cycle_direction = {}
            self.post_sell_block_until = {}
            self.daily_realized_loss_monitor = 0.0
            self.morning_alert_state = {}
            self._5min_cache = {}       # V3.0: 5分钟缓存每日重置
            self.trend_regimes = {}     # V3.0: 趋势状态机每日重置（开盘从头累积）
            self._renko_states = {}     # V3.1: Renko 砖状态每日重置（开盘从头累积）
            self.t_entry_price = {}     # V3.1: 当日做T买入价每日清空
            self.state_reset_date = today

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
        self._last_sig_price = price  # 供 record_trade_action 记录成交价
        # C1' 口径B（W33 软消费，默认关=零行为变化）：仅 cap 开启时计数已记录买信号
        if "SELL" not in action and PARAMS.get("buy_daily_cap"):
            self.buy_recorded_today[code] = self.buy_recorded_today.get(code, 0) + 1
        self._persist_intraday_state()  # V1.30

    def buy_daily_cap_reached(self, code: str) -> bool:
        """C1' 口径B（W33 验证开关软消费，默认关=生产行为不变）：全部买信号单股日限判定。
        计数口径 = record_signal 层已记录买信号数（与 signals.jsonl 逐条对应；
        不分 ctl 原有/接回/二阶增量；卖信号不受限）。第 cap+1 条起返回 True。
        依据: t_io/validation/w32_c1p/C1P_PREREG.md（用户 2026-08-08 拍板口径 B）"""
        cap = PARAMS.get("buy_daily_cap")
        if not cap:
            return False
        self._reset_daily_state_if_needed()
        return self.buy_recorded_today.get(code, 0) >= int(cap)

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
        elif action in ["SELL_HIGH", "PANIC_SELL"]:
            self.sell_count_per_stock[code] = self.sell_count_per_stock.get(code, 0) + 1
            self.cycle_direction[code] = "sell"
            self.post_sell_block_until[code] = _now() + timedelta(minutes=PARAMS["post_sell_rebuild_minutes"])
            if qty > 0:
                bucket = VIRTUAL_TRADES.setdefault(code, {})
                _px = float(getattr(self, '_last_sig_price', 0) or 0)
                bucket.setdefault("SELL_HIGH", []).append({"qty": qty, "ts": _now(), "action": action, "price": _px})
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
        price = feats.get("price", 0)
        hold_qty = feats.get("hold_qty", 0)
        buy_score = 0.0
        sell_score = 0.0
        sig = None
        decision_reason = "HOLD_NO_SWING"
        # ===== 高抛低吸纯两点 (2026-08-13 用户拍板) =====
        # 高抛: 5分收盘≥上轨(bb_pct_5m>=1.0) 且 5分RSI(6)>75
        # 低吸: 5分收盘≤下轨(bb_pct_5m<=0.0) 且 5分RSI(6)<35
        # 只参考这两点，其余条件(风控/闭环/冷却/限频)全部移除；决策新鲜重采样5分K
        self._check_morning_alert(code, name, df, feats)  # 保留飞书早盘预警(纯通知，不再阻断)
        try:
            if PARAMS.get("swing_use_renko"):
                # ===== V3.1 (2026-08-26): Renko 向下砖买入 + 目标止盈 =====
                # 依据: 39支×1年复验 Renko买入择时 60.6%(39/39支>50%) / target+0.5%止盈 78.5%胜率
                sig, buy_score, sell_score, decision_reason = self._swing_renko_eval(
                    code, name, df, feats, price)
            else:
                _df5 = resample_to_5min(df) if 'resample_to_5min' in globals() else pd.DataFrame()
                if not _df5.empty:
                    _df5 = add_5min_indicators(_df5) if 'add_5min_indicators' in globals() else _df5
                if not _df5.empty and len(_df5) >= int(PARAMS.get("swing_min_5m_bars", 13)):
                    _l5 = _df5.iloc[-1]
                    _bb = _l5.get("bb_pct_5m")
                    _rsi6 = _l5.get("rsi_5m_p6")
                    if _bb is not None and _rsi6 is not None and not (pd.isna(_bb) or pd.isna(_rsi6)):
                        _bbv = float(_bb)
                        _rv = float(_rsi6)
                        # 2026-08-15 实施: 确认点升级 — 用 15 分钟 MACD 方向替代 5 分 RSI
                        # （实证 macd15_bb5: 样本内 58.8%/过滤后 73.9%，样本外 75.0%）
                        _use_macd15 = bool(PARAMS.get("swing_macd15_dir", True))
                        if _use_macd15:
                            _bb_up = float(PARAMS.get("swing_macd15_bb_upper", 0.85))
                            _bb_dn = float(PARAMS.get("swing_macd15_bb_lower", 0.15))
                            _m15 = float(feats.get("f15_macd_hist_15m") or 0.0)
                            _sell_ok = _m15 < 0   # 15分MACD死叉(dif<dea) → 高抛
                            _buy_ok = _m15 > 0    # 15分MACD金叉(dif>dea) → 低吸
                            _ck = f"15分MACD{'死叉' if _m15 < 0 else ('金叉' if _m15 > 0 else '0')}({_m15:.2f})"
                            _kind = "swing_bb_macd15"
                        else:
                            _bb_up = float(PARAMS.get("swing_bb_upper", 1.0))
                            _bb_dn = float(PARAMS.get("swing_bb_lower", 0.0))
                            _sell_ok = _rv > float(PARAMS.get("swing_sell_rsi", 75.0))
                            _buy_ok = _rv < float(PARAMS.get("swing_buy_rsi", 35.0))
                            _ck = f"RSI6={_rv:.1f}"
                            _kind = "swing_bb_rsi"
                        _ind = {
                            "vwap": feats.get("vwap", price),
                            "today_ret": feats.get("today_ret", 0),
                            "market_state": daily_ctx.get("daily_status", "unknown"),
                            "entry_kind": _kind,
                            "macd_hist_15m": feats.get("f15_macd_hist_15m", 0.0),
                        }
                        _fac = {"threshold": 35.0, "entry_kind": _kind}
                        # 2026-08-15 因子实验实施: 高抛放量确认（样本内+6.0pp/样本外+6.4pp 稳健）
                        _sell_vol_ratio = float(PARAMS.get("swing_sell_vol_ratio", 0) or 0)
                        _sell_vol_ok, _sell_vol_txt = True, ""
                        if _sell_vol_ratio > 0:
                            _vol_5m = float(_l5.get("volume") or 0)
                            _vol_avg = float(_df5["volume"].mean()) if len(_df5) > 0 else 0.0
                            _vol_r = _vol_5m / _vol_avg if _vol_avg > 0 else 0.0
                            _sell_vol_ok = _vol_r >= _sell_vol_ratio
                            _sell_vol_txt = f" 量比{_vol_r:.1f}≥{_sell_vol_ratio}"
                        if _bbv >= _bb_up and _sell_ok and _sell_vol_ok:
                            sell_score = 100.0
                            _det = f"布林上轨(bb_pct={_bbv:.2f}) + {_ck}{_sell_vol_txt}"
                            sig = Signal(code, name, "SELL_HIGH", price, sell_score,
                                         [_det], [{"指标": "高抛", "当前": _det, "加分": 100.0}],
                                         _ind, dict(_fac))
                            decision_reason = "SELL_HIGH"
                        elif _bbv <= _bb_dn and _buy_ok:
                            buy_score = 100.0
                            _det = f"布林下轨(bb_pct={_bbv:.2f}) + {_ck}"
                            sig = Signal(code, name, "BUY_LOW", price, buy_score,
                                         [_det], [{"指标": "低吸", "当前": _det, "加分": 100.0}],
                                         _ind, dict(_fac))
                            decision_reason = "BUY_LOW"
        except Exception:
            sig = None
            buy_score = 0.0
            sell_score = 0.0
            decision_reason = "HOLD_NO_SWING"

        _buy_factors = {d["指标"]: d.get("加分", 0) for d in (sig.details if sig and sig.action == "BUY_LOW" else [])}
        _sell_factors = {d["指标"]: d.get("加分", 0) for d in (sig.details if sig and sig.action == "SELL_HIGH" else [])}

        # 2026-08-24 方案A: 分标的做T门控优化
        # 纯两点信号已经生成（buy_score/sell_score/sig），现在检查是否需要应用分标的门控调整
        # 注意：这里 sig 已经代表纯两点的决策，门控（daily_overheated等）仅在推送/GUI层应用
        # 为了真正放宽门控，需要在推送前通知 main.py 这是一个"绕过门控"的信号
        if sig and sig.action == "BUY_LOW":
            _stock_param = {}
            try:
                from config import STOCK_PARAMS
                _stock_param = STOCK_PARAMS.get(code, {})
            except:
                pass

            # 标记该信号是否应该绕过某些门控，供 main.py/notify 层消费
            if not hasattr(sig, 'override_gates'):
                sig.override_gates = {}

            if _stock_param.get("allow_overheated_buy", False):
                sig.override_gates['daily_overheated'] = True
            if _stock_param.get("allow_breakdown_buy", False):
                sig.override_gates['daily_breakdown_risk'] = True

        self.last_decision[code] = {
            "reason": decision_reason, "ts": _now(),
            "buy_block": [], "sell_block": [],
        }
        _append_jsonl(_trace_path("decision_trace"), {
            "scan_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": name,
            "price": price, "vwap": feats.get("vwap", 0), "rsi": feats.get("rsi", 50),
            "buy_score": buy_score, "sell_score": sell_score,
            "buy_threshold": 100.0, "sell_threshold": 100.0,
            "decision": sig.action if sig else "HOLD",
            "decision_reason": decision_reason,
            "buy_block": [], "sell_block": [],
            "buy_factors": _buy_factors,
            "sell_factors": _sell_factors,
            "engine": "v2_swing2pt",
        })
        return buy_score, sell_score, sig

    # ===== V3.1 (2026-08-26): Renko 买入 + 目标止盈 做T =====
    def _swing_renko_eval(self, code, name, df, feats, price):
        """Renko 向下砖+15分MACD金叉 买入 / +target% 目标止盈 卖出（做T当日闭环）。

        依据: 39支×1年1min复验 — Renko买入择时 +30min 60.6%(39/39支>50%);
              target+0.5%止盈 完整做T闭环胜率 78.5% (vs 等MACD死叉 55.7%)。
        状态: self._renko_states[code] 增量砖; self.t_entry_price[code] 当日买入价。
        """
        try:
            from analysis.renko_builder import RenkoBuilder
        except Exception:
            return None, 0.0, 0.0, "HOLD_NO_SWING"
        today = get_today_str()
        t_val = int(feats.get("t_val", 0))
        # 1) Renko 增量状态机（实盘/回放共用，避免每次全量重建）
        rs = self._renko_states.get(code)
        if rs is None or rs.get("date") != today:
            rs = {"date": today,
                  "builder": RenkoBuilder(brick_size_pct=float(PARAMS.get("swing_renko_brick_pct", 0.003))),
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
                created = builder.update(row.time, float(row.close), float(row.high), float(row.low),
                                         float(getattr(row, "volume", 0) or 0))
            except Exception:
                created = False
            if created:
                last_down = (builder.brick_direction == "down")
        if len(new_rows) > 0:
            rs["last_ts"] = df.iloc[-1]["time"]
        self._renko_states[code] = rs

        entry = self.t_entry_price.get(code)
        has_entry = bool(entry and entry.get("date") == today)
        m15 = float(feats.get("f15_macd_hist_15m") or 0.0)
        tp = float(PARAMS.get("swing_take_profit_pct", 0.005))
        max_hold = int(PARAMS.get("swing_t_max_hold_min", 0) or 0)
        force_tval = int(PARAMS.get("swing_force_exit_tval", 1455))
        _ind = {"vwap": feats.get("vwap", price), "today_ret": feats.get("today_ret", 0),
                "market_state": feats.get("daily_status", "unknown"),
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
                return sig, 0.0, sell_score, "SELL_HIGH"

        # 3) 买入：最新向下砖 + 15分MACD金叉（当日未持有做T仓）
        if not has_entry and last_down and m15 > 0 and price > 0:
            buy_score = 100.0
            _det = f"Renko向下砖+15分MACD金叉({m15:.2f})"
            sig = Signal(code, name, "BUY_LOW", price, buy_score,
                         [_det], [{"指标": "低吸", "当前": _det, "加分": 100.0}], _ind, dict(_fac))
            self.t_entry_price[code] = {"date": today, "price": price, "ts": df.iloc[-1]["time"]}
            return sig, buy_score, 0.0, "BUY_LOW"

        return None, 0.0, 0.0, "HOLD_NO_SWING"


# ====================================================================
# V2 Engine: FeatureExtractor → Signal (纯两点规则)
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
        feats["hold_qty"] = int(holding.get("t_qty") or 0)  # 纯底仓(t_qty=0)不应用qty回退
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
        # v1.1.1: 变体A开关(默认关, 软消费无KeyError) — below_ma5_weak且ma5_slope>=0 视为回升放行
        # 依据: t_io/validation/e2_daily_gate/E2门控量化报告.md §3(wr 0.4764/量+37%/阴跌不恶化)
        _st = feats["daily_ma5_state"]
        _state_ok = _st in {"near_ma5_chop", "above_ma5_trend"} or (
            PARAMS.get("daily_gate_allow_below_ma5_rebound", False)
            and _st == "below_ma5_weak" and feats["daily_ma5_slope"] >= 0)
        feats["daily_buy_t_ok"] = dc.get("daily_status") == "ok" and feats["daily_ma5"] > 0 and _state_ok
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
# 实盘用真实时钟；回测/回放把 SIM_NOW 设为当前 K 线时间，使 _now() 在模拟时间轴上正确流逝。
SIM_NOW = None
PERSIST_INTRADAY_STATE = True   # 回测/回放置 False，避免污染实盘盘中状态文件


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


# ==================== 集合竞价驱动做T优化 ====================


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
